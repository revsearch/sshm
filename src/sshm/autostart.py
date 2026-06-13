"""Cross-platform autostart management for sshmd daemon."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from .procutil import daemon_interpreter
from .state import log_file, port_file, resolve_port, write_port


def _find_sshmd() -> str:
    sshmd = shutil.which("sshmd")
    if sshmd:
        return sshmd
    return f"{sys.executable} -m sshm.daemon"


# --- Windows ---

def _win_daemon_cmd() -> str:
    """Task command line: pythonw (no console window) from the current environment."""
    return f'"{daemon_interpreter()}" -m sshm.daemon'


def _win_install() -> None:
    cmd = [
        "schtasks", "/create",
        "/tn", "sshmd",
        "/tr", _win_daemon_cmd(),
        "/sc", "onlogon",
        "/rl", "limited",
        "/f",  # force overwrite
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Failed to create scheduled task: {result.stderr}")


def _win_uninstall() -> None:
    cmd = ["schtasks", "/delete", "/tn", "sshmd", "/f"]
    subprocess.run(cmd, capture_output=True, text=True)


# --- Linux (systemd) ---

_SYSTEMD_UNIT = """\
[Unit]
Description=SSH Session Manager Daemon
After=network.target

[Service]
Type=simple
ExecStart={cmd}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def _linux_install() -> None:
    sshmd = _find_sshmd()
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_file = unit_dir / "sshmd.service"
    unit_file.write_text(_SYSTEMD_UNIT.format(cmd=sshmd), encoding="utf-8")

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "sshmd.service"], check=True)
    subprocess.run(["systemctl", "--user", "start", "sshmd.service"], check=True)


def _linux_uninstall() -> None:
    subprocess.run(["systemctl", "--user", "stop", "sshmd.service"], capture_output=True)
    subprocess.run(["systemctl", "--user", "disable", "sshmd.service"], capture_output=True)
    unit_file = Path.home() / ".config" / "systemd" / "user" / "sshmd.service"
    unit_file.unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)


# --- macOS (launchd) ---

_LAUNCHD_PLIST = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.sshm.daemon</string>
    <key>ProgramArguments</key>
    <array>
{args}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
</dict>
</plist>
"""


def _macos_install() -> None:
    sshmd = _find_sshmd()
    parts = sshmd.split()
    args_xml = "\n".join(f"        <string>{p}</string>" for p in parts)
    log_path = log_file()

    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_file = plist_dir / "com.sshm.daemon.plist"
    plist_file.write_text(
        _LAUNCHD_PLIST.format(args=args_xml, log_path=log_path),
        encoding="utf-8",
    )

    subprocess.run(["launchctl", "load", str(plist_file)], check=True)


def _macos_uninstall() -> None:
    plist_file = Path.home() / "Library" / "LaunchAgents" / "com.sshm.daemon.plist"
    if plist_file.exists():
        subprocess.run(["launchctl", "unload", str(plist_file)], capture_output=True)
        plist_file.unlink(missing_ok=True)


# --- Public API ---

def install_autostart() -> str:
    # Persist the effective port so the autostarted daemon — which doesn't
    # inherit the interactive shell's SSHM_PORT — binds the same port the CLI
    # will dial.
    write_port(resolve_port())

    if sys.platform == "win32":
        _win_install()
        return "Installed as Windows scheduled task (runs on logon)"
    elif sys.platform == "darwin":
        _macos_install()
        return "Installed as macOS LaunchAgent"
    else:
        _linux_install()
        return "Installed as systemd user service"


def uninstall_autostart() -> str:
    port_file().unlink(missing_ok=True)

    if sys.platform == "win32":
        _win_uninstall()
        return "Removed Windows scheduled task"
    elif sys.platform == "darwin":
        _macos_uninstall()
        return "Removed macOS LaunchAgent"
    else:
        _linux_uninstall()
        return "Removed systemd user service"
