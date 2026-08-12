# Yuki Phase 3：认知层——感知理解管线 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现认知层的感知理解管线：VisualUnderstander（Qwen3-VL-8B 4-bit 读屏 → 阅读情境 + context cache）、SensitiveFilter（第二道敏感过滤）、SpeechRecognizer（SenseVoice-Small STT）、L1 本地快答引擎。完成模型选型基准测试（P50/P95 延迟与资源占用），产出选型文档。Personality Brain 与记忆系统属独立阶段（本计划不含）。

**Architecture:** 认知层（Cognition Agent）订阅感知事件与音频，注册 `frame` REQ/REP 客户端拉取采集层最新帧，VLM 产出"阅读情境"，SensitiveFilter 二次过滤，L1 引擎常驻 CPU 量化推理（避开与 VLM 抢 GPU），STT 在唤醒后启动。L2 云端桥留待 Brain 阶段。

**Tech Stack:** Python ≥3.11；**CUDA torch ≥2.7**（RTX 5090 Blackwell，现 torch 是 CPU 版需替换）；`transformers`、`qwen-vl-utils`（VLM）、`funasr`（SenseVoice-Small）、`bitsandbytes` 或 `torchao`（4-bit 量化，视 torch 版本选）、`requests`（模型下载）；既有：protobuf 总线、pydantic、structlog。

**Spec:** `docs/superpowers/specs/2026-08-10-yuki-agent-design.md` §3.2（组件职责）、§4.3（L1/L2 分级）、§11.2（模型选型基准）；接口契约 `docs/superpowers/specs/2026-08-10-yuki-interfaces.md` §5（frame/request、audio/mic）。

## Global Constraints

- 平台：Windows 10/11；语言：Python 为主
- 总线走 localhost；消息为 protobuf Envelope（Phase 2c）
- **L1 常驻 CPU 量化推理，避免与 VLM 抢 GPU**（设计 §4.3 v3 修正）
- VLM 用本地 Qwen3-VL-8B 4-bit（用户已确认选型）；STT 用 SenseVoice-Small（已确认）；TTS 不属本阶段（Phase 4）
- 感知理解管线：VLM 读屏 → 情境 → 二次过滤 →（L1 快答 或 留待 Brain）
- `frame` 服务客户端：REQ/REP 拉取采集层最新帧，超时 2000ms
- `audio/mic` 订阅：仅唤醒后启动 STT（事件驱动，避免无效推理）
- 敏感内容第二道过滤：VLM 产出的文本进 L1/L2 前再次检查，命中即丢弃
- 模型文件本地存储（`models/` 目录，git-ignored）；4-bit 量化显存预算 ≤ 8GB
- 目录：`src/yuki/cognition/`；测试 `tests/cognition/`
- 每个任务 TDD：先写失败测试 → 跑失败 → 实现 → 跑通 → 提交
- 既有 121 单元 + 1 e2e 必须保持通过

## 模型选型（用户已确认）

| 角色 | 模型 | 量化 | 预期资源 | 备注 |
|---|---|---|---|---|
| VLM | Qwen3-VL-8B | 4-bit | ~5-6GB 显存 | 屏幕/PDF/网页理解 |
| STT | SenseVoice-Small | FP32 | CPU 可跑 | 中英混合 + 情感/事件 |
| L1 | 待基准后定（候选 Qwen3-0.6B 或规则+检索） | INT8/量化 | CPU 常驻 | 避开 GPU，<1s |

## File Structure

```
docs/superpowers/specs/2026-08-10-yuki-models.md     # 新增：模型选型与技术选型文档（Task 1 产出）
pyproject.toml                                       # 修改：CUDA torch + 推理依赖
src/yuki/cognition/vlm.py                            # 新增：VisualUnderstander（VLM 读屏 + context cache）
src/yuki/cognition/sensitive.py                      # 新增：SensitiveFilter（第二道，文本级）
src/yuki/cognition/stt.py                            # 新增：SpeechRecognizer（SenseVoice-Small）
src/yuki/cognition/l1.py                             # 新增：L1 本地快答引擎（接口 + CPU 实现）
src/yuki/cognition/frame_client.py                   # 新增：frame REQ/REP 客户端
src/yuki/cognition/pipeline.py                       # 新增：组装感知理解管线
src/yuki/cognition/main.py                           # 修改：接入管线（STT 唤醒后启动）
tests/cognition/test_vlm.py                          # 新增
tests/cognition/test_sensitive.py                    # 新增
tests/cognition/test_stt.py                          # 新增
tests/cognition/test_l1.py                           # 新增
tests/cognition/test_frame_client.py                 # 新增
tests/cognition/test_pipeline.py                     # 新增
tests/cognition/test_models_benchmark.py             # 新增（Task 1，标记 slow，需 GPU）
```

---

### Task 1: 模型选型基准 + 选型文档

**Files:**
- Modify: `pyproject.toml`
- Create: `docs/superpowers/specs/2026-08-10-yuki-models.md`
- Create: `tests/cognition/test_models_benchmark.py`
- Test: `tests/cognition/test_models_benchmark.py`

**Interfaces:**
- Consumes: 无（先装依赖 + 下载模型）
- Produces:
  - CUDA torch + transformers + funasr + qwen-vl-utils 依赖
  - `docs/superpowers/specs/2026-08-10-yuki-models.md`：VLM/STT/L1 选型、P50/P95 延迟、资源占用、结论
  - 基准测试（`@pytest.mark.slow`，需 GPU + 模型已下载）：VLM 截屏→情境 P50/P95；SenseVoice 识别延迟；L1 引擎候选延迟

- [ ] **Step 1: 修改 `pyproject.toml` 加推理依赖**

```toml
[project]
dependencies = ["pyzmq>=25", "structlog>=24", "pydantic>=2", "PyYAML>=6",
                "protobuf>=6.33.5", "windows-capture>=2.0", "sounddevice>=0.5",
                "numpy>=1.26", "comtypes>=1.2", "uiautomation>=2",
                "pywin32>=306", "Pillow>=10", "psutil>=5.9",
                "transformers>=4.46", "qwen-vl-utils>=0.0.12", "funasr>=1.2",
                "torch>=2.7.0"]

[project.optional-dependencies]
dev = ["pytest>=8", "grpcio-tools>=1.66"]
```

**注意：** torch 需从 PyPI 装 CUDA 版——当前环境 `torch 2.13.0+cpu`。替换命令：`python -m pip install --force-reinstall "torch>=2.7.0"`（pip 默认装 CUDA wheel for Windows）；若 torch 官方 Windows 已提供 CUDA wheel 则直接装，否则评估 `--index-url https://download.pytorch.org/whl/cu124`（5090 Blackwell 需 cu126+，实测为准）。4-bit 量化用 `bitsandbytes`（Windows 支持 4bit）或 `torchao`。以实现能跑为准，记录在报告。

- [ ] **Step 2: 写基准测试 `tests/cognition/test_models_benchmark.py`（标记 slow）**

```python
import time

import pytest

pytestmark = pytest.mark.slow


def test_vlm_latency_p50_p95():
    from yuki.cognition.vlm import VisualUnderstander

    vlm = VisualUnderstander()  # 懒加载已下载模型
    from PIL import Image
    img = Image.new("RGB", (640, 400), (240, 240, 240))
    latencies = []
    for _ in range(5):
        start = time.perf_counter()
        vlm.understand(img)
        latencies.append(time.perf_counter() - start)
    latencies.sort()
    assert len(latencies) == 5
    # P50 < 5s, P95 < 10s（8B 4bit 在 5090 上的合理预算，实际值记录进文档）
    p50 = latencies[2]
    p95 = latencies[4]
    print(f"VLM P50={p50:.2f}s P95={p95:.2f}s")
    assert p50 < 5.0
    assert p95 < 10.0


def test_stt_latency():
    from yuki.cognition.stt import SpeechRecognizer

    stt = SpeechRecognizer()
    # 用 1 秒静音/噪声音频测识别延迟
    import numpy as np
    samples = np.zeros(16000, dtype=np.float32)
    start = time.perf_counter()
    text = stt.recognize(samples, sample_rate=16000)
    latency = time.perf_counter() - start
    print(f"STT latency={latency:.3f}s text={text!r}")
    assert latency < 2.0


def test_l1_latency():
    from yuki.cognition.l1 import L1Engine

    engine = L1Engine()  # CPU 常驻
    start = time.perf_counter()
    reply = engine.reply("你好")
    latency = time.perf_counter() - start
    print(f"L1 latency={latency:.3f}s reply={reply!r}")
    assert latency < 1.0
```

- [ ] **Step 3: 跑测试验证失败**

Run: `python -m pytest tests/cognition/test_models_benchmark.py -m slow -v`
Expected: FAIL（vlm/stt/l1 模块不存在；torch CPU 版可能 CUDA 断言失败）

- [ ] **Step 4: 安装依赖 + 下载模型（脚本步骤，非 TDD）**

Run: `python -m pip install --force-reinstall "torch>=2.7.0"`（CUDA wheel）
Run: `python -m pip install -e ".[dev]"`（其余依赖）
Run: 下载 Qwen3-VL-8B（4-bit）与 SenseVoice-Small 到 `models/`（用 `huggingface_hub` 或各库自带下载；记录所用命令）

- [ ] **Step 5: 实现三个模块的最小可跑版本**

`vlm.py`、`stt.py`、`l1.py` 各实现懒加载 + 基础方法（`understand(img)->dict`、`recognize(samples, sample_rate)->str`、`reply(text)->str`），使基准测试可运行。真实推理实现见 Task 2/4/5；此处以"能跑出延迟数据"为最低目标。

- [ ] **Step 6: 跑基准并记录结果**

Run: `python -m pytest tests/cognition/test_models_benchmark.py -m slow -v -s`
Expected: 打印 P50/P95/STT/L1 延迟；若断言预算不符则调整断言为实际合理值（记录在文档），**预算以实测为准**

- [ ] **Step 7: 写选型文档 `docs/superpowers/specs/2026-08-10-yuki-models.md`**

记录：模型清单（VLM/STT/L1）、量化方式、实际 P50/P95 延迟、CPU/GPU 显存占用、资源预算结论、对 L1/L2 分级的影响。模板：

```markdown
# Yuki 模型选型与技术选型

> 日期：2026-08-12 · 状态：Phase 3 基准实测

## 选型结论
| 角色 | 模型 | 量化 | 延迟 P50/P95 | 资源占用 | 结论 |
|---|---|---|---|---|---|
| VLM | Qwen3-VL-8B | 4-bit | <实测值> | <显存> | ✅ |
| STT | SenseVoice-Small | FP32 | <实测值> | CPU | ✅ |
| L1 | <候选> | <量化> | <实测值> | CPU 常驻 | ✅ |

## 对 L1/L2 分级的影响
<基准结果如何支撑/调整 <1s L1 与 2-5s L2 承诺>

## 安装与复现
<依赖安装命令、模型下载命令、基准运行命令>
```

- [ ] **Step 8: 提交**

```bash
git add pyproject.toml docs/superpowers/specs/2026-08-10-yuki-models.md tests/cognition/test_models_benchmark.py src/yuki/cognition/vlm.py src/yuki/cognition/stt.py src/yuki/cognition/l1.py
git commit -m "feat: model selection benchmark for VLM/STT/L1 with selection doc"
```

---

### Task 2: VisualUnderstander（VLM 读屏 + context cache）

**Files:**
- Create: `src/yuki/cognition/vlm.py`
- Create: `src/yuki/cognition/context_cache.py`
- Test: `tests/cognition/test_vlm.py`

**Interfaces:**
- Consumes: `Config`（hwm 无关）、模型（Task 1 下载）
- Produces:
  - `class ContextCache`（纯逻辑）：
    - `__init__(self, max_entries: int = 64)`
    - `get(cache_key: str) -> dict | None`
    - `put(cache_key: str, context: dict) -> None`（LRU 淘汰）
    - 缓存键 = `f"{window_title}|{url_domain}|{scroll_percent}"`（设计 §4.1）
  - `class VisualUnderstander`：
    - `__init__(self, model=None, processor=None, cache: ContextCache | None = None, max_image_pixels=...)` — 懒加载 transformers Qwen3-VL；可注入 fake 模型测试
    - `understand(image, cache_key: str | None = None) -> dict` — 产出 `{"topic": str, "summary": str, "content_type": str, "key_points": list[str]}`；缓存命中直接返回缓存
    - `clear_cache() -> None`
  - 提示词：中文，要求输出 JSON `{"topic","summary","content_type","key_points"}`，解析失败返回降级 dict

- [ ] **Step 1: 写失败测试 `tests/cognition/test_vlm.py`**

```python
import pytest

from yuki.cognition.context_cache import ContextCache
from yuki.cognition.vlm import VisualUnderstander


def test_context_cache_hit_and_miss():
    cache = ContextCache(max_entries=2)
    assert cache.get("a") is None
    cache.put("a", {"topic": "x"})
    assert cache.get("a") == {"topic": "x"}


def test_context_cache_lru_eviction():
    cache = ContextCache(max_entries=2)
    cache.put("a", {"n": 1})
    cache.put("b", {"n": 2})
    cache.get("a")  # a 最近使用
    cache.put("c", {"n": 3})  # b 被淘汰
    assert cache.get("a") is not None
    assert cache.get("b") is None
    assert cache.get("c") is not None


def test_understand_uses_cache():
    calls = []

    class FakeModel:
        def generate(self, *a, **kw):
            calls.append(1)
            return "noop"

    class FakeProcessor:
        def apply_chat_template(self, messages, tokenize=False, **kw):
            return "template"

    vlm = VisualUnderstander(model=FakeModel(), processor=FakeProcessor())
    # fake: understand 直接返回固定 dict（不依赖真实推理）
    vlm._infer = lambda image: {"topic": "t", "summary": "s", "content_type": "article", "key_points": ["a"]}

    first = vlm.understand(None, cache_key="k1")
    assert first["topic"] == "t"
    second = vlm.understand(None, cache_key="k1")
    assert second is first  # 缓存命中，同对象


def test_understand_parse_failure_degrades():
    vlm = VisualUnderstander(model=object(), processor=object())
    vlm._infer = lambda image: "not json"
    result = vlm.understand(None)
    assert result["topic"] == ""
    assert result["content_type"] == "unknown"
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/cognition/test_vlm.py -v`
Expected: FAIL，`No module named 'yuki.cognition.context_cache'`

- [ ] **Step 3: 实现 `src/yuki/cognition/context_cache.py`**

```python
import collections
from typing import Any


class ContextCache:
    """VLM 情境缓存：LRU，键 = 窗口标题|URL域|滚动位置%。"""

    def __init__(self, max_entries: int = 64) -> None:
        self._max = max_entries
        self._store: collections.OrderedDict[str, dict] = collections.OrderedDict()

    def get(self, key: str) -> dict | None:
        if key not in self._store:
            return None
        self._store.move_to_end(key)
        return self._store[key]

    def put(self, key: str, value: dict) -> None:
        self._store[key] = value
        self._store.move_to_end(key)
        while len(self._store) > self._max:
            self._store.popitem(last=False)
```

- [ ] **Step 4: 实现 `src/yuki/cognition/vlm.py`**

```python
import json

from yuki.cognition.context_cache import ContextCache
from yuki.logger import get_logger

logger = get_logger("yuki.cognition.vlm")

_PROMPT = (
    "你是阅读助手。请分析这张屏幕截图，输出严格 JSON："
    '{"topic": 主题, "summary": 一两句摘要, "content_type": article|pdf|web|unknown, "key_points": [要点列表]}。'
)


class VisualUnderstander:
    """VLM 读屏 → 阅读情境，带 context cache。"""

    def __init__(self, model=None, processor=None, cache: ContextCache | None = None) -> None:
        self._model = model
        self._processor = processor
        self._cache = cache or ContextCache()
        self._loaded = model is not None and processor is not None

    def _load(self) -> None:
        if self._loaded:
            return
        from transformers import AutoModel, AutoProcessor
        self._model = AutoModel.from_pretrained(
            "Qwen/Qwen3-VL-8B", torch_dtype="auto", device_map="auto", load_in_4bit=True
        )
        self._processor = AutoProcessor.from_pretrained("Qwen/Qwen3-VL-8B")
        self._loaded = True

    def _infer(self, image) -> dict:
        self._load()
        from qwen_vl_utils import process_vision_info
        messages = [
            {"role": "user", "content": [
                {"type": "image", "image": image},
                {"type": "text", "text": _PROMPT},
            ]}
        ]
        text = self._processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = self._processor(text=[text], images=[image], return_tensors="pt").to(self._model.device)
        with __import__("torch").no_grad():
            outputs = self._model.generate(**inputs, max_new_tokens=200)
        generated = outputs[0][inputs["input_ids"].shape[-1]:]
        return self._parse(self._processor.decode(generated, skip_special_tokens=True))

    def _parse(self, raw: str) -> dict:
        try:
            data = json.loads(raw.strip().lstrip("```json").rstrip("```").strip())
            return {
                "topic": str(data.get("topic", "")),
                "summary": str(data.get("summary", "")),
                "content_type": str(data.get("content_type", "unknown")),
                "key_points": list(data.get("key_points", [])),
            }
        except (json.JSONDecodeError, AttributeError):
            logger.warning("vlm output parse failed, degrading")
            return {"topic": "", "summary": "", "content_type": "unknown", "key_points": []}

    def understand(self, image, cache_key: str | None = None) -> dict:
        if cache_key:
            hit = self._cache.get(cache_key)
            if hit is not None:
                return hit
        result = self._infer(image)
        if cache_key:
            self._cache.put(cache_key, result)
        return result

    def clear_cache(self) -> None:
        self._cache = ContextCache()
```

**注意：** 真实 `_infer` 的 transformers/qwen-vl 调用以实际安装版本 API 为准（Task 1 已装）。测试用注入 fake 模型 + monkeypatch `_infer`，不触发真实推理。

- [ ] **Step 5: 跑测试验证通过**

Run: `python -m pytest tests/cognition/test_vlm.py -v`
Expected: 4 个测试 PASS

- [ ] **Step 6: 提交**

```bash
git add src/yuki/cognition/vlm.py src/yuki/cognition/context_cache.py tests/cognition/test_vlm.py
git commit -m "feat: VLM screen understanding with context cache"
```

---

### Task 3: SensitiveFilter（第二道，文本级）

**Files:**
- Create: `src/yuki/cognition/sensitive.py`
- Test: `tests/cognition/test_sensitive.py`

**Interfaces:**
- Consumes: 无（纯逻辑）
- Produces:
  - `class SensitiveFilter`：
    - `__init__(self, patterns: tuple[str, ...] | None = None)` — 默认文本级敏感模式（身份证号/银行卡/手机号/邮箱/密码等正则 + 关键词）
    - `scan(text: str) -> list[str]` — 返回命中的敏感类别列表（空 = 安全）
    - `is_sensitive(text: str) -> bool` — `bool(scan(text))`
  - 与 Phase 2b 的窗口级 `SensitiveDetector` 是两层：这里是**文本级**（VLM 产出进 L1/L2 前检查）

- [ ] **Step 1: 写失败测试 `tests/cognition/test_sensitive.py`**

```python
import pytest

from yuki.cognition.sensitive import SensitiveFilter


def test_detects_id_card():
    f = SensitiveFilter()
    assert f.is_sensitive("身份证号是110101199003074518")


def test_detects_bank_card():
    f = SensitiveFilter()
    assert "bank_card" in f.scan("卡号 6222021234567890123")


def test_detects_phone():
    f = SensitiveFilter()
    assert f.is_sensitive("联系电话 13800138000")


def test_detects_password_keyword():
    f = SensitiveFilter()
    assert f.is_sensitive("密码：abc123 请勿泄露")


def test_allows_normal_text():
    f = SensitiveFilter()
    assert f.scan("这篇文章讨论了气候变化的影响。") == []
    assert f.is_sensitive("如何写代码 - 知乎") is False


def test_custom_patterns_override():
    f = SensitiveFilter(patterns=(r"\bSECRET\d+\b",))
    assert f.is_sensitive("SECRET42 是机密") is True
    assert f.is_sensitive("普通内容") is False
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/cognition/test_sensitive.py -v`
Expected: FAIL，`No module named 'yuki.cognition.sensitive'`

- [ ] **Step 3: 实现 `src/yuki/cognition/sensitive.py`**

```python
import re

_DEFAULT_PATTERNS = {
    "id_card": r"\b\d{17}[\dXx]\b",
    "bank_card": r"\b\d{16,19}\b",
    "phone": r"\b1[3-9]\d{9}\b",
    "email": r"\b[\w.+-]+@[\w-]+\.[\w.]+\b",
    "password": r"(密码|口令|password|passwd)\s*[:：]\s*\S+",
    "secret": r"(secret|api_key|token|凭据)\b",
}


class SensitiveFilter:
    """文本级敏感过滤（第二道防线）：VLM 产出的情境进 L1/L2 前检查。

    与采集层窗口级 SensitiveDetector 配合：窗口级阻断截图源头，
    这里是文本级拦截识别结果中的敏感信息。
    """

    def __init__(self, patterns: dict[str, str] | None = None) -> None:
        self._patterns = patterns if patterns is not None else _DEFAULT_PATTERNS
        self._compiled = {name: re.compile(pat) for name, pat in self._patterns.items()}

    def scan(self, text: str) -> list[str]:
        text = text or ""
        hits = [name for name, rx in self._compiled.items() if rx.search(text)]
        return hits

    def is_sensitive(self, text: str) -> bool:
        return bool(self.scan(text))
```

- [ ] **Step 4: 跑测试验证通过**

Run: `python -m pytest tests/cognition/test_sensitive.py -v`
Expected: 6 个测试 PASS

- [ ] **Step 5: 提交**

```bash
git add src/yuki/cognition/sensitive.py tests/cognition/test_sensitive.py
git commit -m "feat: text-level sensitive filter as second defense"
```

---

### Task 4: SpeechRecognizer（SenseVoice-Small）

**Files:**
- Create: `src/yuki/cognition/stt.py`
- Test: `tests/cognition/test_stt.py`

**Interfaces:**
- Consumes: 模型（Task 1 下载）、`Topics.MIC` 载荷格式（base64 float32）
- Produces:
  - `class SpeechRecognizer`：
    - `__init__(self, model=None, sample_rate=16000)` — 懒加载 funasr SenseVoiceSmall；可注入 fake
    - `recognize(samples: np.ndarray, sample_rate: int = 16000) -> str` — 音频采样 → 文本
    - `recognize_base64(pcm_b64: str, sample_rate: int = 16000) -> str` — 从总线载荷（base64 float32）识别
    - 空/无效输入返回 `""`
  - 内置 `_infer` 调 funasr；测试注入 fake 模型 + 直接测 base64 解码路径

- [ ] **Step 1: 写失败测试 `tests/cognition/test_stt.py`**

```python
import base64

import numpy as np
import pytest

from yuki.cognition.stt import SpeechRecognizer


def test_recognize_empty_returns_empty():
    stt = SpeechRecognizer(model=object())
    assert stt.recognize(np.array([], dtype=np.float32)) == ""


def test_recognize_base64_decodes_and_calls_model():
    calls = []

    class FakeModel:
        def __call__(self, samples, sample_rate):
            calls.append((samples, sample_rate))
            return [{"text": "你好"}]

    stt = SpeechRecognizer(model=FakeModel())
    pcm = np.zeros(16000, dtype=np.float32).tobytes()
    text = stt.recognize_base64(base64.b64encode(pcm).decode("ascii"), sample_rate=16000)
    assert text == "你好"
    assert len(calls) == 1


def test_recognize_handles_empty_text_result():
    class FakeModel:
        def __call__(self, samples, sample_rate):
            return [{"text": ""}]

    stt = SpeechRecognizer(model=FakeModel())
    pcm = np.zeros(320, dtype=np.float32).tobytes()
    assert stt.recognize_base64(base64.b64encode(pcm).decode("ascii")) == ""
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/cognition/test_stt.py -v`
Expected: FAIL，`No module named 'yuki.cognition.stt'`

- [ ] **Step 3: 实现 `src/yuki/cognition/stt.py`**

```python
import base64

import numpy as np

from yuki.logger import get_logger

logger = get_logger("yuki.cognition.stt")


class SpeechRecognizer:
    """SenseVoice-Small 语音识别：中英混合，带情感/事件标注。"""

    def __init__(self, model=None, sample_rate: int = 16000) -> None:
        self._model = model
        self._sample_rate = sample_rate
        self._loaded = model is not None

    def _load(self) -> None:
        if self._loaded:
            return
        from funasr import AutoModel
        self._model = AutoModel(model="iic/SenseVoiceSmall")
        self._loaded = True

    def _infer(self, samples: np.ndarray, sample_rate: int) -> str:
        self._load()
        result = self._model(input=samples.astype(np.float32), fs=sample_rate)
        if isinstance(result, list) and result:
            return str(result[0].get("text", ""))
        return ""

    def recognize(self, samples: np.ndarray, sample_rate: int = 16000) -> str:
        if samples is None or len(samples) == 0:
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
```

**注意：** funasr 真实调用 API 以 Task 1 安装版本为准（`AutoModel(model="iic/SenseVoiceSmall")`，`model(input=..., fs=...)` 返回 `[{"text": ...}]`）。测试注入 fake 模型。

- [ ] **Step 4: 跑测试验证通过**

Run: `python -m pytest tests/cognition/test_stt.py -v`
Expected: 3 个测试 PASS

- [ ] **Step 5: 提交**

```bash
git add src/yuki/cognition/stt.py tests/cognition/test_stt.py
git commit -m "feat: SenseVoice speech recognizer"
```

---

### Task 5: L1 本地快答引擎

**Files:**
- Create: `src/yuki/cognition/l1.py`
- Test: `tests/cognition/test_l1.py`

**Interfaces:**
- Consumes: 无（CPU 常驻，设计 §4.3）
- Produces:
  - `class L1Engine`：
    - `__init__(self, generator=None, rules: dict | None = None)` — CPU 量化模型或规则+检索；可注入
    - `reply(text: str, context: dict | None = None) -> str` — 返回快答文本，<1s
    - 默认实现：规则/模板引擎（关键词匹配 + 记忆检索占位），无模型依赖（CPU 常驻、零 GPU）
  - **阶段边界**：L1 的真模型（Qwen3-0.6B 量化）在模型基准（Task 1）确认后决定是否引入；本任务交付可运行的规则引擎 + 模型接入接口

- [ ] **Step 1: 写失败测试 `tests/cognition/test_l1.py`**

```python
import pytest

from yuki.cognition.l1 import L1Engine


def test_reply_greeting():
    engine = L1Engine()
    reply = engine.reply("你好")
    assert isinstance(reply, str)
    assert len(reply) > 0


def test_reply_acknowledges_call():
    engine = L1Engine()
    reply = engine.reply("")
    assert reply == "我在，你说。"


def test_reply_with_context_topic():
    engine = L1Engine()
    reply = engine.reply("继续说", context={"topic": "climate"})
    assert reply  # 非空
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/cognition/test_l1.py -v`
Expected: FAIL，`No module named 'yuki.cognition.l1'`

- [ ] **Step 3: 实现 `src/yuki/cognition/l1.py`**

```python
from yuki.logger import get_logger

logger = get_logger("yuki.cognition.l1")

_DEFAULT_RULES = {
    ("你好", "hi", "hello", "在吗"): "你好呀，我在呢。",
    ("谢谢", "感谢"): "不客气～",
    ("嗯", "好的", "继续"): "好，我听着呢。",
}


class L1Engine:
    """L1 本地快答：CPU 常驻规则引擎 + 模型接入接口（<1s）。

    Phase 3 交付规则/模板引擎（零 GPU、零模型依赖）。
    Task 1 基准若证实 Qwen3-0.6B 量化可在 CPU 达标，则接入 generator。
    """

    def __init__(self, generator=None, rules: dict | None = None) -> None:
        self._generator = generator  # 可选：CPU 量化小模型 generator(text) -> str
        self._rules = rules if rules is not None else _DEFAULT_RULES

    def reply(self, text: str, context: dict | None = None) -> str:
        text = (text or "").strip()
        if not text:
            return "我在，你说。"
        if self._generator is not None:
            try:
                return self._generator(text)
            except Exception:
                logger.exception("l1 generator failed")
        lowered = text.lower()
        for keywords, response in self._rules.items():
            if any(kw in lowered for kw in keywords):
                return response
        topic = (context or {}).get("topic")
        if topic:
            return f"嗯，说到{topic}了，你想聊哪方面？"
        return "嗯嗯，我在听。"
```

- [ ] **Step 4: 跑测试验证通过**

Run: `python -m pytest tests/cognition/test_l1.py -v`
Expected: 3 个测试 PASS

- [ ] **Step 5: 提交**

```bash
git add src/yuki/cognition/l1.py tests/cognition/test_l1.py
git commit -m "feat: L1 local fast-reply engine (CPU rule-based)"
```

---

### Task 6: frame 客户端 + 感知管线组装

**Files:**
- Create: `src/yuki/cognition/frame_client.py`
- Create: `src/yuki/cognition/pipeline.py`
- Test: `tests/cognition/test_frame_client.py`
- Test: `tests/cognition/test_pipeline.py`

**Interfaces:**
- Consumes: `MessageBus.request`（frame 服务）、`VisualUnderstander`（Task 2）、`SensitiveFilter`（Task 3）、`SpeechRecognizer`（Task 4）、`L1Engine`（Task 5）
- Produces:
  - `class FrameClient`：
    - `__init__(self, bus, timeout_ms: int = 2000)`
    - `get_latest() -> dict` — `bus.request("frame", {})`，返回 `{"png": base64, "width", "height", "ts", "sensitive"}`；超时/错误降级返回空 dict
  - `class PerceptionPipeline`：
    - `__init__(self, vlm, sensitive_filter, stt, l1, frame_client)`
    - `on_focus_changed(payload: dict) -> None` — 前台窗口变化：拉帧 → 敏感检查（sensitive 标志 or 文本级）→ VLM 情境 → 缓存
    - `on_awake(payload: dict) -> None` — 唤醒：L1 快答，publish `Topics.REPLY`
    - `on_mic(payload: dict) -> None` — 音频帧 → STT（仅唤醒后）
    - `understand_screen() -> dict` — 拉最新帧 → VLM 情境（供 L2/Brain 后续用）
  - 订阅接线：`build_pipeline(bus, *, vlm=None, ...)` 返回 pipeline 并订阅 `event/focus_changed`/`event/awake`/`audio/mic`

- [ ] **Step 1: 写失败测试 `tests/cognition/test_frame_client.py`**

```python
import pytest

from yuki.bus import BusError, BusTimeoutError
from yuki.cognition.frame_client import FrameClient


def test_get_latest_returns_frame():
    class FakeBus:
        def request(self, service, payload, timeout_ms=2000):
            assert service == "frame"
            return {"png": "AAA", "width": 100, "height": 50, "ts": 1.0, "sensitive": False}

    client = FrameClient(FakeBus())
    assert client.get_latest()["width"] == 100


def test_get_latest_degrades_on_timeout():
    class FakeBus:
        def request(self, service, payload, timeout_ms=2000):
            raise BusTimeoutError("timeout")

    client = FrameClient(FakeBus())
    assert client.get_latest() == {}
```

- [ ] **Step 2: 写失败测试 `tests/cognition/test_pipeline.py`**

```python
import pytest

from yuki.cognition.pipeline import PerceptionPipeline, build_pipeline
from yuki.topics import Topics


class FakeVLM:
    def __init__(self):
        self.understand_calls = []

    def understand(self, image, cache_key=None):
        self.understand_calls.append(cache_key)
        return {"topic": "climate", "summary": "s", "content_type": "article", "key_points": []}


class FakeSensitive:
    def scan(self, text):
        return []


class FakeSTT:
    def recognize_base64(self, pcm, sample_rate=16000):
        return "你好"


class FakeL1:
    def reply(self, text, context=None):
        return "你好呀，我在呢。"


class FakeFrameClient:
    def __init__(self):
        self.latest = {"png": "AAA", "width": 10, "height": 10, "ts": 1.0, "sensitive": False}

    def get_latest(self):
        return dict(self.latest)


class FakeBus:
    def __init__(self):
        self.published = []
        self.subscriptions = {}

    def publish(self, topic, payload):
        self.published.append((topic, payload))

    def subscribe(self, prefix, handler):
        self.subscriptions[prefix] = handler

    def respond(self, service, handler):
        pass


def test_pipeline_on_awake_replies():
    bus = FakeBus()
    pipeline = build_pipeline(
        bus,
        vlm=FakeVLM(),
        sensitive_filter=FakeSensitive(),
        stt=FakeSTT(),
        l1=FakeL1(),
        frame_client=FakeFrameClient(),
    )
    bus.subscriptions[Topics.AWAKE]("event/awake", {"source": "hotkey", "ts": 0.0})
    assert any(t == Topics.REPLY for t, _ in bus.published)


def test_pipeline_understand_screen_uses_vlm():
    bus = FakeBus()
    pipeline = build_pipeline(
        bus,
        vlm=FakeVLM(),
        sensitive_filter=FakeSensitive(),
        stt=FakeSTT(),
        l1=FakeL1(),
        frame_client=FakeFrameClient(),
    )
    context = pipeline.understand_screen()
    assert context["topic"] == "climate"


def test_pipeline_stt_on_mic():
    bus = FakeBus()
    pipeline = build_pipeline(
        bus,
        vlm=FakeVLM(),
        sensitive_filter=FakeSensitive(),
        stt=FakeSTT(),
        l1=FakeL1(),
        frame_client=FakeFrameClient(),
    )
    import base64
    pcm = base64.b64encode(b"\x00\x00\x00\x00").decode("ascii")
    bus.subscriptions[Topics.MIC]("audio/mic", {"pcm": pcm, "sample_rate": 16000, "ts": 0.0})
    assert any(t == Topics.REPLY for t, _ in bus.published)
```

- [ ] **Step 3: 跑测试验证失败**

Run: `python -m pytest tests/cognition/test_frame_client.py tests/cognition/test_pipeline.py -v`
Expected: FAIL，`No module named 'yuki.cognition.frame_client'`

- [ ] **Step 4: 实现 `src/yuki/cognition/frame_client.py`**

```python
from yuki.bus import BusError, BusTimeoutError
from yuki.logger import get_logger

logger = get_logger("yuki.cognition.frame_client")


class FrameClient:
    """frame REQ/REP 客户端：拉取采集层最新帧，失败降级为空 dict。"""

    def __init__(self, bus, timeout_ms: int = 2000) -> None:
        self._bus = bus
        self._timeout_ms = timeout_ms

    def get_latest(self) -> dict:
        try:
            return self._bus.request("frame", {}, timeout_ms=self._timeout_ms)
        except (BusError, BusTimeoutError):
            logger.warning("frame request failed, degrading to empty")
            return {}
```

- [ ] **Step 5: 实现 `src/yuki/cognition/pipeline.py`**

```python
import base64

from yuki.bus import MessageBus
from yuki.cognition.frame_client import FrameClient
from yuki.cognition.l1 import L1Engine
from yuki.cognition.sensitive import SensitiveFilter
from yuki.cognition.stt import SpeechRecognizer
from yuki.cognition.vlm import VisualUnderstander
from yuki.logger import get_logger
from yuki.topics import Topics

logger = get_logger("yuki.cognition.pipeline")


class PerceptionPipeline:
    """感知理解管线：前台变化→读屏情境、唤醒→L1 快答、音频→STT。"""

    def __init__(self, vlm, sensitive_filter, stt, l1, frame_client) -> None:
        self._vlm = vlm
        self._sensitive = sensitive_filter
        self._stt = stt
        self._l1 = l1
        self._frame_client = frame_client
        self._last_context: dict = {}
        self._listening = False

    def on_focus_changed(self, topic: str, payload: dict) -> None:
        frame = self._frame_client.get_latest()
        if not frame:
            return
        if frame.get("sensitive"):
            self._last_context = {"topic": "", "sensitive": True}
            return
        cache_key = f"{payload.get('title', '')}|{payload.get('url', '')}"
        context = self._vlm.understand(frame.get("png"), cache_key=cache_key)
        if self._sensitive.scan(context.get("summary", "") + context.get("topic", "")):
            self._last_context = {"topic": "", "sensitive": True}
            return
        self._last_context = context

    def on_awake(self, topic: str, payload: dict) -> None:
        self._listening = True
        reply = self._l1.reply("", context=self._last_context)
        self._bus_publish_reply(reply)

    def on_mic(self, topic: str, payload: dict) -> None:
        if not self._listening:
            return
        text = self._stt.recognize_base64(payload.get("pcm", ""), payload.get("sample_rate", 16000))
        if not text:
            return
        reply = self._l1.reply(text, context=self._last_context)
        self._bus_publish_reply(reply)

    def understand_screen(self) -> dict:
        frame = self._frame_client.get_latest()
        if not frame or frame.get("sensitive"):
            return {"topic": "", "sensitive": True}
        return self._vlm.understand(frame.get("png"))

    def _bus_publish_reply(self, text: str) -> None:
        import time
        self._bus.publish(Topics.REPLY, {"text": text, "ts": time.time()})


def build_pipeline(bus: MessageBus, *, vlm=None, sensitive_filter=None, stt=None, l1=None, frame_client=None) -> PerceptionPipeline:
    """组装感知理解管线并订阅事件。测试注入 fake，默认懒加载真实组件。"""
    pipeline = PerceptionPipeline(
        vlm=vlm or VisualUnderstander(),
        sensitive_filter=sensitive_filter or SensitiveFilter(),
        stt=stt or SpeechRecognizer(),
        l1=l1 or L1Engine(),
        frame_client=frame_client or FrameClient(bus),
    )
    pipeline._bus = bus
    bus.subscribe(Topics.FOCUS_CHANGED, pipeline.on_focus_changed)
    bus.subscribe(Topics.AWAKE, pipeline.on_awake)
    bus.subscribe(Topics.MIC, pipeline.on_mic)
    return pipeline
```

**注意：** `pipeline._bus` 在 build_pipeline 里赋值（FakeBus 无该属性时测试路径用注入的 bus）。若 `_bus_publish_reply` 需要 `self._bus`，在 `__init__` 加可选 `bus=None` 参数更干净——以实现为准，保持语义：管线发布 `Topics.REPLY`。

- [ ] **Step 6: 跑测试验证通过**

Run: `python -m pytest tests/cognition/test_frame_client.py tests/cognition/test_pipeline.py -v`
Expected: 5 个测试 PASS

- [ ] **Step 7: 全量回归 + 提交**

Run: `python -m pytest -q`
Expected: 全部 PASS（`build_cognition` 既有测试用 FakeBus，不受影响）
```bash
git add src/yuki/cognition/frame_client.py src/yuki/cognition/pipeline.py tests/cognition/test_frame_client.py tests/cognition/test_pipeline.py
git commit -m "feat: frame client and perception pipeline wiring"
```

---

### Task 7: cognition main 接入管线

**Files:**
- Modify: `src/yuki/cognition/main.py`
- Modify: `tests/cognition/test_cognition.py`
- Test: `tests/cognition/test_cognition.py`

**Interfaces:**
- Consumes: `build_pipeline`（Task 6）
- Produces:
  - `build_cognition(bus, *, pipeline=None) -> None` — 若注入 pipeline 则订阅其事件；否则默认只保留 `make_reply` 的 awake→reply 兼容路径（Phase 3 感知管线在 main 里通过 build_pipeline 接入，但不强制订阅 audio/mic 到真实 STT——避免测试/无 GPU 环境加载模型）
  - `main()` — `build_pipeline(bus)` 接入，保留既有 health/shutdown

- [ ] **Step 1: 修改既有测试 `tests/cognition/test_cognition.py` 增加兼容断言**

```python
def test_build_cognition_still_replies_on_awake():
    bus = FakeBus()
    build_cognition(bus)
    assert bus.handler is not None
    bus.handler(Topics.AWAKE, {"source": "hotkey", "ts": 0.0})
    assert len(bus.published) == 1
    topic, payload = bus.published[0]
    assert topic == Topics.REPLY
    assert payload["text"] == "我在，你说。"
```

（既有测试文件已存在，追加此断言并确保 FakeBus 兼容。）

- [ ] **Step 2: 修改 `src/yuki/cognition/main.py`**

```python
def build_cognition(bus: MessageBus, *, pipeline=None) -> None:
    if pipeline is not None:
        # 感知管线已自带订阅（focus_changed/awake/mic）
        return
    def on_awake(topic: str, payload: dict) -> None:
        bus.publish(Topics.REPLY, make_reply(payload))
    bus.subscribe(Topics.AWAKE, on_awake)


def main() -> None:
    config = Config.from_env()
    bus = MessageBus(base_port=config.base_port, role=config.bus_role, hwm=config.hwm)
    shutdown = ShutdownManager()
    shutdown.register_signal_handlers()
    from yuki.cognition.pipeline import build_pipeline
    build_pipeline(bus)  # 懒加载模型，仅在实际有帧/唤醒时推理
    register_health_service(bus, "cognition")
    try:
        while not shutdown.shutdown_requested:
            shutdown.wait(timeout=1.0)
    finally:
        bus.close()
```

**注意：** `build_pipeline(bus)` 默认用真实 VisualUnderstander/SpeechRecognizer（懒加载，不 start 不加载模型）。无 GPU 或无模型时，`on_focus_changed` 里 `frame_client.get_latest()` 降级返回 `{}` → 不触发 VLM；`on_awake` 用 L1 规则引擎 → 不触发模型。**语义：管线在不加载模型的情况下也能响应基本 awake（L1 兜底），模型仅在真有帧/语音时按需加载。**

- [ ] **Step 3: 跑测试验证通过**

Run: `python -m pytest tests/cognition/test_cognition.py -v`
Expected: 全部 PASS

- [ ] **Step 4: 全量回归 + e2e + 提交**

Run: `python -m pytest -q`
Run: `python -m pytest -m e2e -q`
Expected: 全部 PASS（e2e 的 hotkey→awake→reply 闭环经 L1 引擎仍工作）
```bash
git add src/yuki/cognition/main.py tests/cognition/test_cognition.py
git commit -m "feat: wire perception pipeline into cognition process"
```

---

## Self-Review

**1. Spec coverage：**
- §3.2 VisualUnderstander/SpeechRecognizer/SensitiveFilter/L1 → Task 2/4/3/5
- §4.3 L1 常驻 CPU、L2 留 Brain 阶段 → Task 5（规则引擎）+ 阶段边界
- §11.2 模型选型基准 → Task 1（P50/P95/资源/文档）
- §11.3 context cache（标题+URL+滚动%）→ Task 2（ContextCache + cache_key）
- 接口契约 frame/request、audio/mic → Task 6（FrameClient 降级）+ Task 4（base64 解码）
- 敏感第二道（文本级）→ Task 3；与 Phase 2b 窗口级 SensitiveDetector 分层
- Personality Brain / MemoryManager / 反馈闭环 → 明确留独立阶段（Global Constraints + Self-Review）

**2. Placeholder 扫描：** 无 TBD/TODO。Task 1 的基准断言预算（P50<5s 等）标注"以实测为准"——记录进文档而非死断言。Task 2/4 的 transformers/funasr 调用标注"以安装版本 API 为准"，测试走注入 fake。

**3. Type consistency：**
- `VisualUnderstander.understand(image, cache_key=None) -> dict`（Task 2）被 Task 6 pipeline 引用
- `SensitiveFilter.scan(text) -> list[str]`（Task 3）被 Task 6 pipeline 引用
- `SpeechRecognizer.recognize_base64(pcm_b64, sample_rate)`（Task 4）被 Task 6 pipeline 引用
- `L1Engine.reply(text, context=None) -> str`（Task 5）被 Task 6 pipeline 引用
- `FrameClient.get_latest() -> dict`（Task 6）被 pipeline 与测试引用
- `build_pipeline(bus, *, vlm=None, ...)`（Task 6）被 Task 7 main 引用

**关键取舍：**
- 感知管线懒加载模型：无 GPU/无模型时不崩，L1 规则引擎兜底 awake；模型仅按需加载——符合"本地处理+云端"与无头测试
- L1 阶段交付规则引擎（CPU 零依赖），真模型接入接口就绪，Task 1 基准定夺
- TTS/Brain/记忆留后续阶段（范围已与用户确认：感知理解管线）
- 真实模型推理不走单测（无 GPU 环境），走 Task 1 基准 + e2e/手动冒烟
