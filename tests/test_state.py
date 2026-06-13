import pytest

from sshm.state import DEFAULT_PORT, resolve_port, write_port


@pytest.fixture(autouse=True)
def fake_home(monkeypatch, tmp_path):
    # state.sshm_dir() is derived from Path.home(); isolate it per test. HOME is
    # what POSIX uses; Windows' Path.home() reads USERPROFILE, so set both.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("SSHM_PORT", raising=False)
    return tmp_path


def test_resolve_port_default():
    assert resolve_port() == DEFAULT_PORT


def test_resolve_port_env_wins(monkeypatch):
    monkeypatch.setenv("SSHM_PORT", "12345")
    write_port(22222)  # env still takes precedence over the persisted file
    assert resolve_port() == 12345


def test_resolve_port_file_fallback():
    write_port(22222)
    assert resolve_port() == 22222


def test_resolve_port_bad_env_falls_through_to_file():
    write_port(22222)
    import os

    os.environ["SSHM_PORT"] = "notaport"
    try:
        assert resolve_port() == 22222
    finally:
        del os.environ["SSHM_PORT"]


def test_resolve_port_bad_file_falls_through_to_default(fake_home):
    (fake_home / ".sshm").mkdir(parents=True, exist_ok=True)
    (fake_home / ".sshm" / "port").write_text("garbage", encoding="utf-8")
    assert resolve_port() == DEFAULT_PORT
