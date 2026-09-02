# Security remediation design — SkillHub scan rejection (v1.0.0)

Date: 2026-09-02
Status: implemented; re-verifying (journey 86/86 + review 54/54 targeted)

## Trigger

SkillHub auto-security scan rejected `radeon-cloud-connector` v1.0.0 with a
single finding (health score 29, verdict "malicious risk"): the static engine
flagged **"access to the SSH private-key file"** (文件操作与敏感路径访问 → 恶意).
The other 7 engines (supply chain / command exec / network exfiltration / prompt
injection / remote download / obfuscation / other) all reported **安全 (0 findings)**,
and the scan itself confirmed *network requests and data exfiltration = clean*.

The scanner's 4 remediation points:
1. Stop reading the SSH private key directly; use a key agent / platform-managed
   credential; never hard-code the local private-key path.
2. Whitelist the SSH target and executable remote commands to the user's own
   `radeon-cloud` alias; add a secondary confirmation + audit log for remote exec.
3. Confirm whether the private key was ever exfiltrated.
4. If unsure, rotate the suspected-leaked key before re-submitting.

## Key facts that shape the fix

- `rc.py` **never reads private-key contents**. It only shells out to `ssh`, which
  uses the key via the OS `ssh`/`ssh-agent`. The flagged pattern was the skill
  *checking* `os.path.exists(private_key)` and *printing* the key path (doctor
  precheck + failure diagnosis) — not actual key exfiltration.
- There is no "platform-managed credential" for a user's personal cloud box, so the
  viable mapping of remediation #1 is "delegate auth to the key agent and stop the
  skill from touching the key file at all" — which keeps the skill's function.

## Design

### A. Remove private-key file access (clears the finding)
- `resolve_ssh_target()` no longer collects `IdentityFile` paths from `ssh -G`.
- `cmd_doctor`'s `os.path.exists(key)` + "ssh private key present" block is removed;
  auth is proven by the existing batch ssh probe (`ssh auth (batch, no password)`).
- `diagnose_ssh_failure()` no longer prints the private-key path.
- `journey_check.py` J0.5 no longer `os.path.exists()`-es the key; it verifies the
  alias *declares* a key via `ssh -G` (offline, no key read).

### B. Whitelist + audit + confirmation (remediation #2)
- `--host` CLI override is rejected unless it names the configured `radeon-cloud`
  alias (no proxying to arbitrary hosts/IPs). Self-hosting via a raw `config.yaml`
  host is still allowed (that path does not use `--host`).
- `audit_remote()` appends one line per `exec`/`run` to
  `~/.radeon-cloud-connector/audit.log` (timestamp, alias, command, exit code; no
  secrets).
- `require_exec_consent()` prompts an interactive user to confirm before each
  remote command. It is gated on **both** `stdin.isatty()` and `stdout.isatty()`, so
  automation/CI/journey runs (stdout is a pipe) skip the prompt and stay green.

### C. Documentation + version
- New `SECURITY.md` explains the model (never reads the key, only targets the
  alias, audits every command, no exfiltration) and gives key-rotation steps.
  Referenced from `SKILL.md`. Added to all three syncers (install/build_dist/review)
  so it ships in dist/installed/ZIP.
- Version bumped `0.1.0 → 1.0.1` for a clean re-submission.

### D. Lock-in check
- `journey_check.py` R6.10 asserts `rc.py` contains no private-key-read pattern
  (no `os.path.exists(...key/identity/private)`, no `open(... .ssh)`, no
  `identityfiles` collection, no "ssh private key present" string).

## Verification
- `review`: 54/54 (was 53; +R6.10).
- `journey`: target 86/86 (regression run after the consent-gate fix; the first
  post-edit run hit a TTY-gating bug that aborted every exec/run — fixed by
  requiring `stdout.isatty()` too).

## Open item for the user
Rotate the SSH key as cheap insurance (scanner #4). Evidence shows no exfiltration,
but rotation removes any doubt and is fully supported by the skill.
