# sshm

[![CI](https://github.com/revsearch/sshm/actions/workflows/ci.yml/badge.svg)](https://github.com/revsearch/sshm/actions/workflows/ci.yml)

An SSH session manager with a background daemon. Remote shells stay alive when you
close the terminal, reconnect on their own when the link drops, and reattach
instantly. State is kept in plain `~/.ssh/config`, so `ssh`, `scp`, and `rsync`
keep working alongside it.

```bash
sshm add prod root@192.0.2.10   # keygen + copy key + write ~/.ssh/config
sshm prod                       # attach to a live shell (or start one)
# close the terminal — the shell keeps running; `sshm prod` reattaches it
```

## Install

With [uv](https://docs.astral.sh/uv/):

```bash
uv tool install git+https://github.com/revsearch/sshm
```

Or with [pipx](https://pipx.pypa.io/):

```bash
pipx install git+https://github.com/revsearch/sshm
```

From a checkout:

```bash
git clone https://github.com/revsearch/sshm
cd sshm
uv tool install .        # or: pip install .
```

Needs Python 3.12+. The `sshm` and `sshmd` commands are installed to your tool bin
directory (`~/.local/bin`, or the Python Scripts dir on Windows) — make sure it's
on your PATH. Update with `uv tool upgrade sshm`.

## Quick start

```bash
# Add a server: generates an ed25519 key, copies it to the remote, writes config
sshm add myserver root@192.168.1.100

# Connect (interactive shell via the daemon)
sshm myserver

# Or explicitly
sshm c myserver
```

## Commands

Most commands have a short alias (shown first). `sshm <alias>` with no command is
shorthand for `connect`.

### Sessions

```bash
sshm <alias>                        # Connect (shorthand)
sshm c,  connect <alias> [name]     # Attach to a session or create a new one
sshm l,  list                       # List all hosts
sshm l,  list <alias>               # List active sessions for a host
sshm a,  add <alias> user@host      # Add a server (keygen + copy key)
sshm r,  remove <alias>             # Remove a host and disconnect all sessions
sshm mv, rename <alias> <new>       # Rename an alias (and its managed key)
```

`add` takes `user@host`, `user@host:port`, or bracketed IPv6
(`user@[2001:db8::1]:22`).

### Port forwarding and SOCKS

Forwards are written as native `LocalForward` / `RemoteForward` / `DynamicForward`
directives in `~/.ssh/config`, so any SSH client sees them. Direction is `-L` /
`-R` / `-D`, same as `ssh`:

```bash
sshm po a, port add <alias> -L <local>:<host>:<remote>     # Local forward
sshm po a, port add <alias> -R <remote>:<host>:<local>     # Reverse forward
sshm po a, port add <alias> -D <port>                      # SOCKS proxy (ssh -D)
sshm po r, port remove <alias> -L <local>:<host>:<remote>  # Remove a forward
sshm po r, port remove <alias> -D <port>                   # Remove a SOCKS proxy
```

`-D <port>` is a dynamic forward — a SOCKS5 proxy on `127.0.0.1:<port>` tunnelled
through the host. Point a browser or any SOCKS-aware app at it.

### Auto-connect

When enabled, the daemon keeps at least one shell alive and reconnects it on
failure, so attaching is instant.

```bash
sshm e, enable <alias>       # Keep a session alive, auto-reconnect
sshm d, disable <alias>      # Stop auto-connect
```

### Import / export

Move hosts, including their keys, between machines as JSON.

```bash
sshm export servers.json                # Export all hosts
sshm export prod.json web db api        # Export specific hosts
sshm l servers.json                     # Preview a JSON file
sshm import servers.json                # Import (skip existing)
sshm import servers.json -o             # Import (override existing)
sshm import servers.json web db         # Import only specific hosts
```

### Daemon

The daemon (`sshmd`) starts on first use.

```bash
sshm status          # Daemon status
sshm stop            # Stop the daemon
sshm install         # Autostart on login (systemd / launchd / Task Scheduler)
sshm uninstall       # Remove autostart
```

## Shell completions

### fish

`sshm` ships fish completions: subcommands, `port -L/-R/-D` flags, and host aliases
pulled live from `~/.ssh/config` (including the bare `sshm <alias>` shorthand).
`uv tool install` does **not** wire these up automatically, so install them once
(`exec fish` reloads the shell so they're active immediately):

```fish
sshm completions fish > ~/.config/fish/completions/sshm.fish && exec fish
```

fish autoloads from that directory — new sessions pick it up with no `source`. From
a checkout you can instead symlink the source file so edits are picked up live:

```fish
ln -s (path resolve src/sshm/completions/sshm.fish) ~/.config/fish/completions/sshm.fish
```

## Session states

| Icon | State    | Meaning                           |
|------|----------|-----------------------------------|
| `●`  | ready    | Shell running, waiting for attach |
| `◆`  | attached | A client is connected             |
| `○`  | dead     | Process exited                    |

## How it works

```
sshm (CLI) ── TCP localhost:19222 ──> sshmd (daemon)
                                        ├── SSH processes (one shell per session)
                                        ├── Reader threads (scrollback buffer)
                                        ├── Watchdog (health, reconnect, keep-warm)
                                        └── IPC server (JSON protocol + I/O bridge)
```

- The daemon spawns `ssh -tt <alias>` under a real PTY (POSIX; pipes on Windows)
  and holds the shell process.
- `connect` bridges your terminal I/O to that process over TCP and forwards your
  terminal size (and resizes / `SIGWINCH`) so the remote shell matches your window.
- Detaching (closing the terminal, Ctrl-C) leaves the shell running; reattach later.
- Typing `exit` in the shell removes the session cleanly — no reconnect.
- A lost connection (SSH exit 255) triggers reconnect with exponential backoff.
- Config stays in `~/.ssh/config`. Every rewrite goes to a temp file and is
  replaced atomically (a `config.bak` is kept), so a crash mid-write won't corrupt it.
- Runtime state lives in `~/.sshm/`: pid file, IPC token, `sshmd.log`.
- The IPC server binds `127.0.0.1` only and checks a random per-daemon token
  (stored `0600` in `~/.sshm/token`) on every request.
- The IPC port defaults to `19222`; set `SSHM_PORT` to change it (e.g. to run sshm
  in both Windows and WSL when mirrored networking shares localhost). `sshm install`
  persists the port to `~/.sshm/port` so the autostarted daemon — which doesn't see
  your shell env — uses the same one.

## Platforms

- Linux — systemd user service for autostart.
- macOS — launchd LaunchAgent.
- Windows — Task Scheduler for autostart, Win32 console VT mode for terminal I/O.

On POSIX, ssh runs under a real PTY, so the remote shell tracks your window size
and resizes. Windows has no `pty` module, so there a session keeps the size it
attached with — full-screen apps (`vim`, `htop`) won't follow later resizes.

## Development

```bash
uv sync           # install dependencies (including the dev group)
uv run pytest     # run the tests
```

Module layout (`src/sshm/`):

| Module         | Responsibility                                            |
|----------------|-----------------------------------------------------------|
| `cli.py`       | CLI commands (click), entry point `sshm`                  |
| `daemon.py`    | `sshmd`: request dispatch, watchdog, entry `sshmd`        |
| `process.py`   | SSH sessions: PTY spawn, scrollback, reconnect, health    |
| `ipc.py`       | TCP IPC client/server on `127.0.0.1:19222` (token auth)   |
| `protocol.py`  | JSON message schema for IPC                               |
| `config.py`    | `~/.ssh/config` parser/writer, port-forward rules         |
| `terminal.py`  | Raw terminal bridge (termios on Unix, Win32 console API)  |
| `procutil.py`  | Cross-platform process helpers (pid checks, Popen flags)  |
| `state.py`     | `~/.sshm` runtime files: pid, token, port, log            |
| `autostart.py` | Task Scheduler / systemd / launchd integration            |

## Troubleshooting

### Windows: `Bad owner or permissions on ~/.ssh/config`

OpenSSH on Windows refuses to read `~/.ssh/config` if the ACL contains extra
principals like `OWNER RIGHTS` (S-1-3-4). Symptom:

```
Bad permissions. Try removing permissions for user: \\OWNER RIGHTS (S-1-3-4) on file C:/Users/<you>/.ssh/config.
Bad owner or permissions on C:\\Users\\<you>/.ssh/config
```

Remove the offending principal:

```powershell
icacls "$env:USERPROFILE\.ssh\config" /remove "OWNER RIGHTS"
```

If other files in `.ssh` have the same issue (private keys, etc.):

```powershell
icacls "$env:USERPROFILE\.ssh\*" /remove "OWNER RIGHTS"
```

If that's not enough (inheritance can pull in other groups), reset the ACL so only
you have access:

```powershell
$f = "$env:USERPROFILE\.ssh\config"
icacls $f /inheritance:r
icacls $f /grant:r "$($env:USERNAME):(F)"
```

If `icacls` returns `Access is denied`, you don't own the file. Take ownership
first, then fix the ACL:

```powershell
takeown /F "$env:USERPROFILE\.ssh\config"
icacls "$env:USERPROFILE\.ssh\config" /grant "$($env:USERNAME):F"
icacls "$env:USERPROFILE\.ssh\config" /inheritance:r
icacls "$env:USERPROFILE\.ssh\config" /grant:r "$($env:USERNAME):(F)"
```

If `takeown` fails, run PowerShell as Administrator and repeat. Verify with
`icacls "$env:USERPROFILE\.ssh\config"` — only your user with `(F)` should remain.
