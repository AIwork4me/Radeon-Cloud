# Changelog

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
