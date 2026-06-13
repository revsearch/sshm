"""SSH config parser/writer with sshm metadata in structured comments."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PortForward:
    direction: str  # "L"/"R" (tunnel) or "D" (SOCKS dynamic proxy)
    local_port: int
    remote_host: str = ""
    remote_port: int = 0

    def to_str(self) -> str:
        if self.direction == "D":
            return f"D:{self.local_port}"
        return f"{self.direction}:{self.local_port}:{self.remote_host}:{self.remote_port}"

    def to_config_line(self) -> str:
        if self.direction == "D":
            return f"    DynamicForward {self.local_port}"
        keyword = "LocalForward" if self.direction == "L" else "RemoteForward"
        return f"    {keyword} {self.local_port} {self.remote_host}:{self.remote_port}"

    @classmethod
    def socks(cls, port: int) -> PortForward:
        """SOCKS proxy through the host (ssh -D / DynamicForward)."""
        return cls("D", port)

    @classmethod
    def parse_rule(cls, rule: str, direction: str) -> PortForward:
        """Parse a CLI rule like '8080:80' or '8080:host:80'."""
        parts = rule.split(":")
        if len(parts) == 2:
            return cls(direction, int(parts[0]), "localhost", int(parts[1]))
        if len(parts) == 3:
            return cls(direction, int(parts[0]), parts[1], int(parts[2]))
        raise ValueError(f"Invalid port rule: {rule}")

    @classmethod
    def from_str(cls, s: str) -> PortForward:
        """Parse the serialized form 'L:8080:host:80' or 'D:1080'."""
        direction, _, rest = s.partition(":")
        if direction == "D":
            if not rest or ":" in rest:
                raise ValueError(f"Invalid port forward: {s}")
            return cls.socks(int(rest))
        if direction not in ("L", "R") or not rest:
            raise ValueError(f"Invalid port forward: {s}")
        return cls.parse_rule(rest, direction)

    @classmethod
    def from_config(cls, direction: str, value: str) -> PortForward:
        """Parse from an SSH config value like '8080 localhost:80' or '1080'."""
        if direction == "D":
            return cls.socks(int(value.strip()))
        parts = value.strip().split()
        if len(parts) == 2 and ":" in parts[1]:
            remote_host, remote_port = parts[1].rsplit(":", 1)
            return cls(direction, int(parts[0]), remote_host, int(remote_port))
        raise ValueError(f"Invalid forward: {value}")


@dataclass
class HostEntry:
    alias: str
    hostname: str = ""
    user: str = ""
    port: int = 22
    identity_file: str | None = None
    enabled: bool = False
    port_forwards: list[PortForward] = field(default_factory=list)
    extra_options: list[tuple[str, str]] = field(default_factory=list)
    _raw_lines: list[str] = field(default_factory=list, repr=False)


_META_RE = re.compile(r"^# sshm:(\w+)=(.*)$")
_HOST_RE = re.compile(r"^Host\s+(\S+)\s*$", re.IGNORECASE)
_OPTION_RE = re.compile(r"^\s+(\S+)\s+(.+)$")


def ssh_config_path() -> Path:
    return Path.home() / ".ssh" / "config"


def _apply_option(entry: HostEntry, key: str, val: str) -> None:
    key_lower = key.lower()
    if key_lower == "hostname":
        entry.hostname = val
    elif key_lower == "user":
        entry.user = val
    elif key_lower == "port":
        try:
            entry.port = int(val)
        except ValueError:
            entry.extra_options.append((key, val))  # malformed Port → keep verbatim, don't crash the parse
    elif key_lower == "identityfile":
        # Keep the first IdentityFile as the primary (what sshm reads/writes);
        # preserve any extras as raw options so regeneration doesn't drop them.
        if entry.identity_file is None:
            entry.identity_file = val
        else:
            entry.extra_options.append((key, val))
    elif key_lower in ("localforward", "remoteforward", "dynamicforward"):
        direction = {"localforward": "L", "remoteforward": "R", "dynamicforward": "D"}[key_lower]
        try:
            entry.port_forwards.append(PortForward.from_config(direction, val))
        except ValueError:
            entry.extra_options.append((key, val))
    else:
        entry.extra_options.append((key, val))


def parse_ssh_config(path: Path | None = None) -> tuple[list[str], list[HostEntry]]:
    path = path or ssh_config_path()
    if not path.exists():
        return [], []

    # surrogateescape: a config with non-UTF-8 bytes (a comment in another
    # encoding, etc.) must not crash the parse or the watchdog that calls it; the
    # bytes round-trip losslessly because write_ssh_config uses it too.
    lines = path.read_text(encoding="utf-8", errors="surrogateescape").splitlines(keepends=True)

    preamble: list[str] = []
    entries: list[HostEntry] = []
    pending_meta: dict[str, str] = {}
    pending_meta_lines: list[str] = []
    current: HostEntry | None = None
    in_preamble = True

    for line in lines:
        stripped = line.rstrip("\n\r")

        meta_match = _META_RE.match(stripped)
        if meta_match:
            pending_meta[meta_match.group(1)] = meta_match.group(2)
            pending_meta_lines.append(line)
            continue

        host_match = _HOST_RE.match(stripped)
        if host_match:
            if current:
                entries.append(current)
            in_preamble = False

            current = HostEntry(alias=host_match.group(1), _raw_lines=[line])
            if "enabled" in pending_meta:
                current.enabled = pending_meta["enabled"].lower() == "true"

            pending_meta.clear()
            pending_meta_lines.clear()
            continue

        if current:
            current._raw_lines.append(line)
            opt_match = _OPTION_RE.match(stripped)
            if opt_match:
                _apply_option(current, opt_match.group(1), opt_match.group(2))
        elif in_preamble:
            if pending_meta_lines:
                preamble.extend(pending_meta_lines)
                pending_meta_lines.clear()
                pending_meta.clear()
            preamble.append(line)

    if current:
        entries.append(current)

    return preamble, entries


def write_ssh_config(
    preamble: list[str],
    entries: list[HostEntry],
    path: Path | None = None,
) -> None:
    path = path or ssh_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    # If ~/.ssh/config is a symlink (dotfile managers point it at a tracked file),
    # write through to the real target so os.replace swaps the file rather than
    # replacing the link with a regular file and orphaning the target.
    target = path.resolve() if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists():
        shutil.copy2(target, target.with_name(target.name + ".bak"))

    # Write to a temp file in the same directory, then atomically replace, so a
    # crash mid-write can never leave a truncated ~/.ssh/config behind.
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=target.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="", errors="surrogateescape") as f:
            for line in preamble:
                f.write(line if line.endswith("\n") else line + "\n")

            for entry in entries:
                # sshm metadata comment (only the enabled flag; forwards are native directives)
                if entry.enabled:
                    f.write("# sshm:enabled=true\n")

                if entry._raw_lines:
                    for line in entry._raw_lines:
                        f.write(line if line.endswith("\n") else line + "\n")
                else:
                    f.write(f"Host {entry.alias}\n")
                    if entry.hostname:
                        f.write(f"    HostName {entry.hostname}\n")
                    if entry.user:
                        f.write(f"    User {entry.user}\n")
                    if entry.port != 22:
                        f.write(f"    Port {entry.port}\n")
                    if entry.identity_file:
                        f.write(f"    IdentityFile {entry.identity_file}\n")
                    for key, val in entry.extra_options:
                        f.write(f"    {key} {val}\n")
                    for pf in entry.port_forwards:
                        f.write(pf.to_config_line() + "\n")
                    f.write("\n")
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        os.unlink(tmp_name)
        raise

    if target.exists():
        shutil.copymode(target, tmp_name)
    os.replace(tmp_name, target)


def load_entries(path: Path | None = None) -> list[HostEntry]:
    _, entries = parse_ssh_config(path)
    return entries


def find_entry(alias: str, path: Path | None = None) -> HostEntry | None:
    for entry in load_entries(path):
        if entry.alias == alias:
            return entry
    return None


def _update_entry(alias: str, mutate, path: Path | None = None) -> None:
    """Apply `mutate(entry)` to the matching host and rewrite the config."""
    preamble, entries = parse_ssh_config(path)
    for e in entries:
        if e.alias == alias:
            mutate(e)
            break
    else:
        raise ValueError(f"Host '{alias}' not found")
    write_ssh_config(preamble, entries, path)


def add_host(
    alias: str,
    hostname: str,
    user: str,
    port: int = 22,
    identity_file: str | None = None,
    path: Path | None = None,
    extra_options: list[tuple[str, str]] | None = None,
) -> None:
    preamble, entries = parse_ssh_config(path)

    if any(e.alias == alias for e in entries):
        raise ValueError(f"Host '{alias}' already exists in SSH config")

    entries.append(
        HostEntry(
            alias=alias,
            hostname=hostname,
            user=user,
            port=port,
            identity_file=identity_file,
            extra_options=extra_options or [],
        )
    )
    write_ssh_config(preamble, entries, path)


def remove_host(alias: str, path: Path | None = None) -> None:
    preamble, entries = parse_ssh_config(path)
    entries = [e for e in entries if e.alias != alias]
    write_ssh_config(preamble, entries, path)


def rename_host(old_alias: str, new_alias: str, path: Path | None = None) -> None:
    """Rename a host's alias, keeping its options, forwards and enabled flag.

    If the IdentityFile is the sshm-managed key for the old alias
    (`~/.ssh/sshm_<old>`), its reference is updated to `~/.ssh/sshm_<new>` to
    match — the caller is responsible for renaming the key file on disk.
    """
    if old_alias == new_alias:
        raise ValueError("New alias is the same as the old one")

    preamble, entries = parse_ssh_config(path)

    if any(e.alias == new_alias for e in entries):
        raise ValueError(f"Host '{new_alias}' already exists in SSH config")

    for e in entries:
        if e.alias == old_alias:
            break
    else:
        raise ValueError(f"Host '{old_alias}' not found")

    e.alias = new_alias
    if e.identity_file == f"~/.ssh/sshm_{old_alias}":
        e.identity_file = f"~/.ssh/sshm_{new_alias}"
    e._raw_lines = []  # force regeneration with the new Host line / identity
    write_ssh_config(preamble, entries, path)


def set_enabled(alias: str, enabled: bool, path: Path | None = None) -> None:
    def mutate(e: HostEntry) -> None:
        e.enabled = enabled

    _update_entry(alias, mutate, path)


def add_port_forward(alias: str, forward: PortForward, path: Path | None = None) -> None:
    def mutate(e: HostEntry) -> None:
        if forward.to_str() in (pf.to_str() for pf in e.port_forwards):
            raise ValueError(f"Port forward {forward.to_str()} already exists")
        e.port_forwards.append(forward)
        e._raw_lines = []  # force regeneration with the new forward

    _update_entry(alias, mutate, path)


def remove_port_forward(alias: str, rule_str: str, path: Path | None = None) -> None:
    def mutate(e: HostEntry) -> None:
        e.port_forwards = [pf for pf in e.port_forwards if pf.to_str() != rule_str]
        e._raw_lines = []  # force regeneration without the forward

    _update_entry(alias, mutate, path)
