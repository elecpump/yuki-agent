import time


def make_reply(awake: dict) -> dict:
    """对唤醒事件的 L1 回应。Phase 3 由真实引擎替换。"""
    return {"text": "我在，你说。", "ts": time.time()}
