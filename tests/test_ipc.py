import socket

from sshm.ipc import _recv_line


def test_recv_line_splits_leftover():
    # The daemon's JSON response and the first bytes of streamed output can land
    # in a single recv(); _recv_line must hand the tail back, not drop it.
    a, b = socket.socketpair()
    try:
        b.sendall(b'{"ok": true}\n\x1b]0;prompt\x07rest')
        result = _recv_line(a)
        assert result is not None
        line, leftover = result
        assert line == b'{"ok": true}'
        assert leftover == b'\x1b]0;prompt\x07rest'
    finally:
        a.close()
        b.close()


def test_recv_line_no_leftover():
    a, b = socket.socketpair()
    try:
        b.sendall(b"hello\n")
        line, leftover = _recv_line(a)
        assert line == b"hello"
        assert leftover == b""
    finally:
        a.close()
        b.close()


def test_recv_line_spans_multiple_recvs():
    a, b = socket.socketpair()
    try:
        b.sendall(b"abc")
        b.sendall(b"def\ntail")
        line, leftover = _recv_line(a)
        assert line == b"abcdef"
        assert leftover == b"tail"
    finally:
        a.close()
        b.close()


def test_recv_line_closed_before_newline_returns_none():
    a, b = socket.socketpair()
    b.sendall(b"no newline")
    b.close()
    assert _recv_line(a) is None
    a.close()
