import copy
import json
import threading
import time
from collections.abc import Callable
from pathlib import Path

from yuki.cognition.brain.soul_contract import (
    SoulConflictError,
    SoulRestoreError,
    SoulUpdateSource,
    SoulValidationError,
    validate_core_values,
    validate_description,
    validate_traits_patch,
)
from yuki.cognition.brain.soul_versions import SoulVersionStore
from yuki.cognition.brain.tuner_state import COOLDOWN_KEY, FLOOR_KEY, TunerStateStore

from yuki.logger import get_audit_logger, get_logger
from yuki.persistence import atomic_write_json

logger = get_logger("yuki.cognition.brain.soul")

DEFAULT_TRAITS = {
    "warmth": 0.5,
    "humor": 0.5,
    "directness": 0.5,
    "proactiveness": 0.5,
    "empathy": 0.5,
}

INITIAL_PERSONALITY_DESCRIPTION = (
    "你是{persona},一个温柔的中文语音陪伴 agent。"
    "回复简短自然(1-3 句),贴合陪伴场景。"
    "不替用户操作系统或浏览器。"
    "用户提到自伤/自杀等危机时,优先表达关怀并建议求助。"
    "可以用工具查询记忆,但不要捏造记忆内容。"
)

INITIAL_CORE_VALUES = (
    {
        "id": "cv.safety",
        "text": "用户提到自伤、自杀等危机时,优先表达关怀并建议求助。",
        "source": "initial",
        "role": "binding",
        "confidence": 1.0,
        "blocks": [],
        "keywords": ["安全", "危机", "自伤", "自杀", "求助"],
    },
    {
        "id": "cv.companionship",
        "text": "陪伴优先于解决问题,先回应感受并尊重用户节奏。",
        "source": "initial",
        "role": "guiding",
        "confidence": 1.0,
        "keywords": ["陪伴", "感受", "节奏"],
    },
)

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _clamp_unit(value: float) -> float:
    return min(max(value, 0.0), 1.0)


class SoulStore:
    """Thread-safe persona kernel store with versioned, auditable updates."""

    def __init__(
        self,
        path: str | Path,
        persona_name: str,
        persona_version: int | None = None,
        *,
        default_description: str | None = None,
        tuner_state_path: str | Path | None = None,
        snapshots_dir: str | Path | None = None,
        max_versions: int = 50,
        min_snapshot_interval_s: float = 60.0,
        max_description_chars: int = 2000,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = Path(path)
        self._persona_name = persona_name
        self._default_description = default_description or INITIAL_PERSONALITY_DESCRIPTION.format(
            persona=persona_name
        )
        self._tuner_state = TunerStateStore(
            tuner_state_path or self._path.with_name("tuner_state.json"),
            persona_name,
        )
        self._ignored_persona_version = persona_version
        self._versions = SoulVersionStore(
            snapshots_dir or self._path.with_name("soul_snapshots"),
            max_versions=max_versions,
            min_snapshot_interval_s=min_snapshot_interval_s,
            clock=clock,
        )
        self._max_description_chars = max(1, int(max_description_chars))
        self._lock = threading.RLock()

    def default_soul(self) -> dict:
        return {
            "persona_name": self._persona_name,
            "core_values": copy.deepcopy(list(INITIAL_CORE_VALUES)),
            "personality_traits": dict(DEFAULT_TRAITS),
            "personality_description": self._default_description,
            "revision": 0,
            "updated_at": _now_iso(),
        }

    @property
    def tuner_state(self) -> TunerStateStore:
        return self._tuner_state

    def load(self) -> dict | None:
        with self._lock:
            return self._load_unlocked()

    def _load_unlocked(self) -> dict | None:
        if not self._path.exists():
            return None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("soul read failed", error=str(exc))
            return None
        if not isinstance(data, dict):
            return None
        if self._is_legacy_params_shape(data):
            migrated = self._migrate_legacy_params(data)
            return migrated
        if data.get("persona_name") != self._persona_name:
            return None
        normalized = self._normalize(data)
        if normalized is None:
            return None
        return normalized

    def load_or_default(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._load_unlocked() or self.default_soul())

    def ensure(self) -> dict:
        with self._lock:
            soul = self._load_unlocked()
            if soul is None:
                soul = self.default_soul()
                self._write_unlocked(soul)
                get_audit_logger().info("soul.save", persona=self._persona_name)
            return copy.deepcopy(soul)

    def save(self, soul: dict) -> None:
        """Compatibility write path for initialization and legacy migration."""
        with self._lock:
            normalized = self._normalize({**soul, "persona_name": self._persona_name})
            if normalized is None:
                normalized = self.default_soul()
            normalized["updated_at"] = _now_iso()
            try:
                self._write_unlocked(normalized)
            except OSError as exc:
                logger.warning("soul write failed", error=str(exc))
            else:
                get_audit_logger().info("soul.save", persona=self._persona_name)

    def reset(self) -> None:
        self.save(self.default_soul())

    def snapshot(self) -> dict:
        with self._lock:
            soul = self._load_unlocked() or self.default_soul()
            return {
                "core_values": copy.deepcopy(soul["core_values"]),
                "personality_traits": dict(soul["personality_traits"]),
            }

    def binding_core_values(self) -> list[dict]:
        with self._lock:
            soul = self._load_unlocked() or self.default_soul()
            return copy.deepcopy([
                value for value in soul["core_values"] if value.get("role") == "binding"
            ])

    def set_personality_description(self, description: str) -> bool:
        description = (description or "").strip()
        if not description:
            return False
        with self._lock:
            soul = self._load_unlocked() or self.default_soul()
            if soul.get("personality_description") == description:
                return False
            soul["personality_description"] = description
            self.save(soul)
            return True

    def update(
        self,
        *,
        traits: dict | None = None,
        core_values: list[dict] | None = None,
        description: str | None = None,
        source: SoulUpdateSource,
        expected_revision: int | None = None,
    ) -> dict:
        """Atomically validate and commit a Soul mutation.

        Traits are a partial patch. Core values are an atomic full replacement.
        """
        if traits is None and core_values is None and description is None:
            raise SoulValidationError("at least one update field is required")
        normalized_traits = (
            validate_traits_patch(traits, set(DEFAULT_TRAITS)) if traits is not None else None
        )
        normalized_values = (
            validate_core_values(core_values) if core_values is not None else None
        )
        normalized_description = (
            validate_description(description, self._max_description_chars)
            if description is not None
            else None
        )
        if normalized_traits == {} and core_values is None and description is None:
            raise SoulValidationError("traits patch must not be empty")

        with self._lock:
            current = self._load_unlocked() or self.default_soul()
            revision = int(current.get("revision", 0))
            if expected_revision is not None and expected_revision != revision:
                raise SoulConflictError(
                    f"stale soul revision: expected {expected_revision}, current {revision}"
                )
            updated = copy.deepcopy(current)
            changed_fields = []
            if normalized_traits is not None:
                next_traits = {**updated["personality_traits"], **normalized_traits}
                if next_traits != updated["personality_traits"]:
                    updated["personality_traits"] = next_traits
                    changed_fields.append("personality_traits")
            if normalized_values is not None and normalized_values != updated["core_values"]:
                updated["core_values"] = normalized_values
                changed_fields.append("core_values")
            if (
                normalized_description is not None
                and normalized_description != updated["personality_description"]
            ):
                updated["personality_description"] = normalized_description
                changed_fields.append("personality_description")
            if not changed_fields:
                return {"changed": False, "revision": revision, "changed_fields": []}

            self._versions.ensure_baseline(current)
            updated["revision"] = revision + 1
            updated["updated_at"] = _now_iso()
            snapshot_path, snapshot_saved_at = self._versions.stage(updated)
            try:
                self._write_unlocked(updated)
            except OSError:
                self._versions.discard(snapshot_path)
                raise
            self._versions.finalize(updated["revision"], snapshot_saved_at)
            get_audit_logger().info(
                "soul.update",
                persona=self._persona_name,
                source=source,
                revision=updated["revision"],
                previous_revision=revision,
                changed_fields=changed_fields,
            )
            return {
                "changed": True,
                "revision": updated["revision"],
                "changed_fields": changed_fields,
            }

    def restore(self, revision: int) -> dict:
        with self._lock:
            current = self._load_unlocked() or self.default_soul()
            current_revision = int(current.get("revision", 0))
            restored = self._versions.load(revision, current_revision=current_revision)
            normalized = self._normalize(restored)
            if normalized is None:
                raise SoulRestoreError(f"invalid soul snapshot: {revision}")
            self._versions.ensure_baseline(current)
            normalized["revision"] = current_revision + 1
            normalized["updated_at"] = _now_iso()
            snapshot_path, snapshot_saved_at = self._versions.stage(normalized)
            try:
                self._write_unlocked(normalized)
            except OSError:
                self._versions.discard(snapshot_path)
                raise
            self._versions.finalize(normalized["revision"], snapshot_saved_at)
            get_audit_logger().info(
                "soul.restore",
                persona=self._persona_name,
                restored_revision=revision,
                revision=normalized["revision"],
                previous_revision=current_revision,
            )
            return {
                "changed": True,
                "revision": normalized["revision"],
                "restored_revision": revision,
            }

    def _is_legacy_params_shape(self, data: dict) -> bool:
        return isinstance(data.get("params"), dict) and (
            "persona_version" in data or COOLDOWN_KEY in data.get("params", {})
        )

    def _migrate_legacy_params(self, data: dict) -> dict | None:
        if data.get("persona_name") != self._persona_name:
            return None
        params = data.get("params") or {}
        if isinstance(params.get(COOLDOWN_KEY), (int, float)):
            self._tuner_state.save({COOLDOWN_KEY: float(params[COOLDOWN_KEY])})
        soul = self.default_soul()
        self.save(soul)
        return soul

    def _normalize(self, data: dict) -> dict | None:
        if data.get("persona_name") != self._persona_name:
            return None
        core_values = data.get("core_values")
        traits = data.get("personality_traits")
        description = data.get("personality_description")
        if not isinstance(core_values, list) or not isinstance(traits, dict):
            return None
        if not isinstance(description, str):
            return None
        normalized = self.default_soul()
        normalized["core_values"] = [
            value for value in (self._normalize_core_value(v) for v in core_values) if value
        ]
        if not normalized["core_values"]:
            normalized["core_values"] = copy.deepcopy(list(INITIAL_CORE_VALUES))
        normalized["personality_traits"] = {
            name: _clamp_unit(float(traits.get(name, default)))
            if isinstance(traits.get(name, default), (int, float))
            else default
            for name, default in DEFAULT_TRAITS.items()
        }
        normalized["personality_description"] = description
        revision = data.get("revision", 0)
        normalized["revision"] = (
            revision if isinstance(revision, int) and not isinstance(revision, bool) else 0
        )
        normalized["revision"] = max(0, normalized["revision"])
        normalized["updated_at"] = data.get("updated_at") or _now_iso()
        return normalized

    def _write_unlocked(self, soul: dict) -> None:
        atomic_write_json(self._path, soul)

    def _normalize_core_value(self, value: object) -> dict | None:
        if not isinstance(value, dict):
            return None
        if not isinstance(value.get("id"), str) or not isinstance(value.get("text"), str):
            return None
        role = value.get("role")
        if role not in ("guiding", "binding"):
            role = "guiding"
        normalized = {
            "id": value["id"],
            "text": value["text"],
            "source": value.get("source") or "unknown",
            "role": role,
            "confidence": _clamp_unit(self._float_or_default(value.get("confidence"), 0.5)),
        }
        if role == "binding":
            blocks = value.get("blocks")
            normalized["blocks"] = (
                [block for block in blocks if isinstance(block, str)]
                if isinstance(blocks, list)
                else []
            )
        if isinstance(value.get("keywords"), list):
            normalized["keywords"] = [kw for kw in value["keywords"] if isinstance(kw, str)]
        return normalized

    def _float_or_default(self, value: object, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
