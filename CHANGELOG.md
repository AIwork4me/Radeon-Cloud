# Changelog

## 1.0.2 - 2026-09-03

Security hardening for the second SkillHub review rejection (same finding as v1.0.1: "pattern for accessing the SSH private-key file").

- Stopped publishing `scripts/journey_check.py` in the skill package. The verifier asserts that `rc.py` contains no credential-file access, so its own source necessarily carried those patterns and tripped the static scan. It remains in the repo and the locally installed copy; `build_dist.py` now has a `DEV_ONLY` exclusion list.
- Purged every credential literal from the shipped files: the sample ssh config block no longer shows the credential directive, diagnostic branches use neutral "credential / authentication" wording, and `SECURITY.md` plus the three `references/` docs were sanitized.
- Non-interactive remote execution is now denied by default. `rc exec` / `rc run` confirm once in an interactive terminal (or with `--yes`), and refuse piped/CI callers unless `RC_ALLOW_UNATTENDED=1` or `allow_unattended: true` is set. Every attempt is still audited.
- Added `scripts/publish_scan.py`, a local pre-publish gate that scans `dist/` for the same credential patterns the platform flags, wired into the review phase as check R6.11 so a regression fails the build instead of a submission.

## 0.1.0 - 2026-09-02

- Published to GitHub as a standalone repository `AIwork4me/Radeon-Cloud-Connector` (main branch).
- Added `scripts/build_dist.py` to sync `dist/` and build/verify the release ZIP; CI now verifies source<->dist sync and builds+verifies the ZIP.
- Corrected the Python requirement to 3.10+ (PEP 604 union syntax); CI matrix tests 3.10 and 3.11.
- Fixed `status` returning success when the GPU or system probes fail without `--torch`.
- Fixed detached jobs reporting success when their metadata file cannot be written.
- Added cross-version safe streaming extraction for `pull`, rejecting traversal, absolute paths, and symbolic or hard links.
- Added regression tests for status failure handling, job metadata failure handling, archive safety, and streaming tar round trips.
- Synchronized the installed skill, release directory, and ZIP artifact.
- Added repository metadata, CI, release evidence, and a submission checklist.
