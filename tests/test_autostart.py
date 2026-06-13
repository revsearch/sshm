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


# --- per-platform install/uninstall (subprocess + platform mocked) ---

class _FakeRun:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    def saw(self, needle):
        return any(needle in str(c) for c in self.calls)


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


@pytest.fixture
def fakerun(monkeypatch):
    import subprocess

    r = _FakeRun()
    monkeypatch.setattr(subprocess, "run", r)
    return r


def test_linux_install_writes_unit_and_enables(monkeypatch, home, fakerun):
    monkeypatch.setattr(sys, "platform", "linux")
    from sshm.autostart import install_autostart

    msg = install_autostart()
    assert "systemd" in msg
    unit = home / ".config" / "systemd" / "user" / "sshmd.service"
    assert unit.exists() and "ExecStart=" in unit.read_text(encoding="utf-8")
    assert fakerun.saw("daemon-reload") and fakerun.saw("enable") and fakerun.saw("start")


def test_linux_uninstall_removes_unit(monkeypatch, home, fakerun):
    monkeypatch.setattr(sys, "platform", "linux")
    from sshm.autostart import install_autostart, uninstall_autostart

    install_autostart()
    msg = uninstall_autostart()
    assert "systemd" in msg
    assert not (home / ".config" / "systemd" / "user" / "sshmd.service").exists()


def test_macos_install_writes_plist_and_loads(monkeypatch, home, fakerun):
    monkeypatch.setattr(sys, "platform", "darwin")
    from sshm.autostart import install_autostart

    msg = install_autostart()
    assert "LaunchAgent" in msg
    plist = home / "Library" / "LaunchAgents" / "com.sshm.daemon.plist"
    assert plist.exists() and "com.sshm.daemon" in plist.read_text(encoding="utf-8")
    assert fakerun.saw("launchctl")


def test_windows_install_creates_scheduled_task(monkeypatch, home, fakerun):
    monkeypatch.setattr(sys, "platform", "win32")
    from sshm.autostart import install_autostart

    msg = install_autostart()
    assert "scheduled task" in msg.lower()
    assert fakerun.saw("schtasks")
