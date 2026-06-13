"""ProcessManager orchestration tests. POSIX-only: we substitute `cat` under a
real PTY for ssh, so sessions are live processes without needing a remote host."""

import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="PTY substitution is POSIX-only")


@pytest.fixture
def pm(monkeypatch):
    monkeypatch.setattr("sshm.process._find_ssh", lambda: "ssh")
    from sshm.process import ProcessManager, _spawn_with_pty

    manager = ProcessManager()
    # Every "ssh" spawn becomes `cat` on a PTY: it echoes input and stays alive.
    monkeypatch.setattr(manager, "_spawn", lambda alias: _spawn_with_pty(["cat"]))
    try:
        yield manager
    finally:
        manager.disconnect_all()


def _wait(cond, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_connect_creates_live_echoing_session(pm):
    s = pm.connect("web")
    assert s.alive and pm.get_sessions("web") == [s]
    assert s._write_input(b"hello\n") is True
    assert _wait(lambda: b"hello" in bytes(s.scrollback))


def test_next_name_increments(pm):
    a = pm.connect("web")
    b = pm.connect("web")
    assert {a.name, b.name} == {"web-1", "web-2"}


def test_attach_reserves_and_never_double_hands(pm):
    s = pm.connect("web")
    first = pm.attach("web")
    assert first is s and first.attached
    # No unattached session left → a second attach must spawn a *different* one.
    second = pm.attach("web")
    assert second is not None and second is not first


def test_attach_by_name_then_none_when_taken(pm):
    pm.connect("web", "web-1")
    assert pm.attach("web", "web-1").attached
    assert pm.attach("web", "web-1") is None  # already reserved


def test_count_and_ensure_unattached(pm):
    s = pm.connect("web")
    assert pm.count_unattached("web") == 1
    pm.attach("web")
    assert pm.count_unattached("web") == 0
    pm.ensure_unattached("web")  # should spawn a fresh ready session
    assert pm.count_unattached("web") == 1
    assert s.attached


def test_disconnect_removes_and_kills(pm):
    s = pm.connect("web")
    assert pm.disconnect("web", s.name) is True
    assert pm.get_sessions("web") == []
    assert not s.alive
    assert pm.disconnect("web", "ghost") is False


def test_disconnect_alias_kills_all(pm):
    pm.connect("web")
    pm.connect("web")
    assert pm.disconnect_alias("web") == 2
    assert pm.get_sessions("web") == []


def test_check_orphaned_attaches_detaches_dead_client(pm):
    s = pm.connect("web")
    pm.attach("web", cli_pid=2**22 + 999)  # a pid that isn't alive
    assert s.attached
    pm.check_orphaned_attaches()
    assert not s.attached and s.attached_pid is None


def test_rebuild_session_restarts(pm):
    s = pm.connect("web")
    old_pid = s.pid
    new = pm.rebuild_session("web", s.name)
    assert new is not None and new.alive and new.pid != old_pid
    assert not s.alive  # the original was killed


def test_check_health_removes_clean_exit_without_reconnect(pm):
    s = pm.connect("web")
    s.reconnect = False
    s.process.terminate()
    s.process.wait(timeout=5)
    pm.check_health()
    assert pm.get_sessions("web") == []


def test_check_health_reconnects_on_failure(pm):
    s = pm.connect("web")
    old_pid = s.pid
    s.process.terminate()
    s.process.wait(timeout=5)
    pm.check_health()  # reconnect=True (default) → respawned
    assert s.alive and s.pid != old_pid
