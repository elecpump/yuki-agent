import time

from yuki.config import Config
from yuki.process import ProcessAgent
from yuki.topics import Topics


class RecorderAgent(ProcessAgent):
    name = "recorder"

    def __init__(self, config: Config, *, bus=None, shutdown=None,
                 session=None, grabber=None, interval_sec: float = 1.0) -> None:
        super().__init__(config, bus=bus, shutdown=shutdown)
        self._session = session
        self._grabber = grabber
        self._interval_sec = interval_sec

    def setup(self) -> None:
        def on_event(topic: str, payload: dict) -> None:
            self._session.record_event(topic, payload)

        self.bus.subscribe("event/", on_event)

    def loop(self) -> None:
        next_grab = time.time()
        while not self.shutdown.shutdown_requested:
            now = time.time()
            if now >= next_grab and self._grabber is not None:
                self._session.save_frame(self._grabber())
                next_grab = now + self._interval_sec
            self.shutdown.wait(timeout=0.05)

    def teardown(self) -> None:
        self._session.close()
