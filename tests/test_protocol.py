from sshm import protocol


def test_make_request_includes_token_and_kwargs():
    req = protocol.make_request(protocol.CMD_CONNECT, "tok", alias="web", name=None)
    assert req == {"cmd": "connect", "token": "tok", "alias": "web", "name": None}


def test_response_shapes():
    assert protocol.ok() == {"ok": True}
    assert protocol.ok({"x": 1}) == {"ok": True, "data": {"x": 1}}
    assert protocol.err("boom") == {"ok": False, "error": "boom"}


def test_encode_decode_roundtrip():
    msg = {"cmd": "list", "token": "t", "alias": "тест"}
    data = protocol.encode(msg)
    assert data.endswith(b"\n")
    assert protocol.decode(data) == msg
