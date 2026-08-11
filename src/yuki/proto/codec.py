"""Envelope codec converting dict payloads to/from google.protobuf.Struct.

Number fidelity: Struct stores every number as a double, so integers up to
2**53 round-trip exactly; beyond that precision is lost (e.g. 2**53 + 1 comes
back as 2**53). Integral floats collapse to ints by design (1.0 -> 1) via
``_recover_ints``.
"""

from google.protobuf import json_format
from google.protobuf.struct_pb2 import Struct

from yuki.proto import yuki_pb2

VERSION = 1


def _to_struct(payload: dict) -> Struct:
    struct = Struct()
    struct.update(payload)
    return struct


def _from_struct(struct: Struct) -> dict:
    data = json_format.MessageToDict(struct, preserving_proto_field_name=True)
    return _recover_ints(data)


def _recover_ints(value):
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, list):
        return [_recover_ints(v) for v in value]
    if isinstance(value, dict):
        return {k: _recover_ints(v) for k, v in value.items()}
    return value


def build_request(
    service: str,
    request_id: str,
    trace_id: str,
    payload: dict,
    version: int = VERSION,
) -> yuki_pb2.Envelope:
    env = yuki_pb2.Envelope(version=version, trace_id=trace_id)
    env.request.service = service
    env.request.request_id = request_id
    env.request.payload.CopyFrom(_to_struct(payload))
    return env


def build_response_result(
    request_id: str, result: dict, version: int = VERSION
) -> yuki_pb2.Envelope:
    env = yuki_pb2.Envelope(version=version)
    env.response.request_id = request_id
    env.response.result.CopyFrom(_to_struct(result))
    return env


def build_response_error(
    request_id: str, error: str, version: int = VERSION
) -> yuki_pb2.Envelope:
    env = yuki_pb2.Envelope(version=version)
    env.response.request_id = request_id
    env.response.error = error
    return env


def build_event(topic: str, payload: dict, version: int = VERSION) -> yuki_pb2.Envelope:
    env = yuki_pb2.Envelope(version=version)
    env.event.topic = topic
    env.event.payload.CopyFrom(_to_struct(payload))
    return env


def parse_envelope(raw: bytes) -> yuki_pb2.Envelope:
    env = yuki_pb2.Envelope()
    env.ParseFromString(raw)
    return env


def request_payload(env: yuki_pb2.Envelope) -> dict:
    return _from_struct(env.request.payload)


def response_result(env: yuki_pb2.Envelope) -> dict:
    return _from_struct(env.response.result)


def event_payload(env: yuki_pb2.Envelope) -> dict:
    return _from_struct(env.event.payload)
