# radeon-cloud troubleshooting

## `REMOTE HOST IDENTIFICATION HAS CHANGED` / `Host key verification failed`

The instance was re-imaged, so its SSH host keys rotated. This is expected and happens periodically; it is not an attack as long as the fingerprint is stable across repeated scans.

Diagnose and repair in one step:

```bash
"$PY" "$RC" doctor
```

`doctor` detects the mismatch, runs `ssh-keyscan`, prints the freshly served fingerprints, backs up the known-hosts file ssh is using to a timestamped copy, then asks before trusting. Only the entries for the exact target host and port are removed, so sibling containers on the same IP but different ports keep their records.

If you must act manually, the safe sequence is: back up `known_hosts`, run `ssh-keygen -R "[<ip>]:<port>"`, then append the output of `ssh-keyscan -p <port> -H <ip>`. Never disable `StrictHostKeyChecking` wholesale.

## `ModuleNotFoundError: No module named 'torch'`

Every command through `rc exec` / `rc run` sources `/workspace/env.sh`, so you inherit whatever venv it puts first on `PATH`. When that venv cannot import torch, `import torch` fails.

This used to be the box's standing defect: `env.sh` pointed at a venv with no torch at all. The user fixed it on 2026-09-01 by installing the standard stack into `/workspace/venv` and updating `env.sh`. The file is theirs to edit and the instance gets re-imaged, so the defect can come back — treat it as a state to check, not a fact to remember.

You normally should not hit this either way: the connector detects that `env.sh`'s default venv cannot import torch and automatically prepends a torch-capable one, printing an advisory line naming both paths. The inventory is cached for six hours in `~/.radeon-cloud-connector/venv-cache.json`.

If it still happens, the cache is stale or no venv has a working torch. Refresh and inspect:

```bash
"$PY" "$RC" env                                              # refreshes cache, lists live venvs
"$PY" "$RC" exec --no-auto-venv -- python -c "import torch"  # reproduce the raw failure
```

Choose any explicit `--venv` path from what `rc env` currently prints, never from older notes. Venvs get deleted during disk cleanups: `venv-torch212`, `venv-53615-statea`, `venv-mainline-probe` and `bench-venv` were all removed on 2026-09-01.

```bash
"$PY" "$RC" exec --venv /workspace/venv -- python -c "import torch; print(torch.__version__)"
```

If the user wants the underlying problem gone permanently, the change is to `/workspace/env.sh` on the remote host, but ask first because that file is shared with their other projects.

## `... is outside the persistent volume /workspace`

The command targeted a path on the container overlay, where data is destroyed on the next re-image. This is the guard working. Either retarget into `/workspace`, or pass `--allow-ephemeral` when writing to scratch really is intended; the connector then prints an `EPHEMERAL (explicitly allowed)` warning so the choice is visible in the transcript.

## Job disappeared, or died when the terminal closed

`rc run` detaches with `setsid` and `nohup`, so the job survives losing the local terminal. If a job is genuinely gone, read its log:

```bash
"$PY" "$RC" logs <job-id> -n 200
```

`rc jobs` reports state by probing the recorded pid, so an `exited` entry means the process ended; the log holds the reason. There is no tmux on this box, so never rely on a shell session to keep work alive.

## `stop` reports success but the process is still running

The default signal is SIGTERM, which a process may ignore or trap while it checkpoints. Either wait and re-check with `rc jobs`, or escalate:

```bash
"$PY" "$RC" stop <job-id> --force -y
```

## Push or pull fails midway

Check free space first, since `/workspace` has been seen at 87% used:

```bash
"$PY" "$RC" status
```

For `push`, confirm the local path exists and that `--exclude` patterns match relative paths or basenames. For `pull`, the local destination must not already exist unless `--overwrite` is passed. Both support `--dry-run` to print intent without transferring.

## GPU out of memory

Only one card is passed through, so VRAM is contended by anything else running. Check what is resident:

```bash
"$PY" "$RC" status
"$PY" "$RC" exec -- rocm-smi --showmemuse
```

If another job you own is holding VRAM, stop it with `rc stop <job-id>`. Do not kill processes you do not recognise without asking, since the host is shared.

## `rocm-smi` reports a suspicious GPU count

`rocm-smi --showproductname` prints one line per attribute and every line carries the same `GPU[0]` prefix, so counting matching lines overcounts badly (it reports 9 for a single card). Count unique device indices instead:

```bash
rocm-smi --showproductname | grep -oE 'GPU\[[0-9]+\]' | sort -u | wc -l
```

## `cannot reach the remote workstation` / exit code 2

The connector probed the host before doing any work and it did not answer. Exit code `2` means a connection or authentication problem, as opposed to `1` for a real failure of the command itself. The message that follows is specific to the cause and names the next step:

| Symptom in the message | Cause and fix |
|---|---|
| `ssh alias '<name>' does not resolve to anything in your ssh client configuration` | The alias is missing or misspelled, so `ssh -G` falls back to defaults. Add a `Host` block with `HostName`, `User` and `Port`; the connection setup guide shows the complete block. |
| `does not resolve` | The container is stopped, or its IP/port changed on rebuild. Update the ssh config. |
| `refused the credentials ssh offered` | The public key is not authorised on the box, or the credential line in your ssh config names the wrong file. |
| `could not load the credential its config names` | The credential line names a file that does not exist. |
| `actively refused the connection` | Host is up, nothing listening on the port yet. Wait and retry. |
| `did not answer within Ns` | Firewall, VPN down, or stopped instance. Raise `connect_timeout`. |
| `SSH host key mismatch` | Re-imaged. Run `rc doctor` and accept the repair. |

A remote command failing with exit `1` and a python traceback is **not** reported this way — that is the user's bug, and it stays a traceback.

## Connector itself misbehaves

The connector's own verdicts are the fastest starting point — they are written to say what to run next:

```bash
"$PY" "$RC" doctor
"$PY" "$RC" status --torch
```

Reset local config to defaults:

```bash
"$PY" "$RC" config --reset
```

Config lives at `~/.radeon-cloud-connector/config.json`. Point the connector at a different ssh alias with `--host <alias>` or `rc config --set host=<alias>`.
