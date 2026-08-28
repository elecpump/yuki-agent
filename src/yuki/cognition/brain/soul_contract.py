import math
from typing import Literal


SoulUpdateSource = Literal["realtime", "periodic"]


class SoulValidationError(ValueError):
    """Raised when a requested Soul mutation violates the update contract."""


class SoulConflictError(ValueError):
    """Raised when a mutation was generated from a stale Soul revision."""


class SoulRestoreError(SoulValidationError):
    """Raised when a requested Soul revision cannot be restored."""


def validate_traits_patch(traits: dict, allowed_traits: set[str]) -> dict[str, float]:
    if not isinstance(traits, dict):
        raise SoulValidationError("traits must be an object")
    unknown = sorted(set(traits) - allowed_traits)
    if unknown:
        raise SoulValidationError(f"unknown trait keys: {', '.join(unknown)}")
    normalized = {}
    for name, value in traits.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SoulValidationError(f"trait {name} must be a number")
        if not math.isfinite(float(value)):
            raise SoulValidationError(f"trait {name} must be finite")
        normalized[name] = min(max(float(value), 0.0), 1.0)
    return normalized


def validate_core_values(values: list[dict]) -> list[dict]:
    if not isinstance(values, list) or not values:
        raise SoulValidationError("core_values must be a non-empty list")
    normalized = []
    ids = set()
    for value in values:
        if not isinstance(value, dict):
            raise SoulValidationError("each core value must be an object")
        value_id = value.get("id")
        text = value.get("text")
        role = value.get("role")
        if not isinstance(value_id, str) or not value_id.strip():
            raise SoulValidationError("core value id must be non-empty")
        value_id = value_id.strip()
        if value_id in ids:
            raise SoulValidationError(f"duplicate core value id: {value_id}")
        if not isinstance(text, str) or not text.strip():
            raise SoulValidationError(f"core value {value_id} text must be non-empty")
        if role not in ("guiding", "binding"):
            raise SoulValidationError(f"core value {value_id} has invalid role")
        confidence = value.get("confidence", 0.5)
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise SoulValidationError(f"core value {value_id} confidence must be a number")
        if not math.isfinite(float(confidence)):
            raise SoulValidationError(f"core value {value_id} confidence must be finite")
        item = {
            "id": value_id,
            "text": text.strip(),
            "source": value.get("source") or "unknown",
            "role": role,
            "confidence": min(max(float(confidence), 0.0), 1.0),
        }
        for key in ("blocks", "keywords"):
            extra = value.get(key)
            if extra is not None:
                if not isinstance(extra, list) or not all(
                    isinstance(entry, str) for entry in extra
                ):
                    raise SoulValidationError(
                        f"core value {value_id} {key} must be a list of strings"
                    )
                item[key] = list(extra)
        normalized.append(item)
        ids.add(value_id)
    return normalized


def validate_description(description: str, max_chars: int) -> str:
    if not isinstance(description, str) or not description.strip():
        raise SoulValidationError("description must be non-empty")
    normalized = description.strip()
    if len(normalized) > max_chars:
        raise SoulValidationError(f"description exceeds {max_chars} characters")
    return normalized
