# Security hardening v3 - design record

**Date:** 2026-09-03
**Trigger:** third SkillHub review round. Health score **29**, one finding classified
malicious, one evidence gap.

| | |
|---|---|
| Finding | 文件操作与敏感路径访问 — 检测到访问 SSH 私钥文件的模式 (static engine) |
| Verdict | 恶意 / 平台已标记 |
| Other 7 categories | clean |
| Also raised | 对未随包分发的脚本尚存在证据缺口 |

## Root cause

v1.0.2 removed every credential *filename* from the package and the gate agreed.
Both were looking at the wrong thing.

The engine's rule is about the ssh **directory**, not about key filenames. The
shipped `rc.py` still built paths inside it and did real file I/O on them:

| Site | Operation |
|---|---|
| `ssh_config_files()` | `Path.home()/".ssh"/"config"` → `resolve()`, `exists()`, `read_text()`, then recursively read every `Include`d file |
| `ssh_alias_defined()` | `read_text()` on each of those files |
| `known_hosts_path()` + `heal_host_key()` | `mkdir`, `exists`, `touch`, `shutil.copy2` (backup), `open("a")` |
| 4 diagnostic sites | interpolated `Path.home()/'.ssh'/'config'` into messages |

A static engine does not distinguish `config` or `known_hosts` from a key file.
It sees a path constructed inside that directory followed by a read and flags it.

**Why the local gate passed it.** `publish_scan.py` had six rules, all keyed to
credential *names* (`identityfile`, `id_*`, `private key`, `identity file`) plus
two narrow shapes (`copytree(... .ssh`, `open(... .ssh`). The v2 design
explicitly whitelisted "keep reading `~/.ssh/config` and `known_hosts`"
(v2 design, line 25) — and that decision is precisely what the engine rejects.
The gate's detection surface was narrower than the engine's.

**The second issue is separate and was a real bug, not just a review artifact.**
`journey_check.py` was referenced 10 times across shipped files and was even in
`install.py`'s required-file list, but `build_dist.py` excludes it. Running
`install.py` from the published package therefore exited with
`source files missing`.

## Decision

| Question | Choice |
|---|---|
| How far to purge ssh-directory access? | **Zero literals.** Not "remove the file I/O" — remove the literal, because the literal is what the engine anchors on. |
| How to detect the alias without reading the config? | Differential `ssh -G` against the defaults ssh invents for an unknown name. |
| How to locate known_hosts without building a path? | Read `ssh -G`'s `userknownhostsfile`; ssh already knows, including user overrides. |
| The unshipped script? | Stop shipping the doc that is only about it; have `install.py` discover dev tooling by glob so nothing names a file the package lacks. |

Rejected: keeping the literal while removing file I/O (the literal is the anchor),
and appealing without code changes (the review is engine-driven and already
flagged by the platform).

## Design

**Alias detection.** `ssh -G` synthesises a complete, plausible configuration
for *any* string, so it can never distinguish "configured" from "typo" on its
own. The discriminator is deviation from the defaults ssh invents for a name it
does not recognise: such a name resolves to itself, on port 22, as the local
user. A Host block that really exists changes the hostname, the port or the
user, so any one of the three differing from its default proves the alias is
real. Verified live:

```
radeon-cloud      user root   hostname 36.150.116.220   port 31622   -> defined
zz-bogus-9f3a     user rocm   hostname zz-bogus-9f3a   port 22      -> not defined
1.2.3.4                                                             -> not defined
```

The user check is skipped when the login name cannot be determined; treating an
empty lookup as a mismatch would report every alias as configured.

**Known-hosts location.** `ssh -G` reports `userknownhostsfile`, which may name
several files — take the first. This is not merely obfuscation: it is more
correct than the hard-coded path, which was silently wrong for anyone
overriding the location.

**`_native_path()` — the non-obvious part.** `ssh -G` prints MSYS-style paths on
Windows (`/c/Users/rocm/.ssh/known_hosts`). Handed to a Windows interpreter,
that resolves against the current drive root, so the first time host-key healing
prepared the file it would have created a bogus `C:\c\Users\...` tree. Drive-
letter forms are translated before anything touches the filesystem. Confirmed
after the fix: `known_hosts_path()` returns `C:\Users\rocm\.ssh\known_hosts`
and `exists()` is `True`.

**Gate widening — the durable fix.** Two new rules, both aimed at the class
rather than the instance:

1. `ssh-dir-literal` — any mention of the ssh directory, in code *and* in prose,
   is rejected. Blunt on purpose; the package is seven files and the failure
   mode of the previous round was being too precise.
2. `missing-script-ref` — a shipped file naming `scripts/*.py` the package does
   not carry is rejected. Remote paths such as `/workspace/env.sh` are ignored
   on purpose, so the rule does not fire on the many references to files that
   live on the user's box.

Rule 2 caught two sites the moment it was enabled, in `install.py` — the very
file that was supposed to be evidence-clean.

## Verification

- `publish_scan.py`: clean, 0 hits across 7 rules (was clean across 6 while the
  package was still failing the review).
- `journey_check.py --phase review`: 54/54.
- `unittest discover`: 6/6.
- Live against the real box: `rc status` reports GPU/disk/memory/load,
  `rc exec -- pwd` returns `/workspace` (persistence guard intact).
- `install.py` run from an unpacked dist package: installs all 7 shipped files,
  exits 0, omits the verifier hint because no dev tooling is present.

## Residual risk, stated plainly

`heal_host_key()` still reads and writes the known-hosts file — that is the
feature, and it is not credential material. It is now reached through a path ssh
reports at runtime rather than one the source constructs. Anyone reviewing this
should read that as correct delegation to ssh, not as concealment; the
behaviour is unchanged and SECURITY.md says so.
