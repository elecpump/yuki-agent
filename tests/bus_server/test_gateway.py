import time

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient

from yuki.bus import BUS_HEALTH_SERVICE, BusError, BusTimeoutError
from yuki.bus_server.ws_channels import WsChannelSpec
from yuki.bus_server.gateway import (
    COGNITION_CHAT_SERVICE,
    ChatTaskStore,
    GatewayRuntime,
    create_gateway_app,
)
from yuki.cognition.assembly import (
    COGNITION_VOICE_CANCEL_SERVICE,
    COGNITION_VOICE_START_SERVICE,
    COGNITION_VOICE_STATUS_SERVICE,
)
from yuki.config import Config
from yuki.cognition.local_model_control import LocalModelControlError
from yuki.topics import Topics

from tests.fakes import FakeBus


def _client(config=None, bus=None, local_model_control=None):
    runtime = GatewayRuntime(
        config or Config(),
        bus or FakeBus(),
        local_model_control=local_model_control,
    )
    return runtime, TestClient(create_gateway_app(runtime))


def test_gateway_health_aggregates_hub_and_heartbeats():
    bus = FakeBus()
    bus.respond(BUS_HEALTH_SERVICE, lambda payload: {"healthy": True, "process": "bus_server"})
    runtime, client = _client(bus=bus)

    with client:
        runtime.on_heartbeat(
            Topics.HEARTBEAT,
            {"process": "perception", "healthy": True, "ts": time.time()},
        )
        response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["hub"]["healthy"] is True
    assert body["gateway"]["started"] is True
    assert body["processes"]["perception"]["healthy"] is True
    assert body["processes"]["perception"]["fresh"] is True
    assert body["processes"]["perception"]["last_seen_age_s"] >= 0
    assert "health/gateway" in bus.services


def test_gateway_health_prefers_in_process_hub_snapshot():
    class Hub:
        def health_snapshot(self):
            return {"healthy": True, "process": "bus_server", "source": "in-process"}

    bus = FakeBus()
    bus.respond(
        BUS_HEALTH_SERVICE,
        lambda payload: pytest.fail("local service bus must not query remote hub health"),
    )
    runtime = GatewayRuntime(Config(), bus, hub=Hub())

    snapshot = runtime.health_snapshot()

    assert snapshot["hub"] == {
        "healthy": True,
        "process": "bus_server",
        "source": "in-process",
    }


def test_gateway_desktop_frontend_health_contract_and_windows_cors():
    bus = FakeBus()
    bus.respond(
        BUS_HEALTH_SERVICE,
        lambda payload: {
            "healthy": True,
            "process": "bus_server",
            "components": {"proxy": {"ok": True, "last_forwarded_s": 0.5}},
        },
    )
    runtime, client = _client(bus=bus)

    with client:
        runtime.on_heartbeat(
            Topics.HEARTBEAT,
            {
                "process": "yuki",
                "ts": time.time(),
                "healthy": True,
                "components": {"cognition.brain": {"ok": True, "detail": {}}},
            },
        )
        preflight = client.options(
            "/api/health",
            headers={
                "Origin": "http://tauri.localhost",
                "Access-Control-Request-Method": "GET",
            },
        )
        with client.websocket_connect("/ws/status") as ws:
            initial = ws.receive_json()

    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "http://tauri.localhost"
    assert initial["type"] == "health"
    assert initial["data"]["gateway"]["healthy"] is True
    assert initial["data"]["hub"]["components"]["proxy"]["ok"] is True
    assert initial["data"]["processes"]["yuki"]["fresh"] is True
    assert initial["data"]["processes"]["yuki"]["last_seen_age_s"] >= 0


def test_gateway_exposes_fixed_voice_control_endpoints():
    bus = FakeBus()
    idle = {"available": True, "state": "idle", "session_id": None, "active": False}
    listening = {"available": True, "state": "listening", "session_id": 1, "active": True}
    bus.respond(COGNITION_VOICE_STATUS_SERVICE, lambda payload: idle)
    bus.respond(COGNITION_VOICE_START_SERVICE, lambda payload: listening)
    bus.respond(COGNITION_VOICE_CANCEL_SERVICE, lambda payload: idle)
    _, client = _client(bus=bus)

    with client:
        assert client.get("/api/voice").json() == idle
        assert client.post("/api/voice/listen").json() == listening
        assert client.delete("/api/voice/listen").json() == idle


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (BusError("offline"), 503, "voice_unavailable"),
        (BusTimeoutError("slow"), 504, "voice_timeout"),
    ],
)
def test_gateway_maps_voice_bus_failures(error, status_code, code):
    bus = FakeBus()

    def fail(payload):
        raise error

    bus.respond(COGNITION_VOICE_START_SERVICE, fail)
    _, client = _client(bus=bus)

    with client:
        response = client.post("/api/voice/listen")

    assert response.status_code == status_code
    assert response.json()["error"]["code"] == code


def test_gateway_rejects_start_when_stt_is_disabled():
    bus = FakeBus()
    unavailable = {"available": False, "state": "idle", "session_id": None, "active": False}
    bus.respond(COGNITION_VOICE_START_SERVICE, lambda payload: unavailable)
    _, client = _client(bus=bus)

    with client:
        response = client.post("/api/voice/listen")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "voice_unavailable"


def test_gateway_health_marks_stale_or_invalid_heartbeats_not_fresh():
    runtime, _ = _client(config=Config(health={"heartbeat_interval_s": 5.0}))
    runtime.on_heartbeat(
        Topics.HEARTBEAT,
        {"process": "stale", "healthy": True, "ts": time.time() - 16.0},
    )
    runtime.on_heartbeat(
        Topics.HEARTBEAT,
        {"process": "missing-ts", "healthy": True},
    )

    snapshot = runtime.cached_health_snapshot()

    assert snapshot["processes"]["stale"]["fresh"] is False
    assert snapshot["processes"]["stale"]["last_seen_age_s"] >= 16.0
    assert snapshot["processes"]["missing-ts"]["fresh"] is False
    assert snapshot["processes"]["missing-ts"]["last_seen_age_s"] is None


def test_gateway_does_not_expose_memory_management_endpoints():
    runtime, client = _client(bus=FakeBus())

    with client:
        assert client.get("/api/memory").status_code == 404
        assert client.get("/api/memory/7").status_code == 404
        assert client.delete("/api/memory/7").status_code == 404

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
        response = client.post("/api/chat", json={"text": "你好"})
        body = response.json()
        task = client.get(f"/api/chat/{body['task_id']}").json()

    assert response.status_code == 200
    assert calls[0]["text"] == "你好"
    assert "session_id" not in calls[0]
    assert calls[0]["task_id"] == body["task_id"]
    assert body["status"] == "completed"
    assert body["result"]["text"] == "reply:你好"
    assert task == body
    assert not any(topic == Topics.REPLY for topic, _ in bus.published)


def test_gateway_does_not_expose_soul_endpoint():
    _, client = _client(bus=FakeBus())

    with client:
        response = client.get("/api/soul")

    assert response.status_code == 404


def test_gateway_ws_chat_wraps_single_rpc_reply_as_done_chunk():
    bus = FakeBus()
    bus.respond(
        COGNITION_CHAT_SERVICE,
        lambda payload: {
            "text": "pong",
            "ts": 1.0,
            "spoke": True,
            "reason": "chat_local",
            "emotion": "warm",
        },
    )
    runtime, client = _client(bus=bus)

    with client:
        with client.websocket_connect("/ws/chat") as ws:
            ws.send_json({"type": "user_input", "text": "ping"})
            message = ws.receive_json()

    assert message["type"] == "assistant_chunk"
    assert message["text"] == "pong"
    assert message["done"] is True
    assert message["status"] == "completed"
    assert message["reason"] == "chat_local"
    assert message["ts"] == 1.0
    assert message["spoke"] is True
    assert message["emotion"] == "warm"


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
        soul={"path": "C:/Users/me/soul.json", "cooldown_state_path": "C:/Users/me/cooldown.json"},
    )
    runtime, client = _client(config=config)

    with client:
        body = client.get("/api/config").json()

    assert body["bus"]["auth_token"] == "<redacted>"
    assert body["memory"]["db_path"] == "<redacted>"
    assert body["soul"]["path"] == "<redacted>"
    assert body["soul"]["cooldown_state_path"] == "<redacted>"
    assert "history_dir" not in body["gateway"]


def test_gateway_does_not_expose_recorder_output_as_chat_history():
    _, client = _client()

    with client:
        assert client.get("/api/history/sessions").status_code == 404
        assert client.get("/api/history/recording-id").status_code == 404


def test_gateway_reads_local_model_control_status():
    expected = {
        "available": True,
        "enabled": False,
        "target_enabled": False,
        "state": "disabled",
        "runtime_state": "disabled",
        "loaded": False,
        "active_calls": 0,
        "operation": None,
        "last_error": "",
    }

    class Control:
        def status(self):
            return expected

    _, client = _client(local_model_control=Control())

    with client:
        response = client.get("/api/local-model")

    assert response.status_code == 200
    assert response.json() == expected


def test_gateway_submits_local_model_switch_as_async_operation():
    calls = []

    class Control:
        def set_enabled(self, enabled, idempotency_key):
            calls.append((enabled, idempotency_key))
            return {
                "operation_id": "op-1",
                "accepted": True,
                "target_enabled": enabled,
            }

    _, client = _client(local_model_control=Control())

    with client:
        response = client.put(
            "/api/local-model",
            json={"enabled": False, "idempotency_key": "request-1"},
        )

    assert response.status_code == 202
    assert response.json() == {
        "operation_id": "op-1",
        "accepted": True,
        "target_enabled": False,
    }
    assert calls == [(False, "request-1")]


@pytest.mark.parametrize("extra_field", ["model", "action"])
def test_gateway_rejects_local_model_switch_extra_fields(extra_field):
    class Control:
        def set_enabled(self, enabled, idempotency_key):
            raise AssertionError("validation must reject extra fields before dispatch")

    _, client = _client(local_model_control=Control())

    with client:
        response = client.put(
            "/api/local-model",
            json={
                "enabled": False,
                "idempotency_key": "request-1",
                extra_field: "local_chat",
            },
        )

    assert response.status_code == 422


def test_gateway_reads_local_model_operation_status():
    expected = {
        "operation_id": "op-1",
        "target_enabled": True,
        "state": "recovering",
        "error_code": None,
    }

    class Control:
        def operation_status(self, operation_id):
            assert operation_id == "op-1"
            return expected

    _, client = _client(local_model_control=Control())

    with client:
        response = client.get("/api/local-model/operations/op-1")

    assert response.status_code == 200
    assert response.json() == expected


@pytest.mark.parametrize(
    ("code", "status_code"),
    [
        ("local_model_operation_not_found", 404),
        ("local_model_config_disabled", 409),
        ("local_model_operation_in_progress", 409),
        ("idempotency_key_conflict", 409),
        ("model_worker_unavailable", 503),
        ("model_worker_timeout", 504),
    ],
)
def test_gateway_maps_local_model_control_errors(code, status_code):
    class Control:
        def operation_status(self, operation_id):
            del operation_id
            raise LocalModelControlError(code)

    _, client = _client(local_model_control=Control())

    with client:
        response = client.get("/api/local-model/operations/op-1")

    assert response.status_code == status_code
    assert response.json() == {
        "error": {"code": code, "message": code, "details": {}},
    }


def test_chat_task_store_missing_ids_are_safe():
    store = ChatTaskStore()

    assert store.complete("missing", {"text": "x"}) is None
    assert store.fail("missing", "boom") is None
