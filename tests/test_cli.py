import pytest
from click.testing import CliRunner

from sshm.cli import _format_uptime, _parse_port_args, _parse_target, main


def test_help_runs():
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "SSH Session Manager" in result.output
    assert "proxy" in result.output
    assert "rename" in result.output


def test_rename_alias_resolves():
    ctx = main.make_context("sshm", [], resilient_parsing=True)
    assert main.get_command(ctx, "mv").name == "rename"


def test_prefix_resolution():
    group = main
    ctx = main.make_context("sshm", [], resilient_parsing=True)
    for argv, expected in [
        (["po", "a", "web", "-L", "80:80"], "port-add"),
        (["p", "web", "r", "-L", "80:80"], "port-remove"),
        (["po", "a", "web", "-D", "1080"], "port-add"),
        (["port", "web", "rm", "-D", "1080"], "port-remove"),
    ]:
        name, _cmd, _args = group.resolve_command(ctx, argv)
        assert name == expected, f"{argv} -> {name}"


def test_parse_port_args():
    assert _parse_port_args(("-L", "80:h:80")) == ("L", "80:h:80")
    assert _parse_port_args(("-R", "80:h:80")) == ("R", "80:h:80")
    assert _parse_port_args(("-D", "1080")) == ("D", "1080")


@pytest.mark.parametrize("bad", [("-L",), ("-X", "1"), ("-L", "a", "b"), ()])
def test_parse_port_args_invalid(bad):
    with pytest.raises(SystemExit):
        _parse_port_args(bad)


def test_format_uptime():
    assert _format_uptime(5) == "5s"
    assert _format_uptime(65) == "1m5s"
    assert _format_uptime(3700) == "1h1m"
    assert _format_uptime(90000) == "1d1h"


def test_parse_target_basic():
    assert _parse_target("root@host") == ("root", "host", 22)
    assert _parse_target("root@host:2222") == ("root", "host", 2222)


def test_parse_target_ipv6_bracketed():
    assert _parse_target("root@[::1]") == ("root", "::1", 22)
    assert _parse_target("root@[::1]:2222") == ("root", "::1", 2222)
    assert _parse_target("u@[2001:db8::1]:22") == ("u", "2001:db8::1", 22)


def test_parse_target_bare_ipv6_no_port():
    # Multiple colons and no brackets: treat the whole thing as the host.
    assert _parse_target("root@2001:db8::1") == ("root", "2001:db8::1", 22)


@pytest.mark.parametrize(
    "bad",
    ["nohost", "root@host:notaport", "root@host:0", "root@host:99999", "root@[::1", "root@[::1]x"],
)
def test_parse_target_invalid(bad):
    with pytest.raises(SystemExit):
        _parse_target(bad)
