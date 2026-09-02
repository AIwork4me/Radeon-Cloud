# Radeon Cloud Connector Release Verification

Date: 2026-09-02 (Asia/Shanghai)

## Scope

This verification covers functionality, stability, compatibility, error handling, security, configuration, documentation, package synchronization, and release readiness for the Radeon Cloud Connector WorkBuddy Skill.

## Fixes completed

- `scripts/rc.py`: `cmd_status` now returns exit code `1` when GPU or system probes fail, including when `--torch` is not requested.
- `scripts/rc.py`: `cmd_run` now checks the metadata write result and reports a failure instead of claiming that a job started when the metadata file was not recorded.
- `scripts/rc.py`: `safe_extract` validates archive member paths and rejects traversal, absolute paths, Windows drive paths, symbolic links, and hard links. It keeps `pull` streaming one member at a time, uses the Python 3.12+ data filter when available, and has a compatibility fallback for older Python versions after equivalent validation.
- `tests/test_rc_regressions.py`: added regression coverage for all three fixes, including real tar archive members, streaming extraction, and stdout assertions.
- `scripts/rc.py`: fixed streaming `r|gz` extraction so `pull` remains streaming and does not seek backwards.
- Release copies: synchronized source, `dist/radeon-cloud-connector`, and the installed skill; regenerated the ZIP.
- Repository files: added `README.md`, `LICENSE`, `CHANGELOG.md`, `.gitignore`, `.github/workflows/ci.yml`, `config.yaml`, and this evidence report.

## Test evidence

The following checks passed during the 2026-09-02 release pass:

- Regression tests: `python -m unittest discover -s tests -v` — 4/4 passed.
- Syntax checks: `python -m py_compile scripts/rc.py scripts/journey_check.py scripts/install.py tests/test_rc_regressions.py` — passed.
- Static package review: `scripts/journey_check.py --phase review` — 44/44 passed, including `config.yaml` metadata checks.
- Installed-copy drift check: `scripts/install.py --check` — `IN SYNC`.
- Independent subagent verification confirmed source, dist, and installed `rc.py` contain the same safe streaming extractor and `cmd_pull` integration.
- ZIP verification: valid archive, 8 skill files, correct `radeon-cloud-connector/` root, no `__pycache__`, integrity test passed.
- Full live journey: `scripts/journey_check.py --phase journey --json` — **73/73 passed** in 4m32s; GPU kernel, torch, tar push/pull round trip, detached jobs, guardrails, host-key recovery, and cleanup all passed.

The full live journey is a separate networked check and was run against the current configured Radeon Cloud endpoint after the streaming pull fix. It passed **73/73** in 4m32s, including GPU execution, torch discovery, push/pull checksum round trip, detached jobs, negative guardrails, surgical host-key recovery, and cleanup. The project history records a prior real run of 113/113 checks, but this report does not substitute that prior evidence for the current run.

## Quality assessment

- Functionality: the documented CLI surface is present and statically cross-checked against the parser.
- Stability: remote failures have explicit exit codes and detached-job metadata failures are no longer silent.
- Compatibility: the package requires Python 3.10+ (it uses PEP 604 union syntax); archive safety does not depend on the Python 3.12 `tarfile` filter API, and CI verifies both 3.10 and 3.11.
- Error handling: ordinary remote command failures remain command failures; SSH-level failures remain connection failures.
- Security: host-key repair is backed up, opt-in, and surgical; remote paths are guarded; pull extraction rejects path and link escapes; private key contents are not read or uploaded by the connector.
- Configuration: config is local under `~/.radeon-cloud-connector`; defaults and environment discovery are documented.
- Documentation: `SKILL.md` and references cover all parser commands, key flags, known failures, safety rails, and verification.
- Packaging: source, dist, installed copy, and ZIP were synchronized after the fixes.

## Official WorkBuddy Connector assessment

The current artifact is a WorkBuddy **Skill** with a local SSH CLI. It is not an MCP server and does not expose a native third-party service Connector manifest, authorization flow, or remote API endpoint. Current developer guidance describes a local `config.yaml` metadata file and a `workbuddy skills publish ./skill-directory` flow, but the command is not available in this environment and was not executed. Therefore no official Marketplace or Connector-library submission was claimed.

To submit through the documented Skill publish flow, the operator still needs a WorkBuddy account, a local WorkBuddy CLI with the `skills publish` command, and any account or review permissions it requires. The prepared package contains the Skill frontmatter, `config.yaml`, README, license, tests, CI, changelog, release ZIP, and evidence needed for that flow. This is distinct from a native service Connector submission.

## GitHub submission assessment

The Skill is published as a standalone public repository at `https://github.com/AIwork4me/Radeon-Cloud-Connector`. It is deliberately separate from the related SSH bootstrap project (`AIwork4me/SSH-Radeon-Cloud`) so the WorkBuddy Skill, its tests, CI, and release artifacts stay self-contained.

The package is committed and pushed to the `main` branch (the repository default branch) using authenticated `gh` access:
- Commit: `dd9a1eca67dcd0eab983cbb092676f95d4b2baa1`
- Branch: `main` (default)
- Tracked files: 25 (the release ZIP is git-ignored and regenerated by `scripts/build_dist.py`)
- Pre-push verification: 4/4 unit tests, `py_compile` clean, 44/44 static review, `install.py --check` IN SYNC, `build_dist.py` ZIP OK.

One-pass local install from GitHub:

```bash
git clone https://github.com/AIwork4me/Radeon-Cloud-Connector.git
cd Radeon-Cloud-Connector
PY="${HOME}/.workbuddy-ai/binaries/python/versions/3.13.12/python.exe"
"$PY" scripts/install.py                 # -> ~/.workbuddy-ai/skills/radeon-cloud-connector
"$PY" ~/.workbuddy-ai/skills/radeon-cloud-connector/scripts/rc.py doctor
```

## Official WorkBuddy contribution: Skill vs Connector (research)

Research against current WorkBuddy developer and enterprise docs (2026-09-02) confirms the contribution model:

- **Connectors** (enterprise admin guide, workbuddy.cn) connect external SaaS services so the Agent can read/write GitHub, Figma, Tencent Docs, etc. They require an MCP server URL plus an OAuth 2.1 / OAuth 2.0 / API-key authorization flow. A local SSH CLI with no remote service, no auth server, and no MCP endpoint cannot satisfy this contract, and forcing it into a Connector would give users a broken one-pass install.
- **Skills** are the correct vehicle for a local CLI. The official developer guide defines the lifecycle: `SKILL.md` + `config.yaml` (name, version, triggers, category) → `workbuddy skills add ./skill` (local install) → `workbuddy skills scan ./skill` (security scan) → `workbuddy skills publish ./skill` (Marketplace / SkillHub). Community skills are distributed through **SkillHub** (skillhub.cn): client "发布到 SkillHub" or web upload of the skill ZIP, then review and listing. User-level skills live in `~/.workbuddy/skills/`.

Recommendation — **contribute as a Skill, not a Connector.** Two one-pass paths for users:
1. **SkillHub / Marketplace (best UX):** once published via `workbuddy skills publish ./Radeon-Cloud-Connector`, any user installs it in one click from inside WorkBuddy (search → install). This requires the `workbuddy skills publish` CLI/account and the SkillHub review, which were not executed in this environment.
2. **GitHub clone + installer (already live):** clone and run `scripts/install.py`; reaches a usable skill in one pass with no marketplace account. This is the fallback when the publish CLI is unavailable and is what ships today.

The prepared package already contains everything both paths need: `SKILL.md`, `config.yaml`, README, LICENSE, tests, CI, changelog, and the release ZIP builder.

## Release status

**Submitted to GitHub (`AIwork4me/Radeon-Cloud-Connector`, `main`, commit `dd9a1ec`).** Full local 360° verification passed (4/4 unit, 44/44 review, IN SYNC, ZIP OK). The only remaining external step is the official WorkBuddy Skill publish intake (SkillHub / `workbuddy skills publish`), which requires the publish CLI/account and was not executed in this environment. No Connector submission is appropriate.
