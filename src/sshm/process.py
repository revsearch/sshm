"""SSH subprocess management: sessions, scrollback, health checks."""

from __future__ import annotations

import logging
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .procutil import no_window_popen_flags, pid_alive

log = logging.getLogger("sshm.process")

RECONNECT_MIN = 1.0
RECONNECT_MAX = 60.0
STABLE_THRESHOLD = 30.0
SCROLLBACK_MAX = 16 * 1024  # scrollback buffer replayed to a newly attached client
READ_CHUNK = 4096

# On POSIX we run ssh under a real PTY so it can negotiate the remote terminal
# size and react to SIGWINCH. Windows has no pty module — there we keep the
# pipe-based model (the remote PTY stays at its default size). fcntl/termios are
# imported at module load (not inside the post-fork child) so preexec_fn can't
# deadlock on the import lock.
_HAS_PTY = sys.platform != "win32"
if _HAS_PTY:
    import fcntl
    import termios

    # Pre-resolve everything the post-fork preexec_fn touches to plain globals, so
    # the child (forked from a multithreaded daemon) does no module attribute
    # lookups — only bound calls and bare syscalls — before exec.
    _setsid = os.setsid
    _ioctl = fcntl.ioctl
    _TIOCSCTTY = termios.TIOCSCTTY


def _find_ssh() -> str:
    ssh = shutil.which("ssh")
    if not ssh:
        raise RuntimeError("ssh not found in PATH")
    return ssh


def _make_controlling_tty() -> None:
    """preexec_fn (child side): own a new session with the slave PTY as our tty.

    setsid() makes the child a session leader; TIOCSCTTY then makes its stdin
    (the slave PTY) the controlling terminal, which is what lets the kernel
    deliver SIGWINCH to ssh when we resize the master.
    """
    _setsid()
    _ioctl(0, _TIOCSCTTY, 0)


def _set_winsize(fd: int, cols: int, rows: int) -> None:
    # struct winsize is { ws_row, ws_col, ws_xpixel, ws_ypixel }
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _spawn_with_pty(cmd: list[str]) -> tuple[subprocess.Popen, int]:
    """Spawn under a PTY; return (process, master_fd). POSIX only."""
    import pty

    master_fd, slave_fd = pty.openpty()
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            preexec_fn=_make_controlling_tty,
            close_fds=True,
        )
    except BaseException:
        os.close(master_fd)
        os.close(slave_fd)
        raise
    os.close(slave_fd)  # the child holds its own copy; the parent keeps the master
    return proc, master_fd


@dataclass
class SshSession:
    alias: str
    name: str
    process: subprocess.Popen | None = None
    pid: int | None = None
    started_at: float = 0.0
    reconnect: bool = True
    attached: bool = False
    attached_pid: int | None = None
    # PTY master fd that ssh's stdin/stdout/stderr are wired to (POSIX). None on
    # Windows, where we fall back to the Popen pipes.
    master_fd: int | None = None
    # Last terminal size a client reported (cols, rows), re-applied across
    # reconnects so the remote shell keeps its size.
    last_winsize: tuple[int, int] | None = None

    scrollback: bytearray = field(default_factory=bytearray, repr=False)
    _active_socket: socket.socket | None = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _reader_thread: threading.Thread | None = field(default=None, repr=False)
    # Bumped on every (re)connect under _lock. A reader stamps the generation it
    # was started for and stops appending once it no longer matches, so a dying
    # reader can't pour stale output into the next process's scrollback.
    _io_gen: int = 0
    _backoff: float = RECONNECT_MIN
    _last_attempt: float = 0.0

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "name": self.name,
            "pid": self.pid,
            "started_at": self.started_at,
            "uptime": time.time() - self.started_at if self.started_at else 0,
            "alive": self.alive,
            "attached": self.attached,
            "port_forwards": [],
        }

    def adopt_process(self, proc: subprocess.Popen, master_fd: int | None = None) -> None:
        """Take ownership of a freshly spawned SSH process and start reading it."""
        with self._lock:
            # Swap in the new process/master and bump the generation atomically, so
            # a concurrent writer/resize sees a consistent (and open) master_fd and
            # the previous reader stops appending into the fresh scrollback.
            old_fd = self.master_fd
            self.process = proc
            self.master_fd = master_fd
            self.pid = proc.pid
            self.started_at = time.time()
            self.scrollback = bytearray()
            self._io_gen += 1
            gen = self._io_gen
            winsize = self.last_winsize

        # Close the old master outside the lock: nothing reads self.master_fd as
        # `old_fd` anymore, and the old reader ends when its captured fd goes away.
        if old_fd is not None and old_fd != master_fd:
            try:
                os.close(old_fd)
            except OSError:
                pass

        if master_fd is not None and winsize is not None:
            self.set_winsize(*winsize)  # re-apply the remembered size to the new PTY

        self._start_reader(gen)

    def set_winsize(self, cols: int, rows: int) -> None:
        """Resize the remote terminal (ssh gets SIGWINCH from the master)."""
        # struct winsize fields are unsigned 16-bit; clamp so a bogus size can't
        # raise struct.error out of the ioctl pack.
        cols = max(0, min(cols, 0xFFFF))
        rows = max(0, min(rows, 0xFFFF))
        with self._lock:
            self.last_winsize = (cols, rows)
            fd = self.master_fd
            if fd is not None:
                try:
                    _set_winsize(fd, cols, rows)  # ioctl doesn't block — safe under the lock
                except OSError:
                    pass

    def _write_input(self, data: bytes) -> bool:
        """Forward client bytes to the SSH process. Returns False on failure."""
        with self._lock:
            fd = self.master_fd
            if fd is not None:
                # dup under the lock so a concurrent _kill/reconnect closing
                # master_fd can't make us write onto a reused fd; the private dup
                # keeps the same PTY master alive for the duration of the write,
                # which happens outside the lock (os.write can block on backpressure
                # and must not stall the reader or deadlock a killer).
                try:
                    wfd = os.dup(fd)
                except OSError:
                    return False
                stdin = None
            else:
                wfd = None
                stdin = self.process.stdin if self.process else None

        if wfd is not None:
            try:
                os.write(wfd, data)
                return True
            except (OSError, ValueError):
                return False
            finally:
                try:
                    os.close(wfd)
                except OSError:
                    pass

        if not stdin:
            return False
        try:
            stdin.write(data)
            stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def _start_reader(self, gen: int) -> None:
        """Background thread: SSH output → scrollback + attached socket, if any."""
        process = self.process
        if process is None:
            return

        # Read from the PTY master where we have one, else the stdout pipe.
        if self.master_fd is not None:
            fd = self.master_fd
        elif process.stdout is not None:
            fd = process.stdout.fileno()
        else:
            return

        # Bind the Popen object to the thread for its lifetime (as a default arg,
        # which the function object holds): a reconnect rebinds self.process, and
        # without this reference the pipe fd could be closed (and reused) while we
        # still read from it. The PTY master is owned by the session and closed
        # explicitly on reconnect/kill, so the keepalive is only load-bearing for
        # the Windows pipe path.
        def _read(_keepalive: subprocess.Popen = process) -> None:
            while True:
                try:
                    data = os.read(fd, READ_CHUNK)  # unbuffered — returns as soon as data is available
                    if not data:
                        break
                except (OSError, ValueError):
                    break

                with self._lock:
                    # A reconnect (new generation) reset scrollback for the new
                    # process; stop so we don't append this dying process's tail.
                    if self._io_gen != gen:
                        break
                    self.scrollback.extend(data)
                    if len(self.scrollback) > SCROLLBACK_MAX:
                        del self.scrollback[:-SCROLLBACK_MAX]

                    sock = self._active_socket
                    if sock:
                        try:
                            sock.sendall(data)
                        except OSError:
                            self._active_socket = None

            log.info("Reader thread ended for %s/%s", self.alias, self.name)

        self._reader_thread = threading.Thread(target=_read, daemon=True, name=f"reader-{self.name}")
        self._reader_thread.start()

    def detach(self) -> None:
        """Drop the attached client, closing its socket if present."""
        with self._lock:
            if self._active_socket:
                try:
                    self._active_socket.close()
                except OSError:
                    pass
                self._active_socket = None
            self.attached = False
            self.attached_pid = None

    def bridge(self, conn: socket.socket, cli_pid: int | None = None) -> None:
        """Bridge a TCP socket to this session's SSH I/O. Blocks until detach."""
        with self._lock:
            self.attached = True
            self.attached_pid = cli_pid
            # Replay scrollback so the client sees recent output immediately
            if self.scrollback:
                try:
                    conn.sendall(bytes(self.scrollback))
                except OSError:
                    self.attached = False
                    self.attached_pid = None
                    return
            self._active_socket = conn

        # Socket → SSH input (PTY master, or stdin pipe on Windows)
        try:
            while True:
                data = conn.recv(READ_CHUNK)
                if not data:
                    break
                if not self._write_input(data):
                    break
        except OSError:
            pass
        finally:
            self.detach()
            log.info("Bridge ended for %s/%s", self.alias, self.name)


class ProcessManager:
    def __init__(self) -> None:
        self.sessions: dict[str, SshSession] = {}
        self._ssh = _find_ssh()
        # Guards every access to self.sessions. Reentrant because the public
        # methods call one another (attach -> connect, rebuild -> _kill/connect).
        # Lock order is always ProcessManager._lock -> SshSession._lock, never the
        # reverse: bridge()/the reader thread take only the session lock, so they
        # can run while a handler holds this one (e.g. during _kill's wait).
        self._lock = threading.RLock()

    @staticmethod
    def _key(alias: str, name: str) -> str:
        return f"{alias}/{name}"

    def _next_name(self, alias: str) -> str:
        existing = {s.name for s in self.sessions.values() if s.alias == alias}
        n = 1
        while f"{alias}-{n}" in existing:
            n += 1
        return f"{alias}-{n}"

    def _spawn(self, alias: str) -> tuple[subprocess.Popen, int | None]:
        cmd = [
            self._ssh, alias,
            "-tt",  # force remote PTY
            "-o", "ServerAliveInterval=10",
            "-o", "ServerAliveCountMax=3",
        ]
        log.info("Spawning: %s", " ".join(cmd))
        if _HAS_PTY:
            return _spawn_with_pty(cmd)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            **no_window_popen_flags(),
        )
        return proc, None

    def connect(self, alias: str, name: str | None = None) -> SshSession:
        with self._lock:
            if name is None:
                name = self._next_name(alias)

            key = self._key(alias, name)
            existing = self.sessions.get(key)
            if existing and existing.alive:
                return existing

            session = SshSession(alias=alias, name=name)
            proc, master_fd = self._spawn(alias)
            session.adopt_process(proc, master_fd)
            self.sessions[key] = session
            log.info("Connected %s (PID %s)", key, session.pid)
            return session

    def attach(
        self, alias: str, name: str | None = None, cli_pid: int | None = None
    ) -> SshSession | None:
        """Reserve the first unattached live session (or create one) for a client.

        The session is marked attached here, atomically, so two concurrent attach
        requests can never hand the same shell to two clients. bridge() later just
        confirms the flag; check_orphaned_attaches reclaims it if the client dies
        before bridging.
        """
        with self._lock:
            if name:
                session = self.sessions.get(self._key(alias, name))
                if session and not session.attached and session.alive:
                    session.attached = True
                    session.attached_pid = cli_pid
                    return session
                return None

            for s in self.sessions.values():
                if s.alias == alias and not s.attached and s.alive:
                    s.attached = True
                    s.attached_pid = cli_pid
                    return s

            session = self.connect(alias)
            session.attached = True
            session.attached_pid = cli_pid
            return session

    # The disconnect paths pop the session(s) out of the dict under the lock, then
    # _kill them OUTSIDE it: killing blocks on process.wait(), and holding the PM
    # lock across that would serialize every other session operation for seconds.
    def disconnect(self, alias: str, name: str) -> bool:
        with self._lock:
            session = self.sessions.pop(self._key(alias, name), None)
        if not session:
            return False
        session.reconnect = False
        self._kill(session)
        return True

    def disconnect_alias(self, alias: str) -> int:
        with self._lock:
            keys = [k for k, s in self.sessions.items() if s.alias == alias]
            sessions = [self.sessions.pop(k) for k in keys]
        for session in sessions:
            session.reconnect = False
            self._kill(session)
        return len(sessions)

    def disconnect_all(self) -> None:
        with self._lock:
            sessions = list(self.sessions.values())
            self.sessions.clear()
        for session in sessions:
            session.reconnect = False
            self._kill(session)

    def _kill(self, session: SshSession) -> None:
        """Terminate a session. Must be called on a session already removed from
        the dict, and NOT while holding self._lock (it blocks on process.wait)."""
        session.detach()  # kick the attached client, if any

        if session.alive:
            try:
                session.process.terminate()
                session.process.wait(timeout=5)
            except Exception:
                try:
                    session.process.kill()
                except Exception:
                    pass
            log.info("Killed %s/%s (PID %s)", session.alias, session.name, session.pid)

        # Null the fd under the session lock (so a racing writer sees None and
        # won't dup it), then close it outside the lock.
        with session._lock:
            fd = session.master_fd
            session.master_fd = None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def get_sessions(self, alias: str | None = None) -> list[SshSession]:
        with self._lock:
            if alias:
                return [s for s in self.sessions.values() if s.alias == alias]
            return list(self.sessions.values())

    def count_unattached(self, alias: str) -> int:
        with self._lock:
            return sum(
                1 for s in self.sessions.values()
                if s.alias == alias and not s.attached and s.alive
            )

    def ensure_unattached(self, alias: str) -> None:
        with self._lock:
            if self.count_unattached(alias) == 0:
                log.info("Spawning new unattached session for %s", alias)
                self.connect(alias)

    def check_health(self) -> None:
        with self._lock:
            to_reconnect: list[SshSession] = []

            for key, session in list(self.sessions.items()):
                if not session.process:
                    continue

                retcode = session.process.poll()
                if retcode is None:
                    if time.time() - session.started_at > STABLE_THRESHOLD:
                        session._backoff = RECONNECT_MIN
                    continue

                log.warning("Session %s exited (code %s)", key, retcode)
                session.detach()

                # Clean exit (exit/logout) = remove session, don't reconnect.
                # Non-zero (e.g. 255 = connection lost) = reconnect if enabled.
                if retcode == 0 or not session.reconnect:
                    self.sessions.pop(key, None)
                else:
                    now = time.time()
                    if now - session._last_attempt >= session._backoff:
                        to_reconnect.append(session)
                        session._last_attempt = now

            for session in to_reconnect:
                log.info(
                    "Reconnecting %s/%s (backoff %.1fs)",
                    session.alias, session.name, session._backoff,
                )
                try:
                    proc, master_fd = self._spawn(session.alias)
                    session.adopt_process(proc, master_fd)
                    log.info("Reconnected %s/%s (PID %s)", session.alias, session.name, session.pid)
                except Exception as e:
                    log.error("Failed to reconnect %s/%s: %s", session.alias, session.name, e)
                finally:
                    session._backoff = min(session._backoff * 2, RECONNECT_MAX)

    def check_orphaned_attaches(self) -> None:
        with self._lock:
            for s in self.sessions.values():
                if s.attached and s.attached_pid and not pid_alive(s.attached_pid):
                    log.warning(
                        "CLI pid %s gone, auto-detaching %s/%s",
                        s.attached_pid, s.alias, s.name,
                    )
                    s.detach()

    def rebuild_session(self, alias: str, name: str) -> SshSession | None:
        """Kill and restart a session (e.g. after a config change)."""
        with self._lock:
            session = self.sessions.pop(self._key(alias, name), None)
        if not session:
            return None
        self._kill(session)  # outside the lock — blocks on process.wait
        return self.connect(alias, name)
