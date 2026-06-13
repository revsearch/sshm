"""Integration tests for the IPC server + client over a real loopback socket."""

import socket

import pytest

from sshm import ipc, protocol
from sshm.ipc import (
    DaemonNotRunning,
    IpcServer,
    StreamingResponse,
    connect_streaming,
    is_daemon_running,
    send_request,
)
from sshm.state import write_token

TOKEN = "test-token-abc123"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    # ~/.sshm (token file) isolated; pick a free IPC port for this test.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(ipc, "IPC_PORT", _free_port())


@pytest.fixture
def server(isolated):
    write_token(TOKEN)
    state = {"handler": lambda req: protocol.ok({"echo": req.get("cmd")})}
    srv = IpcServer(handler=lambda req: state["handler"](req), token=TOKEN)
    srv.start()
    try:
        yield state
    finally:
        srv.stop()


def test_request_response_round_trip(server):
    resp = send_request("ping", x=1)
    assert resp == {"ok": True, "data": {"echo": "ping"}}


def test_invalid_token_rejected(server):
    sock = socket.create_connection(("127.0.0.1", ipc.IPC_PORT), timeout=5)
    sock.sendall(protocol.encode({"cmd": "ping", "token": "WRONG"}))
    line, _ = ipc._recv_line(sock)
    sock.close()
    assert protocol.decode(line) == protocol.err("Invalid token")


def test_handler_exception_becomes_error_response(server):
    def boom(_req):
        raise RuntimeError("kaboom")

    server["handler"] = boom
    assert send_request("ping") == protocol.err("kaboom")


def test_streaming_handshake_delivers_stream_bytes(server):
    class BridgeSession:
        def bridge(self, conn, cli_pid=None):
            conn.sendall(b"SCROLLBACK")  # output streamed right after the response

    server["handler"] = lambda req: StreamingResponse(
        protocol.ok({"name": "web-1"}), BridgeSession(), cli_pid=req.get("cli_pid")
    )

    sock, resp, leftover = connect_streaming("attach", cli_pid=1)
    assert resp == {"ok": True, "data": {"name": "web-1"}}

    # The streamed bytes may have coalesced into `leftover` or still be on the
    # socket — either way the client must end up with all of them.
    sock.settimeout(2)
    rest = bytearray(leftover)
    try:
        while len(rest) < len(b"SCROLLBACK"):
            chunk = sock.recv(64)
            if not chunk:
                break
            rest += chunk
    except OSError:
        pass
    sock.close()
    assert bytes(rest) == b"SCROLLBACK"


def test_send_request_without_token_raises(isolated):
    with pytest.raises(DaemonNotRunning):
        send_request("ping")  # no token file written


def test_send_request_connection_refused(isolated):
    write_token("tok")  # token present, but nothing is listening on the port
    with pytest.raises(DaemonNotRunning):
        send_request("ping")


def test_is_daemon_running_without_pidfile(isolated):
    assert is_daemon_running() is False
