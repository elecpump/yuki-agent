import pytest
from google.protobuf.message import DecodeError

from yuki.proto.codec import (
    VERSION,
    build_event,
    build_request,
    build_response_error,
    build_response_result,
    event_payload,
    parse_envelope,
    request_payload,
    response_result,
)


def test_build_request_roundtrip():
    env = build_request("health/cognition", "rid-1", "trace-1", {"process": "cognition", "n": 3})
    raw = env.SerializeToString()
    parsed = parse_envelope(raw)
    assert parsed.version == VERSION
    assert parsed.trace_id == "trace-1"
    assert parsed.WhichOneof("body") == "request"
    assert parsed.request.service == "health/cognition"
    assert parsed.request.request_id == "rid-1"
    assert request_payload(parsed) == {"process": "cognition", "n": 3}


def test_build_response_result_roundtrip():
    env = build_response_result("rid-1", {"echo": "hi", "ok": True})
    parsed = parse_envelope(env.SerializeToString())
    assert parsed.WhichOneof("body") == "response"
    assert parsed.response.HasField("result")
    assert not parsed.response.HasField("error")
    assert response_result(parsed) == {"echo": "hi", "ok": True}


def test_build_response_error():
    env = build_response_error("rid-1", "service not found")
    parsed = parse_envelope(env.SerializeToString())
    assert parsed.response.HasField("error")
    assert parsed.response.error == "service not found"
    assert not parsed.response.HasField("result")


def test_build_event_roundtrip():
    env = build_event("event/awake", {"source": "hotkey", "ts": 123.5})
    parsed = parse_envelope(env.SerializeToString())
    assert parsed.WhichOneof("body") == "event"
    assert parsed.event.topic == "event/awake"
    assert event_payload(parsed) == {"source": "hotkey", "ts": 123.5}


def test_empty_dict_payload_roundtrip():
    env = build_request("svc", "r1", "t1", {})
    parsed = parse_envelope(env.SerializeToString())
    assert request_payload(parsed) == {}


def test_parse_envelope_rejects_garbage():
    with pytest.raises(DecodeError):
        parse_envelope(b"not a protobuf message")


def test_nested_and_list_payload():
    payload = {"cache": {"url_domain": "x.com", "tags": ["a", "b"]}}
    env = build_request("frame", "r1", "t1", payload)
    parsed = parse_envelope(env.SerializeToString())
    assert request_payload(parsed) == payload


def test_int_and_float_roundtrip():
    env = build_request("svc", "r1", "t1", {"a": 1, "b": 1.5})
    parsed = parse_envelope(env.SerializeToString())
    payload = request_payload(parsed)
    assert payload == {"a": 1, "b": 1.5}
    assert type(payload["a"]) is int
    assert type(payload["b"]) is float


def test_nested_list_ints_roundtrip():
    env = build_request("svc", "r1", "t1", {"tags": [1, 2]})
    parsed = parse_envelope(env.SerializeToString())
    tags = request_payload(parsed)["tags"]
    assert tags == [1, 2]
    assert all(type(t) is int for t in tags)


def test_nested_dict_ints_roundtrip():
    env = build_request("svc", "r1", "t1", {"d": {"x": 3}})
    parsed = parse_envelope(env.SerializeToString())
    d = request_payload(parsed)["d"]
    assert d == {"x": 3}
    assert type(d["x"]) is int


def test_negative_int_roundtrip():
    env = build_request("svc", "r1", "t1", {"n": -2})
    parsed = parse_envelope(env.SerializeToString())
    n = request_payload(parsed)["n"]
    assert n == -2
    assert type(n) is int


def test_true_float_preserved():
    env = build_request("svc", "r1", "t1", {"f": 0.5})
    parsed = parse_envelope(env.SerializeToString())
    f = request_payload(parsed)["f"]
    assert f == 0.5
    assert type(f) is float
