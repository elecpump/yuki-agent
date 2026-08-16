from typing import Literal

from pydantic import BaseModel, Field

from yuki.functions.registry import FunctionRegistry
from yuki.memory.manager import MemoryManager
from yuki.memory.privacy import MemoryAccess, MemoryPurpose

MemoryType = Literal["preference", "personal", "scenario", "reflection"]


class QueryParams(BaseModel):
    text: str = Field(description="检索关键词")
    top_k: int = Field(5, ge=1, le=20)
    type: str | None = None
    min_sensitivity: int = Field(0, ge=0, le=2)


class WriteParams(BaseModel):
    memory_type: MemoryType
    content: str
    confidence: float = Field(0.5, ge=0.0, le=1.0)
    sensitivity: int = Field(0, ge=0, le=2)
    source: str = "brain"
    metadata: dict = Field(default_factory=dict)


class ListParams(BaseModel):
    type: str | None = None
    min_sensitivity: int = Field(0, ge=0, le=2)


class GetParams(BaseModel):
    id: int


def register_memory_functions(registry: FunctionRegistry, manager: MemoryManager) -> None:
    """绑定记忆函数。隐私硬约束：云端经工具只取到公开记忆。"""
    access = MemoryAccess(manager)

    def on_query(p: QueryParams) -> list:
        return access.query(
            p.text,
            purpose=MemoryPurpose.LLM_TOOL_QUERY_RESULT,
            top_k=p.top_k,
            memory_type=p.type,
            min_sensitivity=p.min_sensitivity,
        )

    def on_write(p: WriteParams) -> dict:
        return {"id": manager.write(
            p.memory_type, p.content, confidence=p.confidence,
            sensitivity=p.sensitivity, source=p.source, metadata=p.metadata)}

    def on_list(p: ListParams) -> list:
        return access.list(
            purpose=MemoryPurpose.LLM_TOOL_QUERY_RESULT,
            memory_type=p.type,
            min_sensitivity=p.min_sensitivity,
        )

    def on_get(p: GetParams) -> dict:
        return {"memory": access.get(p.id, purpose=MemoryPurpose.LLM_TOOL_QUERY_RESULT)}

    registry.tool("memory.query", description="检索记忆（私密/高敏自动排除）", params=QueryParams)(on_query)
    registry.tool("memory.write", description="写入一条记忆", params=WriteParams)(on_write)
    registry.tool("memory.list", description="列出记忆（私密/高敏自动排除）", params=ListParams)(on_list)
    registry.tool("memory.get", description="按 id 获取记忆（私密/高敏返回 null）", params=GetParams)(on_get)
