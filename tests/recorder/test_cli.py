import json

import pytest

from yuki.config import Config
from yuki.recorder import cli
from yuki.recorder.agent import RecorderAgent
from yuki.recorder.session import Session

from tests.fakes import FakeBus


class FakeShutdown:
    def __init__(self, iterations=3):
        self._iterations = iterations
        self._calls = 0

    def register_signal_handlers(self):
        pass

    @property
    def shutdown_requested(self):
        return self._calls >= self._iterations

    def wait(self, timeout=None):
        self._calls += 1
        return False


class FakeSession:
    def __init__(self, output_dir):
        self.output_dir = output_dir
        self.closed = False
        self.events = []
        self.frames = []

    def record_event(self, topic, payload):
        self.events.append((topic, payload))

    def save_frame(self, png):
        self.frames.append(png)

    def close(self):
        self.closed = True


def test_recorder_agent_records_events_and_frames():
    bus = FakeBus()
    session = FakeSession("out")
    agent = RecorderAgent(Config(), bus=bus, session=session, grabber=lambda: b"png", interval_sec=0.0)
    agent.shutdown = FakeShutdown(iterations=2)
    agent.setup()
    bus.subscriptions["event/"][0]("event/reply", {"text": "hi", "ts": 0.0})
    assert session.events == [("event/reply", {"text": "hi", "ts": 0.0})]
    agent.loop()
    assert session.frames == [b"png", b"png"]
    agent.teardown()
    assert session.closed is True


def test_main_propagates_run_exception(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.argv", ["yuki.recorder", "--output-dir", str(tmp_path), "--no-frames"])
    monkeypatch.setattr(
        cli.Config, "from_env", classmethod(lambda cls: Config())
    )
    monkeypatch.setattr(
        cli.RecorderAgent,
        "run",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("grab failed")),
    )
    with pytest.raises(RuntimeError, match="grab failed"):
        cli.main()
