import json
import time
from pathlib import Path


class Session:
    def __init__(self, output_dir: Path, session_id: str | None = None) -> None:
        self.output_dir = Path(output_dir)
        self.session_id = session_id or time.strftime("%Y%m%d-%H%M%S")
        self.dir = self.output_dir / self.session_id
        self.frames_dir = self.dir / "frames"
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.dir / "events.jsonl"
        self._frame_seq = 0
        self._closed = False

    def record_event(self, topic: str, payload: dict) -> None:
        if self._closed:
            raise RuntimeError("session closed")
        line = json.dumps({"ts": time.time(), "topic": topic, "payload": payload}, ensure_ascii=False)
        with self.events_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def save_frame(self, image_bytes: bytes, fmt: str = "png") -> Path:
        if self._closed:
            raise RuntimeError("session closed")
        path = self.frames_dir / f"{self._frame_seq:06d}.{fmt}"
        path.write_bytes(image_bytes)
        self.record_event("recorder/frame", {"seq": self._frame_seq, "path": str(path), "fmt": fmt})
        self._frame_seq += 1
        return path

    def close(self) -> None:
        self._closed = True
