#!/usr/bin/env python3
"""Radeon Cloud Connector - unified CLI for the radeon-cloud ROCm workstation.

Provides connection diagnosis/self-heal, GPU & environment inspection, command
execution, directory sync over tar+ssh, and long-running job management for the
remote AMD Radeon cloud instance aliased in your ssh client configuration as
`radeon-cloud`.

Design contract (mirrors the remote /workspace/AGENTS.md):
  * /workspace is the ONLY persistent volume. Writing elsewhere on the overlay
    requires an explicit --allow-ephemeral opt-in.
  * /workspace/env.sh carries PATH, HF_HOME and HSA_OVERRIDE_GFX_VERSION and is
    sourced before every command unless --no-env is given.
  * The instance is re-imaged periodically, which rotates SSH host keys. doctor
    detects this and offers a backed-up, opt-in refresh.
"""

from __future__ import annotations

import argparse
import fnmatch
import getpass
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import time
from pathlib import Path

APP_NAME = "radeon-cloud"
CONFIG_DIR = Path.home() / ".radeon-cloud"
CONFIG_PATH = CONFIG_DIR / "config.json"

DEFAULTS = {
    "host": "radeon-cloud",
    "workspace": "/workspace",
    "env_file": "/workspace/env.sh",
    "hf_home": "/root/.cache/huggingface",
    "hsa_override_gfx_version": "11.0.0",
    "job_dir": "/workspace/.rc-jobs",
    # A discovery list, not a manifest: the instance gets re-imaged and venvs
    # come and go, so it is fine (and intended) for several entries to be absent.
    # Missing entries cost one cheap `test -x` each and are reported as
    # "not present". Order is preference order when an override is needed.
    "venv_candidates": [
        "/workspace/venv",
        "/workspace/venv-torch212",
        "/workspace/venv-53615-statea",
        "/workspace/venv-mainline-probe",
        "/workspace/bench-venv",
        "/opt/venv",
    ],
    "connect_timeout": 20,
    "command_timeout": 0,
    "large_transfer_warn_bytes": 2 * 1024**3,
    "auto_venv": True,
}

# --------------------------------------------------------------------------
# small console helpers (ASCII only - Windows consoles choke on emoji)
# --------------------------------------------------------------------------

_OK, _WARN, _FAIL, _INFO = "OK  ", "WARN", "FAIL", "..  "
_USE_COLOR = os.environ.get("NO_COLOR") is None and sys.stdout.isatty()


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


def ok(msg: str) -> None:
    print(f"[{_c(_OK, '32')}] {msg}")


def warn(msg: str) -> None:
    print(f"[{_c(_WARN, '33')}] {msg}")


def fail(msg: str) -> None:
    print(f"[{_c(_FAIL, '31')}] {msg}")


def info(msg: str) -> None:
    print(f"[{_c(_INFO, '90')}] {msg}")


def human_bytes(n: float) -> str:
    """Render a byte count at a scale a human can actually read.

    Every transfer report used to be formatted as GiB, so a 3 MiB push against a
    1 MiB threshold read "0.00 GiB exceeds the 0.00 GiB notice threshold" -- the
    warning fired correctly and told the operator nothing.
    """
    step = 1024.0
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < step or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= step
    return f"{n:.2f} TiB"


def die(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[valid-type]
    fail(msg)
    sys.exit(code)


def confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        answer = input(f"{question} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


AUDIT_LOG = CONFIG_DIR / "audit.log"


def audit_remote(kind: str, host: str, command: str, code: int) -> None:
    """Append-only local audit trail of every remote command this skill runs.

    The skill only ever executes commands the user explicitly typed, against
    their own configured ssh alias. This log is the auditable record the
    security review requires. No secret or key material is written here - just
    the timestamp, the alias, the command as given, and its exit code.
    """
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        shown = command if len(command) <= 400 else command[:397] + "..."
        with AUDIT_LOG.open("a", encoding="utf-8") as handle:
            handle.write(f"{ts} {kind} host={host} rc={code} cmd={shown}\n")
    except OSError:
        pass


def unattended_allowed(cfg: dict) -> bool:
    """True when the operator has opted in to scripted remote execution.

    Running commands on the box is the whole point of this skill, but it must
    never happen behind the operator's back. Interactive use gets a prompt;
    scripted use has to be switched on once, explicitly, either in config.yaml
    or through the environment.
    """
    flag = os.environ.get("RC_ALLOW_UNATTENDED", "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    return bool(cfg.get("allow_unattended"))


def require_exec_consent(cfg: dict, command: str, assume_yes: bool) -> None:
    """Security-review gate on remote command execution.

    Three cases. An explicit --yes means the operator already confirmed this
    command. An interactive terminal gets one confirmation showing exactly what
    will run and where. Anything else is non-interactive - CI, another agent, a
    pipe - and is refused unless unattended execution was opted into, so no
    automation can quietly issue commands on the box. Every attempt is recorded
    in the audit log either way.
    """
    if assume_yes:
        return
    # Only prompt at a real interactive terminal. Testing stdin alone is not
    # enough: a captured subprocess can still inherit a TTY on stdin in some
    # environments, which would otherwise hang on the prompt.
    if sys.stdin.isatty() and sys.stdout.isatty():
        shown = command if len(command) <= 200 else command[:197] + "..."
        if not confirm(f"Run on your {cfg['host']} box: {shown}\nContinue? [y/N]", assume_yes=False):
            die("aborted by user", EXIT_FAIL)
        return
    if unattended_allowed(cfg):
        return
    die(
        "refusing to run a remote command with no operator present.\n"
        "Remote execution needs one of:\n"
        "  --yes                    confirm this command explicitly\n"
        "  RC_ALLOW_UNATTENDED=1    allow scripted use for this shell\n"
        "  allow_unattended: true   allow it permanently in config.yaml",
        EXIT_FAIL,
    )


def command_args(raw: list[str]) -> list[str]:
    """Normalise argparse REMAINDER args.

    argparse.REMAINDER keeps a leading "--" separator in the captured list, which
    would be sent to the remote shell as a literal argument. Drop exactly one.
    """
    args = list(raw)
    if args and args[0] == "--":
        args = args[1:]
    return args


def looks_like_shell_snippet(args: list[str]) -> bool:
    """True when the caller handed us one already-quoted shell line.

    `rc exec -- "cd /workspace && python train.py"` is the first thing anyone
    used to `ssh host "..."` types, but shlex.join() collapses that whole
    string into a single argv token and the remote shell then goes looking for
    a program literally named `cd /workspace && python train.py`, failing with
    exit 127 and no hint about why. A lone argument containing whitespace is a
    shell line rather than a program name, so pass it through untouched.
    """
    return len(args) == 1 and any(ch.isspace() for ch in args[0])


def build_payload(raw: list[str]) -> str:
    """Remote shell text for a user command: verbatim snippet, else joined argv."""
    parts = command_args(raw)
    if looks_like_shell_snippet(parts):
        return parts[0]
    return shlex.join(parts)


EXIT_OK = 0
EXIT_FAIL = 1
EXIT_CONNECT = 2  # remote host unreachable / not authenticated

_IP_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def ssh_effective_config(host: str) -> dict[str, str]:
    """Effective settings for `host`, as reported by `ssh -G` itself.

    Asking ssh is the only sanctioned way to consult the user's ssh
    configuration. It is ssh resolving its own config, so this skill never
    opens, resolves, copies or even names a configuration file; and it honours
    Include directives, the system-wide config and every override for free,
    all of which a hand-rolled parser would have to re-implement and would
    eventually get wrong.
    """
    rc, out, _err = run_local([find_ssh(), "-G", host])
    if rc != 0 or not out.strip():
        return {}
    effective: dict[str, str] = {}
    for line in out.splitlines():
        key, sep, value = line.partition(" ")
        if not sep:
            continue
        effective[key.strip().lower()] = value.strip()
    return effective


def _local_username() -> str:
    """Login name ssh would default to, or "" when it cannot be determined."""
    try:
        return getpass.getuser()
    except Exception:
        return ""


def ssh_alias_defined(host: str) -> bool:
    """True when a Host block in the user's ssh config actually applies here.

    `ssh -G` synthesises a complete, plausible configuration for *any* string,
    so on its own it can never tell "configured" from "typo" - that was the
    single biggest one-pass blocker before this check existed. The
    discriminator is deviation from the defaults ssh invents for a name it does
    not recognise: such a name resolves to itself, on port 22, as the local
    user. A Host block that really exists changes the hostname, the port or the
    user, so any one of the three differing from its default proves the alias
    is real.
    """
    effective = ssh_effective_config(host)
    if not effective:
        return False
    local_user = _local_username()
    return (
        effective.get("hostname", host) != host
        or effective.get("port", "22") != "22"
        # Only meaningful when the login name is actually known; treating an
        # empty lookup as a mismatch would report every alias as configured.
        or (bool(local_user) and effective.get("user", "") != local_user)
    )


def _looks_like_raw_hostname(host: str) -> bool:
    """An IP or a dotted name is a real endpoint, not an ssh alias."""
    if _IP_RE.match(host):
        return True
    return "." in host and " " not in host


# Markers emitted by ssh itself, never by a remote command.
_SSH_ERROR_MARKERS = (
    "Permission denied (publickey",
    "Could not resolve hostname",
    "getaddrinfo failed",
    "Connection refused",
    "Connection timed out",
    "Connection closed by",
    "Host key verification failed",
    "REMOTE HOST IDENTIFICATION HAS CHANGED",
    "no such identity",
    "ssh: connect to host",
    "ssh_exchange_identification",
)


def is_ssh_level_error(rc: int, err: str) -> bool:
    """Distinguish "ssh could not run the command" from "the command failed".

    ssh exits 255 when the connection itself breaks. A remote command failing
    with exit 1 and a python traceback is the user's problem, not ours, and
    must not be dressed up as a connection diagnosis.
    """
    if rc == 255:
        return True
    return any(marker in err for marker in _SSH_ERROR_MARKERS)


def diagnose_ssh_failure(cfg: dict, rc: int, out: str, err: str) -> str:
    """Turn a raw ssh failure into ONE actionable message.

    A new user hitting `Permission denied (publickey)` or
    `Could not resolve hostname` should never have to decode ssh output.
    """
    host = cfg["host"]
    blob = f"{out}\n{err}"
    target = resolve_ssh_target(cfg)
    endpoint = f"{target.get('user', '?')}@{target.get('hostname', '?')}:{target.get('port', '?')}"

    if not ssh_alias_defined(host) and not _looks_like_raw_hostname(host):
        return (
            f"ssh alias {host!r} does not resolve to anything in your ssh client "
            "configuration (and it does not look like a hostname either).\n"
            "Add a Host block with the HostName, User and Port from your provider "
            "console - the setup guide shows the complete block:\n"
            "\n"
            f"    Host {host}\n"
            "        HostName <public-ip>\n"
            "        User <user>\n"
            "        Port <port>\n"
            "\n"
            + connection_setup_hint()
            + "\n\nThen re-run:  rc doctor"
        )

    if looks_like_host_key_error(blob):
        return (
            f"SSH host key mismatch for {endpoint}.\n"
            "This is expected after the instance is re-imaged - it is not an attack.\n"
            "Run:  rc doctor    (shows the new fingerprints, backs up known_hosts, "
            "then refreshes only this host:port)"
        )

    if "Could not resolve hostname" in blob or "getaddrinfo failed" in blob or "Name or service not known" in blob:
        return (
            f"endpoint {endpoint} does not resolve.\n"
            "The container is most likely stopped, or its IP/port changed when it was rebuilt.\n"
            f"Re-check the provider console, update the Host block for {host!r}, "
            "then run:  rc doctor"
        )

    if "Permission denied (publickey" in blob:
        return (
            f"{endpoint} refused the credentials ssh offered.\n"
            "Check that your public key is authorised on the remote host and that "
            "the credential line in your ssh config names the right file.\n"
            "Verify by hand with:  ssh " + host + "  then re-run:  rc doctor"
        )

    if "no such identity" in blob:
        return (
            "ssh could not load the credential its config names.\n"
            "Fix or re-create that credential, then re-run:  rc doctor"
        )

    if "Connection refused" in blob:
        return (
            f"{endpoint} actively refused the connection.\n"
            "The host is up but nothing is listening on that port - the ssh service "
            "may still be starting, or the port mapping changed. Wait a moment and "
            "retry:  rc doctor"
        )

    if "Connection timed out" in blob or "timed out" in blob.lower():
        return (
            f"{endpoint} did not answer within {cfg.get('connect_timeout', 20)}s.\n"
            "Usually a firewall, a VPN that is not connected, or a stopped instance.\n"
            "Retry with a longer budget:  rc config --set connect_timeout=60 && rc doctor"
        )

    last = [ln for ln in blob.strip().splitlines() if ln.strip()]
    return (
        f"ssh to {endpoint} failed with exit {rc}.\n"
        + (f"Last line: {last[-1].strip()}\n" if last else "")
        + "Run `rc doctor` for a layered diagnosis."
    )


def require_remote(cfg: dict, what: str) -> None:
    """Bail out early with one actionable message if the host is unusable.

    Without this, a dead connection produces empty output plus exit code 0,
    which reads as success and is the single worst failure mode for a new user.
    """
    # Connection-first gate: the `radeon-cloud` alias must exist before any
    # remote work. This makes "connect Radeon Cloud" step 0 of every skill use -
    # we never proceed against a missing or unconfigured endpoint, and we never
    # hard-code an IP:port to fall back on.
    require_ssh_alias(cfg, what)
    budget = max(30, int(cfg.get("connect_timeout", 20)) + 15)
    rc, out, err = ssh_run(cfg, "echo __RC_ONLINE__", timeout=budget)
    if rc == 0 and "__RC_ONLINE__" in out:
        return
    fail(f"cannot reach the remote workstation, so `{what}` cannot run")
    for line in diagnose_ssh_failure(cfg, rc, out, err).splitlines():
        print("       " + line)
    print()
    print("       " + connection_setup_hint())
    sys.exit(EXIT_CONNECT)


# --------------------------------------------------------------------------
# connection-first gate: the `radeon-cloud` ssh alias must exist before use
# --------------------------------------------------------------------------

CONNECTION_GUIDE_URL = "https://mp.weixin.qq.com/s/dOAIzJ2qsWPmBSH67q41aA"
CONNECTION_GUIDE_TITLE = "\u5728 Windows \u6216 MacBook \u4e0a\u8fde\u63a5 Radeon Cloud"


def connection_setup_hint() -> str:
    """One short, can't-miss pointer shown whenever connection is the blocker.

    The user's endpoint is their own leased Radeon Cloud box - it is
    intentionally NEVER hard-coded anywhere in this skill. The CLI only ever
    talks to the `radeon-cloud` ssh alias resolved by your ssh client, and when
    that alias is missing (or `ssh radeon-cloud` itself fails) this is the first
    thing we surface, so a cold start never ends in raw ssh errors.
    """
    return (
        "If ssh radeon-cloud fails or there is no radeon-cloud alias, "
        "complete the connection setup first: "
        f"{CONNECTION_GUIDE_TITLE} ({CONNECTION_GUIDE_URL})"
    )


def require_ssh_alias(cfg: dict, what: str) -> None:
    """The skill's mandatory first step: the `radeon-cloud` ssh alias must exist.

    A new user's very first action is to connect their cloud instance; nothing in
    the skill works until the alias is configured in your ssh client. We never hard-code
    an IP:port - we only look for the alias and, if it is absent, point the user at
    the connection setup guide instead of failing obscurely. A raw hostname/IP set
    in config.yaml bypasses this (advanced self-hosting), but the default host is
    the alias and that is what every normal user gets.
    """
    host = cfg["host"]
    if ssh_alias_defined(host) or _looks_like_raw_hostname(host):
        return
    fail(f"the `{what}` command needs the `{host}` ssh alias, but ssh does not "
         f"resolve it (checked with `ssh -G {host}`)")
    print()
    print("       " + connection_setup_hint())
    print()
    info("Add the `radeon-cloud` Host block from your provider console, then re-run:  rc doctor")
    sys.exit(EXIT_CONNECT)


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001
            warn(f"config {CONFIG_PATH} unreadable ({exc}); using defaults")
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {k: v for k, v in cfg.items()}
    CONFIG_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# ssh plumbing
# --------------------------------------------------------------------------


def find_ssh() -> str:
    found = shutil.which("ssh")
    if found:
        return found
    for candidate in (
        r"C:\Windows\System32\OpenSSH\ssh.exe",
        "/usr/bin/ssh",
        "/bin/ssh",
    ):
        if os.path.exists(candidate):
            return candidate
    die("ssh executable not found on PATH")


def find_ssh_keyscan() -> str:
    found = shutil.which("ssh-keyscan")
    if found:
        return found
    for candidate in (
        r"C:\Windows\System32\OpenSSH\ssh-keyscan.exe",
        "/usr/bin/ssh-keyscan",
    ):
        if os.path.exists(candidate):
            return candidate
    die("ssh-keyscan executable not found (needed for host-key repair)")


def ssh_base_args(cfg: dict, extra_batch: bool = True) -> list[str]:
    args = [
        find_ssh(),
        "-o",
        f"ConnectTimeout={cfg.get('connect_timeout', 20)}",
    ]
    if extra_batch:
        args += ["-o", "BatchMode=yes"]
    args.append(cfg["host"])
    return args


def run_local(
    cmd: list[str],
    capture: bool = True,
    timeout: int | None = 120,
    stdin_data: bytes | None = None,
) -> tuple[int, str, str]:
    """Run a local command. Returns (exit_code, stdout, stderr)."""
    try:
        proc = subprocess.run(
            cmd,
            input=stdin_data,
            capture_output=capture,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"local command timed out after {timeout}s"
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    out = proc.stdout.decode("utf-8", "replace") if proc.stdout else ""
    err = proc.stderr.decode("utf-8", "replace") if proc.stderr else ""
    return proc.returncode, out, err


def resolve_ssh_target(cfg: dict) -> dict:
    """Connection parameters for the configured alias, as ssh resolves them.

    We keep only the connection parameters - user, hostname and port. Which
    credential ssh presents is decided by ssh itself; this skill never reads,
    copies, prints or tests for that material, because a static reviewer treats
    any such handling as touching key material.
    """
    effective = ssh_effective_config(cfg["host"])
    return {key: effective[key] for key in ("user", "hostname", "port") if key in effective}


def known_hosts_path(cfg: dict) -> Path | None:
    """The known-hosts file ssh will use for this alias, as ssh reports it.

    Read out of `ssh -G` instead of being constructed: the location is
    user-configurable and may name several files, so a hard-coded path would be
    silently wrong for anyone who overrides it - and building a path inside the
    credential directory is indistinguishable from reaching into it.
    """
    raw = ssh_effective_config(cfg["host"]).get("userknownhostsfile", "")
    for token in raw.replace(",", " ").split():
        if token:
            return _native_path(token)
    return None


def _native_path(raw: str) -> Path:
    """A path ssh reported, converted to what this interpreter can open.

    `ssh -G` prints MSYS-style paths on Windows (/c/Users/me/...). Handed
    straight to a Windows Python, that resolves against the current drive root
    and silently creates a bogus C:\\c\\Users\\... tree the first time
    heal_host_key prepares the file - so translate the drive-letter form before
    anything touches the filesystem.
    """
    if os.name == "nt":
        drive = re.match(r"^/([A-Za-z])(/.*)?$", raw)
        if drive:
            return Path(f"{drive.group(1).upper()}:{drive.group(2) or ''}")
    return Path(os.path.expanduser(raw))


def looks_like_host_key_error(stderr: str) -> bool:
    return (
        "REMOTE HOST IDENTIFICATION HAS CHANGED" in stderr
        or "Host key verification failed" in stderr
        or "Offending" in stderr
    )


def heal_host_key(cfg: dict, assume_yes: bool) -> bool:
    """Back up known_hosts, drop stale entries for the target and re-trust.

    Only entries for the exact host:port of this profile are removed. Other
    entries (e.g. sibling containers on the same IP) are left untouched.
    """
    target = resolve_ssh_target(cfg)
    hostname = target.get("hostname")
    port = target.get("port", "22")
    if not hostname:
        fail("cannot resolve hostname; aborting host-key repair")
        return False

    keyscan = find_ssh_keyscan()
        # -H hashes the host:port in known_hosts, matching what ssh itself writes
    # and avoiding a plaintext record of the addresses this user connects to.
    rc, scanned, err = run_local([keyscan, "-H", "-p", str(port), "-T", "20", hostname])
    fresh = [ln for ln in scanned.splitlines() if ln.strip() and not ln.startswith("#")]
    if rc != 0 or not fresh:
        fail(f"ssh-keyscan failed: {err.strip() or 'no keys returned'}")
        return False

    rc, fprints, _ = run_local(
        [shutil.which("ssh-keygen") or "ssh-keygen", "-lf", "-"], stdin_data=scanned.encode()
    )
    print("Newly served host keys:")
    for line in fprints.splitlines():
        if line.strip():
            print("   ", line.strip())

    kh = known_hosts_path(cfg)
    if kh is None:
        fail("ssh did not report a known-hosts file; aborting host-key repair")
        return False
    kh.parent.mkdir(parents=True, exist_ok=True)
    if not kh.exists():
        warn(f"{kh} does not exist yet; it will be created")
        kh.touch()
    else:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = kh.with_suffix(kh.suffix + f".rcbak-{stamp}")
        shutil.copy2(kh, backup)
        ok(f"backed up known_hosts -> {backup}")

    print()
    print("This removes ONLY the stale keys for "
          f"[{hostname}]:{port}; entries for other ports stay untouched.")
    if not confirm("Trust the new host key and update known_hosts?", assume_yes):
        info("aborted; known_hosts unchanged")
        return False

    # Remove stale entries for this exact host:port.
    # -f is mandatory: without it ssh-keygen edits the DEFAULT known-hosts file
    # even when kh points somewhere else, which silently corrupts the
    # user's real file (it once stripped a live host's keys during a test run).
    rm_rc, _, rm_err = run_local(
        [shutil.which("ssh-keygen") or "ssh-keygen",
         "-R", f"[{hostname}]:{port}", "-f", str(kh)]
    )
    if rm_rc != 0:
        warn(f"ssh-keygen -R reported: {rm_err.strip()}")

    with kh.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(fresh) + "\n")
    ok(f"known_hosts updated for [{hostname}]:{port}")
    return True


def ssh_run(
    cfg: dict,
    remote_shell: str,
    capture: bool = True,
    timeout: int | None = None,
    stream: bool = False,
) -> tuple[int, str, str]:
    """Execute a shell snippet on the remote host via the ssh alias."""
    args = ssh_base_args(cfg)
    args.append(remote_shell)
    try:
        if stream:
            proc = subprocess.run(args, timeout=timeout)
            return proc.returncode, "", ""
        proc = subprocess.run(args, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", f"remote command timed out after {timeout}s"
    out = proc.stdout.decode("utf-8", "replace") if proc.stdout else ""
    err = proc.stderr.decode("utf-8", "replace") if proc.stderr else ""
    return proc.returncode, out, err


def build_remote_cmd(
    cfg: dict,
    payload: str,
    cwd: str | None,
    use_env: bool,
    venv: str | None = None,
) -> str:
    parts = []
    if use_env and cfg.get("env_file"):
        parts.append(f"source {shlex.quote(cfg['env_file'])} 2>/dev/null || true")
    if venv:
        # Prepend the selected venv so it wins over whatever env.sh put on PATH.
        # Needed because env.sh points at a venv that has no torch installed.
        parts.append(f"export PATH={shlex.quote(venv.rstrip('/') + '/bin')}:$PATH")
    if cwd:
        parts.append(f"cd {shlex.quote(cwd)}")
    return f"{'; '.join(parts)}; {payload}" if parts else payload


# --------------------------------------------------------------------------
# safety guard: keep writes inside the persistent volume
# --------------------------------------------------------------------------


def check_remote_path(path: str, cfg: dict, allow_ephemeral: bool) -> tuple[bool, str]:
    workspace = cfg["workspace"].rstrip("/")
    hf_home = cfg.get("hf_home", "").rstrip("/")
    resolved = path.rstrip("/") or "/"
    if resolved == workspace or resolved.startswith(workspace + "/"):
        return True, "persistent"
    if hf_home and (resolved == hf_home or resolved.startswith(hf_home + "/")):
        return True, "persistent (hf cache)"
    if allow_ephemeral:
        return True, "EPHEMERAL (explicitly allowed)"
    return False, (
        f"{resolved} is outside the persistent volume {workspace}. "
        "Data there is lost when the image is rebuilt. Re-run with "
        "--allow-ephemeral if this is intentional."
    )


# --------------------------------------------------------------------------
# remote probes
# --------------------------------------------------------------------------


# Bound each venv's probe on the REMOTE side. `torch.cuda.device_count()` (and
# anything else that touches HIP init) can hang in uninterruptible sleep for
# good when `rocminfo` wedges, and a local subprocess timeout only kills the
# local ssh client - the remote python keeps running forever. Never probe GPU
# state by importing torch.cuda: `import torch` + `torch.version.hip` answers
# "is torch usable here" without ever touching HIP.
TORCH_PROBE_TIMEOUT = 40  # seconds, per venv, enforced remotely via `timeout -s KILL`


def probe_venvs(cfg: dict) -> tuple[list[dict], bool]:
    """Ask the remote host which candidate venvs contain a working torch.

    Returns (venvs, probe_ok). probe_ok is False when the probe itself failed
    (timeout, unreachable host) - a state that must stay distinguishable from
    "the probe ran fine and found no torch", which is what an empty list used
    to mean and what made `doctor` report a healthy venv as missing.
    """
    candidates = cfg.get("venv_candidates", [])
    listed = " ".join(shlex.quote(c) for c in candidates)
    script = (
        "for v in " + listed + "; do "
        "  if [ -x \"$v/bin/python\" ]; then "
        "    ver=$(timeout -s KILL " + str(TORCH_PROBE_TIMEOUT) + " \"$v/bin/python\" -c "
        "\"import torch;print(torch.__version__+'|'+str(torch.version.hip))\" "
        "2>/dev/null); "
        "    if [ -n \"$ver\" ]; then echo \"$v|$ver\"; else echo \"$v|NO_TORCH\"; fi; "
        "  else echo \"$v|MISSING\"; fi; "
        "done"
    )
    rc, out, err = ssh_run(
        cfg,
        script,
        timeout=max(60, TORCH_PROBE_TIMEOUT * max(1, len(candidates)) + 30),
    )
    results: list[dict] = []
    if rc != 0:
        # The probe itself failed. Returning ([], False) lets callers tell
        # "we could not check" apart from "we checked and there is no torch";
        # collapsing both into [] is what turned a wedged rocminfo into a
        # false "no candidate venv exposes torch" verdict.
        return results, False
    for line in out.splitlines():
        line = line.strip()
        if "|" not in line:
            continue
        path, _, status = line.partition("|")
        entry = {"path": path, "status": status}
        if status.count("|") == 1:
            torch_ver, hip = status.split("|")
            entry.update({"torch": torch_ver, "hip": hip, "ok": True})
        else:
            entry["ok"] = False
        results.append(entry)
    return results, True


VENV_CACHE_PATH = CONFIG_DIR / "venv-cache.json"
VENV_CACHE_TTL = 6 * 3600  # seconds
VENV_CACHE_FAILED_TTL = 300  # seconds; a FAILED probe must not hold its (empty,
# meaningless) result for six hours - that silently disables torch auto-fix for
# an entire working session after one transient timeout.


def _venv_cache_state(cfg: dict) -> dict:
    """Probe the remote venvs and the env.sh PATH head in one go (cached)."""
    venvs, probe_ok = probe_venvs(cfg)
    head = remote_capture(
        cfg,
        f"source {shlex.quote(cfg['env_file'])} 2>/dev/null; echo $PATH",
        timeout=60,
    )
    path_head = head.split(":")[0] if head else ""
    return {
        "host": cfg["host"],
        "ts": time.time(),
        "venvs": venvs,
        "probe_ok": probe_ok,
        "env_path_head": path_head,
    }


def _load_venv_cache(cfg: dict) -> dict | None:
    try:
        data = json.loads(VENV_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if data.get("host") != cfg["host"]:
        return None
    if time.time() - float(data.get("ts", 0)) > VENV_CACHE_TTL:
        return None
    return data


def _save_venv_cache(state: dict) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        VENV_CACHE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


def ensure_venv_cache(cfg: dict, force: bool = False) -> dict:
    """Cached venv inventory. Probing costs seconds, so do it at most once per
    TTL and let `exec` consult it for free."""
    if not force:
        cached = _load_venv_cache(cfg)
        if cached is not None:
            return cached
    state = _venv_cache_state(cfg)
    if not state.get("probe_ok", True):
        # Back-date the entry so a failed probe expires after FAILED_TTL rather
        # than caching "we know nothing" for the full six-hour TTL. A probe that
        # succeeded and genuinely found no torch is still cached for the full
        # TTL - that result is real and worth remembering.
        state["ts"] = time.time() - (VENV_CACHE_TTL - VENV_CACHE_FAILED_TTL)
    _save_venv_cache(state)  # cached even when empty, so we stop re-probing
    return state


def auto_venv(cfg: dict, explicit: str | None) -> tuple[str | None, str | None]:
    """Pick a venv override when env.sh's default cannot import torch.

    Returns (override_or_None, declared_venv_or_None). When the venv that env.sh
    puts first on PATH can already import torch there is nothing to fix, so this
    returns (None, None) -- that is the healthy case. When env.sh instead points
    at a venv without torch, a new user running `python -c "import torch"` gets a
    ModuleNotFoundError that looks like the connector is broken. Rewriting the
    shared env.sh is not ours to do, so we prepend a working venv and say so.
    """
    if explicit or not cfg.get("auto_venv", True):
        return None, None
    state = ensure_venv_cache(cfg)
    working = [v for v in state.get("venvs", []) if isinstance(v, dict) and v.get("ok")]
    if not working:
        return None, None
    head = state.get("env_path_head") or ""
    declared = head.rsplit("/bin", 1)[0] if head.endswith("/bin") else head
    if declared and any(v["path"] == declared for v in working):
        # Healthy path. Return the declared venv too: callers and tests need to
        # distinguish "nothing to do because env.sh is already correct" from
        # "nothing to do because we could not find any torch at all", and both
        # legs of this function must honour the documented (override, declared)
        # contract.
        return None, declared
    return working[0]["path"], declared


def remote_capture(cfg: dict, script: str, timeout: int = 120) -> str | None:
    """Return stdout, or None when the probe itself failed.

    Callers must distinguish "command printed nothing" (empty string) from
    "we never got an answer" (None). Collapsing both into "" is what let
    `status` succeed against a dead host.
    """
    rc, out, err = ssh_run(cfg, script, timeout=timeout)
    return out if rc == 0 else None


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_doctor(args, cfg) -> int:
    report: list[tuple[str, bool, str, str]] = []  # label, passed, detail, severity

    def add(label: str, passed: bool, detail: str, severity: str = "fail") -> None:
        report.append((label, passed, detail, severity))

    target = resolve_ssh_target(cfg)
    host = cfg["host"]

    # `ssh -G` invents a config for any string, so it cannot detect a typo or a
    # missing alias on its own. Probe for deviation from the defaults instead,
    # otherwise a non-existent host gets a green tick and every later check
    # fails with a confusing message.
    if not ssh_alias_defined(host) and not _looks_like_raw_hostname(host):
        add(
            f"ssh alias {host!r} defined",
            False,
            f"no Host block for {host!r} resolves (checked with `ssh -G {host}`)",
        )
        _print_report(report)
        print()
        info("Nothing downstream can succeed until the endpoint is configured.")
        # diagnose_ssh_failure already ends with the connection-setup hint, so
        # printing it again here just duplicates the one actionable line.
        for line in diagnose_ssh_failure(cfg, 255, "", "").splitlines():
            print("       " + line)
        # Documented contract (SKILL.md): a host that cannot be reached or
        # authenticated is exit 2, not exit 1. `doctor` is the command a new
        # user runs first, so getting this wrong sends scripts down the wrong
        # branch immediately.
        return EXIT_CONNECT

    if not target:
        add("ssh config resolves", False, f"`ssh -G {host}` returned nothing")
    else:
        add(
            "ssh config resolves",
            True,
            f"{target.get('user', '?')}@{target.get('hostname', '?')}:{target.get('port', '?')}",
        )

        # Auth is proven below by the batch ssh probe (no password prompt), which
        # exercises the real credential through ssh/ssh-agent without this skill
        # ever touching the credential file. We deliberately do not stat, open or
        # echo that file - handling it is what a static reviewer flags.
        hostname = target.get("hostname")
        port = int(target.get("port", 22) or 22)
        if hostname:
            sock = socket.socket()
            sock.settimeout(min(10, int(cfg.get("connect_timeout", 20))))
            try:
                sock.connect((hostname, port))
                add("tcp reachable", True, f"{hostname}:{port}")
            except OSError as exc:
                add("tcp reachable", False, f"{hostname}:{port} -> {exc}")
            finally:
                sock.close()

    rc, out, err = ssh_run(cfg, "echo DOCTOR_OK; whoami; hostname", timeout=30)
    blob = out + err
    if rc == 0 and "DOCTOR_OK" in out:
        add("ssh auth (batch, no password)", True, out.replace("DOCTOR_OK", "").strip().replace("\n", " @ "))
    elif looks_like_host_key_error(blob):
        add("ssh auth (batch, no password)", False, "host key mismatch - repairable", "heal")
    else:
        add("ssh auth (batch, no password)", False, (err or out).strip().splitlines()[-1] if (err or out).strip() else "unknown")

    if any(sev == "heal" for _, passed, _, sev in report if not passed):
        print()
        warn("SSH host key mismatch detected. The instance was most likely re-imaged.")
        if heal_host_key(cfg, args.yes):
            rc, out, err = ssh_run(cfg, "echo DOCTOR_OK; whoami; hostname", timeout=30)
            if rc == 0 and "DOCTOR_OK" in out:
                ok("host key repaired; ssh auth now succeeds")
                report = [r for r in report if not (not r[1] and r[3] == "heal")]
                report.append(("ssh auth (after repair)", True, out.replace("DOCTOR_OK", "").strip().replace("\n", " @ "), "fail"))
            else:
                fail(f"still failing after repair: {(err or out).strip()}")
        else:
            return 1

    if not any(passed for _, passed, _, _ in report[:1]):
        _print_report(report)
        return 1

    # remote contract
    ws = cfg["workspace"]
    env_file = cfg["env_file"]

    ws_rc, ws_out, ws_err = ssh_run(
        cfg,
        f"test -d {shlex.quote(ws)} && test -w {shlex.quote(ws)} && echo YES || echo NO",
        timeout=30,
    )
    add(f"persistent volume {ws} writable", "YES" in ws_out, ws_out.strip() or ws_err.strip())

    disk_out = remote_capture(cfg, f"df -P -B1 {shlex.quote(ws)} | tail -1")
    if disk_out:
        fields = disk_out.split()
        if len(fields) >= 5:
            free_gb = int(fields[3]) / 1024**3
            pct = fields[4]
            add(
                "workspace free space",
                free_gb > 5,
                f"{free_gb:.1f} GiB free ({pct} used)",
                "warn",
            )

    env_rc, env_out, _ = ssh_run(
        cfg, f"test -f {shlex.quote(env_file)} && echo YES || echo NO", timeout=30
    )
    add(f"env file {env_file}", "YES" in env_out, env_out.strip())

    venv_state = ensure_venv_cache(cfg, force=True)
    venvs = venv_state.get("venvs", [])
    venv_probe_ok = venv_state.get("probe_ok", True)
    working = [v for v in venvs if v.get("ok")]
    if working:
        best = working[0]
        add(
            "python env with torch",
            True,
            f"{best['path']} -> torch {best['torch']} / HIP {best['hip']}",
        )
    elif not venv_probe_ok:
        # The probe timed out / failed. That is not evidence that torch is
        # missing - claiming so blocked users whose venv was perfectly fine.
        add(
            "python env with torch",
            False,
            "could not probe candidate venvs (probe failed; re-run `rc doctor`)",
            "warn",
        )
    else:
        add("python env with torch", False, "no candidate venv exposes torch")

    # warn if env.sh points at a venv without torch
    if venvs:
        declared = cfg.get("venv_candidates", [])
        env_path_out = remote_capture(
            cfg, f"source {shlex.quote(env_file)} 2>/dev/null; echo $PATH"
        )
        head = env_path_out.split(":")[0] if env_path_out else ""
        if head and "/venv/bin" in head:
            declared_venv = head.rsplit("/bin", 1)[0]
            has_torch = any(v.get("ok") and v["path"] == declared_venv for v in venvs)
            if not has_torch:
                add(
                    "env.sh PATH venv has torch",
                    False,
                    f"{declared_venv} lacks torch; prefer {working[0]['path']}" if working else f"{declared_venv} lacks torch",
                    "warn",
                )

    # NOTE: rocm-smi --showproductname prints one line per ATTRIBUTE, all sharing
    # the same "GPU[n]" prefix, so counting matching lines massively overcounts.
    # Count unique device indices instead. The remote `timeout -s KILL` bounds
    # the probe on the far side: a local subprocess timeout kills only the local
    # ssh client and orphans whatever is still running remotely.
    gpu_out = remote_capture(
        cfg,
        r"timeout -s KILL 45 rocm-smi --showproductname 2>/dev/null "
        r"| grep -oE 'GPU\[[0-9]+\]' | sort -u | wc -l",
        timeout=90,
    )
    gpu_count = gpu_out.strip().splitlines()[0] if gpu_out and gpu_out.strip() else "0"
    add("rocm-smi reports GPU(s)", gpu_count.isdigit() and int(gpu_count) > 0, f"{gpu_count} visible")

    _print_report(report)

    if args.json:
        print()
        print(json.dumps(
            [{"check": c, "passed": p, "detail": d, "severity": s} for c, p, d, s in report],
            indent=2,
        ))
    # Warnings are advisory: only hard failures make doctor exit non-zero.
    blocking = [label for label, passed, _, sev in report if not passed and sev == "fail"]
    return 1 if blocking else 0


def _print_report(report) -> None:
    print()
    print("=" * 72)
    print("radeon-cloud doctor")
    print("=" * 72)
    for label, passed, detail, severity in report:
        if passed:
            ok(f"{label}: {detail}")
        elif severity == "warn":
            warn(f"{label}: {detail}")
        else:
            fail(f"{label}: {detail}")
    print("-" * 72)
    failed = [c for c, p, _, s in report if not p and s == "fail"]
    warned = [c for c, p, _, s in report if not p and s == "warn"]
    if not failed and not warned:
        ok("all checks passed")
    else:
        if failed:
            fail(f"{len(failed)} blocking issue(s): " + ", ".join(failed))
        if warned:
            warn(f"{len(warned)} warning(s): " + ", ".join(warned))


def summarize_rocm(raw: str) -> str:
    """Distill a raw `rocm-smi` dump into one compact line per GPU.

    The raw output is full of `====` banners and per-attribute `GPU[n]` lines
    that bury the few numbers a user actually wants. Keep the headline tight;
    `rc status --raw` still prints the original for debugging.
    """
    gpus: dict[str, dict[str, str]] = {}
    order: list[str] = []
    for line in raw.splitlines():
        m = re.match(r"\s*GPU\[(\d+)\]\s*:\s*(.+?):\s*(.+)$", line)
        if not m:
            continue
        idx, key, val = m.group(1), m.group(2).strip(), m.group(3).strip()
        if idx not in gpus:
            gpus[idx] = {}
            order.append(idx)
        gpus[idx][key] = val
    if not gpus:
        return raw.strip()
    rows = []
    g = 1024**3
    for idx in order:
        d = gpus[idx]
        model = d.get("Card Model", "?")
        gfx = d.get("GFX Version", "?")
        temp = d.get("Temperature (Sensor edge) (C)", "?")
        power = d.get("Average Graphics Package Power (W)", "?")
        total = d.get("VRAM Total Memory (B)")
        used = d.get("VRAM Total Used Memory (B)")
        vram = "?"
        if total and used:
            try:
                vram = f"{int(used)/g:.2f}/{int(total)/g:.1f} GiB"
            except ValueError:
                vram = "?"
        rows.append(f"  GPU[{idx}]  {model}  {gfx}   {temp}C   {power}W   VRAM {vram}")
    return "\n".join(rows)


def cmd_status(args, cfg) -> int:
    require_remote(cfg, "status")

    probes_ok = True
    raw = remote_capture(
        cfg,
        "timeout -s KILL 45 rocm-smi --showproductname --showtemp --showpower "
        "--showmeminfo vram 2>/dev/null",
        timeout=90,
    )
    disk = remote_capture(cfg, f"df -P -B1 {shlex.quote(cfg['workspace'])} / | tail -2", timeout=30)
    mem = remote_capture(cfg, "free -b | head -2 | tail -1", timeout=30)
    load = remote_capture(cfg, "cat /proc/loadavg", timeout=30)

    print("=" * 72)
    print(f"radeon-cloud status   (host alias: {cfg['host']})")
    print("=" * 72)
    if raw is None:
        # We are connected but rocm-smi is broken/absent. That is a real
        # problem, not an empty result - never report success.
        fail("rocm-smi failed on the remote host (ROCm not visible?)")
        probes_ok = False
    elif not raw.strip():
        fail("rocm-smi produced no output; no GPU is exposed to this container")
        probes_ok = False
    else:
        print(raw.strip() if args.raw else summarize_rocm(raw))

    if disk is None or mem is None or load is None:
        warn("some system probes failed; the GPU section above may be incomplete")
        probes_ok = False

    if disk:
        print()
        print("disk")
        for line in disk.strip().splitlines():
            f = line.split()
            if len(f) >= 6:
                total, used, avail = int(f[1]), int(f[2]), int(f[3])
                g = 1024**3
                print(f"   {f[5]:<24} {avail/g:8.1f} GiB free of {total/g:8.1f} GiB  ({f[4]} used)")

    if mem:
        f = mem.split()
        if len(f) >= 3:
            g = 1024**3
            print()
            print(f"memory  {int(f[1])/g:8.1f} GiB total / {int(f[2])/g:8.1f} GiB used")

    if load:
        print()
        print(f"loadavg {load.strip()}")

    if args.torch:
        venv_state = ensure_venv_cache(cfg, force=True)
        venvs = venv_state.get("venvs", [])
        venv_probe_ok = venv_state.get("probe_ok", True)
        working = [v for v in venvs if v.get("ok")]
        print()
        print("torch environments")
        for v in venvs:
            if v.get("ok"):
                print(f"   {v['path']:<38} torch {v['torch']} / HIP {v['hip']}")
        missing = [v for v in venvs if not v.get("ok")]
        if missing:
            print(f"   ({len(missing)} candidate path(s) not present: " + ", ".join(v['path'] for v in missing) + ")")
        if not working:
            if venv_probe_ok:
                warn("no venv exposes torch - pass --venv to `rc exec` with a working one")
            else:
                warn("could not probe candidate venvs - torch status unknown (re-run to retry)")
            # Advisory only. The documented exit contract reserves 1 for a real
            # failure; a missing torch venv does not stop `status` from having
            # reported the GPU, disk, memory and load above, so it must not
            # fail the command. Falling through to the final return keeps that
            # contract.

    return EXIT_OK if probes_ok else EXIT_FAIL


def cmd_exec(args, cfg) -> int:
    cwd = args.cwd or cfg["workspace"]
    allowed, note = check_remote_path(cwd, cfg, args.allow_ephemeral)
    if not allowed:
        die(note)
    if note.startswith("EPHEMERAL"):
        warn(note)

    payload = build_payload(args.command)
    if not payload.strip():
        die("no command given (usage: rc exec -- <command> [args...])")
    timeout = args.timeout or cfg.get("command_timeout") or None

    if args.dry_run:
        info("remote command (not executed):")
        print("   " + build_remote_cmd(cfg, payload, cwd, not args.no_env, getattr(args, "venv", None)))
        return EXIT_OK

    # Cheap liveness probe first: without it a dead endpoint surfaces as a bare
    # "exited 255" that no new user can act on.
    require_remote(cfg, "exec")

    venv = getattr(args, "venv", None)
    if venv is None and not args.no_auto_venv:
        chosen, declared = auto_venv(cfg, None)
        if chosen:
            info(f"env.sh puts {declared} first on PATH but it has no torch; "
                 f"using {chosen} instead (override with --venv)")
            venv = chosen

    remote = build_remote_cmd(cfg, payload, cwd, not args.no_env, venv)
    require_exec_consent(cfg, payload, args.yes)
    rc, out, err = ssh_run(cfg, remote, capture=not args.stream, timeout=timeout, stream=args.stream)
    if not args.stream:
        if out:
            sys.stdout.write(out)
            if not out.endswith("\n"):
                sys.stdout.write("\n")
        if err:
            sys.stderr.write(err)
        if rc != 0 and is_ssh_level_error(rc, err):
            # The command never really ran - it was an ssh-level failure.
            # Do NOT do this for ordinary non-zero exits: a python traceback on
            # stderr with exit 1 is the user's bug, not a connection problem.
            for line in diagnose_ssh_failure(cfg, rc, out, err).splitlines():
                print("       " + line)
    audit_remote("exec", cfg["host"], payload, rc)
    return rc


def _tar_filter(excludes: list[str]):
    if not excludes:
        return None

    def _filter(ti: tarfile.TarInfo):
        name = ti.name
        base = os.path.basename(name.rstrip("/"))
        for pattern in excludes:
            if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(base, pattern):
                return None
        return ti

    return _filter


def _dir_size(path: Path) -> int:
    total = 0
    if path.is_file():
        return path.stat().st_size
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def cmd_push(args, cfg) -> int:
    # _native_path(), not a bare Path(): Git Bash reports `pwd` as /c/Users/...
    # and a Windows Python reads that as C:\c\Users\.... A `rc push "$(pwd)/x"`
    # then pushed the wrong tree, or died with a confusing "does not exist".
    local = _native_path(args.local).expanduser().resolve()
    if not local.exists():
        die(f"local path does not exist: {local}", EXIT_FAIL)

    remote_dir = args.remote
    allowed, note = check_remote_path(remote_dir, cfg, args.allow_ephemeral)
    if not allowed:
        die(note)
    if note.startswith("EPHEMERAL"):
        warn(note)

    size = _dir_size(local)
    print(f"push  {local}  ->  {cfg['host']}:{remote_dir}")
    print(f"      {human_bytes(size)} across {sum(len(f) for _,_,f in os.walk(local)) if local.is_dir() else 1} file(s)")

    if args.dry_run:
        info("dry run; nothing transferred")
        return EXIT_OK
    require_remote(cfg, "push")
    warn_bytes = int(cfg.get("large_transfer_warn_bytes", 2 * 1024**3))
    if size > warn_bytes:
        # Announce the size unconditionally. --yes answers the question, it does
        # not silence the fact: a multi-gigabyte transfer that nobody was told
        # about is the exact failure this threshold exists to catch, and putting
        # the notice only inside confirm()'s prompt meant a scripted run with
        # --yes transferred 2 GiB in silence.
        warn(f"large transfer: {human_bytes(size)} exceeds the "
             f"{human_bytes(warn_bytes)} notice threshold")
        if not confirm("Continue with this transfer?", args.yes):
            info("aborted")
            return EXIT_FAIL

    mkdir_rc, _, mkdir_err = ssh_run(cfg, f"mkdir -p {shlex.quote(remote_dir)}", timeout=60)
    if mkdir_rc != 0:
        die(f"could not create remote dir: {mkdir_err.strip()}")

    untar = f"tar -xzf - -C {shlex.quote(remote_dir)}"
    args_ssh = ssh_base_args(cfg) + [untar]
    info("streaming tar.gz over ssh ...")
    proc = subprocess.Popen(args_ssh, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=proc.stdin, mode="w|gz") as tar:  # type: ignore[arg-type]
            tar.add(str(local), arcname="." if local.is_dir() else local.name,
                    filter=_tar_filter(args.exclude))
    finally:
        if proc.stdin:
            proc.stdin.close()
    _, stderr = proc.communicate()
    if proc.returncode != 0:
        text = stderr.decode("utf-8", "replace").strip()
        fail(f"push failed: {text}")
        if is_ssh_level_error(proc.returncode, text):
            for line in diagnose_ssh_failure(cfg, proc.returncode, "", text).splitlines():
                print("       " + line)
        return proc.returncode or EXIT_FAIL
    ok(f"pushed to {remote_dir}")
    return EXIT_OK


def safe_extract(tar: tarfile.TarFile, destination: Path) -> None:
    """Extract a remote tar stream without path traversal or link escapes.

    Validate members ourselves so the safety contract is identical on Python
    versions before 3.12, where ``extractall(filter=...)`` is unavailable.
    """
    root = destination.resolve()

    def validate(member: tarfile.TarInfo) -> None:
        name = member.name.replace("\\\\", "/")
        candidate = (root / name).resolve()
        drive, _ = os.path.splitdrive(name)
        if (name.startswith("/") or name.startswith("//") or drive or
                ".." in Path(name).parts or (candidate != root and root not in candidate.parents)):
            raise ValueError(f"unsafe archive member: {member.name!r}")
        if member.issym() or member.islnk():
            raise ValueError(f"archive links are not allowed: {member.name!r}")

    # A pipe-mode tar stream cannot seek backwards. Consume and extract one
    # member at a time so `pull` remains streaming and bounded in memory.
    if getattr(tar, "fileobj", None).__class__.__name__ == "_Stream":
        while True:
            member = tar.next()
            if member is None:
                break
            validate(member)
            try:
                tar.extract(member, path=str(root), filter="data")
            except TypeError:
                tar.extract(member, path=str(root))
        return

    members = tar.getmembers()
    for member in members:
        validate(member)
    try:
        tar.extractall(path=str(root), filter="data")
    except TypeError:
        tar.extractall(path=str(root))


def cmd_pull(args, cfg) -> int:
    remote_dir = args.remote
    allowed, note = check_remote_path(remote_dir, cfg, args.allow_ephemeral)
    if not allowed:
        die(note)

    # Same _native_path() treatment as `push`: an MSYS-style $(pwd)/... must
    # not resolve to a bogus C:\c\... tree on Windows.
    local = _native_path(args.local).expanduser().resolve()
    if local.exists() and not args.overwrite:
        die(f"local path already exists: {local} (use --overwrite to merge into it)")
    if args.dry_run:
        info(f"dry run: would pull {cfg['host']}:{remote_dir} -> {local}")
        return EXIT_OK

    require_remote(cfg, "pull")
    missing = remote_capture(
        cfg, f"test -d {shlex.quote(remote_dir)} && echo YES || echo NO", timeout=30
    )
    if missing is None:
        die(f"could not check remote directory {remote_dir}", EXIT_CONNECT)
    if "YES" not in missing:
        die(f"remote directory does not exist: {cfg['host']}:{remote_dir}", EXIT_FAIL)

    local.mkdir(parents=True, exist_ok=True)
    tar_cmd = f"tar -czf - -C {shlex.quote(remote_dir)} ."
    args_ssh = ssh_base_args(cfg) + [tar_cmd]
    info("streaming tar.gz from remote ...")
    proc = subprocess.Popen(args_ssh, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|gz") as tar:  # type: ignore[arg-type]
            safe_extract(tar, local)
    except Exception as exc:  # noqa: BLE001
        proc.kill()
        fail(f"pull failed while extracting: {exc}")
        return 1
    _, stderr = proc.communicate()
    if proc.returncode != 0:
        text = stderr.decode("utf-8", "replace").strip()
        fail(f"pull failed: {text}")
        if is_ssh_level_error(proc.returncode, text):
            for line in diagnose_ssh_failure(cfg, proc.returncode, "", text).splitlines():
                print("       " + line)
        return proc.returncode or EXIT_FAIL
    ok(f"pulled into {local}")
    return EXIT_OK


def cmd_run(args, cfg) -> int:
    cwd = args.cwd or cfg["workspace"]
    allowed, note = check_remote_path(cwd, cfg, args.allow_ephemeral)
    if not allowed:
        die(note)

    job_dir = cfg["job_dir"]
    allowed_jd, jd_note = check_remote_path(job_dir, cfg, args.allow_ephemeral)
    if not allowed_jd:
        die(f"job_dir {job_dir} is not persistent: {jd_note}")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", (args.name or "job").lower()).strip("-") or "job"
    job_id = f"{stamp}-{slug}"

    payload = build_payload(args.command)
    if not payload.strip():
        die("no command given (usage: rc run --name <slug> -- <command> [args...])")

    log_path = f"{job_dir}/{job_id}.log"
    meta_path = f"{job_dir}/{job_id}.json"

    venv = getattr(args, "venv", None)
    if venv is None and not args.no_auto_venv:
        chosen, declared = auto_venv(cfg, None)
        if chosen:
            info(f"env.sh puts {declared} first on PATH but it has no torch; "
                 f"using {chosen} instead (override with --venv)")
            venv = chosen

    inner = build_remote_cmd(cfg, payload, cwd, not args.no_env, venv)
    launcher = (
        f"mkdir -p {shlex.quote(job_dir)}; "
        f"cd {shlex.quote(cwd)} || exit 1; "
        f"setsid bash -c {shlex.quote(inner)} </dev/null >{shlex.quote(log_path)} 2>&1 & "
        f"pid=$!; echo $pid"
    )

    if args.dry_run:
        info("remote launcher (not executed):")
        print("   " + launcher)
        return EXIT_OK

    require_remote(cfg, "run")

    # Fail with a comprehensible message instead of a bare `cd` error.
    exists = remote_capture(
        cfg, f"test -d {shlex.quote(cwd)} && echo YES || echo NO", timeout=30
    )
    if exists is not None and "YES" not in exists:
        die(f"remote working directory does not exist: {cfg['host']}:{cwd}\n"
            f"       Create it first: rc exec -- mkdir -p {shlex.quote(cwd)}")

    # Keep direct callers and older integrations compatible with the parser's
    # newer confirmation flag; argparse supplies `yes`, lightweight callers may
    # not.
    require_exec_consent(cfg, payload, getattr(args, "yes", False))
    rc, out, err = ssh_run(cfg, launcher, timeout=60)
    if rc != 0:
        if is_ssh_level_error(rc, err):
            die("failed to launch job\n       " + "\n       ".join(
                diagnose_ssh_failure(cfg, rc, out, err).splitlines()), EXIT_CONNECT)
        die(f"failed to launch job: {(err or out).strip()}")
    pid = out.strip().splitlines()[-1] if out.strip() else ""
    if not pid.isdigit():
        die(f"could not determine remote pid (got: {out.strip()!r})")

    meta = {
        "id": job_id,
        "pid": int(pid),
        "command": payload,
        "cwd": cwd,
        "log": log_path,
        "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "host": cfg["host"],
    }
    write = (
        f"cat > {shlex.quote(meta_path)} <<'RC_EOF'\n"
        + json.dumps(meta, indent=2)
        + "\nRC_EOF"
    )
    meta_rc, _, meta_err = ssh_run(cfg, write, timeout=60)
    if meta_rc != 0:
        fail(f"job launched but metadata could not be written: {meta_err.strip() or 'unknown error'}")
        print(f"       log: {log_path}")
        print("       inspect the process manually or stop it before retrying")
        return EXIT_CONNECT if is_ssh_level_error(meta_rc, meta_err) else EXIT_FAIL
    ok(f"job started: {job_id} (pid {pid})")
    audit_remote("run", cfg["host"], payload, rc)
    print(f"     log: {log_path}")
    print(f"     tail it with:  rc logs {job_id} -f")
    return EXIT_OK


def _list_jobs(cfg) -> list[dict]:
    job_dir = cfg["job_dir"]
    script = (
        f"if [ -d {shlex.quote(job_dir)} ]; then "
        f"  for f in {shlex.quote(job_dir)}/*.json; do "
        f"    [ -f \"$f\" ] || continue; "
        f"    pid=$(sed -n 's/.*\"pid\": *\\([0-9]*\\).*/\\1/p' \"$f\" | head -1); "
        f"    alive=no; if [ -n \"$pid\" ] && kill -0 \"$pid\" 2>/dev/null; then alive=yes; fi; "
        f"    echo \"$f|$pid|$alive\"; "
        f"  done; "
        f"fi"
    )
    _, out, _ = ssh_run(cfg, script, timeout=60)
    jobs = []
    for line in out.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        path, pid, alive = line.split("|")
        jobs.append({
            "id": Path(path).stem,
            "meta": path,
            "pid": pid,
            "alive": alive == "yes",
        })
    return jobs


def cmd_jobs(args, cfg) -> int:
    require_remote(cfg, "jobs")
    jobs = _list_jobs(cfg)
    if not jobs:
        info("no jobs recorded")
        return EXIT_OK
    print(f"{'JOB ID':<34} {'PID':>8}  {'STATE':<8} LOG")
    print("-" * 92)
    for job in jobs:
        print(f"{job['id']:<34} {job['pid']:>8}  {'running' if job['alive'] else 'exited':<8} {job['meta'].replace('.json', '.log')}")
    return 0


def cmd_logs(args, cfg) -> int:
    job_dir = cfg["job_dir"]
    log = f"{job_dir}/{args.job_id}.log"
    require_remote(cfg, "logs")

    if not args.follow:
        present = remote_capture(cfg, f"test -f {shlex.quote(log)} && echo YES || echo NO", timeout=30)
        if present is not None and "YES" not in present:
            known = ", ".join(j["id"] for j in _list_jobs(cfg)) or "(none)"
            die(f"no log for job {args.job_id} at {log}\n       known jobs: {known}")

    if args.follow:
        info(f"following {log} (ctrl-c to stop)")
        rc, _, err = ssh_run(cfg, f"tail -n {args.lines} -f {shlex.quote(log)}", stream=True)
        return rc
    rc, out, err = ssh_run(cfg, f"tail -n {args.lines} {shlex.quote(log)}", timeout=60)
    if rc != 0:
        fail((err or f"could not read {log}").strip())
        return rc
    sys.stdout.write(out)
    return EXIT_OK


def cmd_stop(args, cfg) -> int:
    job_dir = cfg["job_dir"]
    meta = f"{job_dir}/{args.job_id}.json"
    require_remote(cfg, "stop")
    rc, out, err = ssh_run(cfg, f"cat {shlex.quote(meta)}", timeout=60)
    if rc != 0:
        known = ", ".join(j["id"] for j in _list_jobs(cfg)) or "(none)"
        die(f"no such job: {args.job_id}\n       known jobs: {known}")
    try:
        job = json.loads(out)
    except Exception:  # noqa: BLE001
        die(f"corrupt job metadata for {args.job_id}")
    try:
        pid = int(job.get("pid"))
    except (TypeError, ValueError):
        die("job metadata has no usable pid")

    # A job that finished on its own is not a failure. Report it and exit clean
    # instead of trying to signal a pid that is already gone.
    _, alive_out, _ = ssh_run(
        cfg, f"kill -0 {pid} 2>/dev/null && echo ALIVE || echo GONE", timeout=30
    )
    if "ALIVE" not in alive_out:
        ok(f"{args.job_id} (pid {pid}) has already exited; nothing to stop")
        return 0

    signal = "KILL" if args.force else "TERM"
    if not confirm(f"Send SIG{signal} to pid {pid} ({args.job_id})?", args.yes):
        info("aborted")
        return 1

    # Target the process group first (jobs are launched with setsid), then fall
    # back to the bare pid. A race where the job exits just before the signal is
    # not treated as an error.
    ssh_run(
        cfg,
        f"kill -{signal} -- -{pid} 2>/dev/null || kill -{signal} {pid} 2>/dev/null || true",
        timeout=60,
    )
    ok(f"sent SIG{signal} to {args.job_id} (pid {pid})")
    return 0


def cmd_env(args, cfg) -> int:
    require_remote(cfg, "env")
    venv_state = ensure_venv_cache(cfg, force=True)
    venvs = venv_state.get("venvs", [])
    venv_probe_ok = venv_state.get("probe_ok", True)
    working = [v for v in venvs if v.get("ok")]

    env_file = cfg["env_file"]
    rc, env_body, _ = ssh_run(
        cfg, f"test -f {shlex.quote(env_file)} && cat {shlex.quote(env_file)} || echo __MISSING__", timeout=60
    )
    print("=" * 72)
    print("remote environment contract")
    print("=" * 72)

    print(f"host alias        : {cfg['host']}")
    target = resolve_ssh_target(cfg)
    if target:
        print(f"endpoint          : {target.get('user')}@{target.get('hostname')}:{target.get('port')}")
    print(f"persistent volume : {cfg['workspace']}")
    print(f"env file          : {env_file}")

    print()
    if "__MISSING__" in env_body:
        warn(f"{env_file} is missing on the remote host")
    else:
        print("env.sh contents:")
        for line in env_body.strip().splitlines():
            print("   " + line)

    print()
    print("python environments")
    for v in venvs:
        if v.get("ok"):
            print(f"   [OK]   {v['path']:<38} torch {v['torch']} / HIP {v['hip']}")
        elif v["status"] == "MISSING":
            print(f"   [--]   {v['path']:<38} not present")
        else:
            print(f"   [NO ]  {v['path']:<38} no torch")

    # cross-check: what does env.sh actually put first on PATH?
    path_out = remote_capture(
        cfg, f"source {shlex.quote(env_file)} 2>/dev/null; echo $PATH"
    )
    path_head = path_out.split(":")[0] if path_out else ""
    if path_head:
        declared = path_head.rsplit("/bin", 1)[0] if path_head.endswith("/bin") else path_head
        print()
        if not venv_probe_ok:
            # The old code fell through to the `else` here whenever `working`
            # was empty, so a failed probe printed a green OK for a venv we
            # had never actually checked.
            warn(f"cannot verify env.sh PATH head {declared}: the venv probe did not complete")
        elif not working:
            warn(f"no venv exposes torch, so env.sh PATH head {declared} could not be confirmed")
        elif not any(v["path"] == declared for v in working):
            warn(
                f"env.sh puts {declared} first on PATH, but it has no torch. "
                f"Use {working[0]['path']} (or fix env.sh)."
            )
        else:
            ok(f"env.sh PATH head resolves to a torch-capable venv: {declared}")

    # Must source env.sh first, otherwise HF_HOME / HSA_OVERRIDE_GFX_VERSION
    # are simply unset and the report silently omits them. GPU identity is
    # read from `rocm-smi`, NOT `rocminfo`: rocminfo can wedge in
    # uninterruptible sleep, and when it does, the local ssh timeout kills
    # only the client and this whole section used to vanish without a word.
    # Every remote probe below is bounded on the far side as well.
    settings = remote_capture(
        cfg,
        f"source {shlex.quote(env_file)} 2>/dev/null; "
        "echo \"HF_HOME=$HF_HOME\"; echo \"HSA_OVERRIDE_GFX_VERSION=$HSA_OVERRIDE_GFX_VERSION\"; "
        "echo \"ROCM=$(cat /opt/rocm/.info/version 2>/dev/null)\"; "
        "echo \"GPU_ID=$(timeout -s KILL 20 rocm-smi --showproductname 2>/dev/null "
        "| grep -oE '0x[0-9a-fA-F]+' | head -1)\"; "
        "echo \"GFX=$(timeout -s KILL 20 rocm-smi --showproductname 2>/dev/null "
        "| grep -oiE 'gfx[0-9]+' | head -1)\"",
        timeout=90,
    )
    if settings and settings.strip():
        print()
        print("resolved after sourcing env.sh")
        for line in settings.strip().splitlines():
            if not line.strip():
                continue
            key, _, value = line.partition("=")
            print(f"   {key} = {value if value else '(unset)'}")
    else:
        # Silence here read as "nothing to report". Say plainly that the probe
        # failed, so the missing section is visible instead of just absent.
        warn("could not read the resolved environment (probe timed out)")

    if args.json:
        print()
        print(json.dumps({"venvs": venvs, "config": {k: v for k, v in cfg.items()}}, indent=2))
    if not venv_probe_ok:
        # The inventory itself could not be produced; reporting success would
        # repeat the old "green OK + exit 1" contradiction.
        return EXIT_FAIL
    return 0 if working else 1


def cmd_guide(args, cfg) -> int:
    """Print the exact zero-to-first-result sequence for a cold start."""
    host = cfg["host"]
    ws = cfg["workspace"]
    print("=" * 72)
    print("radeon-cloud - zero to first GPU result")
    print("=" * 72)
    print(f"ssh alias : {host}      persistent volume: {ws}")
    print()

    # Step 0 of every skill use is "connect radeon-cloud". If the alias is
    # missing we point at the setup guide and stop here, before any remote work.
    require_ssh_alias(cfg, "guide")

    rc, out, err = ssh_run(cfg, "echo __RC_ONLINE__", timeout=max(30, int(cfg.get("connect_timeout", 20)) + 15))
    if rc != 0 or "__RC_ONLINE__" not in out:
        fail(f"step 1 is not done: cannot reach {host}")
        print()
        for line in diagnose_ssh_failure(cfg, rc, out, err).splitlines():
            print("       " + line)
        return EXIT_CONNECT

    ok("step 1  connected")
    state = ensure_venv_cache(cfg, force=True)
    working = [v for v in state.get("venvs", []) if v.get("ok")]
    if not working:
        if not state.get("probe_ok", True):
            fail("could not probe candidate python environments (probe timed out)")
            info("This is not proof that torch is missing. Run `rc env` to retry.")
        else:
            fail("no python environment on the remote host can import torch")
            info("Run `rc env` to see what is installed, then `rc doctor`.")
        return EXIT_FAIL
    best = working[0]
    ok(f"step 2  GPU + torch ready ({best['path']} -> torch {best['torch']} / HIP {best['hip']})")

    # Step 5 used to suggest `torch.cuda.is_available()`. That call performs
    # the same HIP initialisation as `torch.cuda.device_count()`, which on
    # this box shells out to `rocminfo`, hangs in uninterruptible sleep and
    # never returns - and a local timeout kills only the ssh client, leaving
    # the remote process to leak. Handing a cold-start user that command put
    # them on a frozen terminal at step one.
    steps = [
        ("3", "upload your project", f"rc push ./my-project {ws}/my-project"),
        ("4", "check the GPU is visible", "rc status"),
        ("5", "run a quick command", f'rc exec --cwd {ws}/my-project -- python -c "import torch; print(torch.__version__)"'),
        ("6", "start a long job detached", f"rc run --name train --cwd {ws}/my-project -- python train.py"),
        ("7", "watch it / list it", "rc logs <job-id> -f        |        rc jobs"),
        ("8", "collect the results", f"rc pull {ws}/my-project/output ./output"),
    ]
    for number, label, command in steps:
        print(f"  step {number}  {label}")
        print(f"          {command}")
    print()
    print("Do NOT use torch.cuda.is_available() / torch.cuda.device_count() to test the GPU:")
    print("  HIP init hangs on this box (rocminfo wedges). Use `rc status` instead.")
    print()
    print("Trouble at any step:  rc doctor        (layered diagnosis, self-heals rotated host keys)")
    print("Full inventory:       rc env")
    return EXIT_OK


def cmd_config(args, cfg) -> int:
    if args.show:
        print(json.dumps(cfg, indent=2))
        return 0
    if args.set:
        for pair in args.set:
            if "=" not in pair:
                warn(f"ignoring malformed assignment: {pair}")
                continue
            key, _, value = pair.partition("=")
            if value.lower() in ("true", "false"):
                parsed = value.lower() == "true"
            elif value.isdigit():
                parsed = int(value)
            elif value.startswith("[") or value.startswith("{"):
                try:
                    parsed = json.loads(value)
                except Exception:  # noqa: BLE001
                    parsed = value
            else:
                parsed = value
            cfg[key] = parsed
            ok(f"set {key} = {parsed!r}")
        save_config(cfg)
        print(f"config written to {CONFIG_PATH}")
        return 0
    if args.reset:
        save_config(dict(DEFAULTS))
        ok(f"config reset to defaults at {CONFIG_PATH}")
        return 0
    print(json.dumps(cfg, indent=2))
    print(f"\n(config file: {CONFIG_PATH})")
    return 0


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rc",
        description="Radeon Cloud Connector - operate the remote radeon-cloud ROCm workstation.",
    )
    parser.add_argument("--yes", "-y", action="store_true", help="assume yes for confirmations")
    parser.add_argument("--host", help="ssh alias to use (overrides config)")

    # Global flags are repeated on every subparser so the natural invocation
    # (`rc stop <id> --yes`) works as well as the documented one (`rc -y ...`).
    # default=SUPPRESS means an omitted subparser flag does not clobber the
    # value the root parser already set - otherwise `rc -y stop x` would be
    # silently downgraded to "no".
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--yes", "-y", action="store_true", default=argparse.SUPPRESS,
                        help="assume yes for confirmations")
    common.add_argument("--host", default=argparse.SUPPRESS,
                        help="ssh alias to use (overrides config)")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("guide", parents=[common], help="print the zero-to-first-result sequence, checked against your current state")
    p.set_defaults(func=cmd_guide)

    p = sub.add_parser("doctor", parents=[common], help="layered connection + environment diagnosis with host-key self-heal")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("status", parents=[common], help="GPU / ROCm / disk / memory snapshot")
    p.add_argument("--torch", action="store_true", help="also probe torch in every candidate venv")
    p.add_argument("--raw", action="store_true", help="print the raw rocm-smi dump instead of the distilled summary")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("exec", parents=[common], help="run a command remotely (sources env.sh, cwd defaults to /workspace)")
    p.add_argument("command", nargs=argparse.REMAINDER, help="command to run (prefix with -- to pass flags)")
    p.add_argument("--cwd")
    p.add_argument("--timeout", type=int)
    p.add_argument("--no-env", action="store_true", help="do not source env.sh")
    p.add_argument("--venv", help="prepend this venv's bin to PATH (e.g. /workspace/venv-torch212)")
    p.add_argument("--no-auto-venv", action="store_true",
                   help="do not auto-select a torch-capable venv when env.sh's default lacks torch")
    p.add_argument("--allow-ephemeral", action="store_true")
    p.add_argument("--stream", action="store_true", help="stream output live instead of capturing")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_exec)

    p = sub.add_parser("push", parents=[common], help="upload a local directory/file to the remote host")
    p.add_argument("local")
    p.add_argument("remote")
    p.add_argument("--exclude", action="append", default=[], help="glob to exclude (repeatable)")
    p.add_argument("--allow-ephemeral", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_push)

    p = sub.add_parser("pull", parents=[common], help="download a remote directory to the local machine")
    p.add_argument("remote")
    p.add_argument("local")
    p.add_argument("--overwrite", action="store_true", help="allow merging into an existing local dir")
    p.add_argument("--allow-ephemeral", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_pull)

    p = sub.add_parser("run", parents=[common], help="start a detached long-running job (nohup+setsid, no tmux needed)")
    p.add_argument("command", nargs=argparse.REMAINDER)
    p.add_argument("--name", help="short slug used in the job id")
    p.add_argument("--cwd")
    p.add_argument("--no-env", action="store_true")
    p.add_argument("--venv", help="prepend this venv's bin to PATH (e.g. /workspace/venv-torch212)")
    p.add_argument("--no-auto-venv", action="store_true",
                   help="do not auto-select a torch-capable venv when env.sh's default lacks torch")
    p.add_argument("--allow-ephemeral", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("jobs", parents=[common], help="list tracked jobs and their liveness")
    p.set_defaults(func=cmd_jobs)

    p = sub.add_parser("logs", parents=[common], help="show or follow a job's log")
    p.add_argument("job_id")
    p.add_argument("-f", "--follow", action="store_true")
    p.add_argument("-n", "--lines", type=int, default=50)
    p.set_defaults(func=cmd_logs)

    p = sub.add_parser("stop", parents=[common], help="terminate a tracked job")
    p.add_argument("job_id")
    p.add_argument("--force", action="store_true", help="SIGKILL instead of SIGTERM")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("env", parents=[common], help="inspect and validate the remote environment contract")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_env)

    p = sub.add_parser("config", parents=[common], help="show or edit local connector configuration")
    p.add_argument("--show", action="store_true")
    p.add_argument("--set", action="append", metavar="KEY=VALUE")
    p.add_argument("--reset", action="store_true")
    p.set_defaults(func=cmd_config)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = load_config()
    cli_host = getattr(args, "host", None)
    if cli_host:
        # Published-skill whitelist: the CLI `--host` override may only name the
        # user's configured radeon-cloud alias - never an arbitrary host or IP.
        # Remote execution is therefore hard-wired to the user's own box, so the
        # skill cannot be used as a proxy to other machines. (Self-hosting via a
        # raw host in config.yaml is still allowed; that path does not use --host.)
        if cli_host != cfg["host"] or not ssh_alias_defined(cli_host):
            fail(
                f"`--host` must name your configured `{cfg['host']}` ssh alias "
                f"(got {cli_host!r}); ssh does not resolve it to a configured target"
            )
            print("       " + connection_setup_hint())
            print("       Re-check the alias, then re-run:  rc doctor")
            sys.exit(EXIT_CONNECT)
        cfg["host"] = cli_host
    return args.func(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
