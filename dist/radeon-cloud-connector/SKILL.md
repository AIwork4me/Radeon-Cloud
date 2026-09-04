---
name: radeon-cloud-connector
description: "Operate the AMD Radeon Cloud GPU workstation AMD provides to you for free - one remote Ubuntu box with a 48 GB VRAM gfx1100 GPU, reached through the `radeon-cloud` SSH alias configured on this machine. Diagnose and self-heal SSH connection and rotated host keys, inspect ROCm and GPU status via rocm-smi, discover which Python venvs carry torch, run commands remotely, sync code and results between your machine and the box, and manage detached long-running jobs with logs. Start with `rc guide`. Triggers: radeon-cloud, Radeon cloud, Radeon 云, ROCm 远程, 远程 GPU, rocm-smi, gfx1100, 上传到 radeon, 下载结果, 跑训练, 后台任务, GPU 显存."
agent_created: true
version: 1.0.3
category: developer-tools
platforms: [windows, macos, linux]
---

# Radeon Cloud Connector

> Security model and credential handling are documented in
> [`SECURITY.md`](SECURITY.md) — the skill never touches your SSH credential
> file, only ever targets your configured `radeon-cloud` alias, and audits
> every remote command.

## Overview

`radeon-cloud` is a free-of-charge AMD Radeon Cloud instance: an Ubuntu 24.04 container with one AMD Navi 31 GPU (gfx1100, about 48 GB VRAM) and ROCm installed. This skill wraps it behind a single CLI, `scripts/rc.py`, which exists because five things on this box repeatedly go wrong and each one costs real time when handled by hand: the SSH host key rotates whenever the instance is re-imaged, `/workspace/env.sh` puts a venv without torch first on `PATH`, the container overlay silently discards anything written outside `/workspace`, long jobs die when the local terminal goes away, and a broken endpoint otherwise looks like a working one.

Prefer the CLI over hand-rolled `ssh` one-liners. It sources the environment, applies the persistence guard, handles host-key rotation and tracks job state.

## Invocation

`PY` is any Python 3.10+ interpreter (PEP 604 union syntax); `RC` is this skill's `scripts/rc.py`. Use forward slashes on Windows.

```bash
PY="${HOME}/.workbuddy-ai/binaries/python/versions/3.13.12/python.exe"   # managed interpreter
PY="${PY:-python3}"                                                      # fallback
RC="<skill-dir>/scripts/rc.py"

"$PY" "$RC" doctor
"$PY" "$RC" status --torch
```

Every subcommand accepts `-y/--yes` to skip confirmation prompts. Interactive use prompts once before connecting; scripted use (CI, another agent, a pipe) is refused unless you pass `--yes` for that command, or enable unattended execution with `RC_ALLOW_UNATTENDED=1` or `allow_unattended: true` in `config.yaml`. `--host <alias>` is a whitelist, not a general override: it only accepts the already-configured `radeon-cloud` alias and rejects every other value with exit `2`. That is deliberate — it stops the skill from being used as a proxy to point commands at someone else's machine.

## The remote contract you must respect

`/workspace` is the only persistent volume. Anything written to `/`, `/tmp`, `/dev/shm` or `/run` is destroyed the next time the image is rebuilt, and this instance has already been rebuilt at least once. The CLI enforces this: `exec`, `push`, `pull` and `run` refuse any path outside `/workspace` (and the HuggingFace cache) unless you pass `--allow-ephemeral`.

`/workspace/env.sh` sets `PATH`, `HF_HOME` and `HSA_OVERRIDE_GFX_VERSION`, and the CLI sources it before every command. It is shared with the user's other projects and they edit it, so treat it as a moving target rather than a fixed fact: as of 2026-09-01 it points `PATH` at `/workspace/venv`, which carries the standard stack (torch 2.12.0+rocm7.14.0). Earlier it pointed at a venv with no torch at all. Run `rc env` to see the current truth.

The CLI defends against both states: when `env.sh`'s default venv cannot import torch, `exec` and `run` automatically prepend a torch-capable venv and say so on stdout. `rc exec -- python -c "import torch"` therefore works with no extra flags in either case. Pass `--venv <path>` to choose one explicitly, or `--no-auto-venv` to disable the behaviour. The probe is cached in `~/.radeon-cloud-connector/venv-cache.json`: a successful probe for six hours, a failed one for only five minutes so a transient timeout cannot disable auto-fix for a whole working session. `rc doctor`, `rc env` and `rc status --torch` refresh it.

Which venv is correct changes whenever the user rebuilds environments, and stale venvs get deleted outright during disk cleanups. Never copy a venv path out of this document into a command without checking `rc env` first; the candidate list is discovery-only and any named path may disappear after a rebuild or cleanup.

Disk is the recurring failure mode on this box. `/workspace` reached 97% used (3.3 GiB free) in early September 2026 and had to be cleaned back to 50% before real work could continue; it fills up again quickly. Check `rc status` before pushing anything large, and prefer leaving model weights in the HuggingFace cache at `/root/.cache/huggingface`, which is a separate persistent host mount.

## Commands

| Command | Purpose |
|---|---|
| `guide` | Print the zero-to-first-result sequence, checked live against your current state. Start here on a cold machine. |
| `doctor` | Layered check of ssh config, credential, TCP reachability, auth, workspace, venv and GPU. Detects a rotated host key and offers a backed-up repair. |
| `status [--torch]` | Live GPU summary (model, gfx version, temp, power, VRAM used/total), plus disk, memory, load and the torch inventory of every candidate venv. Add `--raw` to print the full `rocm-smi` dump instead of the distilled summary. |
| `exec -- <cmd>` | Run a command remotely, defaulting to `/workspace`, sourcing `env.sh`, with `--cwd`, `--timeout`, `--venv`, `--no-auto-venv`, `--stream`, `--dry-run`. |
| `push <local> <remote>` | Upload a directory over tar+ssh (there is no local rsync). `--exclude` is repeatable. |
| `pull <remote> <local>` | Download a remote directory. Refuses to clobber an existing local path unless `--overwrite`. |
| `run --name <slug> -- <cmd>` | Start a detached job via `setsid`+`nohup`, recording pid and log under `/workspace/.rc-jobs`. |
| `jobs` | List tracked jobs with a live/exited state derived from the remote pid. |
| `logs <job-id> [-f] [-n N]` | Show or follow a job log. |
| `stop <job-id> [--force]` | Terminate a job: SIGTERM by default, SIGKILL with `--force`. Idempotent on a job that already exited. |
| `env` | Validate the whole remote contract, including the env.sh PATH cross-check. |
| `config [--show|--set K=V|--reset]` | Local connector configuration, stored in `~/.radeon-cloud-connector/config.json`. |

Exit codes: `0` success, `1` a real failure, `2` the remote host could not be reached or authenticated. A command that returns `2` always prints one actionable next step. Two documented exceptions: **`exec` and `run` return the remote command's own exit code** — a script that dies with `exit 3` makes `rc exec` return `3`; only `2` stays reserved for connection problems, so test with `rc exec ...; [ $? -ne 0 ]` rather than `-eq 1`. And `doctor` against an unreachable or unauthenticated host exits `2` (the connection case), not `1`.

## Workflows

On a cold machine, run `rc guide` first; it tells you which step you are actually on rather than assuming a working setup. Otherwise check the box before starting anything: `rc doctor`, then `rc status`.

If `ssh radeon-cloud` fails outright (before `rc` can run at all), complete the connection setup first: [在 Windows 或 MacBook 上连接 Radeon Cloud](https://mp.weixin.qq.com/s/dOAIzJ2qsWPmBSH67q41aA).

Send code up and run it:

```bash
"$PY" "$RC" push ./myproject /workspace/myproject --exclude "*.log" --exclude "__pycache__"
"$PY" "$RC" run --name train --cwd /workspace/myproject -- python train.py
"$PY" "$RC" jobs
"$PY" "$RC" logs <job-id> -f
```

Collect results afterwards with `rc pull /workspace/myproject/outputs ./outputs`.

## Safety rails

The CLI defaults to the persistent volume and requires `--allow-ephemeral` to write anywhere else; the refusal message names the path and the flag. Host-key repair backs up `known_hosts` first, removes only the entries for the exact target host and port (sibling containers on the same IP are untouched), prints the new fingerprints and asks before trusting them. `stop` always confirms unless `-y` is given. `push` and `pull` support `--dry-run`, and `pull` refuses to overwrite an existing local directory without `--overwrite`.

Every command that needs the remote host fails fast with one actionable message instead of a raw ssh error or, worse, an empty success. An unknown ssh alias, a dead endpoint and a refused key are each reported in plain language with the exact thing to run next. A failing remote command is never dressed up as a connection problem: a `ModuleNotFoundError` stays a `ModuleNotFoundError`.

Do not "fix" `/workspace/env.sh` on the remote host on your own initiative. As of 2026-09-01 its PATH entry is correct for torch work (`/workspace/venv` carries torch 2.12.0+rocm7.14.0), but it has pointed at a venv with no torch before, and the user edits it for their other projects. Check with `rc env` before relying on it; if the head of PATH lacks torch, report the mismatch and let the user decide, or use `--venv` per command instead.

## The costliest trap: HIP init can hang forever

On this box `rocminfo` can wedge in uninterruptible sleep (`D` state) and never return. Every call that performs HIP initialisation shells out to it, so these calls hang the same way:

| Call | Behaviour when rocminfo is wedged |
|---|---|
| `rocminfo` (any form) | hangs |
| `torch.cuda.device_count()` | hangs |
| `torch.cuda.is_available()` | hangs |
| `rocm-smi` | fine, returns instantly |
| `import torch` / `torch.version.hip` | fine |

Rules that follow from this:

- **Never probe GPU state with `torch.cuda.is_available()` / `torch.cuda.device_count()`.** Use `import torch` + `torch.version.hip` to check torch, and `rc status` (which reads `rocm-smi`) to check the GPU.
- **When you add a remote probe to this skill, always bound it on the remote side as well as locally** (`timeout -s KILL <n> <cmd>`). A local `subprocess` timeout kills the local ssh client and orphans the remote process — that is exactly how one bug accumulated 100+ leaked processes and pushed loadavg past 90 in half a day.
- **Failed probes must not be cached like successful ones.** The venv probe result is cached for six hours when it succeeds, but a failed probe expires after five minutes so one transient timeout cannot silently disable torch auto-fix for a whole working session.

If commands start hanging and load is inexplicably high, check for leaked probes: `ssh radeon-cloud "ps -eo pid,etimes,stat,args | grep -E 'device_count|is_available' | grep -v grep"`. `D`-state processes survive even SIGKILL; only a container restart clears them.

On Windows/Git Bash, `$(pwd)` produces MSYS-style paths (`/c/Users/...`). `rc push` and `rc pull` translate these via `_native_path()`; hand them to nothing else without converting.

## Self-verification

After any change to `rc.py` or to this file, confirm the connector still
diagnoses rather than guesses:

1. A bare `rc doctor` on a machine where the alias is absent must print one
   actionable message (exactly once) and exit `2`.
2. `rc doctor` on a configured machine must reach the GPU check.
3. No remote probe may rely on a local timeout alone; every probe carries a
   remote `timeout -s KILL` bound.
4. A failed venv probe must read as "unknown", never as "no torch" — in
   `doctor` (warn, not fail), `status --torch` (warn, exit governed by the
   GPU probe), `env` (explicit warn line) and `guide` (distinct message).
5. `rc env` must never print a green OK for a venv it did not actually
   verify; when the probe fails, the resolved-environment section says so
   instead of silently disappearing.

## References

- `references/environment.md` — full machine profile, storage persistence rules, venv inventory, ROCm version strategy.
- `references/troubleshooting.md` — known failures and their fixes, including host-key rotation and the no-torch venv.
