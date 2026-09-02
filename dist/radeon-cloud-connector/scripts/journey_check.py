#!/usr/bin/env python3
"""Radeon Cloud Connector - user-journey verifier and 360 degree reviewer.

Two phases, both exit non-zero on any failure:

  journey  Replays the complete new-user journey from a cold machine to a
           finished GPU job, stage by stage, asserting what a user would
           perceive. This is the "one-pass success" contract: if the journey
           passes here, a user who follows `rc guide` gets there first try.

  review   Static 360 degree review of the skill package itself - packaging,
           documentation drift, console safety, install sync.

    python scripts/journey_check.py                 # both phases
    python scripts/journey_check.py --phase journey
    python scripts/journey_check.py --phase journey --stage 3
    python scripts/journey_check.py --phase review
    python scripts/journey_check.py --keep          # leave the remote scratch dir
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL_DIR = HERE.parent
sys.path.insert(0, str(HERE))

import rc  # noqa: E402  (must come after the sys.path insert)

PY = sys.executable  # whatever interpreter the operator invoked us with
RC = [PY, str(HERE / "rc.py")]

REMOTE_SCRATCH = "/workspace/.rc-journey-check"
BOGUS_HOST = "rc-journey-nonexistent-host"
EXIT_FAIL_EXPECTED = 1
EXIT_CONNECT_EXPECTED = 2

# Raw ssh error signatures that must NEVER reach a user's screen - the connector
# is supposed to translate them into one actionable message. Used by the cold-start
# UX checks (R6 / Stage 9) to prove nothing leaks through.
RAW_SSH_MARKERS = (
    "Permission denied",
    "Could not resolve hostname",
    "Connection refused",
    "Connection timed out",
    "REMOTE HOST IDENTIFICATION HAS CHANGED",
)

GREEN, RED, YELLOW, DIM = "\033[32m", "\033[31m", "\033[33m", "\033[90m"
RESET = "\033[0m"
if os.environ.get("NO_COLOR") is not None or not sys.stdout.isatty():
    GREEN = RED = YELLOW = DIM = RESET = ""


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------

class Results:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self._stage = ""
        self.stage_start = 0.0

    def stage(self, name: str) -> None:
        if self._stage:
            print(f"{DIM}       stage {self._stage} took {time.time() - self.stage_start:.1f}s{RESET}")
        self._stage = name
        self.stage_start = time.time()
        print()
        print(f"{'-' * 74}")
        print(name)
        print(f"{'-' * 74}")

    def check(self, jid: str, label: str, passed: bool, detail: str = "") -> bool:
        self.rows.append({"stage": self._stage, "id": jid, "label": label, "passed": passed, "detail": detail})
        mark = f"{GREEN}PASS{RESET}" if passed else f"{RED}FAIL{RESET}"
        print(f"  [{mark}] {jid} {label}" + (f"  {DIM}{detail}{RESET}" if detail else ""))
        return passed

    def finish(self) -> int:
        if self._stage:
            print(f"{DIM}       stage {self._stage} took {time.time() - self.stage_start:.1f}s{RESET}")
        failed = [r for r in self.rows if not r["passed"]]
        print()
        print("=" * 74)
        print(f"journey result: {len(self.rows) - len(failed)}/{len(self.rows)} passed")
        print("=" * 74)
        for row in failed:
            print(f"  {RED}FAIL{RESET} {row['id']} [{row['stage']}] {row['label']}")
            if row["detail"]:
                print(f"         {row['detail']}")
        if not failed:
            print(f"  {GREEN}every check passed{RESET}")
        return 1 if failed else 0


def run_rc(*args: str, host: str | None = None, timeout: int = 180,
           env: dict | None = None) -> tuple[int, str, str]:
    cmd = list(RC)
    if host:
        cmd += ["--host", host]
    cmd += list(args)
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    return (
        proc.returncode,
        proc.stdout.decode("utf-8", "replace"),
        proc.stderr.decode("utf-8", "replace"),
    )


def ux_env(home: Path) -> dict:
    """An environment that redirects the CLI's notion of HOME at a temp dir.

    On Windows `Path.home()` honours USERPROFILE, not HOME, and ssh does too, so
    both must be pointed at the temp dir for a faithful isolated cold-start.
    """
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    return env


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def local_manifest(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if root.is_file():
        return {root.name: sha256_file(root)}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root)).replace("\\", "/")] = sha256_file(path)
    return out


# --------------------------------------------------------------------------
# phase 1: the user journey
# --------------------------------------------------------------------------


def phase_journey(res: Results, keep: bool) -> None:
    cfg = rc.load_config()
    host = cfg["host"]
    scratch_local = Path(tempfile.mkdtemp(prefix="rc-journey-"))
    teardown: list[tuple[str, list[str]]] = []

    try:
        # -- Stage 0 -------------------------------------------------------
        res.stage("0. prerequisites on the local machine")
        res.check("J0.1", "ssh client on PATH", bool(shutil.which("ssh")), shutil.which("ssh") or "")
        res.check("J0.2", "ssh-keyscan available (host-key heal needs it)",
                  bool(shutil.which("ssh-keyscan")) or os.path.exists(r"C:\Windows\System32\OpenSSH\ssh-keyscan.exe"))
        ssh_cfg = Path.home() / ".ssh" / "config"
        res.check("J0.3", "~/.ssh/config exists", ssh_cfg.exists(), str(ssh_cfg))
        defined = rc.ssh_alias_defined(host)
        res.check("J0.4", f"ssh alias {host!r} is defined", defined, "found Host block" if defined else "no Host block")
        target = rc.resolve_ssh_target(cfg) if defined else {}
        keys = [os.path.expanduser(k.replace("~", str(Path.home()))) for k in target.get("identityfiles", [])]
        have = [k for k in keys if os.path.exists(k)]
        res.check("J0.5", "configured private key exists",
                  bool(have), have[0] if have else f"none of {len(keys)} configured")

        # -- Stage 1 -------------------------------------------------------
        res.stage("1. first contact - a cold user's first two commands")
        rc_code, out, err = run_rc("guide")
        res.check("J1.1", "`rc guide` exits 0", rc_code == 0, f"exit={rc_code}")
        res.check("J1.2", "`rc guide` confirms step 1 connected", "step 1  connected" in out)
        res.check("J1.3", "`rc guide` confirms step 2 GPU + torch ready", "step 2" in out and "torch" in out)
        res.check("J1.4", "`rc guide` prints the concrete next commands",
                  "rc push" in out and "rc run" in out and "rc pull" in out)

        rc_code, out, err = run_rc("doctor")
        res.check("J1.5", "`rc doctor` exits 0", rc_code == 0, f"exit={rc_code}")
        res.check("J1.6", "doctor: no blocking failures", "[FAIL]" not in out,
                  next((ln.strip() for ln in out.splitlines() if "[FAIL]" in ln), ""))
        res.check("J1.7", "doctor: ssh auth works without a password",
                  "ssh auth (batch, no password)" in out and "[FAIL] ssh auth" not in out)
        res.check("J1.8", "doctor: resolves to one concrete endpoint",
                  bool(re.search(r"ssh config resolves: \S+@\S+:\d+", out)),
                  next((ln.strip() for ln in out.splitlines() if "ssh config resolves" in ln), ""))

        steps = re.findall(r"step \d+", out)
        res.check("J1.9", "guide narrates every step (step 1..step 8) on its own line",
                  "step 1" in out and "step 8" in out and len(steps) >= 8,
                  f"{len(steps)} 'step N' labels found")

        # -- Stage 2 -------------------------------------------------------
        res.stage("2. understanding the machine")
        rc_code, out, err = run_rc("status", timeout=180)
        res.check("J2.1", "`rc status` exits 0", rc_code == 0, f"exit={rc_code}")
        res.check("J2.2", "status reports a GPU", "GPU[0]" in out)
        res.check("J2.3", "status reports disk", "disk" in out and "GiB free" in out)
        res.check("J2.4", "status reports memory", "memory" in out)
        res.check("J2.5", "status reports load", "loadavg" in out)

        rc_code, out, err = run_rc("env")
        res.check("J2.6", "`rc env` exits 0", rc_code == 0, f"exit={rc_code}")
        res.check("J2.7", "env lists at least one torch-capable venv",
                  out.count("[OK]") >= 1)
        res.check("J2.8", "env surfaces HF_HOME / HSA_OVERRIDE_GFX_VERSION",
                  "HF_HOME" in out and "HSA_OVERRIDE_GFX_VERSION" in out)
        res.check("J2.9", "env reports on whether env.sh's default venv has torch",
                  ("[WARN]" in out) or ("env.sh PATH head resolves to a torch-capable venv" in out),
                  "either an explicit warning or a clean bill of health")

        status_lines = out.splitlines()
        max_len = max((len(ln) for ln in status_lines), default=0)
        res.check("J2.10", "status is scannable: distilled GPU line, no raw rocm-smi banner, no 200+ char lines",
                  "GPU[0]" in out and not re.search(r"={10,}", out) and max_len <= 200,
                  f"max_line={max_len}, banner={'yes' if re.search(r'={10,}', out) else 'no'}")

        # -- Stage 3 -------------------------------------------------------
        res.stage("3. the aha moment - first GPU computation")
        rc_code, out, err = run_rc("exec", "--", "python", "-c", "import torch; print(torch.__version__)")
        res.check("J3.1", "`rc exec -- python -c 'import torch'` works with no flags",
                  rc_code == 0, f"exit={rc_code} {err.strip()[:120]}")
        res.check("J3.2", "torch imports without the user choosing a venv",
                  "ModuleNotFoundError" not in out + err)

        kernel = (
            "import torch;"
            "x=torch.arange(6,dtype=torch.float32,device='cuda').reshape(2,3);"
            "y=(x@x.T).sum().item();"
            "print('KERNEL', y, torch.cuda.device_count(), torch.cuda.memory_allocated()>0)"
        )
        rc_code, out, err = run_rc("exec", "--", "python", "-c", kernel, timeout=240)
        res.check("J3.3", "a real GPU kernel runs", rc_code == 0, f"exit={rc_code}")
        res.check("J3.4", "GPU result is numerically correct (expected 83.0)", "KERNEL 83.0" in out,
                  next((ln.strip() for ln in out.splitlines() if "KERNEL" in ln), out.strip()[:100]))
        res.check("J3.5", "torch sees exactly 1 device", "KERNEL 83.0 1 " in out,
                  "guards against the 9-GPU overcount regression")
        res.check("J3.6", "VRAM was actually allocated on the GPU", "True" in out.split("KERNEL")[-1][:40])

        # Anyone who has ever typed `ssh host "cd /workspace && python train.py"`
        # writes the same thing here on day one. shlex.join() used to collapse
        # that whole line into one argv token, so the remote shell hunted for a
        # program literally named `echo hello && echo world` and quit with 127.
        rc_code, out, err = run_rc("exec", "--", "echo hello && echo world")
        res.check("J3.7", "`rc exec -- '<snippet> && <snippet>'` runs the shell line",
                  rc_code == 0 and "hello" in out and "world" in out,
                  f"exit={rc_code} {out.strip()[:80]}")
        # ...and the fix must not swallow genuine argv, where the first token is
        # a program and later tokens are its arguments.
        rc_code, out, err = run_rc("exec", "--", "echo", "hello world")
        res.check("J3.8", "program + whitespace argument is still one argv token",
                  rc_code == 0 and out.strip() == "hello world",
                  f"exit={rc_code} {out.strip()[:80]}")

        # -- Stage 4 -------------------------------------------------------
        res.stage("4. moving code and data")
        project = scratch_local / "proj"
        (project / "src").mkdir(parents=True)
        (project / "src" / "train.py").write_text("print('hello radeon')\n", encoding="utf-8")
        (project / "README.md").write_text("demo\n", encoding="utf-8")
        (project / "noise.log").write_text("should be excluded\n", encoding="utf-8")
        (project / "src" / "__pycache__").mkdir()
        (project / "src" / "__pycache__" / "junk.pyc").write_text("junk\n", encoding="utf-8")
        expected = local_manifest(project)

        rc_code, out, err = run_rc("exec", "--", "rm", "-rf", REMOTE_SCRATCH)
        rc_code, out, err = run_rc(
            "push", str(project), f"{REMOTE_SCRATCH}/proj", "--exclude", "*.log", "--exclude", "__pycache__"
        )
        res.check("J4.1", "`rc push` exits 0", rc_code == 0, f"exit={rc_code} {err.strip()[:120]}")

        listing = rc.remote_capture(
            cfg, f"cd {REMOTE_SCRATCH}/proj 2>/dev/null && find . -type f | sort"
        )
        names = sorted(ln.strip() for ln in (listing or "").splitlines() if ln.strip())
        res.check("J4.2", "pushed files are present",
                  "./README.md" in names and "./src/train.py" in names, ", ".join(names))
        # A vacuous pass is worse than a failure. If the push silently did not
        # land, `names` is empty and every "was it excluded?" assertion below
        # trivially succeeds -- which is exactly what masked a failed `push`
        # during the 2026-09-01 run, where J4.1 failed and J4.3/J4.4 still went
        # green. Require evidence the payload actually arrived first.
        res.check("J4.3", "--exclude removed noise.log",
                  bool(names) and "./noise.log" not in names)
        res.check("J4.4", "--exclude removed __pycache__",
                  bool(names) and not any("__pycache__" in n for n in names))

        remote_manifest_raw = rc.remote_capture(
            cfg,
            f"cd {REMOTE_SCRATCH}/proj && find . -type f -print0 | sort -z | xargs -0 sha256sum",
            timeout=120,
        )
        remote_manifest: dict[str, str] = {}
        for line in (remote_manifest_raw or "").splitlines():
            parts = line.split()
            if len(parts) >= 2:
                remote_manifest[" ".join(parts[1:]).lstrip("./").replace("./", "", 1)] = parts[0]
        kept = {k: v for k, v in expected.items() if not k.endswith(".log") and "__pycache__" not in k}
        res.check("J4.5", "pushed content is byte-identical (sha256)",
                  remote_manifest == kept,
                  "" if remote_manifest == kept else f"remote={remote_manifest} local={kept}")

        back = scratch_local / "back"
        rc_code, out, err = run_rc("pull", f"{REMOTE_SCRATCH}/proj", str(back))
        res.check("J4.6", "`rc pull` exits 0", rc_code == 0, f"exit={rc_code}")
        res.check("J4.7", "pulled content is byte-identical", local_manifest(back) == kept,
                  "" if local_manifest(back) == kept else "checksum mismatch after round trip")

        # Must sit inside /workspace, otherwise the persistence guard fires
        # first and we would be testing the wrong branch.
        rc_code, out, err = run_rc("pull", f"{REMOTE_SCRATCH}-missing", str(scratch_local / "nope"))
        res.check("J4.8", "pulling a missing remote dir fails clearly",
                  rc_code != 0 and "does not exist" in out + err, f"exit={rc_code} {out.strip()[:80]}")
        rc_code, out, err = run_rc("push", str(project), "/etc/rc-should-refuse")
        res.check("J4.9", "pushing outside the persistent volume is refused",
                  rc_code != 0 and "persistent" in out + err, f"exit={rc_code}")

        # -- Stage 5 -------------------------------------------------------
        res.stage("5. long-running jobs")
        rc_code, out, err = run_rc(
            "run", "--name", "journey", "--cwd", f"{REMOTE_SCRATCH}/proj", "--",
            "python", "-c",
            "import time,sys\nfor i in range(40):\n    print('tick',i,flush=True)\n    time.sleep(1)\n",
            timeout=120,
        )
        res.check("J5.1", "`rc run` starts a detached job", rc_code == 0, f"exit={rc_code} {err.strip()[:120]}")
        job_id = ""
        match = re.search(r"job started: (\S+)", out)
        if match:
            job_id = match.group(1)
        res.check("J5.2", "run prints a usable job id", bool(job_id), job_id)
        # Quote each path but let the shell see the names verbatim; a quoted
        # glob would never expand and the job files would be left behind.
        q = rc.shlex.quote
        teardown.append(("remote", ["rm", "-f",
                                    q(f"{cfg['job_dir']}/{job_id}.json"),
                                    q(f"{cfg['job_dir']}/{job_id}.log")]))

        rc_code, out, err = run_rc("jobs")
        res.check("J5.3", "`rc jobs` lists the running job",
                  rc_code == 0 and job_id in out and "running" in out, f"exit={rc_code}")

        time.sleep(4)
        rc_code, out, err = run_rc("logs", job_id, "-n", "20")
        res.check("J5.4", "`rc logs` shows the job's output",
                  rc_code == 0 and "tick" in out, f"exit={rc_code}")
        ticks = len([ln for ln in out.splitlines() if "tick" in ln])
        res.check("J5.5", "the job is still progressing (log grew past the first tick)", ticks >= 2, f"{ticks} ticks")

        rc_code, out, err = run_rc("stop", job_id, "--yes")
        res.check("J5.6", "`rc stop` terminates the job", rc_code == 0 and "SIGTERM" in out, f"exit={rc_code}")
        time.sleep(2)
        rc_code, out, err = run_rc("stop", job_id, "--yes")
        res.check("J5.7", "`rc stop` on a finished job is idempotent (exit 0)",
                  rc_code == 0 and "already exited" in out, f"exit={rc_code}")

        rc_code, out, err = run_rc("jobs")
        res.check("J5.8", "jobs reports it as exited", "exited" in out or job_id not in out)

        # -- Stage 6 -------------------------------------------------------
        res.stage("6. guardrails - bad input must fail loudly, never silently")
        rc_code, out, err = run_rc("status", host=BOGUS_HOST)
        body = out + err
        res.check("J6.1", "unknown alias: status fails (no silent success)", rc_code != 0, f"exit={rc_code}")
        res.check("J6.2", "unknown alias: message names the alias and the config file",
                  BOGUS_HOST in body and "config" in body.lower())
        res.check("J6.3", "unknown alias: tells the user what to run next", "rc doctor" in body)
        res.check("J6.4", "unknown alias: doctor says it in ONE failure, not a cascade",
                  run_rc("doctor", host=BOGUS_HOST)[0] != 0
                  and (run_rc("doctor", host=BOGUS_HOST)[1].count("[FAIL]") <= 2))

        for cmd, label in (("status", "status"), ("exec", "exec"), ("run", "run"),
                           ("jobs", "jobs"), ("env", "env")):
            args = [cmd] + (["--", "echo", "hi"] if cmd in ("exec", "run") else [])
            code, o, e = run_rc(*args, host=BOGUS_HOST)
            res.check(f"J6.5.{cmd}", f"{label} exits non-zero on a dead endpoint", code != 0, f"exit={code}")

        rc_code, out, err = run_rc("exec", "--cwd", "/etc", "--", "pwd")
        res.check("J6.6", "writing outside the persistent volume is refused",
                  rc_code != 0 and "persistent" in out + err, f"exit={rc_code}")
        rc_code, out, err = run_rc("exec", "--cwd", "/etc", "--allow-ephemeral", "--", "pwd")
        res.check("J6.7", "--allow-ephemeral lets a deliberate escape through",
                  rc_code == 0 and "EPHEMERAL" in out + err)

        rc_code, out, err = run_rc("logs", "no-such-job-xyz")
        res.check("J6.8", "logs for an unknown job fails with a hint",
                  rc_code != 0 and "known jobs" in out + err, f"exit={rc_code}")
        rc_code, out, err = run_rc("stop", "no-such-job-xyz", "--yes")
        res.check("J6.9", "stop for an unknown job fails with a hint",
                  rc_code != 0 and "known jobs" in out + err, f"exit={rc_code}")

        # `rc stop <id> --yes` is the natural way to type it; argparse used to
        # reject it with exit 2 and "unrecognized arguments".
        after_sub = run_rc("stop", "no-such-job-xyz", "--yes")[0]
        before_sub = run_rc("--yes", "stop", "no-such-job-xyz")[0]
        host_after = run_rc("status", "--host", BOGUS_HOST)[0]
        host_before = run_rc("--host", BOGUS_HOST, "status")[0]
        res.check("J6.11", "-y and --host work before AND after the subcommand",
                  after_sub == before_sub == EXIT_FAIL_EXPECTED
                  and host_after == host_before == EXIT_CONNECT_EXPECTED,
                  f"stop {after_sub}/{before_sub}, status {host_after}/{host_before}")

        rc_code, out, err = run_rc("status", host=BOGUS_HOST)
        res.check("J6.12", "unknown alias: points the user to the connection setup guide",
                  rc.CONNECTION_GUIDE_URL in (out + err),
                  "guide link present" if rc.CONNECTION_GUIDE_URL in (out + err) else "no guide link")

        rc_code, out, err = run_rc("exec", "--", "python", "-c", "import definitely_not_installed")
        res.check("J6.10", "a failing remote command is NOT mislabelled as an ssh error",
                  rc_code != 0 and "ssh alias" not in out + err and "cannot reach" not in out + err,
                  "python traceback must stay a python traceback")

        # -- Stage 7 -------------------------------------------------------
        res.stage("7. recovery - the instance-was-re-imaged path")
        heal = _test_heal_is_surgical(cfg)
        res.check("J7.1", "host-key heal produces a timestamped backup",
                  heal["backup"], heal["detail"])
        res.check("J7.2", "host-key heal touches ONLY the target host:port",
                  heal["siblings_ok"] and heal["target_ok"], heal["detail"])
        res.check("J7.2b", "host-key heal never mutates the real known_hosts during a dry test",
                  heal["real_untouched"], heal["detail"])

        rc_code, out, err = run_rc("config", "--set", "connect_timeout=33")
        res.check("J7.3", "config --set round-trips", rc_code == 0 and rc.load_config()["connect_timeout"] == 33)
        run_rc("config", "--reset")
        res.check("J7.4", "config --reset restores defaults",
                  rc.load_config()["connect_timeout"] == rc.DEFAULTS["connect_timeout"])
        res.check("J7.5", "venv probe cache is written (keeps `exec` fast)",
                  rc.CONFIG_DIR.joinpath("venv-cache.json").exists())

        cached = rc._load_venv_cache(cfg)
        res.check("J7.6", "venv cache is scoped to the right host",
                  cached is not None and cached.get("host") == host)
        # auto_venv() has two legitimate outcomes depending on the machine, and
        # this check must accept both. It used to demand an override
        # unconditionally, so it failed the day the user repaired /workspace/venv
        # -- the connector was right and the test was wrong. What actually matters
        # is that the decision matches reality: override only when env.sh's
        # default is broken, and never point at a venv without torch.
        override, declared = rc.auto_venv(cfg, None)
        cache_state = rc.ensure_venv_cache(cfg) or {}
        working_paths = {v["path"] for v in (cache_state.get("venvs") or [])
                         if isinstance(v, dict) and v.get("ok")}
        if override:
            ok = override in working_paths and declared not in working_paths
            detail = f"override -> {override} (declared {declared or '?'} lacks torch)"
        else:
            ok = (declared in working_paths) or not working_paths
            detail = f"no override (declared {declared or '?'}, {len(working_paths)} with torch)"
        res.check("J7.7", "auto-venv's decision matches the machine's actual venvs",
                  ok, detail)

        # -- Stage 9 -------------------------------------------------------
        res.stage("9. cold-start UX - a simulated brand-new user")
        # Faithful cold-start test: run the CLI against a TEMP HOME so we never
        # touch the operator's real ~/.ssh/config. Scenario A has no alias at all
        # (must bail with the one clear guide message, fully offline). Scenario B
        # copies the operator's real ~/.ssh into the temp HOME (an isolated copy,
        # private key included, deleted afterwards) so the box is reachable, then
        # asserts the full path is one-pass and smooth.
        ux_home = Path(tempfile.mkdtemp(prefix="rc-ux-"))
        guide_url = rc.CONNECTION_GUIDE_URL
        try:
            # Scenario A - cold, no alias (fails before any SSH, fully offline).
            rc_a, out_a, err_a = run_rc("status", env=ux_env(ux_home))
            body_a = out_a + err_a
            res.check("J9.1", "cold start (no alias): `status` exits with the connection code",
                      rc_a == EXIT_CONNECT_EXPECTED, f"exit={rc_a}")
            res.check("J9.2", "cold start (no alias): surfaces the connection guide link",
                      guide_url in body_a, "guide link present" if guide_url in body_a else "no link")
            res.check("J9.3", "cold start (no alias): ONE failure, not a cascade",
                      body_a.count("[FAIL]") <= 2, f"{body_a.count('[FAIL]')} [FAIL]")
            res.check("J9.4", "cold start (no alias): no raw ssh error leaks through",
                      not any(m in body_a for m in RAW_SSH_MARKERS),
                      next((m for m in RAW_SSH_MARKERS if m in body_a), "translated"))
            rc_ag, out_ag, err_ag = run_rc("guide", env=ux_env(ux_home))
            res.check("J9.5", "cold start: `guide` (documented first cmd) bails to the guide link",
                      rc_ag == EXIT_CONNECT_EXPECTED and guide_url in (out_ag + err_ag), f"exit={rc_ag}")

            # Scenario B - alias configured (isolated copy of the real ~/.ssh).
            real_ssh = Path.home() / ".ssh"
            if real_ssh.exists():
                shutil.copytree(real_ssh, ux_home / ".ssh")
                # Auto-accept the host key on first connect into the temp known_hosts
                # so the isolated copy never blocks on a known_hosts prompt.
                cfg_txt = (ux_home / ".ssh" / "config").read_text(encoding="utf-8", errors="replace")
                extra = "\nStrictHostKeyChecking accept-new\nUserKnownHostsFile {}/.ssh/known_hosts\n".format(ux_home)
                (ux_home / ".ssh" / "config").write_text(cfg_txt + extra, encoding="utf-8")
                env_b = ux_env(ux_home)
                rc_b1, out_b1, err_b1 = run_rc("guide", env=env_b, timeout=180)
                res.check("J9.6", "connected (isolated): `guide` reaches step 1 connected one-pass",
                          rc_b1 == 0 and "step 1  connected" in out_b1, f"exit={rc_b1}")
                rc_b2, out_b2, err_b2 = run_rc("doctor", env=env_b, timeout=180)
                res.check("J9.7", "connected (isolated): `doctor` is green, no [FAIL]",
                          rc_b2 == 0 and "[FAIL]" not in out_b2, f"exit={rc_b2}")
                rc_b3, out_b3, err_b3 = run_rc("status", env=env_b, timeout=180)
                res.check("J9.8", "connected (isolated): `status` is scannable (distilled GPU line)",
                          rc_b3 == 0 and "GPU[0]" in out_b3 and not re.search(r"={10,}", out_b3),
                          f"exit={rc_b3}")
                rc_b4, out_b4, err_b4 = run_rc("exec", "--", "python", "-c",
                                              "import torch; print('UX', torch.cuda.is_available())",
                                              env=env_b, timeout=240)
                res.check("J9.9", "connected (isolated): first GPU command works with no flags",
                          rc_b4 == 0 and "UX True" in out_b4, f"exit={rc_b4} {err_b4.strip()[:80]}")
            else:
                res.check("J9.6", "connected (isolated): real ~/.ssh present to copy",
                          False, "skipped - no ~/.ssh on this machine")
        finally:
            shutil.rmtree(ux_home, ignore_errors=True)
            res.check("J9.10", "cold-start temp HOME cleaned up", not ux_home.exists())

    finally:
        # -- Stage 8 -------------------------------------------------------
        res.stage("8. leaving no trace")
        # Sweep anything a previous aborted run left behind, not just this one.
        teardown.append(("remote", ["rm", "-f", f"{rc.shlex.quote(cfg['job_dir'])}/*journey*"]))
        for kind, cmd in teardown:
            if kind == "remote":
                # Already-quoted remote snippets; the caller owns the quoting
                # because a glob must stay unquoted to expand on the far side.
                rc.ssh_run(rc.load_config(), " ".join(cmd), timeout=60)
        if not keep:
            rc_code, out, err = run_rc("exec", "--", "rm", "-rf", REMOTE_SCRATCH)
            leftover = rc.remote_capture(rc.load_config(), f"test -e {REMOTE_SCRATCH} && echo YES || echo NO")
            res.check("J8.1", "remote scratch dir removed", leftover is not None and "YES" not in leftover)
            shutil.rmtree(scratch_local, ignore_errors=True)
            res.check("J8.2", "local scratch dir removed", not scratch_local.exists())
        jobs_out = rc.remote_capture(
            rc.load_config(),
            f"ls {rc.load_config()['job_dir']} 2>/dev/null | grep journey | wc -l",
        )
        # `grep -c` exits 1 on zero matches, which trips an `|| echo 0` fallback
        # and yields "0\n0". Counting through `wc -l` always exits 0.
        leftover_jobs = (jobs_out or "0").strip().splitlines()
        count = leftover_jobs[-1].strip() if leftover_jobs else "0"
        res.check("J8.3", "no journey jobs left behind", count == "0", f"{count} file(s)")


def _test_heal_is_surgical(cfg: dict) -> dict:
    """Run the real heal against a COPY of known_hosts and prove it is surgical.

    The safety property that matters: sibling containers on the same IP (other
    ports) must survive untouched, and a backup must be produced. We never
    mutate the real known_hosts here - the real file is snapshotted before and
    after so any leak is caught rather than merely suspected.
    """
    result = {"healed": False, "backup": False, "siblings_ok": False, "target_ok": False,
              "real_untouched": False, "detail": ""}
    real = rc.known_hosts_path()
    if not real.exists():
        result["detail"] = "no known_hosts to test against"
        return result

    def snapshot(path: Path) -> list[str]:
        return [ln for ln in path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]

    real_before = snapshot(real)
    plaintext_before = [ln for ln in real_before if not ln.startswith("|1|")]

    tmpdir = Path(tempfile.mkdtemp(prefix="rc-heal-"))
    tmp = tmpdir / "known_hosts"
    shutil.copy2(real, tmp)
    original_fn = rc.known_hosts_path
    rc.known_hosts_path = lambda: tmp  # type: ignore[assignment]
    try:
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()):
            result["healed"] = rc.heal_host_key(cfg, assume_yes=True)
    finally:
        rc.known_hosts_path = original_fn  # type: ignore[assignment]

    # The whole point of this test: the REAL file must not move. heal_host_key
    # once stripped a live host's entries because ssh-keygen -R was called
    # without -f, so it edited the default path instead of the injected one.
    result["real_untouched"] = snapshot(real) == real_before
    if not result["real_untouched"]:
        result["detail"] = "heal mutated the real known_hosts (ssh-keygen -f regression)"

    backups = sorted(tmpdir.glob("known_hosts.rcbak-*"))
    result["backup"] = bool(backups)
    if not result["backup"]:
        result["detail"] = result["detail"] or "no timestamped backup produced"

    after = snapshot(tmp)
    target = rc.resolve_ssh_target(cfg)
    prefix = f"[{target.get('hostname')}]:{target.get('port', 22)}"

    def is_other_host(line: str) -> bool:
        """A plaintext entry for a host:port that is NOT the heal target."""
        return not line.startswith("|1|") and not line.startswith(prefix)

    siblings_before = [ln for ln in real_before if is_other_host(ln)]
    siblings_after = [ln for ln in after if is_other_host(ln)]
    # Form-agnostic: the refreshed target keys are simply the lines that were
    # not there before (the stale ones having been removed by ssh-keygen -R).
    refreshed = [ln for ln in after if ln not in real_before]

    result["siblings_ok"] = bool(siblings_before) and siblings_before == siblings_after
    if not result["siblings_ok"]:
        gone = [ln for ln in siblings_before if ln not in siblings_after]
        extra = [ln for ln in siblings_after if ln not in siblings_before]
        result["detail"] = result["detail"] or (
            f"other hosts changed: gone={len(gone)} added={len(extra)}")
    result["target_ok"] = 1 <= len(refreshed) <= 8
    if not result["target_ok"]:
        result["detail"] = result["detail"] or f"refreshed {len(refreshed)} target key(s), expected 1-8"
    result["detail"] = result["detail"] or (
        f"{len(siblings_before)} sibling entry(ies) preserved, "
        f"{len(refreshed)} target key(s) refreshed")
    shutil.rmtree(tmpdir, ignore_errors=True)
    return result


# --------------------------------------------------------------------------
# phase 2: 360 degree static review
# --------------------------------------------------------------------------


def phase_review(res: Results) -> None:
    res.stage("R1. packaging")
    skill_md = SKILL_DIR / "SKILL.md"
    res.check("R1.1", "SKILL.md exists", skill_md.exists())
    text = skill_md.read_text(encoding="utf-8") if skill_md.exists() else ""
    fm = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    res.check("R1.2", "SKILL.md has YAML frontmatter", bool(fm))
    skill_name = ""
    if fm:
        block = fm.group(1)
        name = re.search(r"^name:\s*(.+)$", block, re.M)
        desc = re.search(r"^description:\s*(.+)$", block, re.M)
        skill_name = name.group(1).strip() if name else ""
        # The dev directory is human-readable ("Radeon Cloud Connector"); the
        # install directory is what must match the frontmatter name.
        res.check("R1.3", "frontmatter name is a valid skill id",
                  bool(re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", skill_name)),
                  skill_name or "missing")
        res.check("R1.4", "frontmatter description is present and non-trivial",
                  bool(desc) and len(desc.group(1).strip()) > 40)
        res.check("R1.5", "frontmatter declares agent_created", "agent_created" in block)
    res.check("R1.6", "scripts/ contains the CLI", (SKILL_DIR / "scripts" / "rc.py").exists())
    config = SKILL_DIR / "config.yaml"
    res.check("R1.8", "config.yaml exists for marketplace metadata", config.exists())
    if config.exists():
        config_text = config.read_text(encoding="utf-8")
        res.check("R1.9", "config.yaml has name and version", bool(re.search(r"^name:\s*\S+", config_text, re.M) and re.search(r"^version:\s*\S+", config_text, re.M)))
    for ref in re.findall(r"`?references/([A-Za-z0-9_.-]+\.md)`?", text):
        res.check(f"R1.7.{ref}", f"referenced file references/{ref} exists",
                  (SKILL_DIR / "references" / ref).exists())

    res.stage("R2. documentation drift")
    parser = rc.build_parser()
    commands = sorted(parser._subparsers._group_actions[0].choices)  # type: ignore[attr-defined]

    # The command table lists each command as its own backticked row, which the
    # plain `rc <cmd>` prose form does not cover. Accept either spelling.
    table = re.search(r"## Commands(.*?)(?=\n## )", text, re.S)
    table_text = table.group(1) if table else ""
    documented = set(re.findall(r"\brc\s+([a-z]+)\b", text))
    documented |= set(re.findall(r"^\|\s*`([a-z]+)", table_text, re.M))

    missing = [c for c in commands if c not in documented]
    res.check("R2.1", "every subcommand is documented in SKILL.md",
              not missing, f"undocumented: {missing}" if missing else f"{len(commands)} commands")
    phantom = sorted(d for d in documented if d not in commands)
    res.check("R2.2", "SKILL.md documents no command that does not exist",
              not phantom, f"phantom: {phantom}" if phantom else f"{len(documented)} referenced")
    for flag in ("--venv", "--allow-ephemeral", "--exclude", "--stream", "--dry-run",
                 "--overwrite", "--force", "--no-auto-venv", "--host"):
        res.check(f"R2.4{flag}", f"flag {flag} is explained in SKILL.md", flag in text)
    code = (SKILL_DIR / "scripts" / "rc.py").read_text(encoding="utf-8")
    for key in ("REMOTE HOST IDENTIFICATION", "ModuleNotFoundError", "EPHEMERAL", "re-imaged"):
        res.check(f"R2.3.{key[:14]}", f"troubleshooting covers {key!r}",
                  key in (SKILL_DIR / "references" / "troubleshooting.md").read_text(encoding="utf-8"))

    res.stage("R3. console and platform safety")
    non_ascii = sorted({ch for ch in code if ord(ch) > 127})
    res.check("R3.1", "rc.py is ASCII-only (Windows consoles garble emoji/box drawing)",
              not non_ascii, "".join(non_ascii[:20]))
    res.check("R3.2", "no TODO/FIXME/XXX left in the CLI",
              not re.search(r"\b(TODO|FIXME|XXX|HACK)\b", code))
    # The managed-interpreter path is legitimately machine-specific, but it must
    # be written portably ($HOME) rather than as a literal C:/Users/<user>/...
    portable = re.sub(r"\$HOME|\$\{HOME\}|~|\.workbuddy-ai/binaries", "", text)
    hardcoded = re.findall(r"C:[\\/]Users[\\/][A-Za-z0-9._-]+", portable)
    res.check("R3.3", "SKILL.md has no machine-specific user paths",
              not hardcoded, ", ".join(sorted(set(hardcoded))) or "uses $HOME")
    res.check("R3.4", "no leftover debug prints",
              not re.search(r"^\s*print\(\s*[\"']DEBUG", code, re.M))

    res.stage("R4. install sync")
    installed = Path.home() / ".workbuddy-ai" / "skills" / (skill_name or SKILL_DIR.name)
    res.check("R4.1", "skill is installed at ~/.workbuddy-ai/skills/<name>", installed.exists(), str(installed))
    if installed.exists():
        drift = []
        for rel in ("SKILL.md", "config.yaml", "scripts/rc.py", "scripts/journey_check.py", "scripts/install.py",
                    "references/environment.md", "references/troubleshooting.md",
                    "references/user-journey.md"):
            src, dst = SKILL_DIR / rel, installed / rel
            if not dst.exists():
                drift.append(f"{rel} missing")
            elif sha256_file(src) != sha256_file(dst):
                drift.append(f"{rel} differs")
        res.check("R4.2", "installed copy matches the source of truth",
                  not drift, "; ".join(drift) if drift else "in sync")
        res.check("R4.4", "the verifier itself is installed (self-check works in place)",
                  (installed / "scripts" / "journey_check.py").exists())
        pycache = installed / "scripts" / "__pycache__"
        res.check("R4.3", "no __pycache__ shipped in the installed skill", not pycache.exists())

    res.stage("R5. exit-code contract")
    res.check("R5.1", "distinct exit codes are defined for remote vs connection failure",
              rc.EXIT_CONNECT != rc.EXIT_FAIL and rc.EXIT_FAIL != 0)
    src = code
    for cmd_name in ("cmd_status", "cmd_exec", "cmd_run", "cmd_push", "cmd_pull",
                     "cmd_jobs", "cmd_logs", "cmd_stop", "cmd_env"):
        body = re.search(rf"def {cmd_name}\(.*?(?=\ndef |\Z)", src, re.S)
        res.check(f"R5.2.{cmd_name[4:]}", f"{cmd_name[4:]} calls require_remote (no silent success)",
                  bool(body) and "require_remote(" in body.group(0))

    res.stage("R6. cold-start UX and smoothness (offline)")
    # The bug-fix guarantee, checked WITHOUT a real box so static CI stays green:
    # a missing radeon-cloud alias must surface ONE clear, actionable message that
    # points at the connection setup guide - never a cascade of raw ssh errors.
    guide_url = rc.CONNECTION_GUIDE_URL

    def _bogus(*a):
        return run_rc(*a, host=BOGUS_HOST)

    rc_s, out_s, err_s = _bogus("status")
    body_s = out_s + err_s
    res.check("R6.1", "missing alias: `status` exits with the connection code",
              rc_s == EXIT_CONNECT_EXPECTED, f"exit={rc_s}")
    res.check("R6.2", "missing alias: `status` points at the connection setup guide",
              guide_url in body_s, "guide link present" if guide_url in body_s else "no guide link")
    res.check("R6.3", "missing alias: ONE failure, not a cascade",
              body_s.count("[FAIL]") <= 2, f"{body_s.count('[FAIL]')} [FAIL] line(s)")
    res.check("R6.4", "missing alias: no raw ssh error leaks through",
              not any(m in body_s for m in RAW_SSH_MARKERS),
              next((m for m in RAW_SSH_MARKERS if m in body_s), "translated"))

    rc_d, out_d, err_d = _bogus("doctor")
    body_d = out_d + err_d
    res.check("R6.5", "missing alias: `doctor` also points at the guide, no cascade",
              rc_d != 0 and guide_url in body_d and body_d.count("[FAIL]") <= 2,
              f"exit={rc_d}, {body_d.count('[FAIL]')} [FAIL]")

    rc_g, out_g, err_g = _bogus("guide")
    res.check("R6.6", "missing alias: `guide` (documented first cmd) bails to the guide link",
              rc_g == EXIT_CONNECT_EXPECTED and guide_url in (out_g + err_g),
              f"exit={rc_g}, guide={'yes' if guide_url in (out_g + err_g) else 'no'}")

    # Unit-level regression for the exact required hint text and the gate wiring.
    expected_hint = ("If ssh radeon-cloud fails or there is no radeon-cloud alias, "
                     "complete the connection setup first: "
                     "\u5728 Windows \u6216 MacBook \u4e0a\u8fde\u63a5 Radeon Cloud "
                     "(https://mp.weixin.qq.com/s/dOAIzJ2qsWPmBSH67q41aA)")
    res.check("R6.7", "connection_setup_hint() returns the exact required text",
              rc.connection_setup_hint() == expected_hint,
              "exact match" if rc.connection_setup_hint() == expected_hint else "mismatch")
    res.check("R6.8", "require_ssh_alias() is defined and wired into require_remote",
              hasattr(rc, "require_ssh_alias")
              and "require_ssh_alias(" in re.search(r"def require_remote\(.*?(?=\ndef |\Z)", code, re.S).group(0),
              "gate present")

    # The other half of the fix: the skill must never ship a hard-coded endpoint.
    ip_hits = [rel for rel in ("scripts/rc.py", "references/environment.md", "SKILL.md")
               if (SKILL_DIR / rel).exists()
               and re.search(r"36\.150\.116\.220|31622",
                             (SKILL_DIR / rel).read_text(encoding="utf-8", errors="replace"))]
    res.check("R6.9", "no hard-coded public IP:port in shipped skill files",
              not ip_hits, ", ".join(ip_hits) or "clean")


# --------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify the radeon-cloud user journey and review the skill package.")
    ap.add_argument("--phase", choices=("journey", "review", "all"), default="all")
    ap.add_argument("--stage", help="run only stages whose number matches (e.g. 3)")
    ap.add_argument("--keep", action="store_true", help="leave the remote scratch dir for inspection")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    res = Results()
    if args.stage:
        wanted = args.stage.split(",")

        def _check(jid, label, passed, detail=""):
            if not any(jid.split(".")[0].lstrip("JR").startswith(w) for w in wanted):
                return True
            return Results.check(res, jid, label, passed, detail)
        res.check = _check  # type: ignore[assignment]

    print("=" * 74)
    print("radeon-cloud connector - journey verification & 360 review")
    print("=" * 74)
    print(f"skill source : {SKILL_DIR}")
    print(f"python       : {PY}")
    print(f"time         : {time.strftime('%Y-%m-%d %H:%M:%S')}")

    if args.phase in ("journey", "all"):
        phase_journey(res, args.keep)
    if args.phase in ("review", "all"):
        phase_review(res)

    if args.json:
        print()
        print(json.dumps(res.rows, indent=2))
    return res.finish()


if __name__ == "__main__":
    sys.exit(main())
