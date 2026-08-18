# Design: VLM loads local Qwen3-VL-8B-Instruct (transformers 5.x compatible)

Date: 2026-08-19

## Goal

Make Yuki's `VisualUnderstander` load the locally-downloaded Qwen3-VL-8B-Instruct
model so screen understanding actually works on this machine.

## Background

The local environment has the model at two places:

- `D:\huggingface\hub\models--Qwen--Qwen3-VL-8B-Instruct` — complete HF hub
  cache: all 4 weight shards (~16GB) + config. This is the canonical source.
- `D:\huggingface\models\qwen3-vl-8b` — has processor/tokenizer auxiliary
  files but only 2 of 4 weight shards (incomplete). Not used; auxiliary files
  were copied from it into the hub snapshot to complete the cache.

### Environment verified during capability smoke test

- `torch 2.7.0+cu128`, `torchvision 0.22.0+cu128` (both installed to match
  transformers 5.15.0 — `CPUOffloadPolicy` import failure is resolved).
- `transformers 5.15.0`, `bitsandbytes 0.50.1`, `accelerate 1.14.0`,
  `qwen-vl-utils 0.0.14`.
- GPU: NVIDIA RTX 5090 (25.6 GB VRAM); CUDA available.

The capability smoke test proved the model loads and infers correctly using:

- `AutoModelForImageTextToText` (transformers 5.x name; `AutoModel` and
  `AutoModelForVision2Seq` are wrong for this version — `AutoModel` resolves to
  `Qwen3VLModel`, a base model without the LM/vision heads).
- `BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
  bnb_4bit_compute_dtype="float16")` — the `load_in_4bit=True` kwarg is not
  accepted by transformers 5.x `from_pretrained`.
- Model id `Qwen/Qwen3-VL-8B-Instruct` + `cache_dir="D:\huggingface\hub"`.

## Changes

1. **config.py** — new `VlmConfig`:
   - `enabled: bool = True`
   - `model: str = "Qwen/Qwen3-VL-8B-Instruct"`
   - `cache_dir: str = ""` — empty means HF default cache (resolved via
     `HF_HOME`); this machine has `HF_HOME=D:\huggingface`, which resolves to
     `D:\huggingface\hub`. The concrete local path goes in the local
     `config.yaml` only, keeping committed code machine-independent.
   Mount as `Config.vlm`.
2. **vlm.py** — rewrite `_load()`:
   - `from transformers import AutoProcessor, AutoModelForImageTextToText, BitsAndBytesConfig`
   - `AutoModelForImageTextToText.from_pretrained(model, cache_dir=cache_dir,
     torch_dtype="auto", device_map="auto", quantization_config=quant)`
   - `AutoProcessor.from_pretrained(model, cache_dir=cache_dir)`
   - Read model/cache_dir from config (constructor param with fallback defaults).
   - Keep `qwen_vl_utils.process_vision_info`, `_infer`, `_parse`, `warmup`
     logic unchanged.
3. **pipeline.py / assembly.py** — thread `VlmConfig` into `VisualUnderstander`
   construction (`build_pipeline` / `CognitionAssembler`).
4. **config.example.yaml** — add `vlm:` section (`enabled`, `model`,
   `cache_dir: ""`). The user's local `config.yaml` sets
   `cache_dir: D:\huggingface\hub`.
5. **pyproject.toml** — `ml` extra adds `accelerate`, `bitsandbytes`,
   `torchvision` (verified-required runtime deps for this load path).

## Out of Scope

- No multi-provider/model abstraction.
- No change to `D:\huggingface\models\qwen3-vl-8b` (incomplete dir stays).
- No STT/VLM toggle work beyond wiring config.
- Model download/completion is a machine concern, not code.

## Verification

- `pytest` full suite green (existing VLM tests inject fake model/processor, so
  no real load in tests).
- Live smoke: `VisualUnderstander(model=..., cache_dir=...)` from config, run
  `understand()` on a synthetic test image; assert non-empty topic/summary.
