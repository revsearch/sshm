import os
import sys
from pathlib import Path

from sshm.procutil import (
    daemon_interpreter,
    detached_popen_flags,
    no_window_popen_flags,
    pid_alive,
)


def test_pid_alive_self():
    assert pid_alive(os.getpid())


def test_pid_alive_bogus():
    assert not pid_alive(2**22 + 12345)


def test_popen_flag_helpers_return_dicts():
    assert isinstance(detached_popen_flags(), dict)
    assert isinstance(no_window_popen_flags(), dict)


def test_daemon_interpreter_exists():
    interpreter = Path(daemon_interpreter())
    assert interpreter.exists()
    if sys.platform == "win32":
        # GUI-subsystem interpreter so the daemon can never pop a console window
        assert interpreter.name == "pythonw.exe"
