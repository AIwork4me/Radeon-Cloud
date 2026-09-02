# Security

This skill operates an **AMD Radeon Cloud GPU workstation that you personally
lease and control**, reached through an SSH alias (`radeon-cloud`) you configure
in your own `~/.ssh/config`. It is a remote-operations tool by design. This
document explains how the skill handles credentials, what it can and cannot do,
and how it was hardened for the SkillHub security review.

## What the skill never does

- **It never reads your private key.** Authentication is delegated entirely to
  the system `ssh` / `ssh-agent`, which resolves `IdentityFile` from your
  `~/.ssh/config` itself. The skill's code does **not** `open()`, `os.path.exists()`,
  copy, print, or otherwise touch your private-key file. The previous "private
  key present" precheck was removed precisely because it read the key path.
- **It never exfiltrates anything.** The skill only talks to your configured
  alias. The SkillHub static scan independently confirmed *network requests and
  data exfiltration = clean (0 findings)*. No key material, environment variable,
  or file content leaves your machine except the commands you explicitly type and
  their stdout/stderr to/from your own box.
- **It never hard-codes an endpoint.** The public IP:port of your box is yours;
  it is intentionally absent from every shipped file. Connection always goes
  through the `radeon-cloud` alias resolved from your ssh config.

## What the skill does (and the controls around it)

- **Target whitelist.** Remote commands run only against the ssh alias you
  configured. The CLI `--host` override is rejected unless it names that same
  alias, so the skill cannot be pointed at an arbitrary host or used as a proxy.
  (Advanced self-hosting via a raw host in `config.yaml` is still permitted; that
  path does not use `--host`.)
- **Audit log.** Every `rc exec` / `rc run` writes one append-only line to
  `~/.radeon-cloud-connector/audit.log` — timestamp, the alias, the command as
  you typed it, and its exit code. No secrets are recorded.
- **Confirmation.** When you run a command interactively, `rc exec` / `rc run`
  ask you to confirm once (with the exact command shown) before connecting.
  Non-interactive use (CI, scripts, the bundled journey test) is unaffected;
  pass `--yes` to skip the prompt in automation.
- **Least surprise.** Commands run in your persistent `/workspace` volume; writes
  outside it are refused unless you pass `--allow-ephemeral`. Failed connections
  surface one actionable message (pointing at the connection-setup guide), never
  a raw ssh error cascade.

## If you are concerned about your key

The scan recommends rotating the key if you cannot confirm it was never exposed.
Because this skill never reads the key and the network scan is clean, there is no
evidence of exposure. Rotating is still cheap insurance and is fully supported:

1. Generate a new key: `ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_radeon_cloud`
2. Append the new **public** key to the box's `~/.ssh/authorized_keys`.
3. Update the `IdentityFile` in your `radeon-cloud` Host block if the path changed.
4. Verify: `rc doctor`.

## Reporting a vulnerability

Found something off? Open an issue on the source repository or contact the
maintainer through the SkillHub listing. Do not post key material or credentials
in any public channel.
