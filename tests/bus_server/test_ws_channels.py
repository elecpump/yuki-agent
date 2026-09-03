import asyncio

import pytest

from yuki.bus_server.ws_channels import (
    WsChannelSpec,
    create_ws_handler,
    register_ws_channel,
    ws_channels,
)


def test_ws_channel_spec_is_frozen():
    with pytest.raises(Exception):
        WsChannelSpec(
            route="/ws/x",
            channel_name="x",
            initial_message=lambda runtime: {"type": "x"},
        ).route = "/ws/y"


def test_register_and_list_channels():
    register_ws_channel(WsChannelSpec(
        route="/ws/custom",
        channel_name="custom",
        initial_message=lambda runtime: {"type": "custom"},
    ))
    routes = {spec.route for spec in ws_channels()}
    assert "/ws/custom" in routes
    assert all(isinstance(spec, WsChannelSpec) for spec in ws_channels())


def test_create_ws_handler_returns_async_callable():
    spec = WsChannelSpec(
        route="/ws/x",
        channel_name="x",
        initial_message=lambda runtime: {"type": "x"},
    )
    handler = create_ws_handler(spec, connections=object(), runtime=object())
    assert callable(handler)
    assert asyncio.iscoroutinefunction(handler)


def test_duplex_channel_delivers_requests_and_background_updates():
    asyncio.run(_assert_duplex_channel_delivers_requests_and_background_updates())


async def _assert_duplex_channel_delivers_requests_and_background_updates():
    class WebSocket:
        def __init__(self):
            self.incoming = asyncio.Queue()
            self.sent = []

        async def accept(self):
            pass

        async def receive_json(self):
            message = await self.incoming.get()
            if message is None:
                from starlette.websockets import WebSocketDisconnect

                raise WebSocketDisconnect()
            return message

        async def send_json(self, message):
            self.sent.append(message)

    class Connections:
        async def register(self, websocket, channel):
            return "connection"

        async def touch(self, connection_id):
            pass

        async def unregister(self, connection_id):
            pass

    updates = asyncio.Queue()
    spec = WsChannelSpec(
        route="/ws/chat",
        channel_name="chat",
        queue_factory=lambda runtime: updates,
        message_handler=lambda runtime, message: asyncio.sleep(
            0,
            result={"type": "reply", "text": message["text"]},
        ),
    )
    websocket = WebSocket()
    handler = create_ws_handler(spec, Connections(), object())
    task = asyncio.create_task(handler(websocket))

    await updates.put({"type": "voice_turn", "data": {"turn_id": 1}})
    await asyncio.sleep(0)
    await websocket.incoming.put({"text": "hello"})
    await asyncio.sleep(0)
    await websocket.incoming.put(None)
    await task

    assert {message["type"] for message in websocket.sent} == {"voice_turn", "reply"}


def test_duplex_channel_treats_update_sender_disconnect_as_normal_shutdown():
    asyncio.run(_assert_update_sender_disconnect_is_normal_shutdown())


async def _assert_update_sender_disconnect_is_normal_shutdown():
    from starlette.websockets import WebSocketDisconnect

    class WebSocket:
        def __init__(self):
            self.incoming = asyncio.Queue()

        async def accept(self):
            pass

        async def receive_json(self):
            message = await self.incoming.get()
            if message is None:
                raise WebSocketDisconnect()
            return message

        async def send_json(self, message):
            raise WebSocketDisconnect()

    class Connections:
        async def register(self, websocket, channel):
            return "connection"

        async def touch(self, connection_id):
            pass

        async def unregister(self, connection_id):
            pass

    updates = asyncio.Queue()
    spec = WsChannelSpec(
        route="/ws/chat",
        channel_name="chat",
        queue_factory=lambda runtime: updates,
        message_handler=lambda runtime, message: asyncio.sleep(0, result=None),
    )
    websocket = WebSocket()
    handler = create_ws_handler(spec, Connections(), object())
    task = asyncio.create_task(handler(websocket))

    await updates.put({"type": "voice_turn", "data": {"turn_id": 1}})
    await asyncio.wait_for(task, timeout=1.0)
