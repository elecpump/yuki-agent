# 模型加载容错 Implementation Plan（架构评审主题 2）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除 VLM/STT/LocalChatModel 的 `_load_failed` 永久锁死与失败结果写 cache 的自锁，让瞬时加载失败（如 GPU 显存占用）在时间窗口后自动恢复；health 返回 `degraded=True` 而非 `healthy=False`，避免触发重启风暴。

**Architecture:** 抽取共享 `LoadGate`（`src/yuki/cognition/load_gate.py`）管理三态——disabled（配置关闭，永不加载）、failed-but-retriable（时间窗口后重试）、ready/loaded。三个模型类把 `_load_failed: bool` 换成 `_gate: LoadGate`，入口方法（`understand`/`recognize`/`generate`）加 fast-path 短路，VLM 失败结果不再入 cache。`agent.py` 健康检查改用模型 `health()` 方法（保留对无 `health()` 的注入 fake 的向后兼容回退）。

**Tech Stack:** Python ≥3.11，pytest。无新增运行时依赖。

## Global Constraints

- `LoadGate` 语义：`disabled()` 永远不可加载；`can_load()` 在失败窗口内为 False，窗口过后恢复 True；`mark_failure()/mark_success()` 记录状态；`error_message()` 返回可读拒绝原因。
- **关键修正**（采纳评审盲区修正）：`enabled=False`（disabled）与 `_load_failed`（failed）必须分离——禁用模型永远不尝试加载，不因时间窗口反复触发。
- 注入的已加载模型（`model`/`tokenizer`/`processor` 非空）必须继续工作：fast-path 只拦截 `not self._loaded` 且 gate 拒绝的情况。
- 失败不抛向调用方噪声路径：fast-path 直接返回 degraded 结果 / 空串，不进入 `_infer → except → log` 循环。
- 不新增运行时依赖。每个任务结束跑指定测试；全部完成后跑 `python -m pytest`（e2e 默认跳过）。

---

## 文件结构

**新增**
- `src/yuki/cognition/load_gate.py` — `LoadGate` 三态加载门
- `tests/cognition/test_load_gate.py`
- `tests/cognition/test_local_model.py`（LocalChatModel 原无覆盖测试）

**修改**
- `src/yuki/cognition/vlm.py`（`VisualUnderstander`：gate + fast-path + 不缓存 degraded）
- `src/yuki/cognition/stt.py`（`SpeechRecognizer`：gate + fast-path）
- `src/yuki/cognition/brain/local/model.py`（`LocalChatModel`：gate + warmup 短路）
- `src/yuki/cognition/agent.py`（`_health_vlm`/`_health_stt` 用 health()）
- 测试：`tests/cognition/test_vlm.py`、`tests/cognition/test_stt.py`、`tests/cognition/test_cognition.py`、`tests/cognition/test_assembly.py`

---

### Task 1: 共享 LoadGate + 单测

**Files:**
- Create: `src/yuki/cognition/load_gate.py`
- Create: `tests/cognition/test_load_gate.py`

**Interfaces:**
- Consumes: 无。
- Produces: `LoadGate(*, enabled=True, retry_window_s=60.0, clock=time.monotonic)`，方法 `disabled() -> bool`、`can_load() -> bool`、`mark_failure() -> None`、`mark_success() -> None`、`error_message() -> str | None`、`health() -> dict`。Task 2/3/4 依赖。

- [ ] **Step 1: 创建 `tests/cognition/test_load_gate.py`（先红）**

```python
import time

import pytest

from yuki.cognition.load_gate import LoadGate


def test_enabled_gate_ready():
    gate = LoadGate(enabled=True)
    assert gate.disabled() is False
    assert gate.can_load() is True
    assert gate.error_message() is None


def test_disabled_gate_never_loads():
    gate = LoadGate(enabled=False)
    assert gate.disabled() is True
    assert gate.can_load() is False
    assert gate.error_message() == "model disabled"


def test_failure_blocks_until_window_passes():
    now = [0.0]
    gate = LoadGate(retry_window_s=10.0, clock=lambda: now[0])
    gate.mark_failure()
    assert gate.can_load() is False
    assert gate.error_message() == "model load previously failed"
    now[0] = 9.0
    assert gate.can_load() is False
    now[0] = 10.0
    assert gate.can_load() is True
    assert gate.error_message() is None


def test_success_resets_failure_state():
    now = [0.0]
    gate = LoadGate(retry_window_s=10.0, clock=lambda: now[0])
    gate.mark_failure()
    gate.mark_success()
    assert gate.can_load() is True
    assert gate.error_message() is None


def test_health_reports_degraded_not_just_healthy():
    now = [0.0]
    gate = LoadGate(retry_window_s=10.0, clock=lambda: now[0])
    assert gate.health()["degraded"] is False
    gate.mark_failure()
    health = gate.health()
    assert health["degraded"] is True
    assert health["retry_after_s"] > 0
```

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/cognition/test_load_gate.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'yuki.cognition.load_gate'`）。

- [ ] **Step 3: 创建 `src/yuki/cognition/load_gate.py`**

```python
import time
from typing import Callable


class LoadGate:
    """三态模型加载门：disabled / failed-but-retriable / ready。

    disabled（enabled=False）永远拒绝；失败进入时间窗口，窗口过后可重试。
    注入 clock 便于测试推进时间。
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        retry_window_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._enabled = enabled
        self._retry_window_s = max(0.0, float(retry_window_s))
        self._clock = clock
        self._failed_until: float | None = None

    def disabled(self) -> bool:
        return not self._enabled

    def can_load(self) -> bool:
        if not self._enabled:
            return False
        if self._failed_until is None:
            return True
        return self._clock() >= self._failed_until

    def mark_failure(self) -> None:
        self._failed_until = self._clock() + self._retry_window_s

    def mark_success(self) -> None:
        self._failed_until = None

    def error_message(self) -> str | None:
        if not self._enabled:
            return "model disabled"
        if not self.can_load():
            return "model load previously failed"
        return None

    def health(self) -> dict:
        return {
            "enabled": self._enabled,
            "degraded": not self.can_load(),
            "retry_after_s": max(0.0, (self._failed_until - self._clock())) if self._failed_until else 0.0,
        }
```

- [ ] **Step 4: 运行验证通过**

Run: `python -m pytest tests/cognition/test_load_gate.py -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add src/yuki/cognition/load_gate.py tests/cognition/test_load_gate.py
git commit -m "feat: add LoadGate three-state model load gate with retry window"
```

---

### Task 2: VLM 时间窗口重试 + 不缓存 degraded + fast-path

**Files:**
- Modify: `src/yuki/cognition/vlm.py`
- Modify: `tests/cognition/test_vlm.py`
- Modify: `tests/cognition/test_assembly.py`（`vlm._load_failed` 断言改 gate）

**Interfaces:**
- Consumes: `LoadGate`（Task 1）。
- Produces: `VisualUnderstander(..., enabled=True, retry_window_s=60.0, clock=time.monotonic)`；新增 `health() -> dict`；`understand`/`understand_for_question` 在 gate 拒绝时直接返回 degraded，且失败结果不写 cache。`agent.py` Task 5 依赖 `health()`。

- [ ] **Step 1: 更新 `tests/cognition/test_vlm.py` 的失败语义断言（先红）**

替换 `test_disabled_vlm_never_loads` 与 `test_load_failure_is_remembered`：

```python
def test_disabled_vlm_never_loads():
    vlm = VisualUnderstander(enabled=False)
    assert vlm._gate.disabled() is True
    assert vlm._gate.can_load() is False
    vlm.warmup()
    assert vlm._loaded is False


def test_load_failure_is_remembered_within_window(monkeypatch):
    import sys

    calls = []

    class FakeAutoModel:
        @staticmethod
        def from_pretrained(*args, **kwargs):
            calls.append((args, kwargs))
            raise RuntimeError("missing model")

    fake_transformers = types.SimpleNamespace(
        AutoModelForImageTextToText=FakeAutoModel,
        AutoProcessor=object,
        BitsAndBytesConfig=lambda **kw: {"cfg": kw},
    )
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)

    vlm = VisualUnderstander()

    first = vlm.understand(None)
    second = vlm.understand(None)

    assert first["degraded"] is True
    assert second["degraded"] is True
    assert second["reason"] == "model load previously failed"
    assert len(calls) == 1  # 窗口内 fast-path，不再次尝试加载
```

追加新测试：

```python
def test_understand_fast_path_does_not_hit_infer_within_window(monkeypatch):
    import sys

    calls = []

    class FakeAutoModel:
        @staticmethod
        def from_pretrained(*args, **kwargs):
            calls.append(1)
            raise RuntimeError("missing model")

    fake_transformers = types.SimpleNamespace(
        AutoModelForImageTextToText=FakeAutoModel,
        AutoProcessor=object,
        BitsAndBytesConfig=lambda **kw: {"cfg": kw},
    )
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)

    vlm = VisualUnderstander()
    result = vlm.understand(None)
    assert result["degraded"] is True
    assert vlm._gate.can_load() is False
    assert len(calls) == 1


def test_understand_recovers_after_retry_window(monkeypatch):
    import sys

    now = [0.0]

    class FakeAutoModel:
        @staticmethod
        def from_pretrained(*args, **kwargs):
            raise RuntimeError("missing model")

    fake_transformers = types.SimpleNamespace(
        AutoModelForImageTextToText=FakeAutoModel,
        AutoProcessor=object,
        BitsAndBytesConfig=lambda **kw: {"cfg": kw},
    )
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)

    vlm = VisualUnderstander(retry_window_s=10.0, clock=lambda: now[0])
    first = vlm.understand(None)
    assert first["degraded"] is True
    assert vlm._gate.can_load() is False
    now[0] = 10.0
    assert vlm._gate.can_load() is True  # 窗口过后允许重试


def test_understand_does_not_cache_degraded_result():
    calls = []

    class FakeModel:
        def generate(self, *a, **kw):
            calls.append(1)
            return "noop"

    class FakeProcessor:
        def apply_chat_template(self, messages, tokenize=False, **kw):
            return "template"

    vlm = VisualUnderstander(model=FakeModel(), processor=FakeProcessor())

    def boom(image):
        raise RuntimeError("oom")

    vlm._infer = boom
    first = vlm.understand(None, cache_key="k1")
    assert first["degraded"] is True
    assert vlm._cache.get("k1") is None  # 失败结果不写 cache
```

- [ ] **Step 2: 更新 `tests/cognition/test_assembly.py` 断言**

`tests/cognition/test_assembly.py:108`：

```python
    vlm = assembler._build_vlm()
    assert vlm._gate.disabled() is True
    assert vlm._gate.can_load() is False
```

- [ ] **Step 3: 运行验证失败**

Run: `python -m pytest tests/cognition/test_vlm.py tests/cognition/test_assembly.py -v`
Expected: FAIL（`AttributeError: 'VisualUnderstander' object has no attribute '_gate'` 等）。

- [ ] **Step 4: 改写 `src/yuki/cognition/vlm.py`**

在 `VisualUnderstander` 中：
- import 区新增：`from yuki.cognition.load_gate import LoadGate`
- `__init__` 新增参数与替换状态：

```python
    def __init__(
        self,
        model=None,
        processor=None,
        cache: ContextCache | None = None,
        *,
        model_id: str = "Qwen/Qwen3-VL-8B-Instruct",
        cache_dir: str = "",
        enabled: bool = True,
        retry_window_s: float = 60.0,
        clock=None,
    ) -> None:
        self._model = model
        self._processor = processor
        self._cache = cache or ContextCache()
        self._loaded = model is not None and processor is not None
        self._gate = LoadGate(
            enabled=enabled,
            retry_window_s=retry_window_s,
            clock=clock or time.monotonic,
        )
        self._load_lock = threading.Lock()
        self._model_id = model_id
        self._cache_dir = cache_dir
```

- `_load()` 改为 gate 驱动：

```python
    def _load(self) -> None:
        if self._loaded:
            return
        with self._load_lock:
            if self._loaded:
                return
            error = self._gate.error_message()
            if error:
                raise RuntimeError(error)
            try:
                from transformers import (
                    AutoProcessor,
                    AutoModelForImageTextToText,
                    BitsAndBytesConfig,
                )
                quant = BitsAndBytesConfig(
                    load_in_4bit=True,
                    bnb_4bit_quant_type="nf4",
                    bnb_4bit_compute_dtype="float16",
                )
                self._model = AutoModelForImageTextToText.from_pretrained(
                    self._model_id,
                    cache_dir=self._cache_dir or None,
                    torch_dtype="auto",
                    device_map="auto",
                    quantization_config=quant,
                )
                self._processor = AutoProcessor.from_pretrained(
                    self._model_id, cache_dir=self._cache_dir or None
                )
                self._loaded = True
                self._gate.mark_success()
            except Exception:
                self._gate.mark_failure()
                raise
```

- `warmup()` 用 gate 短路：

```python
    def warmup(self) -> None:
        if self._loaded or not self._gate.can_load():
            return

        def _load_thread():
            try:
                self._load()
            except Exception:
                logger.warning("vlm warmup failed, will degrade to text mode", exc_info=True)

        threading.Thread(target=_load_thread, daemon=True).start()
```

- `understand()` 与 `understand_for_question()` 加 fast-path + 不缓存 degraded：

```python
    def understand(self, image, cache_key: str | None = None) -> dict:
        if cache_key:
            hit = self._cache.get(cache_key)
            if hit is not None:
                return hit
        if not self._loaded:
            error = self._gate.error_message()
            if error is not None:
                return {"topic": "", "summary": "", "content_type": "unknown",
                        "key_points": [], "degraded": True, "reason": error}
        try:
            result = self._infer(image)
        except Exception:
            logger.exception("vlm inference failed, degrading")
            result = {"topic": "", "summary": "", "content_type": "unknown",
                      "key_points": [], "degraded": True, "reason": "inference_failed"}
        if not isinstance(result, dict):
            result = self._parse(result if isinstance(result, str) else "")
        if cache_key and not result.get("degraded"):
            self._cache.put(cache_key, result)
        return result

    def understand_for_question(self, image, question: str, cache_key: str | None = None) -> dict:
        if cache_key:
            hit = self._cache.get(cache_key)
            if hit is not None:
                return hit
        if not self._loaded:
            error = self._gate.error_message()
            if error is not None:
                return {"topic": "", "summary": "", "content_type": "unknown",
                        "key_points": [], "can_answer": False,
                        "degraded": True, "reason": error}
        try:
            result = self._infer_for_question(image, question)
        except Exception:
            logger.exception("vlm question inference failed, degrading")
            result = {
                "topic": "",
                "summary": "",
                "content_type": "unknown",
                "key_points": [],
                "can_answer": False,
                "degraded": True,
                "reason": "inference_failed",
            }
        if not isinstance(result, dict):
            result = self._parse(result if isinstance(result, str) else "", include_can_answer=True)
        result["can_answer"] = bool(result.get("can_answer", False))
        if cache_key and not result.get("degraded"):
            self._cache.put(cache_key, result)
        return result
```

- 新增 `health()`：

```python
    def health(self) -> dict:
        return {"loaded": self._loaded, **self._gate.health()}
```

- [ ] **Step 5: 运行验证通过**

Run: `python -m pytest tests/cognition/test_vlm.py tests/cognition/test_assembly.py -v`
Expected: 全 PASS。

- [ ] **Step 6: Commit**

```bash
git add src/yuki/cognition/vlm.py tests/cognition/test_vlm.py tests/cognition/test_assembly.py
git commit -m "fix: retry-window model loading for VLM, stop caching degraded results"
```

---

### Task 3: STT 时间窗口重试 + fast-path

**Files:**
- Modify: `src/yuki/cognition/stt.py`
- Modify: `tests/cognition/test_stt.py`

**Interfaces:**
- Consumes: `LoadGate`（Task 1）。
- Produces: `SpeechRecognizer(model=None, sample_rate=16000, *, enabled=True, retry_window_s=60.0, clock=time.monotonic)`；新增 `health()`；`recognize` 在 gate 拒绝时直接返回 `""`。

- [ ] **Step 1: 追加失败测试到 `tests/cognition/test_stt.py`**

```python
def test_recognize_fast_path_within_retry_window(monkeypatch):
    import sys

    now = [0.0]
    calls = []

    class FakeAutoModel:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            raise RuntimeError("missing model")

    fake_funasr = types.SimpleNamespace(AutoModel=FakeAutoModel)
    monkeypatch.setitem(sys.modules, "funasr", fake_funasr)

    stt = SpeechRecognizer(retry_window_s=10.0, clock=lambda: now[0])
    samples = np.zeros(320, dtype=np.float32)

    assert stt.recognize(samples) == ""
    assert len(calls) == 1
    # 窗口内 fast-path：不再调用 funasr
    assert stt.recognize(samples) == ""
    assert len(calls) == 1
    # 窗口过后允许重试
    now[0] = 10.0
    assert stt._gate.can_load() is True


def test_health_reports_degraded_when_failed(monkeypatch):
    import sys

    class FakeAutoModel:
        def __init__(self, **kwargs):
            raise RuntimeError("missing model")

    fake_funasr = types.SimpleNamespace(AutoModel=FakeAutoModel)
    monkeypatch.setitem(sys.modules, "funasr", fake_funasr)

    stt = SpeechRecognizer()
    stt.recognize(np.zeros(320, dtype=np.float32))
    health = stt.health()
    assert health["degraded"] is True
    assert health["loaded"] is False
```

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/cognition/test_stt.py -v`
Expected: FAIL（`AttributeError: 'SpeechRecognizer' object has no attribute '_gate'`）。

- [ ] **Step 3: 改写 `src/yuki/cognition/stt.py`**

```python
import base64

import numpy as np

from yuki.cognition.load_gate import LoadGate
from yuki.logger import get_logger

logger = get_logger("yuki.cognition.stt")


class SpeechRecognizer:
    """SenseVoice-Small 语音识别：中英混合，带情感/事件标注。"""

    def __init__(
        self,
        model=None,
        sample_rate: int = 16000,
        *,
        enabled: bool = True,
        retry_window_s: float = 60.0,
        clock=None,
    ) -> None:
        self._model = model
        self._sample_rate = sample_rate
        self._loaded = model is not None
        self._gate = LoadGate(
            enabled=enabled,
            retry_window_s=retry_window_s,
            clock=clock or time.monotonic,
        )

    def _load(self) -> None:
        if self._loaded:
            return
        error = self._gate.error_message()
        if error:
            raise RuntimeError(error)
        try:
            from funasr import AutoModel
            self._model = AutoModel(model="iic/SenseVoiceSmall")
            self._loaded = True
            self._gate.mark_success()
        except Exception:
            self._gate.mark_failure()
            raise

    def _infer(self, samples: np.ndarray, sample_rate: int) -> str:
        self._load()
        result = self._model(input=samples.astype(np.float32), fs=sample_rate)
        if isinstance(result, list) and result:
            return str(result[0].get("text", ""))
        return ""

    def recognize(self, samples: np.ndarray, sample_rate: int = 16000) -> str:
        if samples is None or len(samples) == 0:
            return ""
        if not self._loaded and self._gate.error_message() is not None:
            return ""
        try:
            return self._infer(samples, sample_rate)
        except Exception:
            logger.exception("stt inference failed")
            return ""

    def recognize_base64(self, pcm_b64: str, sample_rate: int = 16000) -> str:
        if not pcm_b64:
            return ""
        try:
            raw = base64.b64decode(pcm_b64)
            samples = np.frombuffer(raw, dtype=np.float32)
        except (ValueError, base64.binascii.Error):
            logger.warning("invalid pcm base64")
            return ""
        return self.recognize(samples, sample_rate)

    def health(self) -> dict:
        return {"loaded": self._loaded, **self._gate.health()}
```

注：`stt.py` 需要补 `import time`（顶部）。

- [ ] **Step 4: 运行验证通过**

Run: `python -m pytest tests/cognition/test_stt.py -v`
Expected: 全 PASS（原 `test_load_failure_is_remembered` 因窗口内 fast-path，`len(calls) == 1` 仍成立）。

- [ ] **Step 5: Commit**

```bash
git add src/yuki/cognition/stt.py tests/cognition/test_stt.py
git commit -m "fix: retry-window model loading for STT with fast-path short-circuit"
```

---

### Task 4: LocalChatModel 时间窗口重试 + warmup 短路

**Files:**
- Modify: `src/yuki/cognition/brain/local/model.py`
- Create: `tests/cognition/test_local_model.py`

**Interfaces:**
- Consumes: `LoadGate`（Task 1）。
- Produces: `LocalChatModel(..., enabled=True, retry_window_s=60.0, clock=time.monotonic)`；新增 `health()`；`warmup` 在 gate 拒绝时跳过。

- [ ] **Step 1: 创建 `tests/cognition/test_local_model.py`（先红）**

```python
import types

import pytest

from yuki.cognition.brain.local.model import LocalChatModel


def test_disabled_model_never_loads():
    model = LocalChatModel(enabled=False)
    assert model._gate.disabled() is True
    model.warmup()
    assert model._loaded is False


def test_load_failure_blocks_until_window(monkeypatch):
    import sys

    now = [0.0]
    calls = []

    class FakeAutoModel:
        @staticmethod
        def from_pretrained(*args, **kwargs):
            calls.append((args, kwargs))
            raise RuntimeError("missing model")

    class FakeTokenizer:
        @staticmethod
        def from_pretrained(*args, **kwargs):
            raise RuntimeError("missing tokenizer")

    fake_transformers = types.SimpleNamespace(
        AutoModelForCausalLM=FakeAutoModel,
        AutoTokenizer=FakeTokenizer,
    )
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)

    model = LocalChatModel(retry_window_s=10.0, clock=lambda: now[0])
    with pytest.raises(RuntimeError, match="load previously failed"):
        model._load()
    assert model._loaded is False
    with pytest.raises(RuntimeError, match="load previously failed"):
        model._load()  # 窗口内再次拒绝
    assert len(calls) == 1
    now[0] = 10.0
    assert model._gate.can_load() is True


def test_warmup_skips_when_gate_rejects(monkeypatch):
    import sys

    class FakeAutoModel:
        @staticmethod
        def from_pretrained(*args, **kwargs):
            raise RuntimeError("missing model")

    class FakeTokenizer:
        @staticmethod
        def from_pretrained(*args, **kwargs):
            raise RuntimeError("missing tokenizer")

    fake_transformers = types.SimpleNamespace(
        AutoModelForCausalLM=FakeAutoModel,
        AutoTokenizer=FakeTokenizer,
    )
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)

    model = LocalChatModel()
    with pytest.raises(RuntimeError):
        model._load()  # 触发失败
    model.warmup()  # 窗口内 warmup 应直接跳过，不 spawn 线程
    assert model._loaded is False
```

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/cognition/test_local_model.py -v`
Expected: FAIL（`AttributeError: 'LocalChatModel' object has no attribute '_gate'`）。

- [ ] **Step 3: 改写 `src/yuki/cognition/brain/local/model.py`**

- import 区新增：

```python
import threading
import time
from collections.abc import Sequence

from yuki.cognition.load_gate import LoadGate
from yuki.logger import get_logger
```

- `__init__`：

```python
    def __init__(
        self,
        model=None,
        tokenizer=None,
        *,
        model_id: str = "Qwen/Qwen3-1.7B-FP8",
        cache_dir: str = "",
        device: str = "auto",
        enabled: bool = True,
        fp8_dequantize: bool = False,
        local_files_only: bool = False,
        retry_window_s: float = 60.0,
        clock=None,
    ) -> None:
        self._model = model
        self._tokenizer = tokenizer
        self._model_id = model_id
        self._cache_dir = cache_dir
        self._device = device
        self._fp8_dequantize = fp8_dequantize
        self._local_files_only = local_files_only
        self._loaded = model is not None and tokenizer is not None
        self._gate = LoadGate(
            enabled=enabled,
            retry_window_s=retry_window_s,
            clock=clock or time.monotonic,
        )
        self._load_lock = threading.Lock()
        self._infer_lock = threading.Lock()
```

- `warmup`：

```python
    def warmup(self) -> None:
        if self._loaded or not self._gate.can_load():
            return

        def _load_thread() -> None:
            try:
                self._load()
            except Exception:
                logger.warning("local model warmup failed", exc_info=True)

        threading.Thread(target=_load_thread, daemon=True, name="yuki-local-model-warmup").start()
```

- `_load`：

```python
    def _load(self) -> None:
        if self._loaded:
            return
        with self._load_lock:
            if self._loaded:
                return
            error = self._gate.error_message()
            if error:
                raise RuntimeError(error)
            try:
                from transformers import AutoModelForCausalLM, AutoTokenizer

                kwargs = {
                    "cache_dir": self._cache_dir or None,
                    "torch_dtype": "auto",
                    "trust_remote_code": True,
                    "local_files_only": self._local_files_only,
                }
                if self._device == "auto":
                    kwargs["device_map"] = "auto"
                if self._fp8_dequantize:
                    from transformers import FineGrainedFP8Config

                    kwargs["quantization_config"] = FineGrainedFP8Config(dequantize=True)
                self._model = AutoModelForCausalLM.from_pretrained(self._model_id, **kwargs)
                self._tokenizer = AutoTokenizer.from_pretrained(
                    self._model_id,
                    cache_dir=self._cache_dir or None,
                    trust_remote_code=True,
                    local_files_only=self._local_files_only,
                )
                generation_config = getattr(self._model, "generation_config", None)
                if generation_config is not None and hasattr(generation_config, "enable_thinking"):
                    generation_config.enable_thinking = False
                if self._device != "auto" and hasattr(self._model, "to"):
                    self._model.to(self._device)
                self._loaded = True
                self._gate.mark_success()
            except Exception:
                self._gate.mark_failure()
                raise
```

- `generate` 保留（`self._load()` 内已 fast 拒绝），新增 `health()`：

```python
    def health(self) -> dict:
        return {"loaded": self._loaded, **self._gate.health()}
```

- [ ] **Step 4: 运行验证通过**

Run: `python -m pytest tests/cognition/test_local_model.py -v`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add src/yuki/cognition/brain/local/model.py tests/cognition/test_local_model.py
git commit -m "fix: retry-window model loading for LocalChatModel"
```

---

### Task 5: 健康检查接入 degraded

**Files:**
- Modify: `src/yuki/cognition/agent.py`
- Modify: `tests/cognition/test_cognition.py`

**Interfaces:**
- Consumes: `VisualUnderstander.health()`、`SpeechRecognizer.health()`（Task 2/3）。
- Produces: `_health_vlm`/`_health_stt` 返回 `degraded=True`（而非 `healthy=False`），并对无 `health()` 的注入 fake 保留回退。

- [ ] **Step 1: 更新 `tests/cognition/test_cognition.py` 的 VLM 健康断言**

现有 `test_cognition_agent_vlm_health_degrades_while_loading` 用 `FakeVlm(loaded=False)`（无 `health()`），必须保留回退路径使其仍返回 `{"loaded": False, "degraded": True, "reason": "loading"}`——无需改。追加真实 VLM 的断言：

```python
def test_cognition_agent_vlm_health_degraded_via_gate(tmp_path):
    bus = FakeBus()
    pipeline = FakePipeline()
    pipeline._vlm = VisualUnderstander(enabled=False)  # 真实 VLM，disabled
    agent = CognitionAgent(
        Config(persona={"snapshots_path": str(tmp_path / "persona.json")}),
        bus=bus,
        pipeline=pipeline,
        memory=MemoryManager(MemoryStore(tmp_path / "mem.db")),
    )
    status = agent.health_components()["vlm"]()
    assert status.ok is True  # 不触发重启
    assert status.detail["degraded"] is True
    assert status.detail["enabled"] is False
```

- [ ] **Step 2: 运行验证失败**

Run: `python -m pytest tests/cognition/test_cognition.py::test_cognition_agent_vlm_health_degraded_via_gate -v`
Expected: FAIL（当前 `_health_vlm` 走 `getattr(vlm, "_loaded", ...)` + `_load_failed` 回退，detail 不含 `enabled`）。

- [ ] **Step 3: 改写 `src/yuki/cognition/agent.py` 健康方法**

```python
    def _health_vlm(self) -> HealthStatus:
        vlm = getattr(self._pipeline, "_vlm", None) if self._pipeline else None
        if vlm is None:
            return HealthStatus(True, {"loaded": False, "degraded": True, "reason": "no_vlm"})
        health_fn = getattr(vlm, "health", None)
        if callable(health_fn):
            detail = health_fn()
            detail["degraded"] = bool(detail.get("degraded", False))
            return HealthStatus(True, detail)
        loaded = bool(getattr(vlm, "_loaded", False))
        detail = {"loaded": loaded, "degraded": not loaded}
        if not loaded:
            detail["reason"] = "unavailable" if getattr(vlm, "_load_failed", False) else "loading"
        return HealthStatus(True, detail)

    def _health_stt(self) -> HealthStatus:
        stt = getattr(self._pipeline, "_stt", None) if self._pipeline else None
        if stt is None:
            return HealthStatus(True, {"installed": False, "degraded": True, "reason": "no_stt"})
        health_fn = getattr(stt, "health", None)
        if callable(health_fn):
            return HealthStatus(True, {"installed": True, **health_fn()})
        return HealthStatus(True, {"installed": True})
```

- [ ] **Step 4: 运行验证通过**

Run: `python -m pytest tests/cognition/test_cognition.py -v`
Expected: 全 PASS。

- [ ] **Step 5: 全仓回归**

Run: `python -m pytest`
Expected: 全 PASS（e2e 默认跳过）。

- [ ] **Step 6: Commit**

```bash
git add src/yuki/cognition/agent.py tests/cognition/test_cognition.py
git commit -m "feat: report degraded (not unhealthy) for model load state in health"
```

---

## Self-Review 记录

- **Spec coverage：** 主题 2 四目标全覆盖——时间窗口重试（Task 2/3/4）、disabled/failed 分离（LoadGate + 各构造器 `enabled`）、VLM 失败不写 cache（Task 2 Step 4）、入口 fast-path 短路（Task 2/3 Step 4）、health degraded（Task 5）。
- **Placeholder scan：** 无 TBD/TODO；每个代码步骤含完整可粘贴代码。
- **Type consistency：** `LoadGate` 在 Task 1 定义、Task 2/3/4 同名调用；`health()` 在 Task 2/3/4 定义、Task 5 用 `callable(health_fn)` 探测 + 回退，兼容 `FakeVlm` 等注入对象。
- **关键行为保持：** 注入已加载模型（`model`/`processor` 非空）不受 gate 影响（fast-path 前置 `not self._loaded`）；STT `test_load_failure_is_remembered` 窗口内 `len(calls) == 1` 仍成立。
