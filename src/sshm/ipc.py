"""IPC client/server for sshm CLI <-> daemon communication over localhost TCP."""

from __future__ import annotations

import hmac
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from . import protocol
from .procutil import daemon_interpreter, detached_popen_flags, pid_alive
from .state import pid_file, read_token, resolve_port, token_file

IPC_HOST = "127.0.0.1"
IPC_PORT = resolve_port()
BUFFER_SIZE = 65536
REQUEST_TIMEOUT = 10.0


def _recv_line(sock: socket.socket) -> tuple[bytes, bytes] | None:
    """Read until a newline.

    Returns ``(line, leftover)`` where ``line`` is the bytes up to (excluding)
    the first newline and ``leftover`` is anything already received past it.
    Returns None if the peer closed before sending a newline. The leftover
    matters for the streaming handshake: the daemon writes the JSON response and
    then immediately starts streaming session output, and on localhost both can
    land in a single recv() — without preserving the tail those first bytes of
    terminal output would be lost (or corrupt the JSON decode).
    """
    data = b""
    while b"\n" not in data:
        chunk = sock.recv(BUFFER_SIZE)
        if not chunk:
            return None
        data += chunk
    line, _, leftover = data.partition(b"\n")
    return line, leftover


# --- Server ---


class StreamingResponse:
    """Marks a response whose connection should become a raw I/O bridge to a session."""

    def __init__(self, response: dict[str, Any], session, cli_pid: int | None = None):
        self.response = response
        self.session = session  # SshSession to bridge to
        self.cli_pid = cli_pid


Handler = Callable[[dict[str, Any]], "dict[str, Any] | StreamingResponse"]


class IpcServer:
    def __init__(self, handler: Handler, token: str):
        self.handler = handler
        self.token = token
        self._sock: socket.socket | None = None
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if sys.platform == "win32":
            # On Windows SO_REUSEADDR lets another process bind an actively
            # listening port and hijack traffic; exclusive use makes it fail.
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((IPC_HOST, IPC_PORT))
        self._sock.listen(5)
        self._sock.settimeout(1.0)
        self._running = True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self) -> None:
        while self._running:
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_client, args=(conn,), daemon=True).start()

    def _handle_client(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(REQUEST_TIMEOUT)
            received = _recv_line(conn)
            if received is None:
                return
            data, _ = received  # a request is exactly one line; nothing trails it

            request = protocol.decode(data)

            token = request.get("token")
            if not isinstance(token, str) or not hmac.compare_digest(token, self.token):
                conn.sendall(protocol.encode(protocol.err("Invalid token")))
                return

            response = self.handler(request)

            if isinstance(response, StreamingResponse):
                # Send the JSON response, then turn this connection into a raw bridge.
                # attach() already reserved the session (attached=True); if anything
                # fails before bridge() starts its own finally-detach, release that
                # reservation so the session isn't leaked out of the attachable pool.
                try:
                    conn.sendall(protocol.encode(response.response))
                    conn.settimeout(None)
                    response.session.bridge(conn, cli_pid=response.cli_pid)  # blocks until detach
                except Exception:
                    response.session.detach()
                    raise
            else:
                conn.sendall(protocol.encode(response))

        except Exception as e:
            try:
                conn.sendall(protocol.encode(protocol.err(str(e))))
            except Exception:
                pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def stop(self) -> None:
        self._running = False
        if self._sock:
            self._sock.close()
        if self._thread:
            self._thread.join(timeout=5)


# --- Client ---


class DaemonNotRunning(Exception):
    pass


def _open_request(cmd: str, **kwargs) -> socket.socket:
    """Connect to the daemon and send a request; the response is not read yet."""
    token = read_token()
    if not token:
        raise DaemonNotRunning("Daemon not running (no token file)")

    request = protocol.make_request(cmd, token, **kwargs)

    try:
        sock = socket.create_connection((IPC_HOST, IPC_PORT), timeout=REQUEST_TIMEOUT)
    except ConnectionRefusedError as e:
        raise DaemonNotRunning("Cannot connect to sshmd (connection refused)") from e

    sock.sendall(protocol.encode(request))
    return sock


def send_request(cmd: str, **kwargs) -> dict[str, Any]:
    sock = _open_request(cmd, **kwargs)
    try:
        received = _recv_line(sock)
    finally:
        sock.close()
    if received is None:
        raise DaemonNotRunning("Connection closed")
    data, _ = received
    return protocol.decode(data)


def connect_streaming(cmd: str, **kwargs) -> tuple[socket.socket, dict[str, Any], bytes]:
    """Send a request; return (socket, response, leftover).

    ``leftover`` is any streamed output that arrived bundled with the response
    line and must be replayed to the terminal before reading further from the
    socket. The socket stays open for streaming.
    """
    sock = _open_request(cmd, **kwargs)

    received = _recv_line(sock)
    if received is None:
        sock.close()
        raise DaemonNotRunning("Connection closed")

    data, leftover = received
    resp = protocol.decode(data)
    if not resp.get("ok"):
        sock.close()
        return sock, resp, b""

    sock.settimeout(None)  # no timeout while streaming
    return sock, resp, leftover


# --- Daemon lifecycle ---


def is_daemon_running() -> bool:
    pf = pid_file()
    if not pf.exists():
        return False
    try:
        pid = int(pf.read_text().strip())
    except (ValueError, OSError):
        # The daemon unlinks the pid file on shutdown; a read race means "not running"
        return False
    return pid_alive(pid)


def ensure_daemon() -> None:
    if is_daemon_running():
        # The pid is alive, but confirm it's actually our daemon answering on the
        # port — guards against PID reuse (a stale pid file pointing at an
        # unrelated process) and a wedged daemon. If it doesn't respond, fall
        # through and (re)spawn.
        try:
            if send_request(protocol.CMD_STATUS).get("ok"):
                return
        except Exception:
            pass

    subprocess.Popen(
        [daemon_interpreter(), "-m", "sshm.daemon"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **detached_popen_flags(),
    )

    for _ in range(30):
        time.sleep(0.2)
        if token_file().exists():
            try:
                if send_request(protocol.CMD_STATUS).get("ok"):
                    return
            except Exception:
                pass
    raise RuntimeError(
        f"Failed to start sshmd daemon (is port {IPC_PORT} taken by another process?)"
    )
