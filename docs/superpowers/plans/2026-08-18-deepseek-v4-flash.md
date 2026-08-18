# DeepSeek V4 Flash Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Switch Yuki's cloud chat backend to DeepSeek V4 Flash via configuration only.

**Architecture:** The cloud layer already speaks OpenAI-compatible `chat/completions` (`CloudConfig` → `CloudClient` → `CloudBridge`). DeepSeek's API is OpenAI-compatible, so we change the cloud section of `config.example.yaml` to DeepSeek defaults and create a local, uncommitted `config.yaml` with `cloud.enabled: true`. No runtime code changes.

**Tech Stack:** Python 3.11, PyYAML, Pydantic config (`src/yuki/config.py`).

## Global Constraints

- No changes to any runtime code (`config.py`, `client.py`, `bridge.py`, `assembly.py`, etc.).
- `cloud.enabled` stays `false` in the committed `config.example.yaml`; only the local `config.yaml` sets it to `true`.
- `api_key_env` stays `YUKI_CLOUD_API_KEY`.
- Model name exactly `deepseek-v4-flash`; base URL exactly `https://api.deepseek.com/v1`.
- `config.yaml` is a local file and must NOT be committed.

---

### Task 1: Point config.example.yaml cloud section at DeepSeek

**Files:**
- Modify: `config.example.yaml:48-54`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: existing `Config.load(path)` (src/yuki/config.py:122) — reads a YAML config file into a `Config` model.
- Produces: `Config.cloud` with `enabled=False`, `base_url="https://api.deepseek.com/v1"`, `model="deepseek-v4-flash"`, `api_key_env="YUKI_CLOUD_API_KEY"`.

- [ ] **Step 1: Write the failing test**

Add `from pathlib import Path` to the imports in `tests/test_config.py` (currently: `import pytest`, `from pydantic import ValidationError`, `from yuki.config import Config`), then append this test at the end of the file:

```python
def test_example_config_cloud_points_to_deepseek():
    example = Path(__file__).resolve().parents[1] / "config.example.yaml"
    config = Config.load(example)
    assert config.cloud.enabled is False
    assert config.cloud.base_url == "https://api.deepseek.com/v1"
    assert config.cloud.model == "deepseek-v4-flash"
    assert config.cloud.api_key_env == "YUKI_CLOUD_API_KEY"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config.py::test_example_config_cloud_points_to_deepseek -v`
Expected: FAIL — assertion error on `base_url`/`model` (example still has OpenAI values).

- [ ] **Step 3: Update config.example.yaml cloud section**

Change lines 48-54 from:

```yaml
cloud:
  enabled: false
  base_url: https://api.openai.com/v1
  model: gpt-4o-mini
  api_key_env: YUKI_CLOUD_API_KEY
  timeout_s: 10.0
  max_turns: 3
```

to:

```yaml
cloud:
  enabled: false
  base_url: https://api.deepseek.com/v1
  model: deepseek-v4-flash
  api_key_env: YUKI_CLOUD_API_KEY
  timeout_s: 10.0
  max_turns: 3
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_config.py::test_example_config_cloud_points_to_deepseek -v`
Expected: PASS

- [ ] **Step 5: Run the full suite to confirm no regressions**

Run: `pytest -q`
Expected: 501 passed (existing `test_cloud_defaults` in tests/test_config.py:141 still passes because code defaults were NOT changed).

- [ ] **Step 6: Commit**

```bash
git add config.example.yaml tests/test_config.py
git commit -m "feat: point example config cloud section at deepseek v4 flash"
```

---

### Task 2: Create local config.yaml with DeepSeek enabled and verify

**Files:**
- Create: `config.yaml` (local, NOT committed)

**Interfaces:**
- Consumes: `Config.load(None)` auto-discovers `config.yaml` in the working directory (src/yuki/config.py:126-129).
- Produces: a runnable local config with `Config.cloud.enabled is True`, `model="deepseek-v4-flash"`, `base_url="https://api.deepseek.com/v1"`.

- [ ] **Step 1: Create config.yaml from the example**

Run:

```powershell
Copy-Item config.example.yaml config.yaml
```

- [ ] **Step 2: Enable the cloud section**

In `config.yaml`, change the `cloud:` block so `enabled` is `true`:

```yaml
cloud:
  enabled: true
  base_url: https://api.deepseek.com/v1
  model: deepseek-v4-flash
  api_key_env: YUKI_CLOUD_API_KEY
  timeout_s: 10.0
  max_turns: 3
```

- [ ] **Step 3: Verify local config parses with DeepSeek enabled**

Run:

```powershell
python -c "from yuki.config import Config; c = Config.load(None); print(c.cloud)"
```

Expected output: `enabled=True base_url='https://api.deepseek.com/v1' model='deepseek-v4-flash' api_key_env='YUKI_CLOUD_API_KEY' timeout_s=10.0 max_turns=3`

- [ ] **Step 4: Live smoke test (only if YUKI_CLOUD_API_KEY is set)**

Run:

```powershell
python -c "from yuki.config import Config; import os; from yuki.cognition.l2.client import CloudClient; c = Config.load(None).cloud; cli = CloudClient(c.base_url, c.model, os.environ.get(c.api_key_env), c.timeout_s); r = cli.chat([{'role':'user','content':'say hi'}]); print(r['choices'][0]['message']['content'])"
```

Expected: a short DeepSeek reply printed; no `CloudError`. If the env var is unset, skip this step (the smoke skips the auth header).

- [ ] **Step 5: Confirm config.yaml is not committed**

Run: `git status --short`
Expected: `config.yaml` appears as untracked (or is not staged). Do NOT `git add config.yaml`. The only committed change for this feature is Task 1's commit.

---

## Self-Review Notes

- Spec coverage: example config update (Task 1) ✓, local enabled config (Task 2) ✓, verification via pytest + offline/live smoke ✓.
- No placeholders; all steps contain exact commands/code.
- Type consistency: `Config.cloud` field names match `CloudConfig` (config.py:67); `Config.load(path)` signature unchanged.
