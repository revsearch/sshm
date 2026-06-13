"""Drive terminal._bridge_unix with a real PTY standing in for the local terminal
and a socketpair for the daemon side, exercising the raw I/O pump. POSIX only."""

import os
import select
import socket
import sys
import threading
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX pty")


class _Stdin:
    def __init__(self, fd, tty=True):
        self._fd, self._tty = fd, tty

    def fileno(self):
        return self._fd

    def isatty(self):
        return self._tty


class _StdoutBuf:
    def __init__(self, fd):
        self._fd = fd

    def write(self, b):
        return os.write(self._fd, b)

    def flush(self):
        pass


class _Stdout:
    def __init__(self, fd):
        self.buffer = _StdoutBuf(fd)


def _saw_on_fd(fd, needle, timeout=3):
    got = b""
    deadline = time.time() + timeout
    while needle not in got and time.time() < deadline:
        r, _, _ = select.select([fd], [], [], 0.2)
        if fd in r:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            got += chunk
    return needle in got


def _saw_on_sock(sock, needle, timeout=3):
    sock.settimeout(timeout)
    got = b""
    deadline = time.time() + timeout
    while needle not in got and time.time() < deadline:
        try:
            chunk = sock.recv(4096)
        except OSError:
            break
        if not chunk:
            break
        got += chunk
    return needle in got


def test_bridge_unix_tty_pumps_both_ways_and_replays_initial(monkeypatch):
    import pty

    from sshm import terminal

    master, slave = pty.openpty()  # the "local terminal"; bridge uses the slave
    cli, srv = socket.socketpair()  # cli = bridge's daemon socket; srv = the daemon
    monkeypatch.setattr(sys, "stdin", _Stdin(slave, tty=True))
    monkeypatch.setattr(sys, "stdout", _Stdout(slave))

    t = threading.Thread(target=terminal._bridge_unix, args=(cli, b"INIT"), daemon=True)
    t.start()
    try:
        # initial replay → shows up on the terminal master
        assert _saw_on_fd(master, b"INIT"), "initial bytes not replayed to the terminal"
        # daemon → terminal
        srv.sendall(b"from-daemon")
        assert _saw_on_fd(master, b"from-daemon"), "socket output not written to the terminal"
        # terminal → daemon (raw mode means no echo back onto the master)
        os.write(master, b"typed-input")
        assert _saw_on_sock(srv, b"typed-input"), "typed input not forwarded to the socket"
    finally:
        srv.close()  # EOF → bridge returns and runs its finally (tcsetattr on the still-open slave)
        t.join(timeout=2)  # let the bridge finish BEFORE we close its fds
        os.close(master)
        os.close(slave)  # cli is closed by the bridge's own finally


def test_bridge_unix_non_tty_still_pumps(monkeypatch):
    from sshm import terminal

    stdin_r, stdin_w = os.pipe()  # redirected stdin is a pipe, not a tty
    stdout_r, stdout_w = os.pipe()
    cli, srv = socket.socketpair()
    monkeypatch.setattr(sys, "stdin", _Stdin(stdin_r, tty=False))
    monkeypatch.setattr(sys, "stdout", _Stdout(stdout_w))

    t = threading.Thread(target=terminal._bridge_unix, args=(cli, b"HI"), daemon=True)
    t.start()
    try:
        assert _saw_on_fd(stdout_r, b"HI")
        srv.sendall(b"xyz")
        assert _saw_on_fd(stdout_r, b"xyz")
        os.write(stdin_w, b"abc")
        assert _saw_on_sock(srv, b"abc")
    finally:
        srv.close()  # EOF → bridge returns
        t.join(timeout=2)  # let the bridge finish before closing its fds
        for fd in (stdin_r, stdin_w, stdout_r, stdout_w):
            try:
                os.close(fd)
            except OSError:
                pass
