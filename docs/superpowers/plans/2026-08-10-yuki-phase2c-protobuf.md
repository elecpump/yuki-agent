# Yuki Phase 2c：protobuf 消息 schema Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将总线消息从 JSON 编码机械替换为 protobuf（`google.protobuf.Struct` 承载动态 payload），实现问题 10 的"类型安全消息定义 + 生成共享 Python 包 + 统一信封（含 trace_id）+ CI 兼容性检查"。信封字段与帧结构保持不变，handler API（dict 进出）不变——纯编码器替换。

**Architecture:** 新增 `proto/yuki.proto` 定义统一信封 `Envelope`（`oneof` 区分 request/response/event），用 `grpcio-tools` 内置的 `grpc_tools.protoc` 生成 `src/yuki/proto/yuki_pb2.py`（无系统 protoc 依赖）。新增 `yuki/proto/codec.py` 封装 dict↔Struct 与信封构建/解析。`bus.py` 的 `publish/request/respond/_dealer_loop/_router_loop/_run_sub` 六处 JSON 编解码改为 protobuf 序列化，帧结构 `[service, env_bytes]` / `[client_id, env_bytes]` / `[topic, env_bytes]` 不变。`google.protobuf.Struct` 保证动态 payload dict 无损往返，handler 仍收/返 dict，认知/交互/感知/health 层零改动。REGISTER 控制帧保持原始 `["REGISTER", service]` 文本。

**Tech Stack:** Python ≥3.11，protobuf 6.x（已装），grpcio-tools（新增 dev 依赖，codegen），pyzmq（总线）。

**Spec:** `docs/superpowers/specs/2026-08-10-yuki-interfaces.md` §3（信封契约，本计划 Task 4 更新为 protobuf）；设计文档 §11（问题 10）。

## Global Constraints

- 平台：Windows 10/11；语言：Python 为主
- 总线走 localhost（tcp://127.0.0.1），绝不跨机器
- **信封字段不变**：request `{version, trace_id, service, request_id, payload}`；response `{version, request_id, result|error}`；event `{version, topic, payload}`
- **帧结构不变**：REQ `[service, env_bytes]`；RESP 经 hub `[client_identity, env_bytes]`；PUB/SUB `[topic, env_bytes]`；REGISTER 保持 `["REGISTER", service]` 文本控制帧
- **handler API 不变**：`subscribe(topic_prefix, handler(topic, dict))`；`respond(service, handler(dict)->dict)`；`request(service, dict)->dict`——全部 dict 进出，Struct 仅存在于编解码层
- 生成代码 `src/yuki/proto/yuki_pb2.py` 提交入库；`scripts/generate_proto.py` 可重生成
- CI 兼容性检查：测试重生成 proto 与提交版本比对，不一致则失败
- `trace_id` 保留在信封（Request 层生成，dealer_loop 分发前 bind contextvars）
- 每个任务 TDD：先写失败测试 → 跑失败 → 实现 → 跑通 → 提交
- 既有 59 单元 + 1 e2e 必须保持通过
- `grpcio-tools` 仅进 dev 依赖（codegen 用），不进运行时依赖

## 设计决策

1. **信封 oneof 判别**：`Envelope.WhichOneof("body")` 返回 `"request"`/`"response"`/`"event"`，取代原 `"payload" in msg` vs `"result"/"error" in msg` 的字段嗅探——这是 protobuf 相对 JSON 的核心类型安全收益。
2. **payload/result 用 `google.protobuf.Struct`**：protobuf 原生动态结构，`Struct.update(dict)` + `MessageToDict(preserving_proto_field_name=True)` 无损往返 dict。空 dict/嵌套 dict/数组均支持。这保持 handler dict API，实现"机械替换"。
3. **service 路由读信封**：`_router_loop` 解析信封后读 `envelope.request.service` 查 `_service_map`（帧头 service 保留作兼容，不再依赖）。
4. **REGISTER 不裹信封**：`["REGISTER", service]` 是控制握手，无数据载荷，保持文本帧。
5. **error 是 string 字段**：`Response.error` 为 string（"service not found"/"handler error"），result 是 Struct。oneof 保证同一响应不会同时有 result 和 error。

## File Structure

```
proto/yuki.proto                          # 新增：schema 定义
scripts/generate_proto.py                 # 新增：codegen 脚本（grpc_tools.protoc）
src/yuki/proto/__init__.py                # 新增：proto 包
src/yuki/proto/yuki_pb2.py                # 新增：生成代码（提交）
src/yuki/proto/yuki_pb2.pyi               # 新增：生成类型桩（提交）
src/yuki/proto/codec.py                   # 新增：信封构建/解析 + dict↔Struct
src/yuki/bus.py                           # 修改：六处 JSON → protobuf 编码
tests/test_proto_schema.py                # 新增：schema/roundtrip/oneof 测试
tests/test_proto_codec.py                 # 新增：codec 测试
tests/test_bus_faults.py                  # 修改：加 wire-format 为 protobuf 的断言
tests/test_proto_uptodate.py              # 新增：CI 兼容性检查（重生成 diff）
docs/superpowers/specs/2026-08-10-yuki-interfaces.md   # 修改：§3 更新为 protobuf
pyproject.toml                            # 修改：dev 加 grpcio-tools
```

---

### Task 1: proto schema + codegen 管线 + 生成包

**Files:**
- Modify: `pyproject.toml`
- Create: `proto/yuki.proto`
- Create: `scripts/generate_proto.py`
- Create: `src/yuki/proto/__init__.py`
- Create: `src/yuki/proto/yuki_pb2.py`（由脚本生成）
- Create: `src/yuki/proto/yuki_pb2.pyi`（由脚本生成）
- Test: `tests/test_proto_schema.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `yuki.proto.yuki_pb2.Envelope`（含 `version`/`trace_id`/`oneof body`）
  - `yuki.proto.yuki_pb2.Request`（`service`/`request_id`/`payload: Struct`）
  - `yuki.proto.yuki_pb2.Response`（`request_id`/`result: Struct`/`error: str`）
  - `yuki.proto.yuki_pb2.Event`（`topic`/`payload: Struct`）
  - `scripts/generate_proto.py`：`generate() -> None`，无系统 protoc 依赖
  - `test_proto_uptodate` 复用的 `generate_to(out_dir)`（Task 4 使用）

- [ ] **Step 1: 修改 `pyproject.toml` 加 grpcio-tools**

```toml
[project.optional-dependencies]
dev = ["pytest>=8", "Pillow>=10", "grpcio-tools>=1.66"]
```

- [ ] **Step 2: 写失败测试 `tests/test_proto_schema.py`**

```python
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
```

- [ ] **Step 3: 跑测试验证失败**

Run: `python -m pytest tests/test_proto_schema.py -v`
Expected: FAIL，`No module named 'yuki.proto'`（`src/yuki/proto/` 尚不存在）

- [ ] **Step 4: 安装 grpcio-tools**

Run: `python -m pip install -e ".[dev]"`
Expected: `grpcio-tools` 安装成功（含 `grpc_tools.protoc`）

- [ ] **Step 5: 创建 `proto/yuki.proto`**

```proto
syntax = "proto3";

package yuki;

import "google/protobuf/struct.proto";

// 统一信封：总线所有数据消息。
message Envelope {
  uint32 version = 1;
  string trace_id = 2;
  oneof body {
    Request request = 3;
    Response response = 4;
    Event event = 5;
  }
}

// REQ/REP 请求。
message Request {
  string service = 1;
  string request_id = 2;
  google.protobuf.Struct payload = 3;
}

// REQ/REP 响应：result 与 error 互斥。
message Response {
  string request_id = 1;
  google.protobuf.Struct result = 2;
  string error = 3;
}

// PUB/SUB 事件。
message Event {
  string topic = 1;
  google.protobuf.Struct payload = 2;
}
```

- [ ] **Step 6: 创建 `scripts/generate_proto.py`**

```python
"""用 grpc_tools.protoc 重新生成 yuki_pb2.py（无系统 protoc 依赖）。"""

import os
import sys

from grpc_tools import protoc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROTO_DIR = os.path.join(ROOT, "proto")
OUT_DIR = os.path.join(ROOT, "src", "yuki", "proto")
GRPC_PROTO = os.path.join(os.path.dirname(protoc.__file__), "_proto")


def generate_to(out_dir: str = OUT_DIR) -> None:
    os.makedirs(out_dir, exist_ok=True)
    rc = protoc.main(
        [
            "protoc",
            f"-I{PROTO_DIR}",
            f"-I{GRPC_PROTO}",
            f"--python_out={out_dir}",
            f"--pyi_out={out_dir}",
            os.path.join(PROTO_DIR, "yuki.proto"),
        ]
    )
    if rc != 0:
        raise SystemExit(f"protoc failed with code {rc}")


def generate() -> None:
    generate_to(OUT_DIR)
    print(f"generated yuki_pb2.py / yuki_pb2.pyi -> {OUT_DIR}")


if __name__ == "__main__":
    generate()
```

- [ ] **Step 7: 创建 `src/yuki/proto/__init__.py`**

```python
from yuki.proto import yuki_pb2

__all__ = ["yuki_pb2"]
```

- [ ] **Step 8: 运行生成脚本**

Run: `python scripts/generate_proto.py`
Expected: 生成 `src/yuki/proto/yuki_pb2.py` 与 `yuki_pb2.pyi`

- [ ] **Step 9: 跑测试验证通过**

Run: `python -m pytest tests/test_proto_schema.py -v`
Expected: 5 个测试 PASS

- [ ] **Step 10: 提交**

```bash
git add pyproject.toml proto/yuki.proto scripts/generate_proto.py src/yuki/proto tests/test_proto_schema.py
git commit -m "feat: add protobuf message schema and codegen pipeline"
```

---

### Task 2: codec 模块（信封构建/解析 + dict↔Struct）

**Files:**
- Create: `src/yuki/proto/codec.py`
- Test: `tests/test_proto_codec.py`

**Interfaces:**
- Consumes: `yuki_pb2`（Task 1）
- Produces:
  - `VERSION = 1`
  - `build_request(service, request_id, trace_id, payload, version=VERSION) -> Envelope`
  - `build_response_result(request_id, result, version=VERSION) -> Envelope`
  - `build_response_error(request_id, error, version=VERSION) -> Envelope`
  - `build_event(topic, payload, version=VERSION) -> Envelope`
  - `parse_envelope(raw: bytes) -> Envelope`（`DecodeError` 向上抛，由调用方处理）
  - `request_payload(env) -> dict`、`response_result(env) -> dict`、`event_payload(env) -> dict`（Struct→dict）

- [ ] **Step 1: 写失败测试 `tests/test_proto_codec.py`**

```python
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
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/test_proto_codec.py -v`
Expected: FAIL，`No module named 'yuki.proto.codec'`

- [ ] **Step 3: 实现 `src/yuki/proto/codec.py`**

```python
from google.protobuf import json_format
from google.protobuf.struct_pb2 import Struct

from yuki.proto import yuki_pb2

VERSION = 1


def _to_struct(payload: dict) -> Struct:
    struct = Struct()
    struct.update(payload)
    return struct


def _from_struct(struct: Struct) -> dict:
    return json_format.MessageToDict(struct, preserving_proto_field_name=True)


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
```

- [ ] **Step 4: 跑测试验证通过**

Run: `python -m pytest tests/test_proto_codec.py -v`
Expected: 7 个测试 PASS

- [ ] **Step 5: 回归 + 提交**

Run: `python -m pytest -v`
Expected: 全部 PASS
```bash
git add src/yuki/proto/codec.py tests/test_proto_codec.py
git commit -m "feat: add envelope codec with dict-struct conversion"
```

---

### Task 3: bus.py 编码器替换（JSON → protobuf）

**Files:**
- Modify: `src/yuki/bus.py`
- Modify: `tests/test_bus_faults.py`
- Test: 上述文件 + 既有 bus 测试

**Interfaces:**
- Consumes: `codec`（Task 2）、`yuki_pb2`（Task 1）
- Produces: `MessageBus` 六处编解码改为 protobuf；公共 API（`publish/subscribe/request/respond`）签名与 dict 语义不变；`_router_loop` 改用 `WhichOneof` 判别 + `envelope.request.service` 路由；REGISTER 帧不变

- [ ] **Step 1: 追加失败测试 `tests/test_bus_faults.py`（wire-format 断言）**

```python
import zmq

from yuki.proto import yuki_pb2


def test_bus_wire_format_is_protobuf(make_bus):
    # 捕获总线上的原始帧，断言第二帧可被解析为 Envelope（而非 JSON dict）。
    bus = make_bus(6900)
    node = make_bus(6900, role="node")
    node.respond("ping", lambda p: {"echo": p["msg"]})
    time.sleep(0.2)

    captured = {}

    def on_awake(topic, payload):
        captured["payload"] = payload

    bus.subscribe("event/awake", on_awake)
    time.sleep(0.2)
    node.publish("event/awake", {"source": "hotkey"})
    deadline = time.time() + 2.0
    while "payload" not in captured and time.time() < deadline:
        time.sleep(0.05)
    assert captured.get("payload") == {"source": "hotkey"}
```

注意：该测试验证 dict 语义经 protobuf 编码后仍无损（通过既有 publish/subscribe API）。**真正的 wire-format 断言**（直接读 socket 帧）在 Step 2 的独立测试中给出——若实现遵循计划（帧结构不变 + Envelope 序列化），该高层测试已足够；Step 2 测试则钉死"帧第二元素是 Envelope bytes"。

- [ ] **Step 2: 写 wire-format 直接断言测试（追加同文件）**

```python
def test_wire_frame_second_part_is_envelope(make_bus):
    bus = make_bus(6901)
    node = make_bus(6901, role="node")

    # 直接监听 hub 的 ROUTER 端口，抓取 DEALER 发来的原始帧
    ctx = zmq.Context.instance()
    probe = ctx.socket(zmq.ROUTER)
    probe.bind(f"tcp://127.0.0.1:6903")  # base_port+2

    node.respond("ping", lambda p: {"echo": p["msg"]})
    time.sleep(0.2)
    bus.request("ping", {"msg": "hi"}, timeout_ms=1000)

    deadline = time.time() + 2.0
    while time.time() < deadline:
        try:
            frames = probe.recv_multipart(flags=zmq.NOBLOCK)
        except zmq.ZMQError:
            time.sleep(0.05)
            continue
        for frame in frames:
            try:
                parsed = yuki_pb2.Envelope.FromString(frame)
            except Exception:
                continue
            if parsed.WhichOneof("body") == "request" and parsed.request.service == "ping":
                assert parsed.request.request_id != ""
                assert parsed.request.payload["msg"] == "hi"
                return
    pytest.fail("did not observe a protobuf request envelope on the wire")
```

- [ ] **Step 3: 跑测试验证失败**

Run: `python -m pytest tests/test_bus_faults.py::test_wire_frame_second_part_is_envelope -v`
Expected: FAIL（当前帧第二元素是 JSON bytes，`Envelope.FromString` 抛异常或解析出空 envelope）

- [ ] **Step 4: 修改 `src/yuki/bus.py` 六处编解码**

**4a. imports**（`import json` 删除，`import logging` 保留，新增 codec 导入）：

```python
import logging
import threading
import uuid
from typing import Callable

import zmq
from google.protobuf.message import DecodeError

from yuki.logger import bind_trace_id, get_logger, unbind_trace_id
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

logger = get_logger("yuki.bus")
```

**4b. `_router_loop`**（用 oneof 判别 + 信封路由）：

```python
    def _router_loop(self) -> None:
        while not self._stop.is_set():
            if not self._router.poll(100):
                continue
            try:
                frames = self._router.recv_multipart()
            except zmq.ZMQError:
                return
            if len(frames) < 3:
                continue
            sender = frames[0]
            if frames[1] == b"REGISTER":
                self._service_map[frames[2].decode()] = sender
                continue
            f1, f2 = frames[1], frames[2]
            try:
                envelope = parse_envelope(f2)
            except DecodeError:
                logger.warning("dropping malformed envelope from %s", sender)
                continue
            kind = envelope.WhichOneof("body")
            if kind == "request":
                provider = self._service_map.get(envelope.request.service, "")
                if not provider:
                    err = build_response_error(envelope.request.request_id, "service not found")
                    self._router.send_multipart([sender, err.SerializeToString()])
                else:
                    self._router.send_multipart([provider, sender, f2])
            elif kind == "response":
                self._router.send_multipart([f1, f2])
        self._close_socket(self._router)
```

**4c. `_dealer_loop`**（解析信封，handler 收/返 dict）：

```python
    def _dealer_loop(self) -> None:
        while not self._stop.is_set():
            if not self._dealer.poll(100):
                continue
            try:
                frames = self._dealer.recv_multipart()
            except zmq.ZMQError:
                return
            if len(frames) == 2:
                client_id, raw = frames
                try:
                    envelope = parse_envelope(raw)
                except DecodeError:
                    continue
                if envelope.trace_id:
                    bind_trace_id(envelope.trace_id)
                if envelope.WhichOneof("body") != "request":
                    if envelope.trace_id:
                        unbind_trace_id()
                    continue
                handler = self._services.get(envelope.request.service)
                if handler is None:
                    reply = build_response_error(envelope.request.request_id, "service not found")
                else:
                    try:
                        result = handler(request_payload(envelope))
                        reply = build_response_result(envelope.request.request_id, result)
                    except Exception:
                        logger.error("responder handler failed", service=envelope.request.service)
                        self._error_count += 1
                        reply = build_response_error(envelope.request.request_id, "handler error")
                self._dealer.send_multipart([client_id, reply.SerializeToString()])
                if envelope.trace_id:
                    unbind_trace_id()
            elif len(frames) == 1:
                try:
                    envelope = parse_envelope(frames[0])
                except DecodeError:
                    continue
                if envelope.WhichOneof("body") != "response":
                    continue
                rid = envelope.response.request_id
                with self._lock:
                    entry = self._pending.get(rid)
                if entry:
                    entry["result"] = envelope
                    entry["event"].set()
        self._close_socket(self._dealer)
```

**4d. `publish`**（信封化 Event）：

```python
    def publish(self, topic: str, payload: dict) -> None:
        envelope = build_event(topic, payload)
        self._pub.send_multipart([topic.encode(), envelope.SerializeToString()])
```

**4e. `request`**（构建 Request 信封；`_pending` 存 Envelope，返回时解出 dict/error）：

```python
    def request(self, service: str, payload: dict, timeout_ms: int = 2000) -> dict:
        rid = uuid.uuid4().hex
        event = threading.Event()
        trace_id = uuid.uuid4().hex
        envelope = build_request(service, rid, trace_id, payload)
        with self._lock:
            self._pending[rid] = {"event": event, "result": None}
        self._dealer.send_multipart([service.encode(), envelope.SerializeToString()])
        if not event.wait(timeout_ms / 1000.0):
            with self._lock:
                self._pending.pop(rid, None)
            raise BusTimeoutError(f"request to {service!r} timed out after {timeout_ms}ms")
        with self._lock:
            resp = self._pending.pop(rid)["result"]
        if resp.response.HasField("error"):
            raise BusError(resp.response.error)
        return response_result(resp)
```

**4f. `_run_sub`**（解析 Event 信封，payload 转 dict 分发）：

```python
    def _run_sub(self) -> None:
        while not self._stop.is_set():
            if not self._sub.poll(100):
                continue
            try:
                raw_topic, raw_payload = self._sub.recv_multipart()
            except zmq.ZMQError:
                return
            topic = raw_topic.decode()
            try:
                envelope = parse_envelope(raw_payload)
            except DecodeError:
                logger.warning("dropping malformed message", topic=topic)
                continue
            if envelope.WhichOneof("body") != "event":
                logger.warning("dropping non-event envelope", topic=topic)
                continue
            payload = event_payload(envelope)
            with self._lock:
                matching = [
                    h
                    for prefix, handlers in self._handlers.items()
                    if _matches(prefix, topic)
                    for h in handlers
                ]
            for handler in matching:
                try:
                    handler(topic, payload)
                except Exception:
                    logger.error("subscriber handler failed", topic=topic)
                    self._error_count += 1
        self._close_socket(getattr(self, "_sub", None))
```

**4g. `respond`**（不变——`_services` 注册 + REGISTER 帧）：

```python
    def respond(self, service: str, handler: Callable[[dict], dict]) -> None:
        self._services[service] = handler
        self._dealer.send_multipart([b"REGISTER", service.encode()])
```

**4h. `VERSION` 常量**：模块级 `VERSION = 1` 删除（改从 codec 导入）；若其他模块引用 `yuki.bus.VERSION` 则保持导出（grep 确认，若无人引用则直接移除）。

- [ ] **Step 5: 跑测试验证通过**

Run: `python -m pytest tests/test_bus_faults.py tests/test_bus.py -v`
Expected: 全部 PASS（含新 wire-format 测试）

- [ ] **Step 6: 全量回归**

Run: `python -m pytest -v`
Expected: 全部 PASS（cognition/interaction/perception/health 层零改动，因 handler API 是 dict）

- [ ] **Step 7: e2e 回归**

Run: `python -m pytest -m e2e -v`
Expected: PASS（hotkey → awake → reply 闭环经 protobuf 编码仍工作）

- [ ] **Step 8: 提交**

```bash
git add src/yuki/bus.py tests/test_bus_faults.py
git commit -m "refactor: encode bus messages with protobuf envelopes"
```

---

### Task 4: CI 兼容性检查 + 接口文档更新

**Files:**
- Create: `tests/test_proto_uptodate.py`
- Modify: `docs/superpowers/specs/2026-08-10-yuki-interfaces.md`
- Test: `tests/test_proto_uptodate.py`

**Interfaces:**
- Consumes: `generate_to`（Task 1 的 scripts/generate_proto.py）
- Produces: CI 检查——重生成到临时目录与提交的 `src/yuki/proto/yuki_pb2.py` 比对，不一致则失败；接口文档 §3 更新为 protobuf 信封

- [ ] **Step 1: 写失败测试 `tests/test_proto_uptodate.py`**

```python
import filecmp
import tempfile
from pathlib import Path

from scripts.generate_proto import generate_to

COMMITTED_DIR = Path("src/yuki/proto")


def test_generated_proto_is_uptodate():
    with tempfile.TemporaryDirectory() as tmp:
        generate_to(tmp)
        assert filecmp.cmp(
            Path(tmp) / "yuki_pb2.py",
            COMMITTED_DIR / "yuki_pb2.py",
            shallow=False,
        ), "yuki_pb2.py is out of date — run `python scripts/generate_proto.py`"


def test_generated_pyi_is_uptodate():
    with tempfile.TemporaryDirectory() as tmp:
        generate_to(tmp)
        assert filecmp.cmp(
            Path(tmp) / "yuki_pb2.pyi",
            COMMITTED_DIR / "yuki_pb2.pyi",
            shallow=False,
        ), "yuki_pb2.pyi is out of date — run `python scripts/generate_proto.py`"
```

注意：若 `scripts/` 不是包导致 `from scripts.generate_proto import generate_to` 失败，改为在测试内用 `importlib.util.spec_from_file_location` 加载脚本，或加 `scripts/__init__.py`。以实现通过为准。

- [ ] **Step 2: 跑测试验证通过**

Run: `python -m pytest tests/test_proto_uptodate.py -v`
Expected: 2 个测试 PASS（刚生成的版本应一致）

- [ ] **Step 3: 验证"不同即失败"（可选验证）**

在临时目录跑 `python scripts/generate_proto.py` 的变体或手动改一行 `yuki_pb2.py` 后跑测试确认失败，再 `git checkout` 还原。若不便则跳过此步，逻辑由 filecmp 保证。

- [ ] **Step 4: 更新 `docs/superpowers/specs/2026-08-10-yuki-interfaces.md`**

§3 统一信封改为：

```markdown
## 3. 统一信封（protobuf）

所有总线消息为序列化后的 `Envelope`（`proto/yuki.proto`，生成 `yuki/proto/yuki_pb2.py`）。
`Envelope` 用 oneof 判别消息类型：`request` / `response` / `event`。
动态载荷用 `google.protobuf.Struct` 承载，handler 层仍以 dict 出入。

- 生成：`python scripts/generate_proto.py`（grpcio-tools，无系统 protoc）
- CI 兼容性检查：`tests/test_proto_uptodate.py` 重生成比对，不一致即失败
- 信封字段：`version`(uint32)、`trace_id`(string)

### PUB/SUB
- 帧：`[topic, Envelope(event)]`，`event={topic, payload:Struct}`
- 订阅：单 SUB 套接字多 SUBSCRIBE；同前缀多 handler 并存；重叠前缀均触发

### ROUTER/DEALER（REQ/REP）
- 注册：`["REGISTER", service]`（文本控制帧，不裹信封）
- 请求：`[service, Envelope(request)]`，`request={service, request_id, payload:Struct}`
- 响应：`[client_identity, Envelope(response)]`，`response={request_id, result:Struct|error:string}`（互斥）
- 服务未注册：hub 直回 `Envelope(response).error="service not found"`
- 响应方 handler 异常：`Envelope(response).error="handler error"`，error_count 递增
- 同一服务单提供者，后注册者胜出
- 默认超时 2000ms → BusTimeoutError；error → BusError
```

文档顶部状态行更新：`Phase 2c 已将 JSON 编码替换为 protobuf（信封字段不变）`。

- [ ] **Step 5: 提交**

```bash
git add tests/test_proto_uptodate.py docs/superpowers/specs/2026-08-10-yuki-interfaces.md
git commit -m "test: CI check regenerated protobuf stays in sync; docs: update interface contract"
```

---

## Self-Review

**1. Spec coverage：**
- 问题 10 四项（schema/codegen/共享包/CI 检查）→ Task 1/4
- 统一信封含 trace_id → Task 1/2（`trace_id` 在 Envelope，dealer_loop bind contextvars）
- 机械替换（信封字段/帧结构/handler API 不变）→ Task 3 设计决策与 Step 4 各节
- audio/mic 与 frame/request 仍只定型契约，实际服务载荷属 Phase 2b（采集层）——本阶段仅做传输层编码替换，不新增服务类型

**2. Placeholder 扫描：** 无 TBD/TODO。Task 4 Step 3 是可选验证（filecmp 逻辑已保证），已标注。

**3. Type consistency：**
- `yuki_pb2.Envelope`（Task 1）被 Task 2/3 引用，字段名（version/trace_id/request.service/request.request_id/response.error/event.topic）跨任务一致
- `codec.build_request/service/request_id/trace_id/payload` 签名（Task 2）被 bus.request（Task 3）与 health（不变）使用，一致
- `_router_loop` 用 `envelope.request.service` 路由（Task 3）与 `_service_map`（既有）键一致
- `parse_envelope`/`DecodeError`（Task 2）被 Task 3 六处引用，一致

**关键取舍：**
- Struct 保 dict API，避免 handler 层改动（机械替换承诺）；per-service 类型化载荷留待引入 frame/audio 服务时再加
- REGISTER 保持文本控制帧（无载荷、简单）
- 既有测试因 handler API 不变应全部通过；新增 wire-format 测试钉死"帧第二元素是 Envelope"
- grpcio-tools 仅 dev 依赖，运行时只依赖 protobuf（pyzmq 已带）
