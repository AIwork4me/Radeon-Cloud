# Radeon Cloud Connector

A WorkBuddy Skill that wraps an AMD Radeon Cloud workstation behind a safe, diagnosable SSH CLI.

## What it does

The connector provides one command surface for SSH diagnosis, GPU and ROCm inspection, torch environment discovery, remote command execution, tar-over-SSH file transfer, detached jobs, logs, job stopping, and local configuration.

It is a **WorkBuddy Skill**, not an MCP service or a native third-party Connector. It runs locally and reaches the user's own Radeon Cloud instance through the `radeon-cloud` SSH alias.

## Install

Copy the skill directory to `~/.workbuddy-ai/skills/radeon-cloud-connector`, or use the bundled installer:

```bash
PY="${HOME}/.workbuddy-ai/binaries/python/versions/3.13.12/python.exe"
"$PY" scripts/install.py
```

The installer synchronizes the source files and removes generated `__pycache__` directories from the installed copy. The managed interpreter path above is the Windows development default; on another machine use any Python 3.10+ interpreter available to that WorkBuddy installation (the CLI uses PEP 604 union syntax).

## First use

```bash
PY="${HOME}/.workbuddy-ai/binaries/python/versions/3.13.12/python.exe"
RC="$HOME/.workbuddy-ai/skills/radeon-cloud-connector/scripts/rc.py"

"$PY" "$RC" guide
"$PY" "$RC" doctor
"$PY" "$RC" status --torch
```

The SSH alias must be configured in `~/.ssh/config` with a current host, port, user, and private key. The connector never disables host-key checking. Host-key repair backs up `known_hosts`, displays fingerprints, requires confirmation, and changes only the exact target host and port.

If `ssh radeon-cloud` fails, complete the connection setup first: [在 Windows 或 MacBook 上连接 Radeon Cloud](https://mp.weixin.qq.com/s/dOAIzJ2qsWPmBSH67q41aA).

## Safety model

- Persistent remote data belongs under `/workspace` or the configured HuggingFace cache.
- Writes outside the persistent volume require `--allow-ephemeral` and are announced.
- `pull` rejects archive traversal, absolute paths, and symbolic or hard links before extraction.
- `stop` requires confirmation unless `--yes` is supplied.
- Large pushes are announced and confirmed above the configured threshold.
- Remote failures and connection failures use distinct exit codes: `0` success, `1` command or validation failure, `2` unreachable or unauthenticated host.

## Verification

Run the local regression suite and package review:

```bash
PY="${HOME}/.workbuddy-ai/binaries/python/versions/3.13.12/python.exe"
"$PY" -m unittest discover -s tests -v
"$PY" -m py_compile scripts/rc.py scripts/journey_check.py
"$PY" scripts/journey_check.py --phase review
"$PY" scripts/install.py --check
```

The live journey requires the configured Radeon Cloud endpoint and exercises real SSH, ROCm, torch, file transfer, and detached-job behavior:

```bash
"$PY" scripts/journey_check.py --phase journey
```

## Build the release artifact

`dist/radeon-cloud-connector` and `dist/radeon-cloud-connector.zip` are generated from the source tree (the source files are the single source of truth). Regenerate them after any source change so the package stays in sync and CI passes:

```bash
PY="${HOME}/.workbuddy-ai/binaries/python/versions/3.13.12/python.exe"
"$PY" scripts/build_dist.py          # sync dist/ and write the ZIP
"$PY" scripts/build_dist.py --check   # verify the ZIP matches dist/ without writing
```

The ZIP is git-ignored (it is a build artifact); `dist/radeon-cloud-connector` is committed so the skill is installable directly from a clone.

## Repository and publication status

The package is prepared for source control and review, including a license, changelog, CI workflow, tests, `config.yaml` marketplace metadata, and a release evidence report. Current WorkBuddy developer guidance describes local testing and scanning and a `workbuddy skills publish ./skill-directory` command. That CLI is not available in this environment, so this package is prepared but has not been published. It should not be described as submitted to the official Connector library: it is a Skill, not a native service Connector.

The Skill is published on GitHub at [`AIwork4me/Radeon-Cloud-Connector`](https://github.com/AIwork4me/Radeon-Cloud-Connector). It is a separate repository from the related SSH bootstrap project so the WorkBuddy Skill, its tests, CI, and release artifacts stay self-contained. Clone it and run the bundled installer (see Install) to use the Skill locally.

## Files

- `SKILL.md` — WorkBuddy Skill instructions and user-facing command reference.
- `scripts/rc.py` — CLI implementation.
- `scripts/journey_check.py` — live journey and static 360-degree reviewer.
- `scripts/install.py` — source-to-install synchronization.
- `references/` — environment, troubleshooting, and journey documentation.
- `tests/` — local regression tests.
- `dist/` — synchronized release directory and ZIP artifact.
- `docs/release-verification-2026-09-02.md` — verification evidence and submission checklist.
