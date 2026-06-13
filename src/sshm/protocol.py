"""IPC message schemas and encoding for sshm daemon communication."""

from __future__ import annotations

import json
from typing import Any


# --- Request commands ---

CMD_LIST = "list"
CMD_CONNECT = "connect"
CMD_DISCONNECT = "disconnect"
CMD_ATTACH = "attach"
CMD_DETACH = "detach"
CMD_RESIZE = "resize"
CMD_PORT_ADD = "port_add"
CMD_PORT_REMOVE = "port_remove"
CMD_ENABLE = "enable"
CMD_DISABLE = "disable"
CMD_REMOVE = "remove"
CMD_RENAME = "rename"
CMD_STATUS = "status"
CMD_SHUTDOWN = "shutdown"


def make_request(cmd: str, token: str, **kwargs) -> dict[str, Any]:
    return {"cmd": cmd, "token": token, **kwargs}


def make_response(ok: bool, data: Any = None, error: str | None = None) -> dict[str, Any]:
    resp: dict[str, Any] = {"ok": ok}
    if data is not None:
        resp["data"] = data
    if error is not None:
        resp["error"] = error
    return resp


def ok(data: Any = None) -> dict[str, Any]:
    return make_response(True, data=data)


def err(message: str) -> dict[str, Any]:
    return make_response(False, error=message)


def encode(msg: dict[str, Any]) -> bytes:
    return json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n"


def decode(data: bytes) -> dict[str, Any]:
    return json.loads(data.decode("utf-8").strip())
