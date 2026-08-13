from yuki.bus import BusHub
from yuki.process import ProcessAgent


class BusServerAgent(ProcessAgent):
    name = "bus_server"
    register_health = False

    def _make_bus(self):
        return BusHub(base_port=self.config.bus.base_port, hwm=self.config.bus.hwm)

    def setup(self) -> None:
        pass

    def teardown(self) -> None:
        pass
