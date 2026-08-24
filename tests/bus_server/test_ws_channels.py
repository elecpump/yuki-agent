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
