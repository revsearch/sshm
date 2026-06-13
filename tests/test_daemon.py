"""Unit tests for the daemon command handlers, with a fake ProcessManager and
the config layer stubbed — no sockets, ssh, or ~/.ssh/config involved."""

import pytest

from sshm import protocol
from sshm.config import HostEntry, PortForward
from sshm.ipc import StreamingResponse


class FakeSession:
    def __init__(self, alias, name, attached=False):
        self.alias = alias
        self.name = name
        self.attached = attached
        self.winsize = None
        self.detached = False

    def to_dict(self):
        return {"alias": self.alias, "name": self.name, "attached": self.attached}

    def set_winsize(self, cols, rows):
        self.winsize = (cols, rows)

    def detach(self):
        self.detached = True
        self.attached = False


class FakePM:
    def __init__(self):
        self.sessions_list: list[FakeSession] = []
        self.calls: list[tuple] = []

    def get_sessions(self, alias=None):
        if alias is None:
            return list(self.sessions_list)
        return [s for s in self.sessions_list if s.alias == alias]

    def connect(self, alias, name=None):
        self.calls.append(("connect", alias, name))
        s = FakeSession(alias, name or f"{alias}-1")
        self.sessions_list.append(s)
        return s

    def attach(self, alias, name=None, cli_pid=None):
        self.calls.append(("attach", alias, name, cli_pid))
        for s in self.sessions_list:
            if s.alias == alias and not s.attached:
                s.attached = True
                return s
        return None

    def disconnect(self, alias, name):
        self.calls.append(("disconnect", alias, name))
        return True

    def disconnect_alias(self, alias):
        self.calls.append(("disconnect_alias", alias))
        return 1

    def ensure_unattached(self, alias):
        self.calls.append(("ensure_unattached", alias))

    def rebuild_session(self, alias, name):
        self.calls.append(("rebuild_session", alias, name))


@pytest.fixture
def daemon(monkeypatch):
    # Avoid the ssh-in-PATH requirement of the real ProcessManager constructor.
    monkeypatch.setattr("sshm.process._find_ssh", lambda: "ssh")
    from sshm.daemon import Daemon

    d = Daemon()
    d.pm = FakePM()
    return d


def _stub_config(monkeypatch, **funcs):
    """Replace named config functions imported into the daemon namespace."""
    for name, fn in funcs.items():
        monkeypatch.setattr(f"sshm.daemon.{name}", fn)


# --- status / list ---

def test_status_counts_sessions(daemon):
    daemon.pm.sessions_list = [FakeSession("a", "a-1"), FakeSession("b", "b-1")]
    resp = daemon._cmd_status({})
    assert resp == {"ok": True, "data": {"status": "running", "sessions": 2}}


def test_list_hosts_skips_wildcards(daemon, monkeypatch):
    entries = [
        HostEntry(alias="web", hostname="1.2.3.4", user="root", port=2222, enabled=True,
                  port_forwards=[PortForward("L", 8080, "localhost", 80)]),
        HostEntry(alias="*", hostname="", user=""),
    ]
    _stub_config(monkeypatch, load_entries=lambda: entries)
    daemon.pm.sessions_list = [FakeSession("web", "web-1", attached=True)]

    data = daemon._cmd_list({})["data"]
    assert [h["alias"] for h in data] == ["web"]  # wildcard dropped
    h = data[0]
    assert h["port"] == 2222 and h["enabled"] is True
    assert h["connections"] == 1 and h["attached"] == 1
    assert h["port_forwards"] == ["L:8080:localhost:80"]


def test_list_sessions_for_alias(daemon):
    daemon.pm.sessions_list = [FakeSession("web", "web-1"), FakeSession("db", "db-1")]
    data = daemon._cmd_list({"alias": "web"})["data"]
    assert [s["name"] for s in data] == ["web-1"]


# --- connect / attach / resize ---

def test_connect_unknown_alias(daemon, monkeypatch):
    _stub_config(monkeypatch, find_entry=lambda a, *args, **kw: None)
    assert daemon._cmd_connect({"alias": "nope"}) == protocol.err("Unknown alias: nope")


def test_connect_ok(daemon, monkeypatch):
    _stub_config(monkeypatch, find_entry=lambda a, *args, **kw: object())
    resp = daemon._cmd_connect({"alias": "web"})
    assert resp["ok"] and resp["data"]["alias"] == "web"
    assert ("connect", "web", None) in daemon.pm.calls


def test_attach_applies_winsize_and_streams(daemon, monkeypatch):
    _stub_config(monkeypatch, find_entry=lambda a, *args, **kw: object())
    daemon.pm.sessions_list = [FakeSession("web", "web-1")]
    resp = daemon._cmd_attach({"alias": "web", "cols": 120, "rows": 40, "cli_pid": 99})
    assert isinstance(resp, StreamingResponse)
    assert resp.response["ok"] and resp.cli_pid == 99
    assert daemon.pm.sessions_list[0].winsize == (120, 40)


def test_attach_no_available_session(daemon, monkeypatch):
    _stub_config(monkeypatch, find_entry=lambda a, *args, **kw: object())
    resp = daemon._cmd_attach({"alias": "web"})  # FakePM has no sessions
    assert resp == protocol.err("No available session for 'web'")


def test_attach_releases_reservation_on_bad_winsize(daemon, monkeypatch):
    _stub_config(monkeypatch, find_entry=lambda a, *args, **kw: object())
    daemon.pm.sessions_list = [FakeSession("web", "web-1")]
    s = daemon.pm.sessions_list[0]
    # cols="abc" makes int() raise inside the handler, after attach() reserved s.
    resp = daemon.handle_request({"cmd": protocol.CMD_ATTACH, "alias": "web", "cols": "abc", "rows": 24})
    assert resp["ok"] is False
    assert s.detached and not s.attached  # reservation released, not leaked


def test_resize_found_and_missing(daemon):
    daemon.pm.sessions_list = [FakeSession("web", "web-1")]
    ok = daemon._cmd_resize({"alias": "web", "name": "web-1", "cols": 100, "rows": 30})
    assert ok == {"ok": True, "data": {"resized": [100, 30]}}
    assert daemon.pm.sessions_list[0].winsize == (100, 30)

    miss = daemon._cmd_resize({"alias": "web", "name": "ghost", "cols": 100, "rows": 30})
    assert miss == protocol.err("No session web/ghost")


# --- detach / disconnect ---

def test_detach_enabled_keeps_one_warm(daemon, monkeypatch):
    entry = HostEntry(alias="web", hostname="h", user="root", enabled=True)
    _stub_config(monkeypatch, find_entry=lambda a, *args, **kw: entry)
    daemon.pm.sessions_list = [FakeSession("web", "web-1", attached=True)]
    resp = daemon._cmd_detach({"alias": "web", "name": "web-1"})
    assert resp["data"]["detached"] is True
    assert daemon.pm.sessions_list[0].detached
    assert ("ensure_unattached", "web") in daemon.pm.calls


def test_disconnect(daemon):
    resp = daemon._cmd_disconnect({"alias": "web", "name": "web-1"})
    assert resp == {"ok": True, "data": {"disconnected": True}}
    assert ("disconnect", "web", "web-1") in daemon.pm.calls


# --- port add / remove (incl. -D SOCKS) ---

@pytest.mark.parametrize("direction,rule,expected", [
    ("L", "8080:localhost:80", "L:8080:localhost:80"),
    ("R", "9090:db:90", "R:9090:db:90"),
    ("D", "1080", "D:1080"),
])
def test_port_add(daemon, monkeypatch, direction, rule, expected):
    added = []
    _stub_config(monkeypatch, add_port_forward=lambda alias, pf, *a: added.append(pf.to_str()))
    resp = daemon._cmd_port_add({"alias": "web", "direction": direction, "rule": rule})
    assert resp == {"ok": True, "data": {"added": expected}}
    assert added == [expected]


def test_port_remove(daemon, monkeypatch):
    removed = []
    _stub_config(monkeypatch, remove_port_forward=lambda alias, rule, *a: removed.append(rule))
    resp = daemon._cmd_port_remove({"alias": "web", "rule": "D:1080"})
    assert resp == {"ok": True, "data": {"removed": "D:1080"}}
    assert removed == ["D:1080"]


# --- enable / disable / remove / rename ---

def test_enable_disable(daemon, monkeypatch):
    flags = []
    _stub_config(monkeypatch, set_enabled=lambda alias, val, *a: flags.append((alias, val)))
    assert daemon._cmd_enable({"alias": "web"})["data"] == {"enabled": "web"}
    assert daemon._cmd_disable({"alias": "web"})["data"] == {"disabled": "web"}
    assert flags == [("web", True), ("web", False)]
    assert ("ensure_unattached", "web") in daemon.pm.calls


def test_remove_disconnects_and_removes(daemon, monkeypatch):
    removed = []
    _stub_config(monkeypatch, remove_host=lambda alias, *a: removed.append(alias))
    resp = daemon._cmd_remove({"alias": "web"})
    assert resp["data"] == {"removed": "web"}
    assert ("disconnect_alias", "web") in daemon.pm.calls
    assert removed == ["web"]


def test_rename_ok(daemon, monkeypatch):
    renamed = []
    _stub_config(
        monkeypatch,
        find_entry=lambda a, *args, **kw: object() if a == "old" else None,
        rename_host=lambda o, n, *a: renamed.append((o, n)),
    )
    resp = daemon._cmd_rename({"alias": "old", "new_alias": "new"})
    assert resp["data"] == {"renamed": "new"}
    assert ("disconnect_alias", "old") in daemon.pm.calls
    assert renamed == [("old", "new")]


def test_rename_unknown_and_collision(daemon, monkeypatch):
    _stub_config(monkeypatch, find_entry=lambda a, *args, **kw: None)
    assert daemon._cmd_rename({"alias": "old", "new_alias": "new"}) == protocol.err("Unknown alias: old")

    _stub_config(monkeypatch, find_entry=lambda a, *args, **kw: object())  # both exist
    assert daemon._cmd_rename({"alias": "old", "new_alias": "new"}) == protocol.err(
        "Alias 'new' already exists"
    )


# --- dispatch / validation ---

def test_shutdown_sets_event(daemon):
    assert not daemon._shutdown_event.is_set()
    assert daemon._cmd_shutdown({})["data"] == {"shutting_down": True}
    assert daemon._shutdown_event.is_set()


def test_handle_request_unknown_command(daemon):
    assert daemon.handle_request({"cmd": "bogus"}) == protocol.err("Unknown command: bogus")


def test_handle_request_reports_validation_error(daemon):
    # _required raises ValueError on a missing field; handle_request turns it into err.
    resp = daemon.handle_request({"cmd": protocol.CMD_CONNECT})
    assert resp["ok"] is False and "Missing alias" in resp["error"]


def test_required_helper():
    from sshm.daemon import _required

    assert _required({"a": 1, "b": 2}, "a", "b") == [1, 2]
    with pytest.raises(ValueError):
        _required({"a": 1}, "a", "b")
