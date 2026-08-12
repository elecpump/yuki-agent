# Yuki Phase 3 修订：感知理解管线重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按六点设计评审重构 Phase 3 感知理解管线：① 感知管线只产出结构化理解（标准事件），不再直接回复；L1 拆为独立消费者（L1Responder，Brain 阶段第一个组件占位）；② 引入 SpeechBuffer（音频帧累积）+ webrtcvad（语音段边界）→ 整段 STT；③ VLM 启动时后台预热 + 不可用降级文本模式；④ 缓存键升级为 source_id + 滚动区间，留接口；⑤ 定义 VLM 失败降级输出（degraded 标志 + 原因）；⑥ 感知输出定义为标准事件（ContextAssembler 槽位数据源）。

**Architecture:** 认知层拆为两层职责：**PerceptionPipeline**（纯感知：focus_changed→VLM→situation_update 事件；mic→SpeechBuffer→VAD→整段 STT→user_utterance 事件；awake→_listening）与 **L1Responder**（独立消费者：订阅感知事件 + awake → L1 规则引擎 → event/reply）。两者都在 cognition 进程内，但职责分离——后续 Brain 阶段直接替换 L1Responder。VLM 启动时后台预热；不可用降级为纯文本模式并记日志。

**Tech Stack:** Python ≥3.11；新增 `webrtcvad`（VAD，仅 SpeechBuffer 内部用，可注入 fake）；既有：protobuf 总线、structlog、numpy、Pillow。

**Spec:** `docs/superpowers/specs/2026-08-10-yuki-agent-design.md` §3.2/§4.3；接口契约 `docs/superpowers/specs/2026-08-10-yuki-interfaces.md` §4（新增 event/perception/* 主题）；Phase 3 原计划（本修订重构其 Task 6/7 的输出）。

## Global Constraints

- 平台：Windows 10/11；语言：Python 为主
- 总线走 localhost；消息为 protobuf Envelope
- **感知管线不产出任何回复**——只发布结构化理解事件；回复由 L1Responder 产出
- 标准事件：`event/perception/situation_update`（source_id/scroll_band/topic/summary/content_type/key_points/sensitive/degraded/ts）、`event/perception/user_utterance`（text/duration_s/ts）
- STT 仅对**完整语音段**（VAD 判定）运行，不对单帧；唤醒后才累积
- VLM 启动时后台预热；不可用（无 GPU/OOM）→ 降级文本模式 + 日志；懒加载仅保留给 L1
- 缓存键：`source_id` + `scroll_band`（如 `https://x.com/a|25-50`）；留接口后续内容 hash
- 降级输出：`degraded: true` + `reason` 字段贯穿 situation_update
- 每个任务 TDD：先写失败测试 → 跑失败 → 实现 → 跑通 → 提交
- 既有 150 单元 + 1 e2e 必须保持通过（e2e 的 awake→reply 闭环经 L1Responder 保留）

## File Structure

```
pyproject.toml                                        # 修改：dev 加 webrtcvad
src/yuki/cognition/topics_ext.py                      # 新增：event/perception/* 主题常量
src/yuki/cognition/speech_buffer.py                   # 新增：SpeechBuffer（帧累积 + VAD 切段，纯逻辑+注入vad）
src/yuki/cognition/pipeline.py                        # 修改：只产事件，不回复；VLM 预热降级
src/yuki/cognition/l1_responder.py                    # 新增：L1Responder（独立消费者 → event/reply）
src/yuki/cognition/vlm.py                             # 修改：预热方法 warmup() + degraded 输出 + 新缓存键
src/yuki/cognition/main.py                            # 修改：组装 pipeline + L1Responder；VLM 后台预热
tests/cognition/test_speech_buffer.py                 # 新增
tests/cognition/test_topics_ext.py                    # 新增
tests/cognition/test_l1_responder.py                  # 新增
tests/cognition/test_pipeline.py                      # 修改：断言事件发布而非回复
tests/cognition/test_vlm.py                           # 修改：缓存键 + degraded
tests/cognition/test_cognition.py                     # 修改：awake→reply 经 L1Responder
```

---

### Task 1: 感知事件主题常量 + SpeechBuffer（VAD 累积）

**Files:**
- Create: `src/yuki/cognition/topics_ext.py`
- Create: `src/yuki/cognition/speech_buffer.py`
- Modify: `pyproject.toml`
- Test: `tests/cognition/test_topics_ext.py`
- Test: `tests/cognition/test_speech_buffer.py`

**Interfaces:**
- Consumes: `Topics`（既有）
- Produces:
  - `TopicsExt.SITUATION_UPDATE = "event/perception/situation_update"`、`TopicsExt.USER_UTTERANCE = "event/perception/user_utterance"`
  - `class SpeechBuffer`（纯逻辑 + 注入 VAD）：
    - `__init__(self, frame_ms=20, sample_rate=16000, vad=None, on_utterance=None, silent_frames=15, max_utterance_s=10.0)` — vad 可注入 fake（默认懒加载 webrtcvad.Vad(0)）
    - `add_frame(samples: np.ndarray) -> None` — 累积帧；VAD 判定语音/静音
    - `on_utterance: Callable[[np.ndarray], None] | None` — 语音段结束时回调（整段采样）
    - 切段规则：连续 `silent_frames` 帧静音 → 若已累积语音则触发 utterance；`max_utterance_s` 强制切段
    - `reset() -> None` — 清空累积（唤醒后/超时用）
  - 纯逻辑核心（VAD 判定）+ 薄适配器（webrtcvad）分离，测试注入 fake vad

- [ ] **Step 1: 写失败测试 `tests/cognition/test_topics_ext.py`**

```python
from yuki.cognition.topics_ext import TopicsExt


def test_perception_topic_constants():
    assert TopicsExt.SITUATION_UPDATE == "event/perception/situation_update"
    assert TopicsExt.USER_UTTERANCE == "event/perception/user_utterance"
```

- [ ] **Step 2: 写失败测试 `tests/cognition/test_speech_buffer.py`**

```python
import numpy as np
import pytest

from yuki.cognition.speech_buffer import SpeechBuffer


class FakeVad:
    """is_speech 交替返回以模拟语音/静音。"""

    def __init__(self, pattern):
        self._pattern = list(pattern)

    def is_speech(self, frame):
        return self._pattern.pop(0) if self._pattern else False


def test_silence_only_no_utterance():
    utterances = []
    buf = SpeechBuffer(vad=FakeVad([False] * 30), on_utterance=utterances.append, silent_frames=15)
    for _ in range(20):
        buf.add_frame(np.zeros(320, dtype=np.float32))
    assert utterances == []


def test_speech_then_silence_triggers_utterance():
    utterances = []
    pattern = [True] * 5 + [False] * 20
    buf = SpeechBuffer(vad=FakeVad(pattern), on_utterance=utterances.append, silent_frames=10)
    for _ in pattern:
        buf.add_frame(np.zeros(320, dtype=np.float32))
    assert len(utterances) == 1
    assert utterances[0].shape[0] == 5 * 320  # 语音段整段


def test_reset_clears_accumulation():
    utterances = []
    buf = SpeechBuffer(vad=FakeVad([True] * 5), on_utterance=utterances.append, silent_frames=10)
    for _ in range(5):
        buf.add_frame(np.zeros(320, dtype=np.float32))
    buf.reset()
    buf.add_frame(np.zeros(320, dtype=np.float32))  # 下一帧静音（pattern 耗尽→False）
    assert utterances == []
```

- [ ] **Step 3: 跑测试验证失败**

Run: `python -m pytest tests/cognition/test_topics_ext.py tests/cognition/test_speech_buffer.py -v`
Expected: FAIL，`No module named 'yuki.cognition.topics_ext'`

- [ ] **Step 4: 实现 `src/yuki/cognition/topics_ext.py`**

```python
class TopicsExt:
    SITUATION_UPDATE = "event/perception/situation_update"
    USER_UTTERANCE = "event/perception/user_utterance"
```

- [ ] **Step 5: 实现 `src/yuki/cognition/speech_buffer.py`**

```python
import numpy as np

from yuki.logger import get_logger

logger = get_logger("yuki.cognition.speech_buffer")


class SpeechBuffer:
    """音频帧累积器：VAD 判定语音/静音，静音超时或最大时长触发整段 utterance。

    vad 可注入 fake（纯逻辑可测）；默认懒加载 webrtcvad。
    """

    def __init__(
        self,
        frame_ms: int = 20,
        sample_rate: int = 16000,
        vad=None,
        on_utterance=None,
        silent_frames: int = 15,
        max_utterance_s: float = 10.0,
    ) -> None:
        self._frame_len = int(sample_rate * frame_ms / 1000)
        self._vad = vad
        self._sample_rate = sample_rate
        self._silent_frames = silent_frames
        self._max_frames = int(max_utterance_s * 1000 / frame_ms)
        self.on_utterance = on_utterance
        self._speech: list[np.ndarray] = []
        self._silence_count = 0

    def _get_vad(self):
        if self._vad is None:
            import webrtcvad
            self._vad = webrtcvad.Vad(0)
        return self._vad

    def add_frame(self, samples: np.ndarray) -> None:
        vad = self._get_vad()
        frame = np.asarray(samples, dtype=np.float32)
        # 20ms@16k = 320 采样；webrtcvad 需要 int16 + 精确长度
        pcm = (frame * 32767).astype(np.int16).tobytes()
        try:
            is_speech = bool(vad.is_speech(pcm, self._sample_rate))
        except Exception:
            logger.warning("vad frame skipped", exc_info=True)
            return
        if is_speech:
            self._speech.append(frame)
            self._silence_count = 0
        else:
            if self._speech:
                self._silence_count += 1
                if self._silence_count >= self._silent_frames:
                    self._flush()
            # 静音帧本身不累积
        if len(self._speech) >= self._max_frames:
            self._flush()

    def _flush(self) -> None:
        if self._speech and self.on_utterance is not None:
            utterance = np.concatenate(self._speech) if len(self._speech) > 1 else self._speech[0]
            try:
                self.on_utterance(utterance)
            except Exception:
                logger.exception("utterance callback failed")
        self._speech = []
        self._silence_count = 0

    def reset(self) -> None:
        self._speech = []
        self._silence_count = 0
```

**注意：** `add_frame` 的 `frame_len` 断言在测试中用 320 采样；webrtcvad 的 `is_speech(pcm, sample_rate)` 要求 10/20/30ms 帧——20ms@16k=320 采样合法。测试注入 FakeVad 避开真实 webrtcvad。

- [ ] **Step 6: 跑测试验证通过**

Run: `python -m pytest tests/cognition/test_topics_ext.py tests/cognition/test_speech_buffer.py -v`
Expected: 4 个测试 PASS

- [ ] **Step 7: 提交**

```bash
git add pyproject.toml src/yuki/cognition/topics_ext.py src/yuki/cognition/speech_buffer.py tests/cognition/test_topics_ext.py tests/cognition/test_speech_buffer.py
git commit -m "feat: perception event topics and VAD-gated speech buffer"
```

---

### Task 2: pipeline 重构——只产事件 + VLM 预热降级

**Files:**
- Modify: `src/yuki/cognition/pipeline.py`
- Modify: `src/yuki/cognition/vlm.py`
- Modify: `tests/cognition/test_pipeline.py`
- Modify: `tests/cognition/test_vlm.py`
- Test: 上述文件

**Interfaces:**
- Consumes: `TopicsExt`（Task 1）、`SpeechBuffer`（Task 1）、`VisualUnderstander`（含新 warmup/degraded）、`SensitiveFilter`、`FrameClient`、`SpeechRecognizer`
- Produces:
  - `VisualUnderstander.warmup() -> None` — 后台线程预热加载模型（幂等，失败记日志不抛）
  - `VisualUnderstander.understand(...) -> dict` — 失败时返回 `{"topic":"","summary":"","content_type":"unknown","key_points":[],"degraded":True,"reason":"..."}`（降级输出）
  - 缓存键：`f"{source_id}|{scroll_band}"`（`source_id`=URL/文件路径，`scroll_band` 如 `"0-25"`）；留 `_cache_key_v2` 接口注释（后续内容 hash）
  - `PerceptionPipeline` 重构：
    - `__init__(vlm, sensitive_filter, stt, l1, frame_client, bus, speech_buffer=None, ...)` — l1 参数移除（L1 不再在管线内）
    - `on_focus_changed(topic, payload) -> None`：拉帧→敏感检查→VLM→二次扫描→**发布 `TopicsExt.SITUATION_UPDATE` 事件**（不回复）
    - `on_mic(topic, payload) -> None`：仅 `_listening` 时 → `speech_buffer.add_frame(解码采样)`；utterance 回调里 STT→**发布 `TopicsExt.USER_UTTERANCE`**
    - `on_awake(topic, payload) -> None`：设 `_listening=True`、`speech_buffer.reset()`（不回复）
    - `understand_screen() -> dict`：返回情境（含 degraded），不发布（供 Brain 后续）
  - `build_pipeline(bus, *, vlm=None, ...) -> PerceptionPipeline` — 签名调整

- [ ] **Step 1: 修改 `src/yuki/cognition/vlm.py`（warmup + degraded + 新缓存键）**

```python
    def warmup(self) -> None:
        if self._loaded:
            return
        def _load_thread():
            try:
                self._load()
            except Exception:
                logger.warning("vlm warmup failed, will degrade to text mode", exc_info=True)
        threading.Thread(target=_load_thread, daemon=True).start()

    def understand(self, image, cache_key: str | None = None) -> dict:
        if cache_key:
            hit = self._cache.get(cache_key)
            if hit is not None:
                return hit
        try:
            result = self._infer(image)
        except Exception:
            logger.exception("vlm inference failed, degrading")
            result = {"topic": "", "summary": "", "content_type": "unknown",
                      "key_points": [], "degraded": True, "reason": "inference_failed"}
        if not isinstance(result, dict):
            result = self._parse(result if isinstance(result, str) else "")
        if cache_key:
            self._cache.put(cache_key, result)
        return result
```

（缓存键格式 `source_id|scroll_band` 在 pipeline 侧构造；`understand` 的 cache_key 就是组合键字符串。）

- [ ] **Step 2: 修改 `tests/cognition/test_vlm.py` 增加 warmup/degraded 测试**

```python
def test_understand_inference_failure_degrades():
    class BoomModel:
        pass
    vlm = VisualUnderstander(model=BoomModel(), processor=object())
    def boom(image):
        raise RuntimeError("oom")
    vlm._infer = boom
    result = vlm.understand(None)
    assert result["degraded"] is True
    assert result["reason"] == "inference_failed"
    assert result["topic"] == ""


def test_warmup_is_idempotent_and_background():
    import time
    class SlowModel:
        def load(self):
            time.sleep(0.05)
    vlm = VisualUnderstander(model=object(), processor=object())
    vlm._load = lambda: setattr(vlm, "_loaded", True)
    vlm.warmup()
    vlm.warmup()  # 幂等
    # 后台线程完成
    deadline = time.time() + 2.0
    while not vlm._loaded and time.time() < deadline:
        time.sleep(0.01)
    assert vlm._loaded
```

- [ ] **Step 3: 修改 `tests/cognition/test_pipeline.py` 断言事件而非回复**

```python
def test_pipeline_focus_publishes_situation_update():
    bus = FakeBus()
    pipeline = build_pipeline(bus, vlm=FakeVLM(), sensitive_filter=FakeSensitive(),
                              stt=FakeSTT(), frame_client=FakeFrameClient())
    bus.subscriptions[Topics.FOCUS_CHANGED]("event/focus_changed",
        {"app": "chrome", "url": "https://x.com/a", "title": "A"})
    events = [t for t, _ in bus.published if t == TopicsExt.SITUATION_UPDATE]
    assert len(events) == 1
    payload = [p for t, p in bus.published if t == TopicsExt.SITUATION_UPDATE][0]
    assert payload["topic"] == "climate"
    assert payload["source_id"] == "https://x.com/a"
    assert "scroll_band" in payload
    assert payload["degraded"] is False
    # 管线不直接回复
    assert not any(t == Topics.REPLY for t, _ in bus.published)


def test_pipeline_awake_no_direct_reply():
    bus = FakeBus()
    pipeline = build_pipeline(bus, vlm=FakeVLM(), sensitive_filter=FakeSensitive(),
                              stt=FakeSTT(), frame_client=FakeFrameClient())
    bus.subscriptions[Topics.AWAKE]("event/awake", {"source": "hotkey", "ts": 0.0})
    assert not any(t == Topics.REPLY for t, _ in bus.published)
```

（原 on_awake/on_mic 直接回复的测试改为断言事件发布；on_mic 改为断言 USER_UTTERANCE 事件。）

- [ ] **Step 4: 重构 `src/yuki/cognition/pipeline.py`**

```python
import base64
import io
import time
from typing import Callable

from PIL import Image

from yuki.cognition.frame_client import FrameClient
from yuki.cognition.sensitive import SensitiveFilter
from yuki.cognition.speech_buffer import SpeechBuffer
from yuki.cognition.stt import SpeechRecognizer
from yuki.cognition.topics_ext import TopicsExt
from yuki.cognition.vlm import VisualUnderstander
from yuki.logger import get_logger
from yuki.topics import Topics

logger = get_logger("yuki.cognition.pipeline")


def decode_png_b64(png_b64: str) -> Image.Image | None:
    try:
        raw = base64.b64decode(png_b64)
        return Image.open(io.BytesIO(raw)).convert("RGB")
    except (ValueError, OSError):
        logger.warning("png decode failed")
        return None


def scroll_band(scroll_percent: float | None) -> str:
    if scroll_percent is None:
        return "unknown"
    return f"{int(scroll_percent // 25) * 25}-{int(scroll_percent // 25) * 25 + 25}"


class PerceptionPipeline:
    """纯感知管线：产出结构化理解事件，不产生任何回复。

    发布 event/perception/situation_update 与 event/perception/user_utterance，
    供 L1Responder（当前）/ ContextAssembler（未来 Brain）消费。
    """

    def __init__(
        self,
        vlm: VisualUnderstander,
        sensitive_filter: SensitiveFilter,
        stt: SpeechRecognizer,
        frame_client: FrameClient,
        bus,
        speech_buffer: SpeechBuffer | None = None,
        cache_scroll: bool = True,
    ) -> None:
        self._vlm = vlm
        self._sensitive = sensitive_filter
        self._stt = stt
        self._frame_client = frame_client
        self._bus = bus
        self._listening = False
        self._speech_buffer = speech_buffer or SpeechBuffer(
            on_utterance=self._on_utterance
        )

    def _on_utterance(self, samples) -> None:
        text = self._stt.recognize(samples, sample_rate=16000)
        if not text:
            return
        self._bus.publish(TopicsExt.USER_UTTERANCE, {
            "text": text, "duration_s": round(len(samples) / 16000, 2), "ts": time.time(),
        })

    def on_focus_changed(self, topic: str, payload: dict) -> None:
        frame = self._frame_client.get_latest()
        if not frame or not frame.get("png") or frame.get("sensitive"):
            return
        image = decode_png_b64(frame["png"])
        if image is None:
            return
        source_id = payload.get("url") or payload.get("title") or "unknown"
        cache_key = f"{source_id}|{scroll_band(payload.get('scroll_percent'))}"
        context = self._vlm.understand(image, cache_key=cache_key)
        text = " ".join([
            context.get("topic", ""),
            context.get("summary", ""),
            " ".join(context.get("key_points", []) or []),
        ])
        if self._sensitive.scan(text):
            self._publish_situation({"topic": "", "sensitive": True, "degraded": True,
                                     "reason": "sensitive"})
            return
        self._publish_situation({
            "source_id": source_id,
            "scroll_band": scroll_band(payload.get("scroll_percent")),
            "topic": context.get("topic", ""),
            "summary": context.get("summary", ""),
            "content_type": context.get("content_type", "unknown"),
            "key_points": context.get("key_points", []),
            "sensitive": False,
            "degraded": context.get("degraded", False),
            "reason": context.get("reason", ""),
        })

    def _publish_situation(self, data: dict) -> None:
        data.setdefault("source_id", "unknown")
        data.setdefault("scroll_band", "unknown")
        data.setdefault("key_points", [])
        data.setdefault("ts", time.time())
        self._bus.publish(TopicsExt.SITUATION_UPDATE, data)

    def on_awake(self, topic: str, payload: dict) -> None:
        self._listening = True
        self._speech_buffer.reset()

    def on_mic(self, topic: str, payload: dict) -> None:
        if not self._listening:
            return
        pcm_b64 = payload.get("pcm", "")
        if not pcm_b64:
            return
        import numpy as np
        raw = base64.b64decode(pcm_b64)
        samples = np.frombuffer(raw, dtype=np.float32)
        self._speech_buffer.add_frame(samples)

    def understand_screen(self) -> dict:
        frame = self._frame_client.get_latest()
        if not frame or not frame.get("png") or frame.get("sensitive"):
            return {"topic": "", "sensitive": True, "degraded": True, "reason": "no_frame"}
        image = decode_png_b64(frame["png"])
        if image is None:
            return {"topic": "", "degraded": True, "reason": "decode_failed"}
        return self._vlm.understand(image)


def build_pipeline(bus, *, vlm=None, sensitive_filter=None, stt=None,
                   frame_client=None, speech_buffer=None) -> PerceptionPipeline:
    pipeline = PerceptionPipeline(
        vlm=vlm or VisualUnderstander(),
        sensitive_filter=sensitive_filter or SensitiveFilter(),
        stt=stt or SpeechRecognizer(),
        frame_client=frame_client or FrameClient(bus),
        bus=bus,
        speech_buffer=speech_buffer,
    )
    bus.subscribe(Topics.FOCUS_CHANGED, pipeline.on_focus_changed)
    bus.subscribe(Topics.AWAKE, pipeline.on_awake)
    bus.subscribe(Topics.MIC, pipeline.on_mic)
    return pipeline
```

- [ ] **Step 5: 跑测试验证通过**

Run: `python -m pytest tests/cognition/test_vlm.py tests/cognition/test_pipeline.py -v`
Expected: 全部 PASS（含重构后的断言）

- [ ] **Step 6: 全量回归 + 提交**

Run: `python -m pytest -q`
Expected: 全部 PASS（test_cognition 的 awake→reply 兼容在 Task 3 迁移到 L1Responder；若此处失败则临时跳过该断言，Task 3 完成后再验）
```bash
git add src/yuki/cognition/pipeline.py src/yuki/cognition/vlm.py tests/cognition/test_pipeline.py tests/cognition/test_vlm.py
git commit -m "refactor: pipeline emits perception events only, VLM warmup and degraded output"
```

---

### Task 3: L1Responder（独立消费者）+ main 接线 + 预热

**Files:**
- Create: `src/yuki/cognition/l1_responder.py`
- Modify: `src/yuki/cognition/main.py`
- Modify: `tests/cognition/test_cognition.py`
- Modify: `tests/cognition/test_l1_responder.py`（新增）
- Test: 上述文件

**Interfaces:**
- Consumes: `L1Engine`、`Topics`、`TopicsExt`、`PerceptionPipeline`（Task 2）
- Produces:
  - `class L1Responder`（Brain 阶段第一个组件占位）：
    - `__init__(self, l1: L1Engine, bus)`
    - `on_awake(topic, payload) -> None` — L1 空输入快答 → publish `Topics.REPLY`
    - `on_user_utterance(topic, payload) -> None` — STT 文本 → L1 快答 → publish `Topics.REPLY`
    - `build_l1_responder(bus, *, l1=None) -> L1Responder` — 订阅 `Topics.AWAKE` + `TopicsExt.USER_UTTERANCE`
  - `main()` — build_pipeline + build_l1_responder + **VLM 后台预热**（`pipeline._vlm.warmup()`）+ health/shutdown

- [ ] **Step 1: 写失败测试 `tests/cognition/test_l1_responder.py`**

```python
import pytest

from yuki.cognition.l1_responder import L1Responder, build_l1_responder
from yuki.cognition.topics_ext import TopicsExt
from yuki.topics import Topics


class FakeL1:
    def reply(self, text, context=None):
        return f"reply:{text}"


class FakeBus:
    def __init__(self):
        self.published = []
        self.subscriptions = {}

    def publish(self, topic, payload):
        self.published.append((topic, payload))

    def subscribe(self, prefix, handler):
        self.subscriptions[prefix] = handler


def test_awake_triggers_l1_reply():
    bus = FakeBus()
    responder = build_l1_responder(bus, l1=FakeL1())
    bus.subscriptions[Topics.AWAKE]("event/awake", {"source": "hotkey", "ts": 0.0})
    assert any(t == Topics.REPLY for t, _ in bus.published)


def test_utterance_triggers_l1_reply_with_text():
    bus = FakeBus()
    responder = build_l1_responder(bus, l1=FakeL1())
    bus.subscriptions[TopicsExt.USER_UTTERANCE](
        TopicsExt.USER_UTTERANCE, {"text": "你好", "duration_s": 1.0, "ts": 0.0})
    replies = [p for t, p in bus.published if t == Topics.REPLY]
    assert replies and replies[0]["text"] == "reply:你好"
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/cognition/test_l1_responder.py -v`
Expected: FAIL，`No module named 'yuki.cognition.l1_responder'`

- [ ] **Step 3: 实现 `src/yuki/cognition/l1_responder.py`**

```python
import time

from yuki.cognition.l1 import L1Engine
from yuki.cognition.topics_ext import TopicsExt
from yuki.logger import get_logger
from yuki.topics import Topics

logger = get_logger("yuki.cognition.l1_responder")


class L1Responder:
    """L1 快答消费者：订阅感知事件 + awake，产出 event/reply。

    职责边界：感知管线只产理解事件；本组件消费并回复。
    Brain 阶段直接替换本组件（同样的订阅，更聪明的 Brain）。
    """

    def __init__(self, l1: L1Engine, bus) -> None:
        self._l1 = l1
        self._bus = bus

    def on_awake(self, topic: str, payload: dict) -> None:
        reply = self._l1.reply("")
        self._publish(reply)

    def on_user_utterance(self, topic: str, payload: dict) -> None:
        text = payload.get("text", "")
        reply = self._l1.reply(text)
        self._publish(reply)

    def _publish(self, text: str) -> None:
        self._bus.publish(Topics.REPLY, {"text": text, "ts": time.time()})


def build_l1_responder(bus, *, l1=None) -> L1Responder:
    responder = L1Responder(l1=l1 or L1Engine(), bus=bus)
    bus.subscribe(Topics.AWAKE, responder.on_awake)
    bus.subscribe(TopicsExt.USER_UTTERANCE, responder.on_user_utterance)
    return responder
```

- [ ] **Step 4: 修改 `src/yuki/cognition/main.py`**

```python
from yuki.cognition.l1_responder import build_l1_responder
from yuki.cognition.pipeline import build_pipeline


def main() -> None:
    config = Config.from_env()
    bus = MessageBus(base_port=config.base_port, role=config.bus_role, hwm=config.hwm)
    shutdown = ShutdownManager()
    shutdown.register_signal_handlers()
    pipeline = build_pipeline(bus)
    pipeline._vlm.warmup()  # VLM 后台预热（不可用则降级文本模式）
    build_l1_responder(bus)
    register_health_service(bus, "cognition")
    try:
        while not shutdown.shutdown_requested:
            shutdown.wait(timeout=1.0)
    finally:
        bus.close()
```

（保留 `build_cognition(bus, *, pipeline=None)` 的既有测试兼容：测试用 build_cognition 的 legacy 路径，或改为 build_l1_responder。test_cognition.py 的 awake→reply 断言改为经 L1Responder 验证。）

- [ ] **Step 5: 修改 `tests/cognition/test_cognition.py` 适配**

既有 `test_build_cognition_wires_awake_to_reply` 改为断言经 L1Responder：`build_l1_responder(bus, l1=FakeL1())` 后 awake → REPLY。FakeBus 需有 subscribe/publish（既有已有）。

- [ ] **Step 6: 跑测试验证通过**

Run: `python -m pytest tests/cognition/test_l1_responder.py tests/cognition/test_cognition.py -v`
Expected: 全部 PASS

- [ ] **Step 7: 全量回归 + e2e + 提交**

Run: `python -m pytest -q`
Run: `python -m pytest -m e2e -q`
Expected: 全部 PASS（e2e 的 hotkey→awake→reply 闭环经 L1Responder 保留）
```bash
git add src/yuki/cognition/l1_responder.py src/yuki/cognition/main.py tests/cognition/test_l1_responder.py tests/cognition/test_cognition.py
git commit -m "feat: L1 responder as independent consumer, VLM background warmup"
```

---

## Self-Review

**1. 六点评审覆盖：**
- ① L1 拆独立消费者 → Task 3（L1Responder）；管线只产事件 → Task 2
- ② SpeechBuffer + VAD → Task 1
- ③ VLM 启动预热 + 降级 → Task 2/3（warmup + degraded 输出）
- ④ 缓存键 source_id + scroll_band → Task 2（scroll_band 函数 + 缓存键组合；内容 hash 留接口注释）
- ⑤ VLM 失败降级输出 → Task 2（degraded/reason 贯穿）
- ⑥ 标准事件 → Task 1（TopicsExt）+ Task 2（situation_update/user_utterance 发布）

**2. Placeholder 扫描：** 无 TBD/TODO。scroll_band 的 scroll_percent 来自 focus_changed payload（当前采集层未发该字段——用 `payload.get('scroll_percent')` 缺省 None → "unknown"，不阻塞；后续采集层补发）。

**3. Type consistency：**
- `TopicsExt.SITUATION_UPDATE/USER_UTTERANCE`（Task 1）被 Task 2/3 引用
- `SpeechBuffer(on_utterance=...)`（Task 1）被 Task 2 pipeline 引用
- `VisualUnderstander.warmup()/understand degraded`（Task 2）被 Task 3 main 引用
- `PerceptionPipeline.__init__(vlm, sensitive_filter, stt, frame_client, bus, speech_buffer=...)`（Task 2）被 build_pipeline 与测试引用
- `L1Responder/build_l1_responder`（Task 3）被 main 与测试引用

**关键取舍：**
- 职责分离：管线零回复，L1Responder 消费；Brain 阶段替换 L1Responder 即可（评审①）
- VAD 用 webrtcvad（用户确认）；SpeechBuffer 纯逻辑 + 注入 fake vad 可测
- scroll_percent 暂缺 → "unknown" band，不阻塞；采集层后续补发后自动生效
- e2e awake→reply 经 L1Responder 保留（用户确认）
