import copy
import json
import os
import time
from pathlib import Path

from yuki.logger import get_logger

logger = get_logger("yuki.cognition.brain.soul")

TRAIT_CENTER = 0.5
TRAIT_CENTER_STEP = 0.005
PREFS_PER_PERSONA_REGEN = 5

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

CORE_VALUE_CATALOG = {
    "yuki.freq.low": {
        "id": "cv.rhythm.restraint",
        "text": "主动克制、尊重节奏。",
        "keywords": ["主动克制", "尊重节奏", "少主动", "安静"],
    },
    "yuki.rhythm.frequency.low": {
        "id": "cv.rhythm.restraint",
        "text": "主动克制、尊重节奏。",
        "keywords": ["主动克制", "尊重节奏", "少主动", "安静"],
    },
}

CORE_VALUE_OPPOSITION_KEYWORDS = (
    "我不认同",
    "我不需要",
    "我不想",
    "不要",
    "别",
    "不喜欢",
    "删掉",
    "取消",
)

COOLDOWN_KEY = "proactive_cooldown_s"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


def _clamp_unit(value: float) -> float:
    return min(max(value, 0.0), 1.0)


class TunerStateStore:
    """Runtime tuning state, intentionally separate from the persona kernel."""

    def __init__(self, path: str | Path, persona_name: str = "yuki") -> None:
        self._path = Path(path)
        self._persona_name = persona_name

    def load(self) -> dict | None:
        if not self._path.exists():
            return None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("tuner state read failed", error=str(exc))
            return None
        if not isinstance(data, dict):
            return None
        if data.get("persona_name", self._persona_name) != self._persona_name:
            return None
        return data

    def save(self, params: dict) -> None:
        current = self.load() or {}
        payload = {
            "persona_name": self._persona_name,
            **{k: v for k, v in current.items() if k not in ("persona_name", "updated_at")},
            **params,
            "updated_at": _now_iso(),
        }
        try:
            _atomic_write_json(self._path, payload)
        except OSError as exc:
            logger.warning("tuner state write failed", error=str(exc))

    def reset(self) -> None:
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("tuner state reset failed", error=str(exc))


class SoulStore:
    """Persona kernel store: core values, traits, description, and regen counter."""

    def __init__(
        self,
        path: str | Path,
        persona_name: str,
        persona_version: int | None = None,
        *,
        default_description: str | None = None,
        tuner_state_path: str | Path | None = None,
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

    def default_soul(self) -> dict:
        return {
            "persona_name": self._persona_name,
            "core_values": copy.deepcopy(list(INITIAL_CORE_VALUES)),
            "personality_traits": dict(DEFAULT_TRAITS),
            "personality_description": self._default_description,
            "prefs_since_regen": 0,
            "updated_at": _now_iso(),
        }

    @property
    def tuner_state(self) -> TunerStateStore:
        return self._tuner_state

    def load(self) -> dict | None:
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
        return self.load() or self.default_soul()

    def ensure(self) -> dict:
        soul = self.load()
        if soul is None:
            soul = self.default_soul()
            self.save(soul)
        return soul

    def save(self, soul: dict) -> None:
        normalized = self._normalize({**soul, "persona_name": self._persona_name})
        if normalized is None:
            normalized = self.default_soul()
        normalized["updated_at"] = _now_iso()
        try:
            _atomic_write_json(self._path, normalized)
        except OSError as exc:
            logger.warning("soul write failed", error=str(exc))

    def reset(self) -> None:
        self.save(self.default_soul())

    def snapshot(self) -> dict:
        soul = self.load_or_default()
        return {
            "core_values": copy.deepcopy(soul["core_values"]),
            "personality_traits": dict(soul["personality_traits"]),
        }

    def binding_core_values(self) -> list[dict]:
        return [
            value
            for value in self.load_or_default()["core_values"]
            if value.get("role") == "binding"
        ]

    def adjust_traits(self, deltas: dict[str, float]) -> dict:
        soul = self.load_or_default()
        traits = dict(soul["personality_traits"])
        for name in DEFAULT_TRAITS:
            current = float(traits.get(name, TRAIT_CENTER))
            if current > TRAIT_CENTER:
                current = max(TRAIT_CENTER, current - TRAIT_CENTER_STEP)
            elif current < TRAIT_CENTER:
                current = min(TRAIT_CENTER, current + TRAIT_CENTER_STEP)
            traits[name] = current
        for name, delta in deltas.items():
            if name in DEFAULT_TRAITS:
                traits[name] = _clamp_unit(float(traits.get(name, TRAIT_CENTER)) + delta)
        soul["personality_traits"] = traits
        self.save(soul)
        return traits

    def on_preference_sedimented(
        self,
        label: str,
        confidence: float,
        *,
        is_new: bool = True,
    ) -> dict:
        soul = self.load_or_default()
        if is_new:
            soul["prefs_since_regen"] = int(soul.get("prefs_since_regen", 0)) + 1
        if label == "yuki.rhythm.length.short":
            soul["personality_traits"] = self._adjust_trait_values(
                soul["personality_traits"],
                {"directness": -0.05, "humor": -0.02},
            )
        self._promote_catalogued_core_value(soul, label, confidence)
        self.save(soul)
        return soul

    def reset_prefs_since_regen(self) -> None:
        soul = self.load_or_default()
        soul["prefs_since_regen"] = 0
        self.save(soul)

    def set_personality_description(self, description: str) -> bool:
        description = (description or "").strip()
        if not description:
            return False
        soul = self.load_or_default()
        if soul.get("personality_description") == description:
            return False
        soul["personality_description"] = description
        self.save(soul)
        return True

    def apply_core_value_feedback(self, text: str) -> bool:
        if not text or not any(kw in text for kw in CORE_VALUE_OPPOSITION_KEYWORDS):
            return False
        soul = self.load_or_default()
        remaining = []
        changed = False
        for value in soul["core_values"]:
            keywords = value.get("keywords") or [value.get("text", "")]
            matches = any(keyword and keyword in text for keyword in keywords)
            if matches and value.get("role") != "binding":
                replacement = self._extract_core_value_replacement(text)
                if replacement:
                    value["text"] = replacement
                    value["source"] = "user"
                    value["confidence"] = 1.0
                    remaining.append(value)
                changed = True
                if not replacement:
                    continue
                continue
            remaining.append(value)
        if changed:
            soul["core_values"] = remaining
            self.save(soul)
        return changed

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
        try:
            prefs_since_regen = int(data.get("prefs_since_regen", 0) or 0)
        except (TypeError, ValueError):
            prefs_since_regen = 0
        normalized["prefs_since_regen"] = max(0, prefs_since_regen)
        normalized["updated_at"] = data.get("updated_at") or _now_iso()
        return normalized

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
            normalized["blocks"] = [b for b in blocks if isinstance(b, str)] if isinstance(blocks, list) else []
        if isinstance(value.get("keywords"), list):
            normalized["keywords"] = [kw for kw in value["keywords"] if isinstance(kw, str)]
        return normalized

    def _promote_catalogued_core_value(self, soul: dict, label: str, confidence: float) -> None:
        item = CORE_VALUE_CATALOG.get(label)
        if item is None or confidence < 0.7:
            return
        for value in soul["core_values"]:
            if value.get("id") == item["id"]:
                value["confidence"] = max(float(value.get("confidence", 0.0)), confidence)
                return
        soul["core_values"].append({
            "id": item["id"],
            "text": item["text"],
            "source": label,
            "role": "guiding",
            "confidence": _clamp_unit(confidence),
            "keywords": list(item.get("keywords", [])),
        })

    def _extract_core_value_replacement(self, text: str) -> str:
        for marker in ("改成", "改为", "而是"):
            if marker in text:
                replacement = text.split(marker, 1)[1].strip(" ：:，,。.!！")
                if replacement:
                    return replacement
        return ""

    def _adjust_trait_values(self, traits: dict, deltas: dict[str, float]) -> dict:
        adjusted = dict(traits)
        for name in DEFAULT_TRAITS:
            current = float(adjusted.get(name, TRAIT_CENTER))
            if current > TRAIT_CENTER:
                current = max(TRAIT_CENTER, current - TRAIT_CENTER_STEP)
            elif current < TRAIT_CENTER:
                current = min(TRAIT_CENTER, current + TRAIT_CENTER_STEP)
            adjusted[name] = current
        for name, delta in deltas.items():
            if name in DEFAULT_TRAITS:
                adjusted[name] = _clamp_unit(float(adjusted.get(name, TRAIT_CENTER)) + delta)
        return adjusted

    def _float_or_default(self, value: object, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
