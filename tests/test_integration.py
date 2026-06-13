"""End-to-end integration: a real sshmd daemon + a fake `ssh` (an echoing shell)
spawned under a real PTY, driven over the real IPC client. POSIX only."""

import os
import socket
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX PTY integration")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def live_daemon(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    # Fake ssh: ignores its args and becomes `cat`, so the "remote shell" echoes
    # whatever the client types — a real process under the daemon's real PTY.
    fake = tmp_path / "fake_ssh"
    fake.write_text("#!/bin/sh\nexec cat\n")
    fake.chmod(0o755)
    monkeypatch.setattr("sshm.process._find_ssh", lambda: str(fake))

    from sshm import ipc

    monkeypatch.setattr(ipc, "IPC_PORT", _free_port())

    from sshm.config import add_host, ssh_config_path

    add_host("test", "127.0.0.1", "root", 22, None, ssh_config_path())

    from sshm.daemon import Daemon
    from sshm.state import write_token

    d = Daemon()
    write_token(d.token)
    d.server.start()
    try:
        yield d
    finally:
        d.pm.disconnect_all()
        d.server.stop()


def test_attach_echo_roundtrip(live_daemon):
    from sshm import protocol
    from sshm.ipc import connect_streaming

    sock, resp, _ = connect_streaming(
        protocol.CMD_ATTACH, alias="test", cli_pid=os.getpid(), cols=80, rows=24
    )
    try:
        assert resp["ok"] and resp["data"]["alias"] == "test"

        sock.sendall(b"echo-me\n")  # → daemon bridge → fake-ssh (cat) echoes → reader → us
        sock.settimeout(3)
        got = b""
        deadline = time.time() + 3
        while b"echo-me" not in got and time.time() < deadline:
            try:
                got += sock.recv(4096)
            except OSError:
                break
        assert b"echo-me" in got
    finally:
        sock.close()


def test_status_and_list_reflect_live_session(live_daemon):
    from sshm import protocol
    from sshm.ipc import connect_streaming, send_request

    # No session yet.
    assert send_request(protocol.CMD_STATUS)["data"]["sessions"] == 0

    sock, resp, _ = connect_streaming(
        protocol.CMD_ATTACH, alias="test", cli_pid=os.getpid(), cols=80, rows=24
    )
    try:
        # A separate IPC request sees the live session (server is threaded).
        assert send_request(protocol.CMD_STATUS)["data"]["sessions"] == 1
        sessions = send_request(protocol.CMD_LIST, alias="test")["data"]
        assert len(sessions) == 1
        assert sessions[0]["alias"] == "test" and sessions[0]["attached"] is True
    finally:
        sock.close()


def test_disconnect_removes_session(live_daemon):
    from sshm import protocol
    from sshm.ipc import connect_streaming, send_request

    sock, resp, _ = connect_streaming(
        protocol.CMD_ATTACH, alias="test", cli_pid=os.getpid()
    )
    name = resp["data"]["name"]
    sock.close()

    resp = send_request(protocol.CMD_DISCONNECT, alias="test", name=name)
    assert resp["data"]["disconnected"] is True
    assert send_request(protocol.CMD_STATUS)["data"]["sessions"] == 0
