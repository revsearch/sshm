"""Cross-platform process helpers."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def pid_alive(pid: int) -> bool:
    """Check whether a process with the given PID exists."""
    try:
        if sys.platform == "win32":
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            kernel32.CloseHandle(handle)
            return True
        os.kill(pid, 0)
        return True
    except (ValueError, OSError, ProcessLookupError):
        return False


def daemon_interpreter() -> str:
    """Interpreter for spawning the background daemon.

    On Windows prefers pythonw.exe (GUI subsystem — can never open a console
    window), falling back to the current interpreter.
    """
    if sys.platform == "win32":
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if pythonw.exists():
            return str(pythonw)
    return sys.executable


def detached_popen_flags() -> dict:
    """Popen kwargs that detach the child from the current console/session."""
    if sys.platform == "win32":
        # CREATE_NO_WINDOW (hidden console) instead of DETACHED_PROCESS (no
        # console): launcher/trampoline exes (uv venv python, script shims)
        # re-spawn the real interpreter as a child, which would allocate a
        # fresh *visible* console when the parent has none. A hidden console
        # is inherited by such children.
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        return {"creationflags": CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def no_window_popen_flags() -> dict:
    """Popen kwargs that suppress a console window on Windows."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}
