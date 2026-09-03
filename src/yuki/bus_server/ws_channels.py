from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable

from starlette.websockets import WebSocketDisconnect

if TYPE_CHECKING:
    from starlette.websockets import WebSocket

    from yuki.bus_server.gateway import ConnectionManager, GatewayRuntime


@dataclass(frozen=True)
class WsChannelSpec:
    route: str
    channel_name: str
    initial_message: Callable[["GatewayRuntime"], dict] | None = None
    queue_factory: Callable[["GatewayRuntime"], asyncio.Queue | None] | None = None
    unregister_queue: Callable[["GatewayRuntime", asyncio.Queue], None] | None = None
    message_handler: Callable[["GatewayRuntime", dict], Awaitable[dict | None]] | None = None


_WS_CHANNELS: dict[str, WsChannelSpec] = {}


def register_ws_channel(spec: WsChannelSpec) -> None:
    _WS_CHANNELS[spec.route] = spec


def ws_channels() -> list[WsChannelSpec]:
    return list(_WS_CHANNELS.values())


async def _wait_for_ws_message_or_queue(
    websocket: "WebSocket",
    updates: asyncio.Queue,
) -> dict | None:
    queue_task = asyncio.create_task(updates.get())
    receive_task = asyncio.create_task(websocket.receive_text())
    done, pending = await asyncio.wait(
        {queue_task, receive_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
    for task in pending:
        try:
            await task
        except asyncio.CancelledError:
            pass
    if receive_task in done:
        receive_task.result()
        return None
    return queue_task.result()


def create_ws_handler(
    spec: WsChannelSpec,
    connections: "ConnectionManager",
    runtime: "GatewayRuntime",
) -> Callable[["WebSocket"], Awaitable[None]]:
    async def handler(websocket: "WebSocket") -> None:
        await websocket.accept()
        connection_id = await connections.register(websocket, spec.channel_name)
        updates = spec.queue_factory(runtime) if spec.queue_factory is not None else None
        send_lock = asyncio.Lock()
        duplex_tasks: list[asyncio.Task] = []

        async def send(message: dict) -> None:
            async with send_lock:
                await websocket.send_json(message)

        async def forward_updates() -> None:
            assert updates is not None
            while True:
                await send(await updates.get())
                await connections.touch(connection_id)

        async def handle_requests() -> None:
            assert spec.message_handler is not None
            while True:
                message = await websocket.receive_json()
                await connections.touch(connection_id)
                reply = await spec.message_handler(runtime, message)
                if reply is not None:
                    await send(reply)

        try:
            if spec.initial_message is not None:
                await send(spec.initial_message(runtime))
            if updates is not None and spec.message_handler is not None:
                duplex_tasks = [
                    asyncio.create_task(handle_requests()),
                    asyncio.create_task(forward_updates()),
                ]
                done, pending = await asyncio.wait(
                    duplex_tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    if task.cancelled():
                        continue
                    error = task.exception()
                    if error is not None and not isinstance(error, WebSocketDisconnect):
                        raise error
                return
            while True:
                if spec.message_handler is not None:
                    message = await websocket.receive_json()
                    await connections.touch(connection_id)
                    reply = await spec.message_handler(runtime, message)
                    if reply is not None:
                        await send(reply)
                    continue
                if updates is None:
                    await websocket.receive_text()
                    await connections.touch(connection_id)
                    continue
                message = await _wait_for_ws_message_or_queue(websocket, updates)
                await connections.touch(connection_id)
                if message is not None:
                    await send(message)
        except WebSocketDisconnect:
            return
        finally:
            for task in duplex_tasks:
                if not task.done():
                    task.cancel()
            if duplex_tasks:
                await asyncio.gather(*duplex_tasks, return_exceptions=True)
            await connections.unregister(connection_id)
            if updates is not None and spec.unregister_queue is not None:
                spec.unregister_queue(runtime, updates)

    return handler
