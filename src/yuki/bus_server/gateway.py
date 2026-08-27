import asyncio
import json
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from yuki.bus import BUS_HEALTH_SERVICE, BusNode
from yuki.bus_server.ws_channels import (
    WsChannelSpec,
    create_ws_handler,
    register_ws_channel,
    ws_channels,
)
from yuki.cognition.brain.hub import COGNITION_CHAT_SERVICE, SOUL_GET_SERVICE
from yuki.config import Config
from yuki.topics import Topics


class ChatRequest(BaseModel):
    text: str
    session_id: str = "default"


class ChatTaskStore:
    def __init__(self) -> None:
        self._tasks: dict[str, dict] = {}
        self._lock = threading.Lock()

    def create(self) -> str:
        task_id = uuid.uuid4().hex
        with self._lock:
            self._tasks[task_id] = {
                "task_id": task_id,
                "status": "pending",
                "created_at": time.time(),
                "result": None,
                "error": "",
            }
        return task_id

    def complete(self, task_id: str, result: dict) -> dict | None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            task["status"] = "completed"
            task["result"] = dict(result)
            return dict(task)

    def fail(self, task_id: str, error: str) -> dict | None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            task["status"] = "failed"
            task["error"] = error
            return dict(task)

    def cancel_requested(self, task_id: str) -> dict | None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            if task["status"] == "pending":
                task["status"] = "cancel_requested"
            return dict(task)

    def get(self, task_id: str) -> dict | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return dict(task) if task is not None else None


class ConnectionManager:
    def __init__(self, *, cleanup_interval_s: float, heartbeat_timeout_s: float) -> None:
        self._cleanup_interval_s = max(1.0, float(cleanup_interval_s))
        self._heartbeat_timeout_s = max(1.0, float(heartbeat_timeout_s))
        self._connections: dict[str, dict] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._cleanup_loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        async with self._lock:
            self._connections.clear()

    async def register(self, websocket: WebSocket, channel: str) -> str:
        connection_id = uuid.uuid4().hex
        async with self._lock:
            self._connections[connection_id] = {
                "websocket": websocket,
                "channel": channel,
                "last_seen": time.monotonic(),
            }
        return connection_id

    async def unregister(self, connection_id: str) -> None:
        async with self._lock:
            self._connections.pop(connection_id, None)

    async def touch(self, connection_id: str) -> None:
        async with self._lock:
            if connection_id in self._connections:
                self._connections[connection_id]["last_seen"] = time.monotonic()

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(self._cleanup_interval_s)
            await self._ping_and_prune()

    async def _ping_and_prune(self) -> None:
        now = time.monotonic()
        async with self._lock:
            items = list(self._connections.items())
        stale: list[str] = []
        for connection_id, entry in items:
            if now - float(entry["last_seen"]) > self._heartbeat_timeout_s:
                stale.append(connection_id)
                continue
            websocket = entry["websocket"]
            try:
                await websocket.send_json({
                    "type": "ping",
                    "channel": entry["channel"],
                    "ts": time.time(),
                })
            except Exception:
                stale.append(connection_id)
                continue
            await self.touch(connection_id)
        if stale:
            async with self._lock:
                for connection_id in stale:
                    self._connections.pop(connection_id, None)


class GatewayRuntime:
    def __init__(self, config: Config, bus) -> None:
        self.config = config
        self.bus = bus
        self.tasks = ChatTaskStore()
        self._heartbeats: dict[str, dict] = {}
        self._foreground: dict = {}
        self._text_extract: dict = {}
        self._status_queues: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self._perception_queues: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self._lock = threading.Lock()
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.bus.subscribe(Topics.HEARTBEAT, self.on_heartbeat)
        self.bus.subscribe(Topics.FOCUS_CHANGED, self.on_focus_changed)
        self.bus.subscribe(Topics.SITUATION_UPDATE, self.on_situation_update)
        if hasattr(self.bus, "respond"):
            self.bus.respond("health/gateway", lambda payload: self.gateway_health())

    def stop(self) -> None:
        self._started = False

    def on_heartbeat(self, topic: str, payload: dict) -> None:
        process = str(payload.get("process") or "unknown")
        with self._lock:
            self._heartbeats[process] = dict(payload)
        self._broadcast(self._status_queues, {"type": "health", "data": self.cached_health_snapshot()})

    def on_focus_changed(self, topic: str, payload: dict) -> None:
        with self._lock:
            self._foreground = dict(payload)
        self._broadcast(self._perception_queues, {"type": "foreground", "data": dict(payload)})

    def on_situation_update(self, topic: str, payload: dict) -> None:
        if payload.get("layer", "fast") != "fast":
            return
        with self._lock:
            self._text_extract = dict(payload)
        self._broadcast(self._perception_queues, {"type": "text_extract", "data": dict(payload)})

    def register_status_queue(self) -> asyncio.Queue:
        return self._register_queue(self._status_queues)

    def unregister_status_queue(self, worker_queue: asyncio.Queue) -> None:
        self._unregister_queue(self._status_queues, worker_queue)

    def register_perception_queue(self) -> asyncio.Queue:
        return self._register_queue(self._perception_queues)

    def unregister_perception_queue(self, worker_queue: asyncio.Queue) -> None:
        self._unregister_queue(self._perception_queues, worker_queue)

    def _register_queue(
        self,
        queues: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]],
    ) -> asyncio.Queue:
        worker_queue: asyncio.Queue = asyncio.Queue(maxsize=32)
        loop = asyncio.get_running_loop()
        with self._lock:
            queues.append((loop, worker_queue))
        return worker_queue

    def _unregister_queue(
        self,
        queues: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]],
        worker_queue: asyncio.Queue,
    ) -> None:
        with self._lock:
            queues[:] = [
                (loop, registered_queue)
                for loop, registered_queue in queues
                if registered_queue is not worker_queue
            ]

    def _broadcast(
        self,
        queues: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]],
        message: dict,
    ) -> None:
        with self._lock:
            targets = list(queues)
        for loop, target in targets:
            loop.call_soon_threadsafe(self._put_async_queue_nowait, target, dict(message))

    @staticmethod
    def _put_async_queue_nowait(target: asyncio.Queue, message: dict) -> None:
        try:
            target.put_nowait(message)
        except asyncio.QueueFull:
            pass

    def gateway_health(self) -> dict:
        return {
            "healthy": True,
            "process": "gateway",
            "started": self._started,
            "ts": time.time(),
        }

    def health_snapshot(self) -> dict:
        try:
            hub = self.bus.request(BUS_HEALTH_SERVICE, {}, timeout_ms=1000)
        except Exception as exc:
            hub = {"healthy": False, "error": str(exc)}
        return self._health_snapshot(hub)

    def cached_health_snapshot(self) -> dict:
        return self._health_snapshot({"healthy": None, "cached": True})

    def _health_snapshot(self, hub: dict) -> dict:
        with self._lock:
            processes = {key: dict(value) for key, value in self._heartbeats.items()}
        return {
            "gateway": self.gateway_health(),
            "hub": hub,
            "processes": processes,
        }

    def public_config(self) -> dict:
        data = self.config.model_dump()
        if "bus" in data:
            data["bus"] = dict(data["bus"])
            data["bus"]["auth_token"] = "<redacted>" if data["bus"].get("auth_token") else ""
        for section, fields in {
            "memory": ("db_path", "embedding_cache_dir"),
            "soul": ("path", "tuner_state_path"),
            "gateway": ("history_dir",),
            "vlm": ("cache_dir",),
            "local_brain": ("cache_dir",),
        }.items():
            if section in data:
                data[section] = dict(data[section])
                for field in fields:
                    if data[section].get(field):
                        data[section][field] = "<redacted>"
        return data

    def perception_status(self) -> dict:
        with self._lock:
            perception = dict(self._heartbeats.get("perception") or {})
        components = perception.get("components") or {}
        return {"degraded": not bool(perception), "components": components, "heartbeat": perception}

    def perception_snapshot(self) -> dict:
        with self._lock:
            return {
                "foreground": dict(self._foreground),
                "text_extract": dict(self._text_extract),
            }

    def list_history_sessions(self) -> dict:
        root = Path(self.config.gateway.history_dir)
        if not root.exists():
            return {"degraded": True, "sessions": []}
        sessions = []
        for child in sorted(root.iterdir(), reverse=True):
            events_path = child / "events.jsonl"
            if child.is_dir() and events_path.exists():
                sessions.append({
                    "session_id": child.name,
                    "events_path": str(events_path),
                })
        return {"degraded": False, "sessions": sessions}

    def read_history(self, session_id: str) -> dict:
        root = Path(self.config.gateway.history_dir).resolve()
        session_dir = (root / session_id).resolve()
        if root not in session_dir.parents and session_dir != root:
            raise HTTPException(status_code=400, detail="invalid session_id")
        events_path = session_dir / "events.jsonl"
        if not events_path.exists():
            raise HTTPException(status_code=404, detail="history session not found")
        turns = []
        with events_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                topic = event.get("topic")
                payload = event.get("payload") or {}
                if topic == Topics.USER_UTTERANCE:
                    turns.append({"role": "user", "text": payload.get("text", ""), "ts": event.get("ts")})
                if topic == Topics.REPLY and payload.get("kind", "final") == "final":
                    turns.append({"role": "assistant", "text": payload.get("text", ""), "ts": event.get("ts")})
        return {"session_id": session_id, "turns": turns}

    def request(self, service: str, payload: dict, *, timeout_ms: int | None = None) -> dict:
        return self.bus.request(
            service,
            payload,
            timeout_ms=timeout_ms or int(self.config.health.timeout_ms),
        )

    def run_chat(self, text: str, session_id: str) -> dict:
        task_id = self.tasks.create()
        payload = {"text": text, "session_id": session_id, "task_id": task_id}
        try:
            result = self.request(
                COGNITION_CHAT_SERVICE,
                payload,
                timeout_ms=int(self.config.gateway.chat_task_timeout_s * 1000),
            )
        except Exception as exc:
            return self.tasks.fail(task_id, str(exc)) or {
                "task_id": task_id,
                "status": "failed",
                "result": None,
                "error": str(exc),
            }
        return self.tasks.complete(task_id, result)


def _error_response(code: str, message: str, status_code: int, details: dict | None = None):
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "details": details or {}}},
    )


async def _chat_message_handler(runtime: GatewayRuntime, message: dict) -> dict | None:
    if message.get("type") == "interrupt":
        task = runtime.tasks.cancel_requested(str(message.get("task_id", "")))
        runtime.bus.publish("chat/interrupt", {"task_id": message.get("task_id")})
        return {"type": "interrupt_ack", "task": task}
    text = str(message.get("text") or message.get("user_input") or "")
    session_id = str(message.get("session_id") or "default")
    try:
        task = await asyncio.to_thread(runtime.run_chat, text, session_id)
    except Exception as exc:
        return {
            "type": "assistant_chunk",
            "task_id": "",
            "text": "",
            "done": True,
            "status": "failed",
            "error": str(exc),
        }
    result = task.get("result") or {}
    return {
        "type": "assistant_chunk",
        "task_id": task["task_id"],
        "text": result.get("text", ""),
        "done": True,
        "status": task["status"],
        "error": task.get("error", ""),
    }


def _register_default_ws_channels() -> None:
    register_ws_channel(WsChannelSpec(
        route="/ws/status",
        channel_name="status",
        initial_message=lambda runtime: {"type": "health", "data": runtime.health_snapshot()},
        queue_factory=lambda runtime: runtime.register_status_queue(),
        unregister_queue=lambda runtime, q: runtime.unregister_status_queue(q),
    ))
    register_ws_channel(WsChannelSpec(
        route="/ws/chat",
        channel_name="chat",
        message_handler=_chat_message_handler,
    ))
    register_ws_channel(WsChannelSpec(
        route="/ws/perception",
        channel_name="perception",
        initial_message=lambda runtime: {"type": "snapshot", "data": runtime.perception_snapshot()},
        queue_factory=lambda runtime: runtime.register_perception_queue(),
        unregister_queue=lambda runtime, q: runtime.unregister_perception_queue(q),
    ))


_register_default_ws_channels()


def create_gateway_app(
    runtime: GatewayRuntime,
    channels: list[WsChannelSpec] | None = None,
) -> FastAPI:
    connections = ConnectionManager(
        cleanup_interval_s=runtime.config.gateway.cleanup_interval_s,
        heartbeat_timeout_s=runtime.config.gateway.ws_heartbeat_timeout_s,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        runtime.start()
        await connections.start()
        try:
            yield
        finally:
            await connections.stop()
            runtime.stop()

    app = FastAPI(title="Yuki Gateway", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=runtime.config.gateway.cors_origins,
        allow_origin_regex=runtime.config.gateway.cors_origin_regex,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        code = {
            400: "bad_request",
            404: "not_found",
            408: "timeout",
        }.get(exc.status_code, "http_error")
        return _error_response(code, str(exc.detail), exc.status_code)

    @app.exception_handler(Exception)
    async def exception_handler(request: Request, exc: Exception):
        return _error_response("internal_error", str(exc), 500)

    @app.get("/api/health")
    def health() -> dict:
        return runtime.health_snapshot()

    @app.get("/api/memory")
    def memory_list(type: str | None = None, min_sensitivity: int = 0) -> dict:
        return runtime.request("memory/list", {"type": type, "min_sensitivity": min_sensitivity})

    @app.get("/api/memory/{memory_id}")
    def memory_get(memory_id: int) -> dict:
        return runtime.request("memory/get", {"id": memory_id})

    @app.delete("/api/memory/{memory_id}")
    def memory_delete(memory_id: int) -> dict:
        return runtime.request("memory/delete", {"id": memory_id})

    @app.get("/api/history/sessions")
    def history_sessions() -> dict:
        return runtime.list_history_sessions()

    @app.get("/api/history/{session_id}")
    def history(session_id: str) -> dict:
        return runtime.read_history(session_id)

    @app.post("/api/chat")
    def chat(request: ChatRequest) -> dict:
        return runtime.run_chat(request.text, request.session_id)

    @app.get("/api/chat/{task_id}")
    def chat_task(task_id: str) -> dict:
        task = runtime.tasks.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="chat task not found")
        return task

    @app.get("/api/config")
    def config() -> dict:
        return runtime.public_config()

    @app.get("/api/soul")
    def soul() -> dict:
        return runtime.request(SOUL_GET_SERVICE, {})

    @app.get("/api/perception/status")
    def perception_status() -> dict:
        return runtime.perception_status()

    for spec in (channels if channels is not None else ws_channels()):
        app.router.add_websocket_route(spec.route, create_ws_handler(spec, connections, runtime))

    return app


class GatewayServer:
    def __init__(self, config: Config, bus=None) -> None:
        self.config = config
        self._owns_bus = bus is None
        self.bus = bus or BusNode(
            base_port=config.bus.base_port,
            hwm=config.bus.hwm,
            auth_token=config.bus.auth_token,
            max_msg_size=config.bus.max_msg_size,
        )
        self.runtime = GatewayRuntime(config, self.bus)
        self.app = create_gateway_app(self.runtime)
        self._server = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        import uvicorn

        uvicorn_config = uvicorn.Config(
            self.app,
            host=self.config.gateway.host,
            port=self.config.gateway.port,
            log_level="warning",
        )
        self._server = uvicorn.Server(uvicorn_config)
        self._thread = threading.Thread(target=self._server.run, name="yuki-gateway")
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self.runtime.stop()
        if self._owns_bus and hasattr(self.bus, "close"):
            self.bus.close()
