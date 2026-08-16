from yuki.functions.registry import FunctionRegistry

FUNCTIONS_CALL_SERVICE = "functions/call"


def register_function_services(bus, registry: FunctionRegistry) -> None:
    """Expose FunctionRegistry.dispatch through the process bus."""

    def on_call(payload: dict) -> dict:
        return registry.dispatch(payload)

    bus.respond(FUNCTIONS_CALL_SERVICE, on_call)
