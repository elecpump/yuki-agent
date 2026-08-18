# VLM Local Qwen3-VL-8B-Instruct (transformers 5.x) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `VisualUnderstander` load the locally-cached Qwen3-VL-8B-Instruct model with the transformers 5.x API (verified by a real inference smoke test).

**Architecture:** Add a `VlmConfig` section to the Pydantic config, rewrite `VisualUnderstander._load()` to use `AutoModelForImageTextToText` + `BitsAndBytesConfig` + local `cache_dir`, and wire the config through the cognition assembler. Model loading stays lazy/backgrounded as today.

**Tech Stack:** Python 3.11, transformers 5.15.0, torch 2.7.0+cu128, bitsandbytes 0.50.1, accelerate 1.14.0, qwen-vl-utils.

## Global Constraints

- Model id exactly `Qwen/Qwen3-VL-8B-Instruct`; default `cache_dir` is `""` (empty = HF default, resolved via `HF_HOME`).
- Load must use `AutoModelForImageTextToText` + `BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_compute_dtype="float16")` — NOT the legacy `load_in_4bit=True` kwarg or `AutoModel`/`AutoModelForVision2Seq` (wrong under transformers 5.x).
- `cache_dir` empty string must be passed as `cache_dir or None` to `from_pretrained`.
- `VlmConfig.enabled` defaults `True`; `config.example.yaml` must NOT contain a machine-specific `cache_dir` path.
- Keep `qwen_vl_utils.process_vision_info`, `_infer`, `_parse`, `warmup`, `understand` logic unchanged.
- No model download/completion work; no changes to `D:\huggingface\models\qwen3-vl-8b`.

---

### Task 1: Add VlmConfig and declare ml runtime deps

**Files:**
- Modify: `src/yuki/config.py` (add `VlmConfig` class, mount `Config.vlm`)
- Modify: `config.example.yaml` (add `vlm:` section)
- Modify: `pyproject.toml` (ml extra)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `VlmConfig` (fields `enabled: bool = True`, `model: str = "Qwen/Qwen3-VL-8B-Instruct"`, `cache_dir: str = ""`) mounted as `Config.vlm`, wired into `Config.load` env overrides (`YUKI_VLM_*`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
def test_vlm_defaults():
    config = Config()
    assert config.vlm.enabled is True
    assert config.vlm.model == "Qwen/Qwen3-VL-8B-Instruct"
    assert config.vlm.cache_dir == ""


def test_vlm_env_override(monkeypatch):
    monkeypatch.setenv("YUKI_VLM_ENABLED", "false")
    monkeypatch.setenv("YUKI_VLM_MODEL", "Qwen/Qwen3-VL-8B")
    monkeypatch.setenv("YUKI_VLM_CACHE_DIR", "D:/hf")
    config = Config.load(None)
    assert config.vlm.enabled is False
    assert config.vlm.model == "Qwen/Qwen3-VL-8B"
    assert config.vlm.cache_dir == "D:/hf"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_config.py::test_vlm_defaults tests/test_config.py::test_vlm_env_override -v`
Expected: FAIL — `AttributeError: 'Config' object has no attribute 'vlm'`.

- [ ] **Step 3: Add VlmConfig to config.py**

Insert after `BrainConfig` (src/yuki/config.py:62-64), matching existing style:

```python
class VlmConfig(BaseModel):
    enabled: bool = True
    model: str = "Qwen/Qwen3-VL-8B-Instruct"
    cache_dir: str = ""
```

Add the field to `Config` (after `brain: BrainConfig = Field(default_factory=BrainConfig)`):

```python
    vlm: VlmConfig = Field(default_factory=VlmConfig)
```

Add `("vlm", VlmConfig),` to the `_apply_env` loop tuple list in `Config.load` (after the `("brain", BrainConfig),` entry).

- [ ] **Step 4: Update config.example.yaml**

Append the `vlm:` section after the `brain:` block (line 47):

```yaml
vlm:
  enabled: true
  model: Qwen/Qwen3-VL-8B-Instruct
  cache_dir: ""
```

- [ ] **Step 5: Update pyproject.toml ml extra**

Change line 23 from:

```toml
ml = ["transformers", "torch", "qwen-vl-utils", "funasr", "paddleocr"]
```

to:

```toml
ml = ["transformers", "torch", "torchvision", "qwen-vl-utils", "funasr", "paddleocr",
      "accelerate", "bitsandbytes"]
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_config.py::test_vlm_defaults tests/test_config.py::test_vlm_env_override -v`
Expected: PASS

- [ ] **Step 7: Run full suite**

Run: `pytest -q`
Expected: 503 passed, 1 deselected.

- [ ] **Step 8: Commit**

```bash
git add src/yuki/config.py config.example.yaml pyproject.toml tests/test_config.py
git commit -m "feat: add vlm config section and ml runtime deps"
```

---

### Task 2: Rewrite VisualUnderstander._load for transformers 5.x + local model

**Files:**
- Modify: `src/yuki/cognition/vlm.py`
- Test: `tests/cognition/test_vlm.py`

**Interfaces:**
- Consumes: `VlmConfig` field names from Task 1 (`model`, `cache_dir`).
- Produces: `VisualUnderstander(model=None, processor=None, cache=None, *, model_id="Qwen/Qwen3-VL-8B-Instruct", cache_dir="")` — new keyword-only `model_id` and `cache_dir` params. Existing positional call sites (`model=`, `processor=`, `cache=`) unchanged.

- [ ] **Step 1: Write the failing test**

Append to `tests/cognition/test_vlm.py`:

```python
def test_load_uses_model_id_cache_dir_and_quant_config(monkeypatch):
    import sys

    calls = {}

    class FakeAuto:
        @staticmethod
        def from_pretrained(*args, **kwargs):
            calls.update({"args": args, "kwargs": kwargs})
            return object()

    class FakeProcessor:
        @staticmethod
        def from_pretrained(*args, **kwargs):
            calls.update({"processor_args": args, "processor_kwargs": kwargs})
            return object()

    fake_transformers = types.SimpleNamespace(
        AutoModelForImageTextToText=FakeAuto,
        AutoProcessor=FakeProcessor,
        BitsAndBytesConfig=lambda **kw: {"cfg": kw},
    )
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)

    vlm = VisualUnderstander(
        model_id="Qwen/Qwen3-VL-8B-Instruct", cache_dir="D:/hf"
    )
    vlm._load()

    assert vlm._loaded is True
    model_kwargs = calls["kwargs"]
    assert model_kwargs["cache_dir"] == "D:/hf"
    assert model_kwargs["quantization_config"] == {"cfg": {"load_in_4bit": True,
        "bnb_4bit_quant_type": "nf4", "bnb_4bit_compute_dtype": "float16"}}
    assert calls["processor_kwargs"]["cache_dir"] == "D:/hf"
```

Also update `test_load_failure_is_remembered` (currently lines 85-106) so the fake transformers namespace uses the new API names:

```python
    fake_transformers = types.SimpleNamespace(
        AutoModelForImageTextToText=FakeAutoModel,
        AutoProcessor=object,
        BitsAndBytesConfig=lambda **kw: {"cfg": kw},
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/cognition/test_vlm.py::test_load_uses_model_id_cache_dir_and_quant_config -v`
Expected: FAIL — the new test's `_load()` raises `AttributeError` (`AutoModel`/`load_in_4bit` path no longer matches) or `_loaded` stays False.

- [ ] **Step 3: Rewrite the constructor and _load in vlm.py**

Constructor (lines 18-24):

```python
    def __init__(
        self,
        model=None,
        processor=None,
        cache: ContextCache | None = None,
        *,
        model_id: str = "Qwen/Qwen3-VL-8B-Instruct",
        cache_dir: str = "",
    ) -> None:
        self._model = model
        self._processor = processor
        self._cache = cache or ContextCache()
        self._loaded = model is not None and processor is not None
        self._load_failed = False
        self._load_lock = threading.Lock()
        self._model_id = model_id
        self._cache_dir = cache_dir
```

`_load()` (lines 26-43):

```python
    def _load(self) -> None:
        if self._loaded:
            return
        with self._load_lock:
            if self._loaded:
                return
            if self._load_failed:
                raise RuntimeError("vlm load previously failed")
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
            except Exception:
                self._load_failed = True
                raise
```

- [ ] **Step 4: Run the vlm tests**

Run: `pytest tests/cognition/test_vlm.py -v`
Expected: all pass (including updated `test_load_failure_is_remembered`, which now counts the one `from_pretrained` call).

- [ ] **Step 5: Run full suite**

Run: `pytest -q`
Expected: 503 passed, 1 deselected.

- [ ] **Step 6: Commit**

```bash
git add src/yuki/cognition/vlm.py tests/cognition/test_vlm.py
git commit -m "feat: load vlm via transformers 5.x image-text API from local cache"
```

---

### Task 3: Wire VlmConfig through the cognition assembler

**Files:**
- Modify: `src/yuki/cognition/assembly.py`
- Test: `tests/cognition/test_assembly.py`

**Interfaces:**
- Consumes: `VisualUnderstander(model_id=..., cache_dir=...)` from Task 2; `Config.vlm` from Task 1.
- Produces: `CognitionAssembler._build_vlm() -> VisualUnderstander` returning an instance configured from `self.config.vlm`.

- [ ] **Step 1: Write the failing test**

Append to `tests/cognition/test_assembly.py`:

```python
def test_cognition_assembler_builds_vlm_from_config(tmp_path):
    bus = FakeBus()
    assembler = CognitionAssembler(
        Config(
            vlm={"model": "Qwen/Qwen3-VL-8B-Instruct", "cache_dir": "D:/hf"},
            persona={"snapshots_path": str(tmp_path / "persona.json")},
        ),
        bus,
    )
    vlm = assembler._build_vlm()
    assert vlm._model_id == "Qwen/Qwen3-VL-8B-Instruct"
    assert vlm._cache_dir == "D:/hf"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/cognition/test_assembly.py::test_cognition_assembler_builds_vlm_from_config -v`
Expected: FAIL — `AttributeError: 'CognitionAssembler' object has no attribute '_build_vlm'`.

- [ ] **Step 3: Implement _build_vlm and use it in assemble**

Add the import at the top of `src/yuki/cognition/assembly.py` (with the other `yuki.cognition` imports):

```python
from yuki.cognition.vlm import VisualUnderstander
```

Change `assemble()` line 81-82 from:

```python
        pipeline = self.pipeline or build_pipeline(
            self.bus,
            vlm=self.vlm,
```

to:

```python
        pipeline = self.pipeline or build_pipeline(
            self.bus,
            vlm=self.vlm or self._build_vlm(),
```

Add this method to `CognitionAssembler` (next to `_build_bridge`):

```python
    def _build_vlm(self) -> VisualUnderstander:
        vlm_cfg = self.config.vlm
        return VisualUnderstander(model_id=vlm_cfg.model, cache_dir=vlm_cfg.cache_dir)
```

- [ ] **Step 4: Run the assembly tests**

Run: `pytest tests/cognition/test_assembly.py -v`
Expected: all pass.

- [ ] **Step 5: Run full suite**

Run: `pytest -q`
Expected: 504 passed, 1 deselected.

- [ ] **Step 6: Live smoke — real inference with config values**

Run (Python 3.11 UTF-8 mode):

```powershell
$env:PYTHONUTF8 = "1"
python -c "from yuki.config import Config; from yuki.cognition.assembly import CognitionAssembler; from tests.fakes import FakeBus; c = Config(); c = c.model_copy(update={'vlm': {'cache_dir': 'D:/huggingface/hub'}}); a = CognitionAssembler(c, FakeBus()); v = a._build_vlm(); v.warmup(); import time; [time.sleep(1) for _ in range(120) if not v._loaded and not v._load_failed]; print('loaded:', v._loaded, 'failed:', v._load_failed); assert v._loaded"
```

Expected: prints `loaded: True failed: False` after the model loads from the local cache (takes up to ~2 min). No network access needed.

- [ ] **Step 7: Commit**

```bash
git add src/yuki/cognition/assembly.py tests/cognition/test_assembly.py
git commit -m "feat: wire vlm config through cognition assembler"
```

---

## Self-Review Notes

- Spec coverage: `VlmConfig` (Task 1), transformers 5.x load rewrite + local cache (Task 2), assembler wiring + config.example + pyproject (Tasks 1 & 3), verification via pytest + live smoke (all tasks). ✓
- No placeholders; every step has exact code/commands. ✓
- Type consistency: `VisualUnderstander(model_id=..., cache_dir=...)` matches across Tasks 2 and 3; `VlmConfig` fields consistent with Task 1. ✓
