import pytest

from yuki.proto import yuki_pb2


def test_envelope_roundtrip_serialize():
    env = yuki_pb2.Envelope(version=1, trace_id="t-1")
    env.request.service = "health/cognition"
    env.request.request_id = "rid-1"
    env.request.payload.update({"process": "cognition"})
    raw = env.SerializeToString()
    parsed = yuki_pb2.Envelope.FromString(raw)
    assert parsed.version == 1
    assert parsed.trace_id == "t-1"
    assert parsed.WhichOneof("body") == "request"
    assert parsed.request.service == "health/cognition"
    assert parsed.request.request_id == "rid-1"
    assert parsed.request.payload["process"] == "cognition"


def test_response_oneof_error_vs_result():
    ok_env = yuki_pb2.Envelope(version=1)
    ok_env.response.request_id = "r1"
    ok_env.response.result.update({"echo": "hi"})
    assert ok_env.WhichOneof("body") == "response"
    assert ok_env.response.HasField("result")
    assert not ok_env.response.HasField("error")

    err_env = yuki_pb2.Envelope(version=1)
    err_env.response.request_id = "r2"
    err_env.response.error = "service not found"
    assert err_env.response.HasField("error")
    assert not err_env.response.HasField("result")


def test_event_oneof():
    env = yuki_pb2.Envelope(version=1)
    env.event.topic = "event/awake"
    env.event.payload.update({"source": "hotkey"})
    assert env.WhichOneof("body") == "event"
    raw = env.SerializeToString()
    parsed = yuki_pb2.Envelope.FromString(raw)
    assert parsed.event.topic == "event/awake"
    assert parsed.event.payload["source"] == "hotkey"


def test_nested_struct_payload():
    env = yuki_pb2.Envelope(version=1)
    env.request.service = "frame"
    env.request.payload.update({"cache": {"url_domain": "x.com", "scroll": 0.5}})
    parsed = yuki_pb2.Envelope.FromString(env.SerializeToString())
    assert parsed.request.payload["cache"]["url_domain"] == "x.com"
    assert abs(parsed.request.payload["cache"]["scroll"] - 0.5) < 1e-6


def test_generated_module_importable():
    assert hasattr(yuki_pb2, "Envelope")
    assert hasattr(yuki_pb2, "Request")
    assert hasattr(yuki_pb2, "Response")
    assert hasattr(yuki_pb2, "Event")
