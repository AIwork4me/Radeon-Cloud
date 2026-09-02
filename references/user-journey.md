# radeon-cloud user journey and 360 degree review

This document is the design behind `scripts/journey_check.py`. It answers two questions:

1. What does a brand-new user actually experience, from a machine that has never connected to this box through to a finished GPU job? Where can that experience break?
2. How do we review the connector from every angle before shipping it?

The goal is **one-pass success**: a user who follows `rc guide` should reach a working GPU result on the first attempt, without reading source code or decoding ssh errors.

## Part 1 - the journey map

### Stage 0 - prerequisites (local machine, before any network call)

| # | User expectation | What can go wrong | Connector behaviour |
|---|---|---|---|
| J0.1 | ssh client is available | No ssh on PATH (bare Windows install) | `find_ssh` falls back to `C:\Windows\System32\OpenSSH\ssh.exe`, else a clear error |
| J0.2 | ssh-keyscan is available | Needed only for host-key repair | Fallback to the OpenSSH system directory |
| J0.3 | `~/.ssh/config` exists | File absent | `ssh_alias_defined` returns false; the guide shows the exact block to paste |
| J0.4 | The `radeon-cloud` alias is defined | Typo, or the block was never added | Reported as one failure with a copy-pasteable `Host` block - **not** a cascade |
| J0.5 | The private key exists and is readable | Key moved or deleted | Doctor names the configured-but-missing files, not ssh's internal probe list |

**Design rule.** `ssh -G <host>` invents a plausible config for *any* string, so it can never detect a typo. Reading `~/.ssh/config` (including `Include`d files) is the only reliable signal. This was the single biggest one-pass blocker found: a non-existent alias used to produce a green "ssh config resolves" tick followed by seven confusing failures mentioning key files the user never configured.

### Stage 1 - first contact

A cold user runs `rc guide`. It must say which step they are actually on, not assume a working setup. Then `rc doctor`.

| # | Expectation | Failure mode if unhandled |
|---|---|---|
| J1.1 | `rc guide` exits 0 on a healthy box | - |
| J1.2 | `rc guide` confirms connectivity and a torch-capable venv | User proceeds with a broken env and fails later, expensively |
| J1.3 | `rc doctor` produces no blocking failures | - |
| J1.4 | Doctor resolves to one concrete `user@host:port` | Ambiguity about which box is being operated on |

### Stage 2 - understanding the machine

`rc status` and `rc env`. The user builds a mental model: how many GPUs, how much disk, which venv works.

| # | Expectation | Notes |
|---|---|---|
| J2.1-2.5 | status reports GPU, disk, memory, load | Every section must be present |
| J2.6-2.8 | env lists venvs and surfaces `HF_HOME` / `HSA_OVERRIDE_GFX_VERSION` | The env file must actually be sourced before probing, otherwise these silently vanish |
| J2.9 | env reports whether `env.sh`'s default venv can import torch | Was the machine's standing defect until the user fixed it on 2026-09-01. The check must assert the *invariant* (the effective python has torch), not the defect, so it stays correct after the fix |

### Stage 3 - the aha moment

The user runs their first GPU command. **This is the stage that decides whether the tool is trusted.** It must work with no flags at all.

| # | Expectation | Why it matters |
|---|---|---|
| J3.1 | `rc exec -- python -c "import torch"` exits 0 with no `--venv` | The naive path must never depend on the user knowing which venv is good. It holds two ways: `env.sh` already resolves to a torch venv, or the connector auto-selects one and says so. Both are valid and the test accepts both. |
| J3.3-3.4 | A real GPU kernel runs and is numerically correct | Proves the whole stack: driver, ROCm, torch, device visibility |
| J3.5 | torch reports exactly 1 device | Regression guard - `rocm-smi --showproductname \| grep -c 'GPU\['` reports 9 for one card, because it prints one line per attribute |
| J3.6 | VRAM is actually allocated | Proves it is not silently falling back to CPU |

**Design rule.** Never silently "fix" the shared `/workspace/env.sh`. Overriding `PATH` per command, with a visible advisory line, keeps the user's other projects intact while removing the failure.

### Stage 4 - moving code and data

| # | Expectation | Guard |
|---|---|---|
| J4.1-4.2 | Files arrive | - |
| J4.3-4.4 | `--exclude` honoured | Prevents pushing `__pycache__` and logs |
| J4.5 | Content is byte-identical (sha256) | tar over ssh can corrupt or truncate silently |
| J4.6-4.7 | Round trip back is byte-identical | - |
| J4.8 | Pulling a missing remote directory fails clearly | Previously produced an opaque tar error |

### Stage 5 - long-running jobs

There is no tmux on the box, so detachment is `setsid` + `nohup` with self-managed pids and logs.

| # | Expectation | Guard |
|---|---|---|
| J5.1-5.2 | Job starts and returns a usable id | - |
| J5.3-5.5 | `jobs` and `logs` show live progress | Proves detachment actually worked |
| J5.6 | `stop` terminates it | Signals the process group first, then the pid |
| J5.7 | `stop` on an already-exited job exits 0 | Idempotence - a finished job is not a user error |
| J5.8 | `jobs` reports `exited` | - |

### Stage 6 - guardrails (negative tests)

The most important property here: **nothing ever fails silently.**

| # | Expectation |
|---|---|
| J6.1-6.4 | An unknown alias fails with exit 2, names the alias and the config file, and says what to run next - in ONE failure, not a cascade |
| J6.5 | `status`, `exec`, `run`, `jobs`, `env` all exit non-zero on a dead endpoint |
| J6.6 | Writing outside `/workspace` is refused, and the refusal names the flag |
| J6.7 | `--allow-ephemeral` lets a deliberate escape through |
| J6.8-6.9 | Unknown job ids fail with a "known jobs" hint |
| J6.10 | A failing remote command is **not** relabelled as an ssh error - a `ModuleNotFoundError` stays a `ModuleNotFoundError` |

### Stage 7 - recovery

The instance gets re-imaged periodically. Recovery must be a single command, not a rescue mission.

| # | Expectation |
|---|---|
| J7.1 | A `known_hosts` backup exists |
| J7.2 | Host-key heal is surgical: run against a *copy* of `known_hosts`, sibling entries on other ports survive untouched |
| J7.3-7.4 | `config --set` round-trips and `--reset` restores defaults |
| J7.5-7.7 | The venv cache is written, host-scoped, and auto-venv resolves to a torch-capable venv |

### Stage 8 - leaving no trace

Verification must not become litter. Remote scratch directories and temp dirs are removed; no test jobs are left behind.

## Part 2 - the 360 degree review scheme

The journey tests *behaviour*. The review tests the *package*. Five dimensions, all automated in `--phase review`:

**R1 Packaging** - `SKILL.md` exists with valid YAML frontmatter, a well-formed skill id as its name, `agent_created: true`, the CLI present under `scripts/`, and every referenced file existing. A dangling `references/` link is the classic way a skill silently loses half its knowledge.

**R2 Documentation drift** - every subcommand in the parser appears in `SKILL.md`, no phantom command is documented, every user-facing flag is explained, and `troubleshooting.md` covers the known failure signatures. Docs drift is invisible until someone follows them.

**R3 Console and platform safety** - the CLI is ASCII-only (Windows consoles garble emoji and box drawing), no `TODO`/`FIXME`, no debug prints, and no machine-specific `C:\Users\<name>` paths in the documentation.

**R4 Install sync** - the copy under `~/.workbuddy-ai/skills/` is byte-identical to the source of truth, and no `__pycache__` is shipped. A stale installed copy is the worst possible bug: the source looks fixed and the behaviour does not change.

**R5 Exit-code contract** - distinct codes for success (0), real failure (1) and unreachable host (2), and every remote command calls `require_remote` so none of them can return a silent success.

### The one-pass-success principles behind all of this

1. **Never return success on a dead connection.** Empty output plus exit 0 is worse than any error message, because the user keeps going.
2. **One failure, not a cascade.** If the endpoint is unknown, stop and say so. Seven downstream failures about key files the user never configured is noise.
3. **Every error names the next action.** "Alias not defined" is not actionable. "Add this block, then run `rc doctor`" is.
4. **Do not relabel user errors as infrastructure errors.** A python traceback must stay a python traceback.
5. **Auto-correct what you can, announce it, and always allow an override.** Auto-venv selection is silent in effect but visible in the transcript, and `--no-auto-venv` restores the old behaviour.
6. **Never mutate shared remote state on your own initiative.** `env.sh` is wrong for torch, but it is not ours to fix.
7. **Destructive and cross-cutting repairs must be opt-in, backed up, and surgical.** Host-key healing backs up `known_hosts`, removes only the target `host:port`, prints fingerprints and requires confirmation.

## Running it

```bash
"$PY" <skill-dir>/scripts/journey_check.py --phase review    # ~5s, no network
"$PY" <skill-dir>/scripts/journey_check.py --phase journey   # ~2min, live
"$PY" <skill-dir>/scripts/journey_check.py --stage 3         # one stage
"$PY" <skill-dir>/scripts/journey_check.py --json            # machine-readable
```

Both phases exit non-zero on any failure, so this is CI-ready.
