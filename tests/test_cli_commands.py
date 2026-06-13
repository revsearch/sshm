"""Exercise the CLI command bodies via CliRunner, with the daemon round-trip
(_send / ensure_daemon / connect_streaming / stream_bridge) mocked out."""

import json

import pytest
from click.testing import CliRunner

from sshm import protocol
from sshm.cli import main


def run(*args):
    return CliRunner().invoke(main, list(args))


class FakeSend:
    def __init__(self):
        self.calls = []
        self.ret = {}

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        return self.ret


@pytest.fixture
def send(monkeypatch):
    f = FakeSend()
    monkeypatch.setattr("sshm.cli._send", f)
    return f


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


# --- simple _send commands ---

def test_remove(send):
    r = run("remove", "web")
    assert r.exit_code == 0 and "Removed 'web'" in r.output
    assert send.calls == [(protocol.CMD_REMOVE, {"alias": "web"})]


def test_enable(send):
    assert "Enabled auto-connect for 'web'" in run("enable", "web").output
    assert send.calls[0] == (protocol.CMD_ENABLE, {"alias": "web"})


def test_disable(send):
    assert "Disabled auto-connect for 'web'" in run("disable", "web").output
    assert send.calls[0] == (protocol.CMD_DISABLE, {"alias": "web"})


def test_status(send):
    send.ret = {"status": "running", "sessions": 3}
    assert "Daemon: running, sessions: 3" in run("status").output


def test_stop(send):
    assert "Daemon stopping" in run("stop").output
    assert send.calls[0][0] == protocol.CMD_SHUTDOWN


def test_rename(send, home):
    r = run("rename", "web", "prod")  # isolated home → find_entry None → not a managed key
    assert r.exit_code == 0 and "Renamed 'web' -> 'prod'" in r.output
    assert send.calls[0] == (protocol.CMD_RENAME, {"alias": "web", "new_alias": "prod"})


# --- list ---

def test_list_hosts(send):
    send.ret = [{
        "alias": "web", "hostname": "1.2.3.4", "user": "root", "port": 2222,
        "enabled": True, "connections": 2, "attached": 1,
        "port_forwards": ["L:8080:localhost:80"],
    }]
    r = run("list")
    assert "web" in r.output and "1.2.3.4:2222" in r.output and "yes" in r.output
    assert send.calls[0] == (protocol.CMD_LIST, {"alias": None})


def test_list_no_hosts(send):
    send.ret = []
    assert "No hosts configured" in run("list").output


def test_list_sessions(send):
    send.ret = [{
        "name": "web-1", "pid": 123, "alive": True, "attached": False,
        "uptime": 65, "port_forwards": ["L:8080:localhost:80"],
    }]
    r = run("list", "web")
    assert "web-1" in r.output and "ready" in r.output and "1m5s" in r.output


def test_list_sessions_empty(send):
    send.ret = []
    assert "No active connections for 'web'" in run("list", "web").output


def test_list_json_file(tmp_path):
    f = tmp_path / "hosts.json"
    f.write_text(json.dumps({"hosts": [
        {"alias": "web", "hostname": "1.2.3.4", "user": "root", "port": 2222, "port_forwards": []}
    ]}), encoding="utf-8")
    r = run("list", str(f))
    assert "web" in r.output and "2222" in r.output


# --- port (prefix resolution + add/remove) ---

def test_port_add_local(send):
    send.ret = {"added": "L:8080:localhost:80"}
    r = run("port", "web", "a", "-L", "8080:80")
    assert "Added port forward: L:8080:localhost:80" in r.output
    assert send.calls[0] == (protocol.CMD_PORT_ADD, {"alias": "web", "direction": "L", "rule": "8080:80"})


def test_port_add_socks(send):
    send.ret = {"added": "D:1080"}
    r = run("port", "web", "a", "-D", "1080")
    assert "Added SOCKS proxy: D:1080 (socks5://127.0.0.1:1080)" in r.output
    assert send.calls[0][1]["direction"] == "D"


def test_port_remove_local(send):
    r = run("port", "web", "r", "-L", "8080:80")
    assert "Removed port forward: L:8080:localhost:80" in r.output
    assert send.calls[0] == (protocol.CMD_PORT_REMOVE, {"alias": "web", "rule": "L:8080:localhost:80"})


def test_port_remove_socks(send):
    r = run("port", "web", "r", "-D", "1080")
    assert "Removed SOCKS proxy: D:1080" in r.output
    assert send.calls[0] == (protocol.CMD_PORT_REMOVE, {"alias": "web", "rule": "D:1080"})


# --- connect (streaming, fully mocked) ---

def test_connect_ok(monkeypatch):
    monkeypatch.setattr("sshm.cli.ensure_daemon", lambda: None)
    monkeypatch.setattr(
        "sshm.cli.connect_streaming",
        lambda *a, **k: (object(), {"ok": True, "data": {"name": "web-1"}}, b""),
    )
    seen = {}
    monkeypatch.setattr("sshm.cli.stream_bridge", lambda sock, initial, on_resize=None: seen.update(initial=initial))
    r = run("connect", "web")
    assert r.exit_code == 0
    assert "Attached to web-1" in r.output and "Detached from web-1" in r.output
    assert seen == {"initial": b""}


def test_connect_error(monkeypatch):
    monkeypatch.setattr("sshm.cli.ensure_daemon", lambda: None)
    monkeypatch.setattr(
        "sshm.cli.connect_streaming",
        lambda *a, **k: (object(), {"ok": False, "error": "boom"}, b""),
    )
    r = run("connect", "web")
    assert r.exit_code == 1 and "boom" in r.output


def test_connect_daemon_not_running(monkeypatch):
    from sshm.ipc import DaemonNotRunning

    def boom():
        raise DaemonNotRunning("not running")

    monkeypatch.setattr("sshm.cli.ensure_daemon", boom)
    r = run("connect", "web")
    assert r.exit_code == 1 and "not running" in r.output


def test_bare_alias_connects(monkeypatch):
    monkeypatch.setattr("sshm.cli.ensure_daemon", lambda: None)
    monkeypatch.setattr(
        "sshm.cli.connect_streaming",
        lambda *a, **k: (object(), {"ok": True, "data": {"name": "web-1"}}, b""),
    )
    monkeypatch.setattr("sshm.cli.stream_bridge", lambda *a, **k: None)
    assert "Attached to web-1" in run("web").output  # bare alias → connect shorthand


# --- add (keygen / copy / test-connection mocked) ---

def test_add(monkeypatch, home):
    (home / ".ssh").mkdir()
    monkeypatch.setattr("sshm.cli._ensure_key", lambda alias: home / ".ssh" / f"sshm_{alias}")
    monkeypatch.setattr("sshm.cli._copy_key_to_remote", lambda *a, **k: None)
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0, "stderr": ""})())

    r = run("add", "web", "root@1.2.3.4:2222")
    assert r.exit_code == 0
    assert "Added 'web' -> root@1.2.3.4:2222" in r.output and "Connection successful!" in r.output

    from sshm.config import find_entry
    e = find_entry("web")
    assert e.hostname == "1.2.3.4" and e.port == 2222


def test_add_duplicate_alias(monkeypatch, home):
    from sshm.config import add_host, ssh_config_path
    add_host("web", "1.1.1.1", "root", 22, None, ssh_config_path())
    r = run("add", "web", "root@2.2.2.2")
    assert r.exit_code == 1 and "already exists" in r.output


# --- export / completions / autostart ---

def test_export(home):
    from sshm.config import add_host, ssh_config_path
    add_host("web", "1.2.3.4", "root", 2222, None, ssh_config_path())
    out = home / "out.json"
    r = run("export", str(out))
    assert r.exit_code == 0 and "Exported 1 host" in r.output
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["hosts"][0]["alias"] == "web" and data["hosts"][0]["port"] == 2222


def test_completions_fish():
    r = run("completions", "fish")
    assert r.exit_code == 0 and "complete -c sshm" in r.output


def test_install(monkeypatch):
    monkeypatch.setattr("sshm.autostart.install_autostart", lambda: "Installed as test service")
    assert "Installed as test service" in run("install").output


def test_uninstall(monkeypatch):
    monkeypatch.setattr("sshm.autostart.uninstall_autostart", lambda: "Removed test service")
    assert "Removed test service" in run("uninstall").output


# --- _send error handling ---

def test_send_exits_when_daemon_not_running(monkeypatch):
    from sshm.cli import _send
    from sshm.ipc import DaemonNotRunning

    def boom():
        raise DaemonNotRunning("no daemon")

    monkeypatch.setattr("sshm.cli.ensure_daemon", boom)
    with pytest.raises(SystemExit):
        _send(protocol.CMD_STATUS)


def test_send_exits_on_error_response(monkeypatch):
    from sshm.cli import _send

    monkeypatch.setattr("sshm.cli.ensure_daemon", lambda: None)
    monkeypatch.setattr("sshm.cli.send_request", lambda *a, **k: {"ok": False, "error": "boom"})
    with pytest.raises(SystemExit):
        _send(protocol.CMD_STATUS)


def test_send_returns_data(monkeypatch):
    from sshm.cli import _send

    monkeypatch.setattr("sshm.cli.ensure_daemon", lambda: None)
    monkeypatch.setattr("sshm.cli.send_request", lambda *a, **k: {"ok": True, "data": {"x": 1}})
    assert _send(protocol.CMD_STATUS) == {"x": 1}


# --- key helpers (subprocess mocked) ---

def test_ensure_key_generates_then_reuses(monkeypatch, home):
    (home / ".ssh").mkdir()
    import subprocess

    calls = []
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: (calls.append(a[0]), type("R", (), {"returncode": 0, "stderr": ""})())[1],
    )
    from sshm.cli import _ensure_key

    kp = _ensure_key("web")
    assert kp == home / ".ssh" / "sshm_web"
    assert any("ssh-keygen" in str(c) for c in calls)  # generated

    kp.write_text("KEY", encoding="utf-8")  # now it exists → reused, no keygen
    calls.clear()
    assert _ensure_key("web") == kp and calls == []


def test_copy_key_to_remote_builds_ssh_command(monkeypatch, home):
    ssh = home / ".ssh"
    ssh.mkdir()
    key = ssh / "sshm_web"
    key.write_text("PRIV", encoding="utf-8")
    (ssh / "sshm_web.pub").write_text("ssh-ed25519 AAAAkey web\n", encoding="utf-8")

    import subprocess

    captured = {}
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, *a, **k: (captured.update(cmd=cmd), type("R", (), {"returncode": 0})())[1],
    )
    from sshm.cli import _copy_key_to_remote

    _copy_key_to_remote(key, "root", "1.2.3.4", 2222)
    cmd = captured["cmd"]
    assert cmd[0] == "ssh" and "root@1.2.3.4" in cmd
    assert "-p" in cmd and "2222" in cmd
    remote = cmd[-1]
    assert "authorized_keys" in remote and "ssh-ed25519 AAAAkey web" in remote
