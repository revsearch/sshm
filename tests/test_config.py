from pathlib import Path

import pytest

from sshm.config import (
    PortForward,
    add_host,
    add_port_forward,
    find_entry,
    load_entries,
    parse_ssh_config,
    remove_host,
    remove_port_forward,
    rename_host,
    set_enabled,
)


@pytest.fixture
def cfg(tmp_path: Path) -> Path:
    return tmp_path / "config"


# --- PortForward ---

def test_portforward_from_str():
    pf = PortForward.from_str("L:8080:db:5432")
    assert (pf.direction, pf.local_port, pf.remote_host, pf.remote_port) == ("L", 8080, "db", 5432)
    assert PortForward.from_str("R:9000:90").remote_host == "localhost"


@pytest.mark.parametrize("bad", ["X:1:2", "8080", "L:1:2:3:4", "L:"])
def test_portforward_from_str_invalid(bad):
    with pytest.raises(ValueError):
        PortForward.from_str(bad)


def test_portforward_parse_rule():
    assert PortForward.parse_rule("8080:80", "L").to_str() == "L:8080:localhost:80"
    assert PortForward.parse_rule("8080:db:80", "R").to_str() == "R:8080:db:80"
    with pytest.raises(ValueError):
        PortForward.parse_rule("1:2:3:4", "L")


def test_portforward_from_config():
    pf = PortForward.from_config("L", "8080 localhost:80")
    assert pf.to_config_line().strip() == "LocalForward 8080 localhost:80"
    assert PortForward.from_config("R", "9090 db:90").to_str() == "R:9090:db:90"
    with pytest.raises(ValueError):
        PortForward.from_config("L", "8080")


def test_socks_forward():
    pf = PortForward.socks(1080)
    assert pf.to_str() == "D:1080"
    assert pf.to_config_line().strip() == "DynamicForward 1080"
    assert PortForward.from_str("D:1080") == pf
    assert PortForward.from_config("D", "1080") == pf
    with pytest.raises(ValueError):
        PortForward.from_str("D:")
    with pytest.raises(ValueError):
        PortForward.from_str("D:1:2")
    with pytest.raises(ValueError):
        PortForward.from_config("D", "localhost:1080")  # bind addr → kept as raw option


# --- parse / write ---

def test_parse_missing_file(cfg):
    preamble, entries = parse_ssh_config(cfg)
    assert preamble == []
    assert entries == []


def test_add_and_find(cfg):
    add_host("web", "1.2.3.4", "root", 2222, "~/.ssh/sshm_web", cfg)
    e = find_entry("web", cfg)
    assert e is not None
    assert e.hostname == "1.2.3.4"
    assert e.user == "root"
    assert e.port == 2222
    assert e.identity_file == "~/.ssh/sshm_web"
    assert find_entry("nope", cfg) is None


def test_add_duplicate_raises(cfg):
    add_host("web", "1.2.3.4", "root", 22, None, cfg)
    with pytest.raises(ValueError):
        add_host("web", "5.6.7.8", "root", 22, None, cfg)


def test_remove_host(cfg):
    add_host("web", "1.2.3.4", "root", 22, None, cfg)
    add_host("db", "5.6.7.8", "admin", 22, None, cfg)
    remove_host("web", cfg)
    assert find_entry("web", cfg) is None
    assert find_entry("db", cfg) is not None


def test_rename_host(cfg):
    add_host("web", "1.2.3.4", "root", 2222, "~/.ssh/sshm_web", cfg)
    rename_host("web", "prod", cfg)
    assert find_entry("web", cfg) is None
    e = find_entry("prod", cfg)
    assert e is not None
    assert e.hostname == "1.2.3.4"
    assert e.port == 2222
    # the managed key reference follows the rename
    assert e.identity_file == "~/.ssh/sshm_prod"


def test_rename_host_preserves_forwards_and_enabled(cfg):
    add_host("web", "1.2.3.4", "root", 22, None, cfg)
    add_port_forward("web", PortForward("L", 8080, "localhost", 80), cfg)
    set_enabled("web", True, cfg)
    rename_host("web", "prod", cfg)

    e = find_entry("prod", cfg)
    assert e.enabled
    assert [pf.to_str() for pf in e.port_forwards] == ["L:8080:localhost:80"]


def test_rename_host_keeps_unmanaged_identity(cfg):
    add_host("web", "1.2.3.4", "root", 22, "~/.ssh/custom_key", cfg)
    rename_host("web", "prod", cfg)
    assert find_entry("prod", cfg).identity_file == "~/.ssh/custom_key"


def test_multiple_identity_files_preserved_on_regeneration(cfg):
    cfg.write_text(
        "Host web\n"
        "    HostName 1.2.3.4\n"
        "    User root\n"
        "    IdentityFile ~/.ssh/a\n"
        "    IdentityFile ~/.ssh/b\n",
        encoding="utf-8",
    )
    # rename clears _raw_lines and regenerates the block from parsed fields.
    rename_host("web", "prod", cfg)
    text = cfg.read_text(encoding="utf-8")
    assert "IdentityFile ~/.ssh/a" in text
    assert "IdentityFile ~/.ssh/b" in text
    assert find_entry("prod", cfg).identity_file == "~/.ssh/a"  # first is primary


def test_rename_host_to_existing_raises(cfg):
    add_host("web", "1.2.3.4", "root", 22, None, cfg)
    add_host("db", "5.6.7.8", "root", 22, None, cfg)
    with pytest.raises(ValueError):
        rename_host("web", "db", cfg)


@pytest.mark.parametrize("old,new", [("nope", "x"), ("web", "web")])
def test_rename_host_invalid_raises(cfg, old, new):
    add_host("web", "1.2.3.4", "root", 22, None, cfg)
    with pytest.raises(ValueError):
        rename_host(old, new, cfg)


def test_enabled_roundtrip(cfg):
    add_host("web", "1.2.3.4", "root", 22, None, cfg)
    set_enabled("web", True, cfg)
    assert "# sshm:enabled=true" in cfg.read_text(encoding="utf-8")
    assert find_entry("web", cfg).enabled

    set_enabled("web", False, cfg)
    assert not find_entry("web", cfg).enabled


def test_set_enabled_unknown_raises(cfg):
    with pytest.raises(ValueError):
        set_enabled("nope", True, cfg)


def test_port_forwards_written_as_native_directives(cfg):
    add_host("web", "1.2.3.4", "root", 22, None, cfg)
    add_port_forward("web", PortForward("L", 8080, "localhost", 80), cfg)
    add_port_forward("web", PortForward("R", 9090, "localhost", 90), cfg)
    add_port_forward("web", PortForward.socks(1080), cfg)

    text = cfg.read_text(encoding="utf-8")
    assert "LocalForward 8080 localhost:80" in text
    assert "RemoteForward 9090 localhost:90" in text
    assert "DynamicForward 1080" in text

    e = find_entry("web", cfg)
    assert [pf.to_str() for pf in e.port_forwards] == [
        "L:8080:localhost:80",
        "R:9090:localhost:90",
        "D:1080",
    ]

    remove_port_forward("web", "D:1080", cfg)
    assert "DynamicForward" not in cfg.read_text(encoding="utf-8")


def test_duplicate_forward_raises(cfg):
    add_host("web", "1.2.3.4", "root", 22, None, cfg)
    pf = PortForward("L", 8080, "localhost", 80)
    add_port_forward("web", pf, cfg)
    with pytest.raises(ValueError):
        add_port_forward("web", pf, cfg)


def test_remove_forward(cfg):
    add_host("web", "1.2.3.4", "root", 22, None, cfg)
    add_port_forward("web", PortForward("L", 8080, "localhost", 80), cfg)
    remove_port_forward("web", "L:8080:localhost:80", cfg)
    assert find_entry("web", cfg).port_forwards == []


def test_foreign_hosts_and_preamble_preserved(cfg):
    cfg.write_text(
        "# my preamble\n"
        "\n"
        "Host other\n"
        "    HostName example.com\n"
        "    ProxyJump bastion\n",
        encoding="utf-8",
    )
    add_host("web", "1.2.3.4", "root", 22, None, cfg)

    text = cfg.read_text(encoding="utf-8")
    assert "# my preamble" in text
    assert "ProxyJump bastion" in text
    assert [e.alias for e in load_entries(cfg)] == ["other", "web"]

    other = find_entry("other", cfg)
    assert other.extra_options == [("ProxyJump", "bastion")]


def test_backup_created_on_rewrite(cfg):
    add_host("a", "1.1.1.1", "root", 22, None, cfg)
    add_host("b", "2.2.2.2", "root", 22, None, cfg)
    assert cfg.with_name(cfg.name + ".bak").exists()


def test_malformed_port_does_not_crash_parse(cfg):
    cfg.write_text(
        "Host web\n    HostName 1.2.3.4\n    User root\n    Port not-a-number\n",
        encoding="utf-8",
    )
    e = find_entry("web", cfg)  # must not raise
    assert e is not None and e.port == 22  # default kept
    assert ("Port", "not-a-number") in e.extra_options
    # and it survives a rewrite verbatim
    add_host("db", "5.6.7.8", "root", 22, None, cfg)
    assert "Port not-a-number" in cfg.read_text(encoding="utf-8")


def test_non_utf8_config_does_not_crash_and_roundtrips(cfg):
    cfg.write_bytes(
        b"# caf\xe9 (latin-1)\nHost web\n    HostName 1.2.3.4\n    User root\n"
    )
    e = find_entry("web", cfg)  # must not raise UnicodeDecodeError
    assert e is not None and e.hostname == "1.2.3.4"
    add_host("db", "5.6.7.8", "root", 22, None, cfg)  # rewrite
    assert b"caf\xe9" in cfg.read_bytes()  # non-UTF-8 byte preserved


def test_write_through_symlinked_config(tmp_path):
    real = tmp_path / "real_config"
    link = tmp_path / "config"
    real.write_text("", encoding="utf-8")
    link.symlink_to(real)

    add_host("web", "1.2.3.4", "root", 22, None, link)

    assert link.is_symlink()  # the link is preserved, not replaced by a file
    assert link.resolve() == real
    assert "Host web" in real.read_text(encoding="utf-8")  # the real target was updated


def test_regenerated_stanza_omits_empty_hostname_and_user(cfg):
    add_host("web", "", "", 22, None, cfg)  # no hostname/user
    # rename forces regeneration (_raw_lines cleared)
    rename_host("web", "prod", cfg)
    text = cfg.read_text(encoding="utf-8")
    assert "HostName" not in text and "User" not in text  # no empty directives emitted
    assert "Host prod" in text
