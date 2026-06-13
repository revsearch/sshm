"""sshmd — SSH session manager daemon."""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from collections.abc import Callable
from typing import Any

from . import protocol
from .config import (
    PortForward,
    add_port_forward,
    find_entry,
    load_entries,
    remove_host,
    remove_port_forward,
    rename_host,
    set_enabled,
)
from .ipc import IPC_HOST, IPC_PORT, IpcServer, StreamingResponse
from .process import ProcessManager
from .state import log_file, new_token, pid_file, write_token

log = logging.getLogger("sshm")

WATCHDOG_INTERVAL = 5.0


def _required(req: dict[str, Any], *keys: str) -> list[Any]:
    values = [req.get(k) for k in keys]
    if not all(values):
        raise ValueError(f"Missing {' or '.join(keys)}")
    return values


class Daemon:
    def __init__(self) -> None:
        self.pm = ProcessManager()
        self.token = new_token()
        self.server = IpcServer(handler=self.handle_request, token=self.token)
        self._shutdown_event = threading.Event()
        self._handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            protocol.CMD_STATUS: self._cmd_status,
            protocol.CMD_LIST: self._cmd_list,
            protocol.CMD_CONNECT: self._cmd_connect,
            protocol.CMD_ATTACH: self._cmd_attach,
            protocol.CMD_DETACH: self._cmd_detach,
            protocol.CMD_RESIZE: self._cmd_resize,
            protocol.CMD_DISCONNECT: self._cmd_disconnect,
            protocol.CMD_PORT_ADD: self._cmd_port_add,
            protocol.CMD_PORT_REMOVE: self._cmd_port_remove,
            protocol.CMD_ENABLE: self._cmd_enable,
            protocol.CMD_DISABLE: self._cmd_disable,
            protocol.CMD_REMOVE: self._cmd_remove,
            protocol.CMD_RENAME: self._cmd_rename,
            protocol.CMD_SHUTDOWN: self._cmd_shutdown,
        }

    def handle_request(self, req: dict[str, Any]) -> dict[str, Any] | StreamingResponse:
        cmd = req.get("cmd", "")
        handler = self._handlers.get(cmd)
        if handler is None:
            return protocol.err(f"Unknown command: {cmd}")
        try:
            return handler(req)
        except ValueError as e:
            # Request validation errors — report to the client without traceback noise
            return protocol.err(str(e))
        except Exception as e:
            log.exception("Error handling %s", cmd)
            return protocol.err(str(e))

    # --- Command handlers ---

    def _cmd_status(self, req: dict[str, Any]):
        return protocol.ok({"status": "running", "sessions": len(self.pm.get_sessions())})

    def _cmd_list(self, req: dict[str, Any]):
        alias = req.get("alias")
        if alias:
            return protocol.ok([s.to_dict() for s in self.pm.get_sessions(alias)])

        result = []
        for e in load_entries():
            if e.alias in ("*", ""):
                continue
            active = self.pm.get_sessions(e.alias)
            result.append({
                "alias": e.alias,
                "hostname": e.hostname,
                "user": e.user,
                "port": e.port,
                "enabled": e.enabled,
                "connections": len(active),
                "attached": sum(1 for s in active if s.attached),
                "port_forwards": [pf.to_str() for pf in e.port_forwards],
            })
        return protocol.ok(result)

    def _cmd_connect(self, req: dict[str, Any]):
        [alias] = _required(req, "alias")
        if not find_entry(alias):
            return protocol.err(f"Unknown alias: {alias}")
        session = self.pm.connect(alias, req.get("name"))
        return protocol.ok(session.to_dict())

    def _cmd_attach(self, req: dict[str, Any]):
        [alias] = _required(req, "alias")
        if not find_entry(alias):
            return protocol.err(f"Unknown alias: {alias}")
        session = self.pm.attach(alias, req.get("name"), cli_pid=req.get("cli_pid"))
        if not session:
            return protocol.err(f"No available session for '{alias}'")

        # Size the remote terminal to the attaching client before streaming starts
        cols, rows = req.get("cols"), req.get("rows")
        if cols and rows:
            session.set_winsize(int(cols), int(rows))

        # The IPC server bridges this connection after sending the response
        return StreamingResponse(
            protocol.ok(session.to_dict()), session, cli_pid=req.get("cli_pid")
        )

    def _cmd_resize(self, req: dict[str, Any]):
        alias, name, cols, rows = _required(req, "alias", "name", "cols", "rows")
        for s in self.pm.get_sessions(alias):
            if s.name == name:
                s.set_winsize(int(cols), int(rows))
                return protocol.ok({"resized": [int(cols), int(rows)]})
        return protocol.err(f"No session {alias}/{name}")

    def _cmd_detach(self, req: dict[str, Any]):
        alias, name = _required(req, "alias", "name")
        # Detach is mostly handled by the bridge ending; explicit detach works too
        for s in self.pm.get_sessions(alias):
            if s.name == name and s.attached:
                s.detach()
        # For enabled aliases, keep an unattached session ready
        entry = find_entry(alias)
        if entry and entry.enabled:
            self.pm.ensure_unattached(alias)
        return protocol.ok({"detached": True})

    def _cmd_disconnect(self, req: dict[str, Any]):
        alias, name = _required(req, "alias", "name")
        return protocol.ok({"disconnected": self.pm.disconnect(alias, name)})

    def _cmd_port_add(self, req: dict[str, Any]):
        alias, direction, rule = _required(req, "alias", "direction", "rule")
        if direction == "D":
            pf = PortForward.socks(int(rule))  # SOCKS proxy: rule is just the port
        else:
            pf = PortForward.parse_rule(rule, direction)
        add_port_forward(alias, pf)
        self._rebuild_alias_sessions(alias)
        return protocol.ok({"added": pf.to_str()})

    def _cmd_port_remove(self, req: dict[str, Any]):
        alias, rule = _required(req, "alias", "rule")  # rule is already serialized
        remove_port_forward(alias, rule)
        self._rebuild_alias_sessions(alias)
        return protocol.ok({"removed": rule})

    def _cmd_enable(self, req: dict[str, Any]):
        [alias] = _required(req, "alias")
        set_enabled(alias, True)
        self.pm.ensure_unattached(alias)
        return protocol.ok({"enabled": alias})

    def _cmd_disable(self, req: dict[str, Any]):
        [alias] = _required(req, "alias")
        set_enabled(alias, False)
        return protocol.ok({"disabled": alias})

    def _cmd_remove(self, req: dict[str, Any]):
        [alias] = _required(req, "alias")
        self.pm.disconnect_alias(alias)
        remove_host(alias)
        return protocol.ok({"removed": alias})

    def _cmd_rename(self, req: dict[str, Any]):
        old_alias, new_alias = _required(req, "alias", "new_alias")
        if not find_entry(old_alias):
            return protocol.err(f"Unknown alias: {old_alias}")
        if find_entry(new_alias):
            return protocol.err(f"Alias '{new_alias}' already exists")
        # Sessions are keyed by alias; drop the old ones (an enabled host gets a
        # fresh session under the new alias on the next watchdog tick).
        self.pm.disconnect_alias(old_alias)
        rename_host(old_alias, new_alias)
        return protocol.ok({"renamed": new_alias})

    def _cmd_shutdown(self, req: dict[str, Any]):
        self._shutdown_event.set()
        return protocol.ok({"shutting_down": True})

    # --- Background maintenance ---

    def _rebuild_alias_sessions(self, alias: str) -> None:
        """Restart active sessions so they pick up config changes (e.g. forwards)."""
        for s in self.pm.get_sessions(alias):
            self.pm.rebuild_session(alias, s.name)

    def _watchdog(self) -> None:
        while not self._shutdown_event.is_set():
            try:
                self.pm.check_orphaned_attaches()
                self.pm.check_health()
                self._ensure_enabled_sessions()
            except Exception:
                log.exception("Watchdog error")
            self._shutdown_event.wait(WATCHDOG_INTERVAL)

    def _ensure_enabled_sessions(self) -> None:
        for entry in load_entries():
            if entry.enabled and entry.alias not in ("*", ""):
                try:
                    self.pm.ensure_unattached(entry.alias)
                except Exception:
                    # One failing alias must not block the others
                    log.exception("Failed to ensure session for %s", entry.alias)

    def run(self) -> None:
        try:
            self.server.start()
        except OSError as e:
            # Don't touch the live daemon's pid/token files if the port is taken
            log.error(
                "Cannot bind %s:%s (%s) — is another sshmd already running?",
                IPC_HOST, IPC_PORT, e,
            )
            return

        # Publish credentials only after the port is ours
        write_token(self.token)
        pf = pid_file()
        pf.write_text(str(os.getpid()), encoding="utf-8")

        log.info("sshmd started (PID %s)", os.getpid())
        log.info("IPC server listening on %s:%s", IPC_HOST, IPC_PORT)

        # First watchdog tick auto-connects enabled aliases
        watchdog_thread = threading.Thread(target=self._watchdog, daemon=True)
        watchdog_thread.start()

        def handle_signal(sig, frame):
            log.info("Received signal %s, shutting down...", sig)
            self._shutdown_event.set()

        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)

        self._shutdown_event.wait()

        log.info("Shutting down...")
        self.server.stop()
        self.pm.disconnect_all()
        pf.unlink(missing_ok=True)
        log.info("sshmd stopped")


def main() -> None:
    handlers: list[logging.Handler] = [logging.FileHandler(log_file(), encoding="utf-8")]
    if sys.stderr is not None:  # absent under pythonw / detached start
        handlers.append(logging.StreamHandler())

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=handlers,
    )

    Daemon().run()


if __name__ == "__main__":
    main()
