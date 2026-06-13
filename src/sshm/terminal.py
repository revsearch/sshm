"""Raw terminal I/O bridge between the local console and a daemon socket."""

from __future__ import annotations

import os
import socket
import sys
import threading
from collections.abc import Callable

CHUNK = 4096

# Called with (cols, rows) when the local terminal is resized. Returns nothing;
# implementations should be non-blocking / best-effort.
ResizeCallback = Callable[[int, int], None]


def stream_bridge(
    sock: socket.socket, initial: bytes = b"", on_resize: ResizeCallback | None = None
) -> None:
    """Pump bytes between the local terminal and the socket until either side closes.

    ``initial`` is session output that arrived bundled with the attach response
    and is written to the terminal before streaming begins. ``on_resize`` is
    invoked with the new (cols, rows) whenever the local terminal is resized
    (POSIX only; Windows keeps the size it attached with).
    """
    if sys.platform == "win32":
        _bridge_windows(sock, initial)
    else:
        _bridge_unix(sock, initial, on_resize)


def _bridge_unix(
    sock: socket.socket, initial: bytes = b"", on_resize: ResizeCallback | None = None
) -> None:
    import select
    import signal
    import termios
    import tty

    stdin_fd = sys.stdin.fileno()
    # Raw mode only makes sense (and tcgetattr only works) on a real terminal.
    # When stdin is redirected (a pipe, /dev/null) we still bridge, just non-raw.
    is_tty = sys.stdin.isatty()
    old_attrs = termios.tcgetattr(stdin_fd) if is_tty else None

    # SIGWINCH self-pipe (set up inside the try so the finally always reclaims the
    # fds and the handler). The handler does almost nothing — just writes a byte;
    # the select loop does the signal-unsafe work of reading the size.
    winch_r = winch_w = -1
    old_winch = None
    use_winch = is_tty and on_resize is not None and hasattr(signal, "SIGWINCH")

    def _notify_resize() -> None:
        try:
            sz = os.get_terminal_size(stdin_fd)
            on_resize(sz.columns, sz.lines)
        except Exception:
            pass  # never let a resize hiccup break the bridge

    try:
        if use_winch:
            winch_r, winch_w = os.pipe()
            os.set_blocking(winch_r, False)
            os.set_blocking(winch_w, False)

            def _winch_handler(_signum, _frame):
                try:
                    os.write(winch_w, b"\x01")
                except OSError:
                    pass

            old_winch = signal.signal(signal.SIGWINCH, _winch_handler)

        if is_tty:
            tty.setraw(stdin_fd)

        if initial:
            sys.stdout.buffer.write(initial)
            sys.stdout.buffer.flush()

        watch = [stdin_fd, sock]
        if use_winch:
            watch.append(winch_r)

        while True:
            ready, _, _ = select.select(watch, [], [], 1.0)

            for fd in ready:
                if fd is sock:
                    data = sock.recv(CHUNK)
                    if not data:
                        return
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
                elif use_winch and fd == winch_r:
                    while True:  # fully drain coalesced signals (winch_r is non-blocking)
                        try:
                            if not os.read(winch_r, 4096):
                                break
                        except OSError:
                            break
                    _notify_resize()
                elif fd == stdin_fd:
                    data = os.read(stdin_fd, CHUNK)
                    if not data:
                        return
                    sock.sendall(data)
    except OSError:
        pass
    finally:
        if old_winch is not None:
            signal.signal(signal.SIGWINCH, old_winch)
        if winch_r != -1:
            os.close(winch_r)
        if winch_w != -1:
            os.close(winch_w)
        if is_tty and old_attrs is not None:
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_attrs)
        sock.close()


def _bridge_windows(sock: socket.socket, initial: bytes = b"") -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    STD_INPUT_HANDLE = -10
    STD_OUTPUT_HANDLE = -11

    h_in = kernel32.GetStdHandle(STD_INPUT_HANDLE)
    h_out = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)

    old_in_mode = wintypes.DWORD()
    old_out_mode = wintypes.DWORD()
    kernel32.GetConsoleMode(h_in, ctypes.byref(old_in_mode))
    kernel32.GetConsoleMode(h_out, ctypes.byref(old_out_mode))

    ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200
    kernel32.SetConsoleMode(h_in, ENABLE_VIRTUAL_TERMINAL_INPUT)

    ENABLE_PROCESSED_OUTPUT = 0x0001
    ENABLE_WRAP_AT_EOL_OUTPUT = 0x0002
    ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
    kernel32.SetConsoleMode(
        h_out,
        ENABLE_PROCESSED_OUTPUT | ENABLE_WRAP_AT_EOL_OUTPUT | ENABLE_VIRTUAL_TERMINAL_PROCESSING,
    )

    if initial:
        written = wintypes.DWORD()
        kernel32.WriteFile(h_out, initial, len(initial), ctypes.byref(written), None)

    stop_event = threading.Event()

    def _read_socket() -> None:
        try:
            written = wintypes.DWORD()
            while not stop_event.is_set():
                data = sock.recv(CHUNK)
                if not data:
                    break
                kernel32.WriteFile(h_out, data, len(data), ctypes.byref(written), None)
        except OSError:
            pass
        finally:
            stop_event.set()

    def _read_stdin() -> None:
        try:
            buf = ctypes.create_string_buffer(CHUNK)
            bytes_read = wintypes.DWORD()
            while not stop_event.is_set():
                success = kernel32.ReadFile(h_in, buf, CHUNK, ctypes.byref(bytes_read), None)
                if success and bytes_read.value > 0:
                    sock.sendall(buf.raw[: bytes_read.value])
                elif not success:
                    break
                else:
                    sock.sendall(b"\x1a")  # Ctrl-Z: signal EOF to the remote shell
        except OSError:
            pass
        finally:
            stop_event.set()

    t_sock = threading.Thread(target=_read_socket, daemon=True)
    t_stdin = threading.Thread(target=_read_stdin, daemon=True)
    t_sock.start()
    t_stdin.start()

    try:
        stop_event.wait()
    finally:
        kernel32.SetConsoleMode(h_in, old_in_mode.value)
        kernel32.SetConsoleMode(h_out, old_out_mode.value)
        sock.close()
        t_sock.join(timeout=1)
        t_stdin.join(timeout=1)
