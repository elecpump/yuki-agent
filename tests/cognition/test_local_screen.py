import base64
import io

from PIL import Image

from yuki.cognition.brain.local.screen import VisionScreenAdapter


def png_b64():
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color=(10, 20, 30)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


class FakeFrameClient:
    def __init__(self, frame):
        self.frame = frame

    def get_latest(self):
        return self.frame


class FakeVlm:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def understand_for_question(self, image, question, cache_key=None):
        self.calls.append((image, question, cache_key))
        return self.result


def test_screen_uses_question_cache_key_prefix():
    vlm = FakeVlm({"topic": "t", "summary": "s", "key_points": [], "can_answer": True})
    adapter = VisionScreenAdapter(
        FakeFrameClient({"frame_id": 7, "png": png_b64()}),
        vlm,
    )
    result = adapter.inspect("这页讲什么")
    assert result["can_answer"] is True
    assert vlm.calls[0][2].startswith("vision_route:7:")


def test_screen_no_frame_degrades():
    adapter = VisionScreenAdapter(FakeFrameClient({}), FakeVlm({}))
    result = adapter.inspect("这页讲什么")
    assert result["degraded"] is True
    assert result["can_answer"] is False
