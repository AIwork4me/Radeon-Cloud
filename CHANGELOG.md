# Changelog

## Unreleased - 2026-09-02

- Fixed `status` returning success when the GPU or system probes fail without `--torch`.
- Fixed detached jobs reporting success when their metadata file cannot be written.
- Added cross-version safe streaming extraction for `pull`, rejecting traversal, absolute paths, and symbolic or hard links.
- Added regression tests for status failure handling, job metadata failure handling, archive safety, and streaming tar round trips.
- Synchronized the installed skill, release directory, and ZIP artifact.
- Added repository metadata, CI, release evidence, and a submission checklist.
