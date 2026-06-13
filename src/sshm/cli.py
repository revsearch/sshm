"""sshm — SSH session manager CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import ClassVar

import click

from . import protocol
from .ipc import DaemonNotRunning, connect_streaming, ensure_daemon, send_request
from .terminal import stream_bridge


def _send(cmd: str, **kwargs) -> dict:
    try:
        ensure_daemon()
        resp = send_request(cmd, **kwargs)
    except DaemonNotRunning as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    if not resp.get("ok"):
        click.echo(f"Error: {resp.get('error', 'Unknown error')}", err=True)
        sys.exit(1)

    return resp.get("data", {})


class AliasedGroup(click.Group):
    """Click group with short aliases and custom help formatting."""

    COMMAND_ALIASES: ClassVar[dict[str, str]] = {
        "l": "list",
        "c": "connect",
        "a": "add",
        "r": "remove",
        "mv": "rename",
        "e": "enable",
        "d": "disable",
    }

    def get_command(self, ctx, cmd_name):
        cmd_name = self.COMMAND_ALIASES.get(cmd_name, cmd_name)
        return super().get_command(ctx, cmd_name)

    # Prefix groups with flexible add/remove syntax:
    #   sshm p a <alias> -L ...      → port-add
    #   sshm p <alias> a -L ...      → port-add <alias> ...
    #   sshm p a <alias> -D <port>   → port-add (SOCKS proxy)
    PREFIX_COMMANDS: ClassVar[dict[tuple[str, ...], tuple[str, str]]] = {
        ("port", "po", "p"): ("port-add", "port-remove"),
    }

    def resolve_command(self, ctx, args):
        if args:
            for prefixes, (add_name, remove_name) in self.PREFIX_COMMANDS.items():
                if args[0] not in prefixes:
                    continue
                rest = list(args[1:])
                for i in range(min(2, len(rest))):
                    if rest[i] in ("add", "a"):
                        rest.pop(i)
                        return super().resolve_command(ctx, [add_name] + rest)
                    elif rest[i] in ("remove", "r", "rm"):
                        rest.pop(i)
                        return super().resolve_command(ctx, [remove_name] + rest)
                # A prefix command (port) with no add/remove action: fail with a
                # usage hint instead of silently falling through to `connect`,
                # which would report a baffling "Unknown alias: port".
                if not ctx.resilient_parsing:
                    ctx.fail(
                        f"'{args[0]}' needs an action: a|add or r|remove "
                        f"(e.g. sshm port <alias> a -L 8080:localhost:80)"
                    )
                break

        # "sshm <alias>" → "sshm connect <alias>" if not a known command
        if args and args[0] not in self.COMMAND_ALIASES and args[0] not in self.list_commands(ctx):
            args = ["connect"] + list(args)

        return super().resolve_command(ctx, args)

    def format_help(self, ctx, formatter):
        formatter.write("sshm — SSH Session Manager\n\n")

        formatter.write("Usage:\n")
        formatter.write("  sshm <alias>                                 Connect (shorthand)\n")
        formatter.write("  sshm <command> [args]\n\n")

        formatter.write("Sessions:\n")
        formatter.write("  l,  list    [alias]                          List hosts or sessions\n")
        formatter.write("  c,  connect <alias> [name]                   Attach to session\n")
        formatter.write("  a,  add     <alias> user@host[:port]         Add server\n")
        formatter.write("  r,  remove  <alias>                          Remove host\n")
        formatter.write("  mv, rename  <alias> <new-alias>              Rename host alias\n")
        formatter.write("\n")

        formatter.write("Forwarding:\n")
        formatter.write("  p,  port <alias> a|r -L|-R <local>:<host>:<remote>   Port forward\n")
        formatter.write("  p,  port <alias> a|r -D <port>                       SOCKS proxy (ssh -D)\n")
        formatter.write("\n")

        formatter.write("Auto-connect:\n")
        formatter.write("  e,  enable  <alias>                          Keep session alive\n")
        formatter.write("  d,  disable <alias>                          Stop auto-connect\n")
        formatter.write("\n")

        formatter.write("Import/Export:\n")
        formatter.write("  export <file> [names]                        Export hosts + keys\n")
        formatter.write("  import <file> [-o] [name|name=new]            Import hosts + keys\n")
        formatter.write("  l,  list <file.json>                         Preview JSON file\n")
        formatter.write("\n")

        formatter.write("Daemon:\n")
        formatter.write("  status                                       Daemon status\n")
        formatter.write("  stop                                         Stop daemon\n")
        formatter.write("  install                                      Autostart on login\n")
        formatter.write("  uninstall                                    Remove autostart\n")
        formatter.write("  completions [fish]                           Print shell completion script\n")


@click.group(cls=AliasedGroup)
def main():
    pass


# --- list ---

@main.command("list")
@click.argument("alias", required=False)
def list_cmd(alias: str | None):
    """List hosts or sessions for alias, or contents of a JSON file."""
    if alias and alias.endswith(".json"):
        _list_json(alias)
        return

    data = _send(protocol.CMD_LIST, alias=alias)

    if alias:
        _print_sessions(alias, data)
    else:
        _print_hosts(data)


def _print_sessions(alias: str, sessions: list[dict]) -> None:
    if not sessions:
        click.echo(f"No active connections for '{alias}'")
        return
    click.echo(f"{'NAME':<20} {'PID':<8} {'STATE':<12} {'UPTIME':<12} {'FORWARDS'}")
    click.echo("-" * 70)
    for s in sessions:
        uptime = _format_uptime(s.get("uptime", 0))
        fwds = ", ".join(s.get("port_forwards", []))
        if not s.get("alive", False):
            state, icon = "dead", "○"
        elif s.get("attached", False):
            state, icon = "attached", "◆"
        else:
            state, icon = "ready", "●"
        click.echo(f"{icon} {s['name']:<18} {s.get('pid', '-'):<8} {state:<12} {uptime:<12} {fwds}")


def _print_hosts(hosts: list[dict]) -> None:
    if not hosts:
        click.echo("No hosts configured. Use 'sshm add' to add one.")
        return
    click.echo(f"{'ALIAS':<16} {'HOST':<20} {'USER':<12} {'SESS':<6} {'ENABLED':<8} {'FORWARDS'}")
    click.echo("-" * 80)
    for e in hosts:
        fwds = ", ".join(e.get("port_forwards", []))
        enabled = "yes" if e.get("enabled") else "-"
        host_port = e["hostname"]
        if e.get("port", 22) != 22:
            host_port += f":{e['port']}"
        conns = e.get("connections", 0)
        attached = e.get("attached", 0)
        sess = f"{attached}/{conns}" if conns else "0"
        click.echo(
            f"{e['alias']:<16} {host_port:<20} {e.get('user', '-'):<12} "
            f"{sess:<6} {enabled:<8} {fwds}"
        )


def _list_json(filepath: str) -> None:
    hosts = _read_hosts_file(filepath)
    if not hosts:
        click.echo("No hosts in file.")
        return
    click.echo(f"{'ALIAS':<16} {'HOST':<20} {'USER':<12} {'PORT':<6} {'ENABLED':<8} {'FORWARDS'}")
    click.echo("-" * 80)
    for h in hosts:
        fwds = ", ".join(h.get("port_forwards", []))
        enabled = "yes" if h.get("enabled") else "-"
        host_port = h.get("hostname", "")
        port = h.get("port", 22)
        if port != 22:
            host_port += f":{port}"
        click.echo(
            f"{h['alias']:<16} {host_port:<20} {h.get('user', '-'):<12} "
            f"{port:<6} {enabled:<8} {fwds}"
        )


def _read_hosts_file(filepath: str) -> list[dict]:
    p = Path(filepath)
    if not p.exists():
        click.echo(f"Error: file not found: {filepath}", err=True)
        sys.exit(1)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        click.echo(f"Error: cannot read {filepath}: {e}", err=True)
        sys.exit(1)
    if not isinstance(data, dict) or not isinstance(data.get("hosts"), list):
        click.echo(f"Error: {filepath} is not a valid sshm export (no 'hosts' list)", err=True)
        sys.exit(1)
    # Keep only well-formed host objects that at least carry an alias.
    return [h for h in data["hosts"] if isinstance(h, dict) and h.get("alias")]


# --- connect (attach) ---

def _terminal_size() -> tuple[int, int]:
    """Current (cols, rows) of the controlling terminal, with a sane fallback."""
    try:
        sz = os.get_terminal_size()
        return sz.columns, sz.lines
    except OSError:
        return 80, 24


@main.command("connect")
@click.argument("alias")
@click.argument("name", required=False)
def connect_cmd(alias: str, name: str | None):
    """Attach to session (or create new)."""
    cols, rows = _terminal_size()
    try:
        ensure_daemon()
        sock, resp, initial = connect_streaming(
            protocol.CMD_ATTACH, alias=alias, name=name, cli_pid=os.getpid(),
            cols=cols, rows=rows,
        )
    except DaemonNotRunning as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    if not resp.get("ok"):
        click.echo(f"Error: {resp.get('error', 'Unknown error')}", err=True)
        sys.exit(1)

    session_name = resp["data"]["name"]
    click.echo(f"Attached to {session_name}")

    # Forward later terminal resizes to the daemon on a separate connection (the
    # bridge socket is a raw byte stream). Fire-and-forget so a slow/again-busy
    # daemon never stalls the interactive session.
    def on_resize(new_cols: int, new_rows: int) -> None:
        def _send() -> None:
            try:
                send_request(
                    protocol.CMD_RESIZE, alias=alias, name=session_name,
                    cols=new_cols, rows=new_rows,
                )
            except Exception:
                pass
        threading.Thread(target=_send, daemon=True).start()

    stream_bridge(sock, initial, on_resize=on_resize)

    click.echo(f"\nDetached from {session_name}")


# --- add ---

def _parse_port(port_str: str) -> int:
    try:
        port = int(port_str)
    except ValueError:
        click.echo(f"Error: invalid port '{port_str}'", err=True)
        sys.exit(1)
    if not 1 <= port <= 65535:
        click.echo(f"Error: port out of range: {port}", err=True)
        sys.exit(1)
    return port


def _parse_target(target: str) -> tuple[str, str, int]:
    """Split 'user@host[:port]' into (user, hostname, port).

    Supports bracketed IPv6 literals: user@[::1] or user@[::1]:2222.
    """
    if "@" not in target:
        click.echo("Error: target must be user@host[:port]", err=True)
        sys.exit(1)

    user, host_part = target.split("@", 1)

    def _ok(hostname: str, port: int) -> tuple[str, str, int]:
        if not user or not hostname:
            click.echo(f"Error: target must be user@host[:port], got '{target}'", err=True)
            sys.exit(1)
        return user, hostname, port

    if host_part.startswith("["):  # bracketed IPv6, optional :port after the ]
        end = host_part.find("]")
        if end == -1:
            click.echo("Error: unterminated '[' in IPv6 address", err=True)
            sys.exit(1)
        hostname = host_part[1:end]
        rest = host_part[end + 1:]
        if rest.startswith(":"):
            return _ok(hostname, _parse_port(rest[1:]))
        if rest == "":
            return _ok(hostname, 22)
        click.echo(f"Error: unexpected '{rest}' after IPv6 address", err=True)
        sys.exit(1)

    # A bare IPv6 literal has multiple colons and no port; everything else uses
    # the last colon as the host/port separator.
    if host_part.count(":") == 1:
        hostname, port_str = host_part.rsplit(":", 1)
        return _ok(hostname, _parse_port(port_str))
    return _ok(host_part, 22)


def _ensure_key(alias: str) -> Path:
    """Generate an ed25519 key for the alias if it doesn't exist yet."""
    ssh_dir = Path.home() / ".ssh"
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    key_path = ssh_dir / f"sshm_{alias}"

    if key_path.exists():
        click.echo(f"Using existing key: {key_path}")
        return key_path

    click.echo(f"Generating SSH key: {key_path}")
    result = subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", "", "-C", f"sshm_{alias}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        click.echo(f"ssh-keygen failed: {result.stderr}", err=True)
        sys.exit(1)
    return key_path


def _copy_key_to_remote(key_path: Path, user: str, hostname: str, port: int) -> None:
    click.echo(f"Copying key to {user}@{hostname}...")
    pub_key = key_path.with_suffix(".pub").read_text(encoding="utf-8").strip()

    # Install the key idempotently and fix up everything StrictModes cares about:
    #  - tighten $HOME perms (group/other-writable home makes sshd ignore the key)
    #  - create ~/.ssh and authorized_keys with correct modes
    #  - skip the append if the key is already present (no duplicates)
    #  - the leading printf newline guards against a file with no trailing newline,
    #    which would otherwise glue our key onto the previous one
    remote = (
        'chmod go-w ~ 2>/dev/null; '
        'mkdir -p ~/.ssh && chmod 700 ~/.ssh && '
        'touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && '
        f'grep -qxF "{pub_key}" ~/.ssh/authorized_keys || '
        f'printf \'\\n%s\\n\' "{pub_key}" >> ~/.ssh/authorized_keys'
    )
    cmd = ["ssh"]
    if port != 22:
        cmd.extend(["-p", str(port)])
    cmd.extend([f"{user}@{hostname}", remote])

    if subprocess.run(cmd).returncode != 0:
        click.echo("Warning: failed to copy key. You may need to copy it manually.", err=True)


@main.command("add")
@click.argument("alias")
@click.argument("target")
def add_cmd(alias: str, target: str):
    """Add server (keygen + copy key)."""
    from .config import add_host, find_entry, ssh_config_path

    user, hostname, port = _parse_target(target)

    if find_entry(alias):
        click.echo(f"Error: alias '{alias}' already exists", err=True)
        sys.exit(1)

    key_path = _ensure_key(alias)
    _copy_key_to_remote(key_path, user, hostname, port)

    add_host(
        alias, hostname, user, port, f"~/.ssh/sshm_{alias}", ssh_config_path(),
        # IdentitiesOnly stops ssh from offering agent/default keys first and
        # exhausting MaxAuthTries before it ever tries this host's key.
        extra_options=[("IdentitiesOnly", "yes")],
    )
    click.echo(f"Added '{alias}' -> {user}@{hostname}:{port}")

    click.echo("Testing connection...")
    test = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
         "-o", "IdentitiesOnly=yes",
         "-i", str(key_path), "-p", str(port),
         f"{user}@{hostname}", "echo ok"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15,
    )
    if test.returncode == 0:
        click.echo("Connection successful!")
    else:
        click.echo("Warning: test connection failed — key auth is not working yet.")
        err = (test.stderr or "").strip()
        if err:
            click.echo(err, err=True)


# --- remove ---

@main.command("remove")
@click.argument("alias")
def remove_cmd(alias: str):
    """Remove host and disconnect."""
    _send(protocol.CMD_REMOVE, alias=alias)
    click.echo(f"Removed '{alias}'")


# --- rename ---

def _managed_key_paths(alias: str) -> tuple[Path, Path]:
    ssh_dir = Path.home() / ".ssh"
    key = ssh_dir / f"sshm_{alias}"
    return key, key.with_suffix(".pub")


def _rename_key_files(old_alias: str, new_alias: str) -> None:
    """Rename the sshm-managed key pair to match a renamed alias."""
    old_key, old_pub = _managed_key_paths(old_alias)
    new_key, new_pub = _managed_key_paths(new_alias)
    for src, dst in ((old_key, new_key), (old_pub, new_pub)):
        if src.exists():
            src.rename(dst)


@main.command("rename")
@click.argument("alias")
@click.argument("new_alias")
def rename_cmd(alias: str, new_alias: str):
    """Rename a host alias."""
    from .config import find_entry

    # Capture the identity file before the rename to decide whether the managed
    # key pair should follow along (the daemon updates the config reference).
    entry = find_entry(alias)
    managed_key = entry is not None and entry.identity_file == f"~/.ssh/sshm_{alias}"

    # Pre-flight: refuse if the destination key files already exist, BEFORE the
    # daemon rewrites the config. Otherwise the rename could point IdentityFile
    # at a key we then fail to create, leaving a dangling reference.
    if managed_key:
        for dst in _managed_key_paths(new_alias):
            if dst.exists():
                click.echo(
                    f"Error: {dst} already exists; remove or rename it first", err=True
                )
                sys.exit(1)

    _send(protocol.CMD_RENAME, alias=alias, new_alias=new_alias)

    if managed_key:
        _rename_key_files(alias, new_alias)

    click.echo(f"Renamed '{alias}' -> '{new_alias}'")


# --- port add / port remove (forwards + SOCKS proxy via -D) ---

def _parse_port_args(args: tuple[str, ...]) -> tuple[str, str]:
    """Parse raw forward args into (direction, rule).

    -L/-R take a <local>:<host>:<remote> rule; -D takes a single <port> and
    declares a SOCKS proxy (DynamicForward).
    """
    if len(args) == 2 and args[0] in ("-L", "-R", "-D"):
        return args[0][1], args[1]  # "L" / "R" / "D", rule
    click.echo(
        "Usage: sshm port <alias> a|r -L|-R <local>:<host>:<remote>\n"
        "       sshm port <alias> a|r -D <port>",
        err=True,
    )
    sys.exit(1)


@main.command("port-add", context_settings=dict(ignore_unknown_options=True))
@click.argument("alias")
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def port_add_cmd(alias: str, args: tuple[str, ...]):
    """Add a port forward (-L/-R) or SOCKS proxy (-D)."""
    direction, rule = _parse_port_args(args)
    data = _send(protocol.CMD_PORT_ADD, alias=alias, direction=direction, rule=rule)
    added = data.get("added", rule)
    if direction == "D":
        click.echo(f"Added SOCKS proxy: {added} (socks5://127.0.0.1:{rule})")
    else:
        click.echo(f"Added port forward: {added}")


@main.command("port-remove", context_settings=dict(ignore_unknown_options=True))
@click.argument("alias")
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def port_remove_cmd(alias: str, args: tuple[str, ...]):
    """Remove a port forward (-L/-R) or SOCKS proxy (-D)."""
    from .config import PortForward

    direction, rule = _parse_port_args(args)
    try:
        if direction == "D":
            rule_str = PortForward.socks(int(rule)).to_str()
        else:
            rule_str = PortForward.parse_rule(rule, direction).to_str()
    except ValueError:
        click.echo(f"Error: invalid rule format '{rule}'", err=True)
        sys.exit(1)
    _send(protocol.CMD_PORT_REMOVE, alias=alias, rule=rule_str)
    label = "SOCKS proxy" if direction == "D" else "port forward"
    click.echo(f"Removed {label}: {rule_str}")


# --- enable / disable ---

@main.command("enable")
@click.argument("alias")
def enable_cmd(alias: str):
    """Keep session alive automatically."""
    _send(protocol.CMD_ENABLE, alias=alias)
    click.echo(f"Enabled auto-connect for '{alias}'")


@main.command("disable")
@click.argument("alias")
def disable_cmd(alias: str):
    """Stop auto-connect."""
    _send(protocol.CMD_DISABLE, alias=alias)
    click.echo(f"Disabled auto-connect for '{alias}'")


# --- export / import ---

@main.command("export")
@click.argument("filepath")
@click.argument("names", nargs=-1)
def export_cmd(filepath: str, names: tuple[str, ...]):
    """Export hosts with SSH keys to JSON."""
    from .config import load_entries

    entries = [e for e in load_entries() if e.alias not in ("*", "")]
    if names:
        entries = [e for e in entries if e.alias in names]

    hosts = []
    for e in entries:
        h: dict = {
            "alias": e.alias,
            "hostname": e.hostname,
            "user": e.user,
            "port": e.port,
            "port_forwards": [pf.to_str() for pf in e.port_forwards],
        }
        if e.identity_file:
            h["identity_file"] = e.identity_file
            key_path = Path(e.identity_file).expanduser()
            if key_path.exists():
                h["private_key"] = key_path.read_text(encoding="utf-8")
            pub_path = key_path.with_suffix(".pub")
            if pub_path.exists():
                h["public_key"] = pub_path.read_text(encoding="utf-8")
        hosts.append(h)

    try:
        Path(filepath).write_text(
            json.dumps({"hosts": hosts}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as e:
        click.echo(f"Error: cannot write {filepath}: {e}", err=True)
        sys.exit(1)
    click.echo(f"Exported {len(hosts)} host(s) to {filepath}")


@main.command("import")
@click.argument("filepath")
@click.option("-o", "--override", is_flag=True, help="Override existing hosts")
@click.argument("names", nargs=-1)
def import_cmd(filepath: str, override: bool, names: tuple[str, ...]):
    """Import hosts from JSON. A name may be `<json-alias>=<new-alias>` to rename."""
    from .config import PortForward, add_host, add_port_forward, find_entry, remove_host, ssh_config_path

    rename = _parse_import_names(names)  # {json_alias: target_alias}; empty → import all
    hosts = _read_hosts_file(filepath)
    if rename:
        hosts = [h for h in hosts if h["alias"] in rename]

    imported = 0
    skipped = 0
    for h in hosts:
        src_alias = h["alias"]
        alias = rename.get(src_alias, src_alias)  # target alias (renamed or as-is)
        existing = find_entry(alias)
        label = f"{src_alias} -> {alias}" if alias != src_alias else alias

        if existing and not override:
            click.echo(f"  skip  {label} (already exists, use -o to override)")
            skipped += 1
            continue

        if existing:
            remove_host(alias)

        # If the export's key is the managed ~/.ssh/sshm_<src>, retarget it to the
        # new alias so the renamed host gets its own sshm_<new> key (like rename).
        identity = h.get("identity_file")
        if alias != src_alias and identity == f"~/.ssh/sshm_{src_alias}":
            identity = f"~/.ssh/sshm_{alias}"
        if identity and h.get("private_key"):
            _write_key_files(identity, h, override)

        add_host(
            alias=alias,
            hostname=h.get("hostname", ""),
            user=h.get("user", "root"),
            port=h.get("port", 22),
            identity_file=identity,
            path=ssh_config_path(),
        )

        for pf_str in h.get("port_forwards", []):
            try:
                add_port_forward(alias, PortForward.from_str(pf_str))
            except Exception:
                pass

        click.echo(f"  {'update' if existing else 'add':>6}  {label}")
        imported += 1

    click.echo(f"\nImported {imported}, skipped {skipped}")


def _parse_import_names(names: tuple[str, ...]) -> dict[str, str]:
    """Parse import selectors into {json_alias: target_alias}.

    Each name is either `<alias>` (import as-is) or `<alias>=<new-alias>` (import
    that host under a new alias). An empty tuple means "import everything".
    """
    mapping: dict[str, str] = {}
    for n in names:
        src, sep, dst = n.partition("=")
        if sep and not (src and dst):
            click.echo(f"Error: invalid selector '{n}', expected <name> or <name>=<new-name>", err=True)
            sys.exit(1)
        mapping[src] = dst if sep else src
    return mapping


def _write_key_files(identity: str, host: dict, override: bool) -> None:
    key_path = Path(identity).expanduser()
    key_path.parent.mkdir(mode=0o700, exist_ok=True)
    if not key_path.exists() or override:
        key_path.write_text(host["private_key"], encoding="utf-8")
        if sys.platform != "win32":
            os.chmod(key_path, 0o600)
    pub_path = key_path.with_suffix(".pub")
    if host.get("public_key") and (not pub_path.exists() or override):
        pub_path.write_text(host["public_key"], encoding="utf-8")


# --- daemon control ---

@main.command("stop")
def stop_cmd():
    """Stop daemon."""
    _send(protocol.CMD_SHUTDOWN)
    click.echo("Daemon stopping...")


@main.command("status")
def status_cmd():
    """Show daemon status."""
    data = _send(protocol.CMD_STATUS)
    click.echo(f"Daemon: {data.get('status', 'unknown')}, sessions: {data.get('sessions', 0)}")


@main.command("install")
def install_cmd():
    """Autostart daemon on login."""
    from .autostart import install_autostart
    click.echo(install_autostart())


@main.command("uninstall")
def uninstall_cmd():
    """Remove autostart."""
    from .autostart import uninstall_autostart
    click.echo(uninstall_autostart())


# --- shell completions ---

@main.command("completions")
@click.argument("shell", type=click.Choice(["fish"]), default="fish", required=False)
def completions_cmd(shell: str):
    """Print the shell completion script (currently: fish).

    Install with:
      sshm completions fish > ~/.config/fish/completions/sshm.fish && exec fish
    """
    from importlib import resources

    script = resources.files("sshm").joinpath(f"completions/sshm.{shell}").read_text(encoding="utf-8")
    click.echo(script, nl=False)


# --- helpers ---

def _format_uptime(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    elif s < 3600:
        return f"{s // 60}m{s % 60}s"
    elif s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60}m"
    else:
        return f"{s // 86400}d{(s % 86400) // 3600}h"


if __name__ == "__main__":
    main()
