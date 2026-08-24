import json
import time

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient

from yuki.bus import BUS_HEALTH_SERVICE
from yuki.bus_server.agent import BusServerAgent
from yuki.bus_server.ws_channels import WsChannelSpec
from yuki.bus_server.gateway import (
    COGNITION_CHAT_SERVICE,
    SOUL_GET_SERVICE,
    ChatTaskStore,
    GatewayRuntime,
    create_gateway_app,
)
from yuki.config import Config
from yuki.topics import Topics

from tests.fakes import FakeBus


def _client(config=None, bus=None):
    runtime = GatewayRuntime(config or Config(), bus or FakeBus())
    return runtime, TestClient(create_gateway_app(runtime))


def test_gateway_health_aggregates_hub_and_heartbeats():
    bus = FakeBus()
    bus.respond(BUS_HEALTH_SERVICE, lambda payload: {"healthy": True, "process": "bus_server"})
    runtime, client = _client(bus=bus)

    with client:
        runtime.on_heartbeat(Topics.HEARTBEAT, {"process": "perception", "healthy": True})
        response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["hub"]["healthy"] is True
    assert body["gateway"]["started"] is True
    assert body["processes"]["perception"]["healthy"] is True
    assert "health/gateway" in bus.services


def test_gateway_memory_endpoints_proxy_memory_rpc():
    bus = FakeBus()
    bus.respond("memory/list", lambda payload: {"results": [{"id": 1}]})
    bus.respond("memory/get", lambda payload: {"memory": {"id": payload["id"]}})
    bus.respond("memory/delete", lambda payload: {"deleted": True})
    runtime, client = _client(bus=bus)

    with client:
        assert client.get("/api/memory").json() == {"results": [{"id": 1}]}
        assert client.get("/api/memory/7").json() == {"memory": {"id": 7}}
        assert client.delete("/api/memory/7").json() == {"deleted": True}

    assert runtime._started is False


def test_gateway_chat_rest_uses_cognition_chat_rpc_and_stores_task():
    bus = FakeBus()
    calls = []

    def chat(payload):
        calls.append(payload)
        return {"text": f"reply:{payload['text']}", "ts": 1.0, "spoke": True}

    bus.respond(COGNITION_CHAT_SERVICE, chat)
    runtime, client = _client(bus=bus)

    with client:
        response = client.post("/api/chat", json={"text": "你好", "session_id": "s1"})
        body = response.json()
        task = client.get(f"/api/chat/{body['task_id']}").json()

    assert response.status_code == 200
    assert calls[0]["text"] == "你好"
    assert calls[0]["session_id"] == "s1"
    assert calls[0]["task_id"] == body["task_id"]
    assert body["status"] == "completed"
    assert body["result"]["text"] == "reply:你好"
    assert task == body
    assert not any(topic == Topics.REPLY for topic, _ in bus.published)


def test_gateway_soul_endpoint_proxies_registered_soul_rpc():
    bus = FakeBus()
    bus.respond(SOUL_GET_SERVICE, lambda payload: {"soul": {"persona_name": "yuki"}})
    runtime, client = _client(bus=bus)

    with client:
        response = client.get("/api/soul")

    assert response.status_code == 200
    assert response.json() == {"soul": {"persona_name": "yuki"}}


def test_gateway_ws_chat_wraps_single_rpc_reply_as_done_chunk():
    bus = FakeBus()
    bus.respond(
        COGNITION_CHAT_SERVICE,
        lambda payload: {"text": "pong", "ts": 1.0, "spoke": True},
    )
    runtime, client = _client(bus=bus)

    with client:
        with client.websocket_connect("/ws/chat") as ws:
            ws.send_json({"type": "user_input", "text": "ping", "session_id": "s1"})
            message = ws.receive_json()

    assert message["type"] == "assistant_chunk"
    assert message["text"] == "pong"
    assert message["done"] is True
    assert message["status"] == "completed"


def test_gateway_mounts_injected_custom_channel():
    spec = WsChannelSpec(
        route="/ws/custom",
        channel_name="custom",
        initial_message=lambda runtime: {"type": "custom", "data": "hello"},
    )
    runtime = GatewayRuntime(Config(), FakeBus())

    with TestClient(create_gateway_app(runtime, channels=[spec])) as client:
        with client.websocket_connect("/ws/custom") as ws:
            assert ws.receive_json() == {"type": "custom", "data": "hello"}


def test_gateway_ws_status_pushes_heartbeat_updates():
    bus = FakeBus()
    bus.respond(BUS_HEALTH_SERVICE, lambda payload: {"healthy": True})
    runtime, client = _client(bus=bus)

    with client:
        with client.websocket_connect("/ws/status") as ws:
            initial = ws.receive_json()
            runtime.on_heartbeat(Topics.HEARTBEAT, {"process": "cognition", "healthy": True})
            update = ws.receive_json()

    assert initial["type"] == "health"
    assert update["type"] == "health"
    assert update["data"]["processes"]["cognition"]["healthy"] is True


def test_gateway_heartbeat_broadcast_does_not_request_hub_health():
    class CountingBus(FakeBus):
        def __init__(self):
            super().__init__()
            self.requests = []

        def request(self, service, payload, timeout_ms=2000):
            self.requests.append((service, payload, timeout_ms))
            return super().request(service, payload, timeout_ms=timeout_ms)

    bus = CountingBus()
    runtime = GatewayRuntime(Config(), bus)

    runtime.on_heartbeat(Topics.HEARTBEAT, {"process": "cognition", "healthy": True})

    assert bus.requests == []
    assert runtime.cached_health_snapshot()["processes"]["cognition"]["healthy"] is True


def test_gateway_ws_status_unregisters_queue_on_client_disconnect():
    runtime, client = _client()

    with client:
        with client.websocket_connect("/ws/status") as ws:
            assert ws.receive_json()["type"] == "health"
            assert len(runtime._status_queues) == 1

        deadline = time.monotonic() + 1.0
        while runtime._status_queues and time.monotonic() < deadline:
            time.sleep(0.01)

    assert runtime._status_queues == []


def test_gateway_ws_perception_pushes_focus_and_text_updates():
    runtime, client = _client()

    with client:
        with client.websocket_connect("/ws/perception") as ws:
            assert ws.receive_json()["type"] == "snapshot"
            runtime.on_focus_changed(Topics.FOCUS_CHANGED, {"app": "chrome"})
            focus = ws.receive_json()
            runtime.on_situation_update(
                Topics.SITUATION_UPDATE,
                {"layer": "fast", "summary": "visible text"},
            )
            text = ws.receive_json()

    assert focus == {"type": "foreground", "data": {"app": "chrome"}}
    assert text == {"type": "text_extract", "data": {"layer": "fast", "summary": "visible text"}}


def test_gateway_config_redacts_bus_auth_token():
    config = Config(
        bus={"auth_token": "secret"},
        memory={"db_path": "C:/Users/me/yuki.db"},
        soul={"path": "C:/Users/me/soul.json", "tuner_state_path": "C:/Users/me/tuner.json"},
        gateway={"history_dir": "C:/Users/me/recordings"},
    )
    runtime, client = _client(config=config)

    with client:
        body = client.get("/api/config").json()

    assert body["bus"]["auth_token"] == "<redacted>"
    assert body["memory"]["db_path"] == "<redacted>"
    assert body["soul"]["path"] == "<redacted>"
    assert body["soul"]["tuner_state_path"] == "<redacted>"
    assert body["gateway"]["history_dir"] == "<redacted>"


def test_gateway_errors_use_uniform_shape(tmp_path):
    config = Config(gateway={"history_dir": str(tmp_path)})
    runtime, client = _client(config=config)

    with client:
        response = client.get("/api/history/missing")

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "not_found",
            "message": "history session not found",
            "details": {},
        }
    }


def test_gateway_history_returns_degraded_when_dir_missing(tmp_path):
    config = Config(gateway={"history_dir": str(tmp_path / "missing")})
    runtime, client = _client(config=config)

    with client:
        body = client.get("/api/history/sessions").json()

    assert body == {"degraded": True, "sessions": []}


def test_gateway_history_reads_user_and_assistant_turns(tmp_path):
    session = tmp_path / "sess-1"
    session.mkdir()
    events = [
        {"ts": 1.0, "topic": Topics.USER_UTTERANCE, "payload": {"text": "hi"}},
        {"ts": 2.0, "topic": Topics.REPLY, "payload": {"text": "hello"}},
    ]
    (session / "events.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events),
        encoding="utf-8",
    )
    config = Config(gateway={"history_dir": str(tmp_path)})
    runtime, client = _client(config=config)

    with client:
        sessions = client.get("/api/history/sessions").json()
        history = client.get("/api/history/sess-1").json()

    assert sessions["degraded"] is False
    assert sessions["sessions"][0]["session_id"] == "sess-1"
    assert history["turns"] == [
        {"role": "user", "text": "hi", "ts": 1.0},
        {"role": "assistant", "text": "hello", "ts": 2.0},
    ]


def test_bus_server_agent_starts_and_stops_gateway():
    class FakeGateway:
        def __init__(self):
            self.started = False
            self.stopped = False

        def start(self):
            self.started = True

        def stop(self):
            self.stopped = True

    gateway = FakeGateway()
    agent = BusServerAgent(Config(), bus=FakeBus(), gateway=gateway)

    agent.setup()
    agent.teardown()

    assert gateway.started is True
    assert gateway.stopped is True


def test_bus_server_agent_does_not_import_gateway_when_disabled():
    agent = BusServerAgent(Config(gateway={"enabled": False}), bus=FakeBus())

    agent.setup()
    agent.teardown()

    assert agent._gateway is None


def test_chat_task_store_missing_ids_are_safe():
    store = ChatTaskStore()

    assert store.complete("missing", {"text": "x"}) is None
    assert store.fail("missing", "boom") is None
