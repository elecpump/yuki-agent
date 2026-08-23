from yuki.bus import BusHub
from yuki.process import ProcessAgent


class BusServerAgent(ProcessAgent):
    name = "bus_server"
    register_health = False

    def __init__(self, config, *, bus=None, shutdown=None, gateway=None) -> None:
        super().__init__(config, bus=bus, shutdown=shutdown)
        self._gateway = gateway

    def _make_bus(self):
        return BusHub(
            base_port=self.config.bus.base_port,
            hwm=self.config.bus.hwm,
            auth_token=self.config.bus.auth_token,
            max_msg_size=self.config.bus.max_msg_size,
        )

    def setup(self) -> None:
        if self._gateway is None and not self.config.gateway.enabled:
            return
        if self._gateway is None:
            from yuki.bus_server.gateway import GatewayServer

            self._gateway = GatewayServer(self.config)
        self._gateway.start()

    def teardown(self) -> None:
        if self._gateway is not None:
            self._gateway.stop()
