# radeon-cloud environment reference

Observed 2026-08-30/31 by direct probe. Re-verify with `rc doctor` and `rc env` before relying on any of it, because this instance is rebuilt periodically.

## Host

| Item | Value |
|---|---|
| ssh alias | `radeon-cloud` |
| endpoint | `root@36.150.116.220:31622` |
| hostname | `u-18147-942e8579` |
| OS | Ubuntu 24.04.4 LTS, kernel 6.8.0-79-generic |
| CPU | 128 logical cores (AMD EPYC 9334 class) |
| memory | 1 TiB, no swap |
| GPU visible to container | 1x AMD `0x744b` (Navi 31 / RDNA3), gfx1100, 51,522,830,336 bytes VRAM (~48 GiB) |
| GPUs on the PCIe bus | 8 AMD `0x744b` plus 1 ASPEED BMC; only one AMD card is passed through |
| ROCm in `/opt/rocm` | 7.2.1 (image layer, not persistent) |
| sshd | OpenSSH 9.6p1 Ubuntu |

## Storage and persistence

| Mount | Persistence | Use |
|---|---|---|
| `/workspace` | yes, survives re-image | all code, venvs, configs, job state, outputs |
| `/root/.cache/huggingface` | yes, host bind mount | model and dataset cache (`HF_HOME`) |
| `/` (overlay, ~3.5 TB) | no, destroyed on re-image | nothing you want to keep |
| `/tmp`, `/dev/shm`, `/run` | no | scratch only |

`/workspace` is 100 GiB total. It has swung hard: 87% used (13 GiB free) in late August 2026, then 97% used (3.3 GiB free) on 2026-09-01, then back to 50% after the user cleaned it. The largest consumers at the worst point were `SenseNova-U1.5-ROCm` (30 G), vLLM build dirs (26.5 G) and `bench-venv` (24 G). Treat free space as volatile and check `rc status` before pushing anything large.

## Python environments

`rc env` probes the candidate list and reports whether torch imports. The list is configurable via `rc config --set venv_candidates='["/workspace/venv"]'`.

The table below is a **snapshot taken 2026-09-01**, not a contract. Venvs on this box get created, rebuilt and deleted as the user works — four of them were removed outright during that day's disk cleanup. Run `rc env` for the live inventory instead of trusting any row here.

| venv | torch | HIP | persistent | state on 2026-09-01 |
|---|---|---|---|---|
| `/workspace/venv` | 2.12.0+rocm7.14.0 | 7.14.60850 | yes | **present; what `env.sh` puts on PATH** |
| `/opt/venv` | 2.9.1+gitff65f5b | 7.2.53211 | no | present; overlay, will vanish on re-image |
| `/workspace/venv-torch212` | 2.12.0+rocm7.14.0 | 7.14.60850 | yes | removed 2026-09-01 |
| `/workspace/venv-53615-statea` | 2.12.0+rocm7.14.0 | 7.14.60850 | yes | removed 2026-09-01 |
| `/workspace/venv-mainline-probe` | 2.12.0+rocm7.14.0 | 7.14.60850 | yes | removed 2026-09-01 |
| `/workspace/bench-venv` | 2.8.0+rocm6.3 | 6.3.42131 | yes | removed 2026-09-01 |

The documented standard is the ROCm 7.14 wheel stack (torch 2.12.0). `/opt/venv`'s older 2.9.1 build is a fallback only, and it lives on the overlay so it does not survive a re-image.

## ROCm version strategy

The documented standard is the ROCm 7.14 wheel stack, not the 7.2.1 image layer. Install with the AMD index, not the PyTorch rocm index:

```bash
python -m pip install --index-url https://repo.amd.com/rocm/whl-multi-arch/ \
    "torch[device-gfx1100]==2.12.0+rocm7.14.0" \
    "torchvision[device-gfx1100]==0.27.0+rocm7.14.0" \
    "torchaudio==2.11.0+rocm7.14.0"
```

The `[device-gfx1100]` extra matters: it pulls the architecture leaf package correctly. Use system ROCm 7.2.1 only when you need `hipcc` or CMake for a source build, and still run the result on the 7.14 runtime.

## Environment variables

Sourced from `/workspace/env.sh` before every `rc exec` / `rc run`:

- `PATH=/workspace/venv/bin:$PATH` — as of 2026-09-01 this venv carries the standard torch 2.12.0+rocm7.14.0 stack, so no `--venv` is needed. It did not always; the file is the user's to edit, so verify with `rc env` rather than assuming.
- `HF_HOME=/root/.cache/huggingface`
- `HSA_OVERRIDE_GFX_VERSION=11.0.0`

## Available remote tooling

`rsync`, `tar`, `nohup`, `timeout`, `setsid`, `pkill`, `python3` (3.12.3), `rocm-smi`, `rocminfo`. There is **no tmux or screen**, which is why `rc run` uses `setsid`+`nohup` and tracks pids itself. Locally there is **no rsync**, which is why `push`/`pull` stream tar over ssh instead.

## Text convention for anything written for this project

No 76-column hard wrapping. Every paragraph is written as a single line and left to the viewer to soft-wrap. This applies to prose in Markdown documents, GitHub comments and issues, commit messages and chat replies. Code blocks are exempt and follow their own language style. Existing hard-wrapped history is left alone unless reformatting is explicitly requested.

## Image comparison galleries

When presenting reproduction-versus-baseline image comparisons, follow the convention already shipped in `awesome-sensenova-u1.5` at `results/gallery/wip-round1/`: a two-column table with the reference baseline on the left and the reproduction on the right, one reproduction image per case at deterministic seed k=0, both columns normalised to the same canvas aspect ratio (long edge 1200, corner-average matte) and rendered at identical `width="420"`, prompts in per-side collapsible blocks, per-case reproduction parameters, and a scoring-status banner at the top of the page.
