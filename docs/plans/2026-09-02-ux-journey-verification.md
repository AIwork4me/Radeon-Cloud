# UX journey verification — 2026-09-02

**Subject:** end-to-end user-experience verification of the Radeon Cloud Connector WorkBuddy Skill, from the user's journey, with emphasis on **one-pass success** and a **smooth (丝滑) feel**.
**Scope chosen:** full chain including the live Radeon Cloud endpoint (user selected "全链路含真机(推荐)").
**Latest verified commit:** `515f9b7` (status-UX distillation fix).
**Verdict:** ✅ **one-pass success, smooth, clean** — independent fresh-clone re-verification: live journey **73/73**, static review **44/44**, zero secret leaks.

---

## 1. Why this verification

The prior release-hardening pass proved the connector *works*; this pass proves a brand-new user reaches a GPU result **on the first attempt, without decoding ssh errors or reading source**. The user's own words: *"重点保证 one-pass success 和使用感受的丝滑"*.

Two things were explicitly in scope:
1. The full user journey J0–J8 against the **real** Radeon Cloud box (not a mock).
2. The *feel* of first contact — is the output scannable and actionable, or does it bury the user in machine noise?

## 2. Method

1. **First-contact capture** — ran `rc guide`, `rc doctor`, `rc status --torch` on a healthy box and read the real stdout to judge smoothness.
2. **Friction fix** — the first capture exposed one real defect: `rc status` printed a ~30-line `rocm-smi` banner dump with repeated `GPU[0]` rows, burying the headline numbers. Fixed in `scripts/rc.py` (`summarize_rocm()` + `--raw` fallback + collapsed missing-venv line) and re-synced to `dist/` and the installed copy. See §4.
3. **Authoritative live journey** — `python scripts/journey_check.py --phase journey` (background, ~5 min) → **73/73 passed**, exit 0.
4. **Independent fresh-clone re-verification** — a general-purpose subagent cloned `515f9b7` from GitHub into a clean temp dir, ran `install.py`, read the *real* console output of guide/doctor/status/exec/jobs, re-ran the live journey (73/73) and the static review (44/44), and audited for leaked secrets. This is the gate that actually proves the one-pass claim, since it uses a clone a brand-new user would get.

## 3. Per-stage journey result (live, J0–J8)

| Stage | Theme | Result | Smoothness |
|---|---|---|---|
| J0 | Prerequisites (ssh, config, key, alias) | ✅ J0.1–J0.5 | Smooth — missing alias is one copy-pasteable failure, not a 7-failure cascade |
| J1 | First contact (guide + doctor) | ✅ J1.1–J1.4 | Smooth — guide names the live step; doctor resolves one concrete `user@host:port` |
| J2 | Understand the machine (status/env) | ✅ J2.1–J2.9 | **Improved** — `status` GPU now one line; `env` surfaces `HF_HOME`/`HSA_OVERRIDE_GFX_VERSION` after sourcing |
| J3 | The aha moment (first GPU command) | ✅ J3.1, J3.3–J3.6 | Smooth — `rc exec -- python -c "import torch"` exits 0 with no `--venv`; real kernel, 1 device, VRAM allocated |
| J4 | Move code/data (push/pull) | ✅ J4.1–J4.8 | Smooth — byte-identical sha256 round trip; `--exclude` honoured; missing remote dir fails clearly |
| J5 | Long-running jobs | ✅ J5.1–J5.8 | Smooth — id returned, `jobs`/`logs` live, `stop` terminates + idempotent on exited job |
| J6 | Guardrails (negative tests) | ✅ J6.1–J6.10 | Smooth — unknown alias = exit 2 naming the alias + config + next command; no silent failure; user errors not relabelled as ssh errors |
| J7 | Recovery (re-image) | ✅ J7.1–J7.7 | Smooth — `known_hosts` backed up, host-key heal surgical (sibling ports survive), config round-trips, venv cache host-scoped |
| J8 | Leave no trace | ✅ J8.1–J8.3 | Smooth — remote scratch + temp removed, 0 leftover journey jobs |

**Journey total: 73/73 passed, exit 0, duration ~5m35s (fresh-clone re-run).** No failed stage. The `[FAIL]` string in J4.8 is the *expected* error text inside a passing assertion about failing-loudly — not a failed check.

## 4. The one friction fix: `rc status` distillation

Before the fix, `rc status --torch` dumped the raw `rocm-smi` output — ~30 lines of `====` banners and one `GPU[0]` row per attribute, with the torch-venv probe also printing a separate line per *missing* candidate path. A new user had to scroll to find "is my GPU alive and how much VRAM is free?".

After `515f9b7` (verified live and on the fresh clone):

```
GPU[0]  0x744b  gfx1100   25.0C   15.0W   VRAM 0.03/48.0 GiB
disk
   /workspace                   69.8 GiB free of    100.0 GiB  (31% used)
...
torch environments
   /workspace/venv                        torch 2.12.0+rocm7.14.0 / HIP 7.14.60850 / 1 dev
   /opt/venv                              torch 2.9.1+gitff65f5b / HIP 7.2.53211-e1a6bc5663 / 1 dev
   (4 candidate path(s) not present: /workspace/venv-torch212, /workspace/venv-53615-statea, /workspace/venv-mainline-probe, /workspace/bench-venv)
```

- GPU section: **~30 lines → 1 line** per GPU (model, gfx, temp, power, VRAM used/total).
- `--raw` still prints the full dump for debugging.
- Missing candidate venvs collapsed into one parenthetical line instead of one line each.

## 5. First-contact smoothness (real output, fresh clone)

| Command | Exit | Smoothness | Evidence |
|---|---|---|---|
| `rc guide` | 0 | Excellent | 8-step zero-to-result path with the resolved torch version; tells the user the exact next command |
| `rc doctor` | 0 | Fully actionable | 8/8 `[OK]`; ends with a single "all checks passed" line |
| `rc status --torch` | 0 | Tight & scannable | one GPU line + compact disk/mem/load + collapsed venv list (see §4) |
| `rc exec -- python -c "import torch…"` | 0 | Proves the stack in one command | prints `torch 2.12.0+rocm7.14.0` |
| `rc jobs` | 0 | Clean | readable table even with a pre-existing exited job; no noise |

## 6. Independent fresh-clone re-verification

- Clone `515f9b7` → `git status` clean, all source files present.
- `python scripts/install.py` → exit 0, installed to `~/.workbuddy-ai/skills/radeon-cloud-connector/`, static review R4.2 confirms in-sync, no `__pycache__` shipped.
- Live journey → **73/73**, exit 0. Static review → **44/44**, exit 0.
- **Secret/leak audit:** scanned clone + installed copy for private keys, passwords, tokens, `.env`, `known_hosts`, `*.pem/id_*`. **0 hits.** No temp/journey logs committed.

## 7. Friction found / fixed

| Severity | Item | Status |
|---|---|---|
| IMPORTANT | `rc status` raw `rocm-smi` dump buried headline numbers | **Fixed** in `515f9b7` (distilled to one line; `--raw` fallback) |
| RESOLVED | `references/environment.md` no longer hard-codes any public IP:port. The skill only ever references the `radeon-cloud` ssh alias; when the alias is missing or `ssh radeon-cloud` fails it points the user to the connection setup guide, and the endpoint is configured exclusively in the user's own `~/.ssh/config`. |

No blocking or confusing friction remains for a new user.

## 8. One-pass success verdict

**Yes.** A brand-new user who clones `515f9b7`, runs `install.py` (exit 0), then follows `rc guide` reaches a working GPU result with no intervention: `doctor` is all-green, `status --torch` is a scannable one-liner, `exec` proves the torch/ROCm stack in a single command, and the full live journey passes 73/73. The experience is smooth end-to-end (J0–J8), fails loudly and actionably at every guardrail (J6), and leaves no trace (J8).

## 9. Follow-ups (none blocking)

- Keep `journey_check.py --phase journey` green in CI on a schedule (the live journey is the only thing that catches real-endpoint regressions like disk-full or a rotated host key). The current CI runs `--phase review` (static); adding the live journey as a nightly job is the recommended hardening.
- Re-confirm the IP:port disclosure posture if the skill is ever published as a *public* template rather than a personal tool — at that point the literal endpoint should move to a placeholder.
