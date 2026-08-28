from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from yuki.cognition.brain.soul import SoulStore, SoulValidationError
from yuki.functions.registry import ArgumentValidationError, FunctionRegistry


class CoreValueParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    text: str
    role: Literal["guiding", "binding"]
    confidence: float = 0.5
    source: str | None = None
    blocks: list[str] | None = None
    keywords: list[str] | None = None


class SoulUpdateParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    traits: dict[str, float] | None = None
    core_values: list[CoreValueParams] | None = None
    description: str | None = None

    @model_validator(mode="after")
    def require_update(self) -> "SoulUpdateParams":
        if self.traits is None and self.core_values is None and self.description is None:
            raise ValueError("at least one update field is required")
        return self


def register_soul_functions(
    registry: FunctionRegistry,
    store: SoulStore,
    *,
    on_updated: Callable[[], None] | None = None,
) -> None:
    """Register the model-facing Soul mutation tool with a fixed realtime source."""
    if "soul.update" in registry.names():
        return
    if on_updated is not None:
        store.set_on_updated(on_updated)

    def on_update(params: SoulUpdateParams) -> dict:
        try:
            result = store.update(
                traits=params.traits,
                core_values=(
                    [item.model_dump(exclude_none=True) for item in params.core_values]
                    if params.core_values is not None
                    else None
                ),
                description=params.description,
                source="realtime",
            )
        except SoulValidationError as exc:
            raise ArgumentValidationError(str(exc)) from exc
        return {"updated": bool(result["changed"])}

    registry.tool(
        "soul.update",
        description=(
            "更新人格。traits 是局部修改；core_values 是经过原子校验的全量替换；"
            "description 是完整人格描述。仅在有明确、长期的人格演化理由时调用。"
        ),
        params=SoulUpdateParams,
    )(on_update)
