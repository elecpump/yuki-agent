import json

import pytest

from yuki.recorder.session import Session


def test_session_records_events_and_frames(tmp_path):
    session = Session(tmp_path, session_id="sess-001")
    session.record_event("event/awake", {"source": "hotkey"})
    path = session.save_frame(b"\x89PNG\r\n\x1a\nfakepng")
    session.close()

    assert path.exists()
    assert path.name == "000000.png"
    lines = (session.dir / "events.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["topic"] == "event/awake"
    assert first["payload"] == {"source": "hotkey"}
    second = json.loads(lines[1])
    assert second["topic"] == "recorder/frame"


def test_session_save_frame_sequence_increments(tmp_path):
    session = Session(tmp_path, session_id="sess-002")
    first = session.save_frame(b"frame1")
    second = session.save_frame(b"frame2")
    session.close()
    assert first.name == "000000.png"
    assert second.name == "000001.png"


def test_session_rejects_writes_after_close(tmp_path):
    session = Session(tmp_path, session_id="sess-003")
    session.close()
    with pytest.raises(RuntimeError):
        session.record_event("event/x", {})


def test_session_id_defaults_to_timestamp(tmp_path):
    session = Session(tmp_path)
    assert session.session_id != ""
    session.close()
