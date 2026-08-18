# Design: Integrate DeepSeek V4 Flash as Cloud LLM

Date: 2026-08-18

## Goal

Switch Yuki's cloud chat backend to DeepSeek V4 Flash via pure configuration.
No code changes to the LLM client layer.

## Background

The cloud layer already speaks OpenAI-compatible `chat/completions`:

- `CloudConfig` (src/yuki/config.py:67) — `base_url`, `model`, `api_key_env`,
  `timeout_s`, `max_turns`.
- `CloudClient` (src/yuki/cognition/l2/client.py:23) — OpenAI-compatible HTTP
  client; appends `/chat/completions` to `base_url`.
- `CloudBridge` (src/yuki/cognition/l2/bridge.py:28) — request building, tool-call
  multi-turn loop, persona refinement, and turn summarization.
- `_build_bridge` (src/yuki/cognition/assembly.py:191) — wires config to
  `CloudBridge`; returns `None` when `cloud.enabled` is false.

DeepSeek's API is OpenAI-compatible, so integrating V4 Flash is a configuration
change only.

## Decisions

- **Model**: `deepseek-v4-flash` (per user, matches the DeepSeek API model name).
- **Base URL**: `https://api.deepseek.com/v1`.
- **API key**: reuse `YUKI_CLOUD_API_KEY` env var; `api_key_env` stays as-is.
- **Approach**: pure config switch; code defaults remain OpenAI and untouched.
  No multi-provider abstraction (YAGNI).
- **Default state**: `cloud.enabled` stays `false` in the committed example; the
  user enables it in their local `config.yaml`.

## Changes

1. **config.example.yaml** (committed) — cloud section updated to DeepSeek
   defaults:
   - `base_url: https://api.deepseek.com/v1`
   - `model: deepseek-v4-flash`
   - `api_key_env: YUKI_CLOUD_API_KEY` (unchanged)
   - `enabled: false`, `timeout_s: 10.0`, `max_turns: 3` (unchanged)
2. **config.yaml** (local, not committed) — copy of example with
   `cloud.enabled: true` so the running instance uses DeepSeek.

## Out of Scope

- No code changes (no edits to `config.py`, `client.py`, `bridge.py`,
  `assembly.py`).
- No provider abstraction layer.
- No change to VLM/STT/embedding providers (DeepSeek is text-only chat here).

## Verification

- `pytest` full suite still green (config parsing, bridge logic unaffected).
- Offline smoke: construct `CloudConfig` with DeepSeek values and confirm
  `CloudClient` is built with the expected `base_url`/`model`.
- Live smoke (when `YUKI_CLOUD_API_KEY` is set): one real `/chat/completions`
  call confirms connectivity.
