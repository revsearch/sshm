"""Runtime state files shared by the CLI and the daemon (~/.sshm)."""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path


def sshm_dir() -> Path:
    d = Path.home() / ".sshm"
    d.mkdir(parents=True, exist_ok=True)
    return d


def pid_file() -> Path:
    return sshm_dir() / "sshmd.pid"


def token_file() -> Path:
    return sshm_dir() / "token"


def log_file() -> Path:
    return sshm_dir() / "sshmd.log"


def port_file() -> Path:
    return sshm_dir() / "port"


DEFAULT_PORT = 19222


def resolve_port() -> int:
    """Effective IPC port: SSHM_PORT env > persisted ~/.sshm/port > default.

    The env var wins for ad-hoc overrides; the persisted file lets an
    autostarted daemon (systemd/launchd/Task Scheduler), which never sees the
    interactive shell's environment, agree with the CLI on a non-default port.
    """
    env = os.environ.get("SSHM_PORT")
    if env:
        try:
            return int(env)
        except ValueError:
            pass

    pf = port_file()
    try:
        return int(pf.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return DEFAULT_PORT


def write_port(port: int) -> None:
    port_file().write_text(str(port), encoding="utf-8")


def read_token() -> str | None:
    tf = token_file()
    if tf.exists():
        return tf.read_text(encoding="utf-8").strip()
    return None


def new_token() -> str:
    return secrets.token_hex(32)


def write_token(token: str) -> None:
    tf = token_file()
    tf.write_text(token, encoding="utf-8")
    if sys.platform != "win32":
        os.chmod(tf, 0o600)
