import sys

import pytest

from sshm.autostart import _find_sshmd


def test_find_sshmd_returns_runnable_command():
    cmd = _find_sshmd()
    assert "sshmd" in cmd or "-m sshm.daemon" in cmd


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")
def test_win_daemon_cmd_is_windowless_and_quoted():
    from sshm.autostart import _win_daemon_cmd

    cmd = _win_daemon_cmd()
    assert cmd.startswith('"')  # interpreter path quoted (may contain spaces)
    assert cmd.endswith('-m sshm.daemon')
    assert "pythonw.exe" in cmd.lower()  # venv interpreters ship pythonw.exe
