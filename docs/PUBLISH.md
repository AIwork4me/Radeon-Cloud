# Publishing Radeon Cloud Connector

Two install paths exist. **Path A (SkillHub one-click) is the best UX** and is what a brand-new user should get; **Path B (GitHub clone + `install.py`) is the already-live fallback** and is what this repository ships today.

## Pre-publish checklist (all green as of 2026-09-02)

Run from the repo root with the managed Python (`C:/Users/rocm/.workbuddy-ai/binaries/python/versions/3.13.12/python.exe`):

- `python scripts/journey_check.py --phase review` → **44/44** (packaging, doc drift, console/platform safety, install sync, exit-code contract).
- `python scripts/build_dist.py --check` → **ZIP OK** (8 files; `dist/radeon-cloud` byte-identical to source, `dist/radeon-cloud.zip` passes `testzip()`).
- `python scripts/journey_check.py --phase journey` → **73/73** (full live journey against the real Radeon Cloud box).

## Path A — SkillHub one-click publish (best UX)

This is the recommended distribution: a user installs the skill from SkillHub without cloning or running anything.

1. Open the **WorkBuddy** app.
2. Go to **Skills** (left sidebar) → your `radeon-cloud` skill (it is already installed at `~/.workbuddy-ai/skills/radeon-cloud/`).
3. Choose **Publish** (or run the publish CLI if available in your environment):
   - CLI form (when the `workbuddy` binary is on PATH): `workbuddy skills publish ./dist/radeon-cloud`
   - If the CLI is not available, use the in-app publish flow — the publish package is `dist/radeon-cloud/` (or its ZIP).
4. After publish, a new user installs it with one click and follows `rc guide`.

> Note: in the build environment used for this repo the `workbuddy` publish CLI was **not** on PATH and official publish permission/entry may be required, so Step 3 may need to be performed from the WorkBuddy desktop app rather than this shell. The publish package itself is verified ready (see checklist).

## Path B — GitHub clone + install.py (fallback, already live)

Already works today; documented for completeness and as the one-pass fallback if SkillHub publishing is unavailable.

```bash
git clone https://github.com/AIwork4me/Radeon-Cloud-Connector.git
cd Radeon-Cloud-Connector
python scripts/install.py          # copies the skill to ~/.workbuddy-ai/skills/radeon-cloud/
```

Then in WorkBuddy, the `radeon-cloud` skill is available. First run: `rc guide`.

## Publish artifact

- Directory: `dist/radeon-cloud/` (synced source of truth).
- Archive: `dist/radeon-cloud.zip` (git-ignored; rebuilt by CI and `build_dist.py`).

## Requirements for the end user

- Their own SSH alias `radeon-cloud` pointing at their AMD Radeon cloud GPU container (key auth). The skill operates the box the alias resolves to; it does **not** ship connection credentials.
- A Python 3.10+ interpreter available to the WorkBuddy installation (the CLI uses PEP 604 union syntax).
- The live box must expose a GPU via `rocm-smi` and a torch-capable venv (the skill auto-selects one and says so).

## What the skill does NOT do

- It never mutates the shared remote `/workspace/env.sh` on its own initiative.
- It never ships the user's private key or any secret (verified: 0 secret hits in the repo and installed copy).
