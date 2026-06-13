import os
import sys

import pytest

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX PTY only")


def _read_winsize(fd: int) -> tuple[int, int]:
    import fcntl
    import struct
    import termios

    rows, cols, _, _ = struct.unpack(
        "HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
    )
    return cols, rows


@posix_only
def test_set_winsize_roundtrip():
    import pty

    from sshm.process import _set_winsize

    master, slave = pty.openpty()
    try:
        _set_winsize(master, cols=120, rows=40)
        # The slave (what the child sees) reflects the size set on the master.
        assert _read_winsize(slave) == (120, 40)
    finally:
        os.close(master)
        os.close(slave)


@posix_only
def test_session_set_winsize_applies_to_master():
    import pty

    from sshm.process import SshSession

    master, slave = pty.openpty()
    s = SshSession(alias="x", name="x-1", master_fd=master)
    try:
        s.set_winsize(100, 30)
        assert s.last_winsize == (100, 30)
        assert _read_winsize(slave) == (100, 30)
    finally:
        os.close(master)
        os.close(slave)


def test_session_set_winsize_remembers_without_master():
    from sshm.process import SshSession

    s = SshSession(alias="x", name="x-1")  # no PTY (e.g. Windows / not yet spawned)
    s.set_winsize(90, 25)
    # Remembered so it can be re-applied when a master is (re)connected.
    assert s.last_winsize == (90, 25)


def test_set_winsize_clamps_to_uint16():
    from sshm.process import SshSession

    s = SshSession(alias="x", name="x-1")
    s.set_winsize(99999, -5)  # out of struct's unsigned-16-bit range
    assert s.last_winsize == (0xFFFF, 0)  # clamped, no struct.error


@posix_only
def test_adopt_reconnect_resets_scrollback_keeps_size_and_closes_old_fd():
    import time

    from sshm.process import SshSession, _spawn_with_pty

    s = SshSession(alias="t", name="t-1")
    proc, master = _spawn_with_pty(["cat"])  # cat echoes its input back
    s.adopt_process(proc, master)
    try:
        assert s._write_input(b"hello\n") is True
        time.sleep(0.2)
        assert b"hello" in bytes(s.scrollback)
        s.set_winsize(120, 40)

        # Simulate a reconnect: kill the old process, adopt a fresh one.
        proc.terminate()
        proc.wait(timeout=5)
        proc2, master2 = _spawn_with_pty(["cat"])
        s.adopt_process(proc2, master2)

        assert s.last_winsize == (120, 40)  # size carried across the reconnect
        with pytest.raises(OSError):
            os.fstat(master)  # the old master was closed, not leaked

        assert s._write_input(b"world\n") is True
        time.sleep(0.2)
        sb = bytes(s.scrollback)
        assert b"world" in sb and b"hello" not in sb  # fresh buffer, no stale output
    finally:
        for p in (proc, proc2):
            try:
                p.kill()
            except Exception:
                pass
        for fd in (s.master_fd,):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


@posix_only
def test_write_input_returns_false_without_io():
    from sshm.process import SshSession

    s = SshSession(alias="x", name="x-1")  # no master_fd, no process
    assert s._write_input(b"data") is False


@posix_only
def test_detach_not_blocked_by_stalled_client_send():
    # Regression: the reader must not hold the session lock while sending to the
    # client. A stalled client (here a socket whose sendall blocks until close)
    # would otherwise wedge detach/kill/resize/write on the lock.
    import threading

    from sshm.process import SshSession, _spawn_with_pty

    reached_send = threading.Event()
    release = threading.Event()

    class StalledSock:
        def sendall(self, data):
            reached_send.set()
            release.wait(5)  # behaves like a full TCP window: blocks until closed
            raise OSError("closed")

        def close(self):
            release.set()

    s = SshSession(alias="t", name="t-1")
    proc, master = _spawn_with_pty(["cat"])
    s.adopt_process(proc, master)
    with s._lock:
        s._active_socket = StalledSock()
    try:
        s._write_input(b"ping\n")  # cat echoes → reader calls sendall → blocks
        assert reached_send.wait(3), "reader never reached sendall"

        # Reader is blocked in sendall; detach() must still complete promptly.
        done = threading.Event()
        threading.Thread(target=lambda: (s.detach(), done.set()), daemon=True).start()
        assert done.wait(3), "detach() blocked — sendall is holding the session lock"
        assert not s.attached
    finally:
        release.set()
        with s._lock:
            fd, s.master_fd = s.master_fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        proc.kill()
