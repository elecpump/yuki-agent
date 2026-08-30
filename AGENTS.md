# Repository Guidelines

## Project Structure & Module Organization

This is a Python 3.11 package using a `src/` layout. Core code lives under `src/yuki/`.
Major runtime modules: `app/` (main process assembly), `model_worker/` (model process),
`perception/`, `cognition/`, `interaction/`, `memory/`, `recorder/`, `supervisor/`, and
`bus_server/` (HTTP/WS Gateway only). Process-boundary utilities live directly in
`src/yuki/` alongside configuration, logging, and process helpers: `runtime_bus.py`
(in-process event bus), `bus_bridge.py` (wire compatibility bridge), `model_client.py`
(remote model clients), and `soul_cli.py` (Soul restore CLI). Protocol definitions are in
`proto/yuki.proto`; generated Python outputs are committed in `src/yuki/proto/`. Tests
live in `tests/` and generally mirror source areas, for example `tests/cognition/`,
`tests/model_worker/`, and `tests/recorder/`. Design notes and implementation plans are
kept in `docs/superpowers/`.

## Build, Test, and Development Commands

- `pip install -e ".[dev,windows]"`: install the package in editable mode with pytest,
  protobuf generation tools, and Windows integration dependencies.
- `pytest`: run the normal test suite. Project config excludes `e2e` tests by default.
- `pytest -m e2e`: run end-to-end tests that spawn real processes.
- `python scripts/generate_proto.py`: regenerate `src/yuki/proto/yuki_pb2.py` and
  `yuki_pb2.pyi` after editing `proto/yuki.proto`.
- `python -m yuki.supervisor`: start the full dual-process runtime (yuki main process +
  model_worker) with health probing and restarts.
- `python -m yuki.app`, `python -m yuki.model_worker`: start each process standalone for
  manual smoke testing (the main process requires a running model_worker).
- `python -m yuki.soul_cli list`: inspect restorable Soul revisions.

## Coding Style & Naming Conventions

Follow existing Python style: 4-space indentation, type annotations on public interfaces,
small modules organized by runtime responsibility, and descriptive `snake_case` names for
functions, variables, files, and test modules. Classes use `PascalCase`. Configuration is
modeled with Pydantic in `src/yuki/config.py`; prefer extending those models over parsing
raw dictionaries. `pyproject.toml` sets Ruff-compatible defaults: Python 3.11 target and
100-character line length.

## Testing Guidelines

Use pytest. Name tests `test_*.py` and keep focused unit tests near the matching package
area under `tests/`. Add or update tests with behavioral changes, especially around bus
messages, process lifecycle, config validation, memory persistence, and generated protobuf
compatibility. Mark real-process integration coverage with `@pytest.mark.e2e` so it stays
out of the default CI run.

## Commit & Pull Request Guidelines

Recent history uses Conventional Commit-style subjects such as `feat: ...` and `fix: ...`;
keep subjects imperative and scoped to one change. Pull requests should include a short
description, test results (`pytest`, plus `pytest -m e2e` when relevant), and links to any
related plan or spec in `docs/superpowers/`. Include logs or reproduction steps for
Windows capture, audio, or multi-process behavior changes.

## Security & Configuration Tips

Copy `config.example.yaml` to `config.yaml` for local settings. Keep secrets in environment
variables such as `YUKI_CLOUD_API_KEY`; do not commit local `data/`, `logs/`, virtualenvs,
or worktrees.

## Agent skills

### Issue tracker

Issues and PRDs live in this repository's GitHub Issues. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the default five-role label vocabulary. See `docs/agents/triage-labels.md`.

### Domain docs

This is a single-context repository. See `docs/agents/domain.md`.
