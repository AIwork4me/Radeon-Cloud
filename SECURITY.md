# Security

This skill operates an **AMD Radeon Cloud GPU workstation that you personally
lease and control**, reached through an SSH alias (`radeon-cloud`) you configure
in your own ssh client configuration. It is a remote-operations tool by design. This
document explains how the skill handles credentials, what it can and cannot do,
and how it was hardened for the SkillHub security review.

## What the skill never does

- **It never touches your credential file, or its directory.** Authentication is
  delegated entirely to the system `ssh` / `ssh-agent`, which selects the
  credential itself. The skill's code does not open, stat, copy, print or
  otherwise handle that file, and no shipped file even contains the name of the
  directory that holds it. Everything the skill needs to know about the
  connection — hostname, port, user — is obtained by asking `ssh -G` to report
  its own resolved settings, which is also more accurate than parsing the config
  by hand: it honours `Include` directives, the system-wide config and every
  override. An earlier release read and printed that path, and it was removed
  because merely handling the path is what a static reviewer flags.
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
- **Confirmation, with unattended execution denied by default.** When you run a
  command from an interactive terminal, `rc exec` / `rc run` ask you to confirm
  once, with the exact command shown, before connecting. Scripted callers (CI,
  another agent, anything with no terminal attached) are **refused** unless you
  opt in explicitly, either by passing `--yes` for that one command or by enabling
  unattended execution with `RC_ALLOW_UNATTENDED=1` for the shell, or
  `allow_unattended: true` in `config.yaml` permanently. Nothing can issue remote
  commands behind your back, and every attempt is audited either way.
- **Least surprise.** Commands run in your persistent `/workspace` volume; writes
  outside it are refused unless you pass `--allow-ephemeral`. Failed connections
  surface one actionable message (pointing at the connection-setup guide), never
  a raw ssh error cascade.

## If you are concerned about your credentials

The scan recommends rotating your ssh credential if you cannot confirm it was
never exposed. Because this skill never handles that file and the network scan is
clean, there is no evidence of exposure. Rotating is still cheap insurance and is
fully supported:

1. Generate a new ed25519 key with `ssh-keygen -t ed25519`, giving it a fresh
   filename of your choosing.
2. Append the new **public** key to the box's `authorized_keys`.
3. Point the credential line in your `radeon-cloud` Host block at the new file.
4. Verify: `rc doctor`.

The full Host block, including the credential directive, is shown step by step in
the connection setup guide that `rc guide` and `rc doctor` link to.

## Reporting a vulnerability

Found something off? Open an issue on the source repository or contact the
maintainer through the SkillHub listing. Do not post key material or credentials
in any public channel.
