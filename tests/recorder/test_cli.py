import json
import sys
from types import SimpleNamespace

import pytest

from yuki.recorder import cli
from yuki.recorder.session import Session


class FakeBus:
    def __init__(self):
        self.closed = False
        self.subscriptions = []

    def subscribe(self, prefix, handler):
        self.subscriptions.append((prefix, handler))

    def close(self):
        self.closed = True


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

    def close(self):
        self.closed = True


def test_run_with_grabber_none_records_events_only(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "ShutdownManager", lambda: FakeShutdown(iterations=3))
    session = Session(tmp_path, session_id="sess-noframes")

    cli.run(session, FakeBus(), None, interval_sec=0.0)

    assert list(session.frames_dir.iterdir()) == []
    events_path = session.dir / "events.jsonl"
    lines = events_path.read_text(encoding="utf-8").splitlines() if events_path.exists() else []
    assert all(json.loads(line)["topic"] != "recorder/frame" for line in lines)


def test_main_closes_session_on_run_exception(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.argv", ["yuki.recorder", "--output-dir", str(tmp_path), "--no-frames"])
    monkeypatch.setattr(
        cli.Config, "from_env", classmethod(lambda cls: SimpleNamespace(base_port=7777, hwm=1000))
    )
    bus = FakeBus()
    monkeypatch.setattr(cli, "BusNode", lambda *a, **kw: bus)
    session = FakeSession(tmp_path)
    monkeypatch.setattr(cli, "Session", lambda path: session)

    def boom(*a, **kw):
        raise RuntimeError("grab failed")

    monkeypatch.setattr(cli, "run", boom)

    with pytest.raises(RuntimeError, match="grab failed"):
        cli.main()

    assert session.closed is True
    assert bus.closed is True
