import io
import json
import struct

from yuki.perception.browser_host import _read_message, _write_message, normalize_dom_message


def test_native_message_round_trip():
    stream = io.BytesIO()
    _write_message({"text": "hello"}, stdout=stream)
    stream.seek(0)

    assert _read_message(stdin=stream) == {"text": "hello"}


def test_read_native_message_accepts_chrome_length_prefix():
    payload = json.dumps({"title": "T"}).encode("utf-8")
    stream = io.BytesIO(struct.pack("<I", len(payload)) + payload)

    assert _read_message(stdin=stream) == {"title": "T"}


def test_normalize_dom_message_marks_source_and_reason():
    result = normalize_dom_message({"text": "body", "title": "Title", "url": "https://x.test"})

    assert result["source"] == "dom"
    assert result["text"] == "body"
    assert result["title"] == "Title"
    assert result["reason"] == "native_dom"
