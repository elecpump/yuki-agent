from yuki.memory.manager import MemoryManager
from yuki.memory.privacy import MemoryAccess, MemoryPurpose
from yuki.memory.store import MemoryError

MEMORY_SERVICES = (
    "memory/write", "memory/query", "memory/list", "memory/get",
    "memory/delete", "memory/strengthen", "memory/wipe",
)


def _require(value, message):
    if value is None:
        raise MemoryError(message)
    return {"memory": value}

def register_memory_services(bus, manager: MemoryManager) -> None:
    """注册 memory/* REQ/REP 服务。handler 抛出的异常由 BusNode 转 response error。"""
    access = MemoryAccess(manager)

    def on_write(payload: dict) -> dict:
        return {"id": manager.write(
            payload["memory_type"], payload["content"],
            confidence=payload.get("confidence", 0.5),
            sensitivity=payload.get("sensitivity", 0),
            source=payload.get("source", "cli"),
            metadata=payload.get("metadata"),
        )}

    def on_query(payload: dict) -> dict:
        return {"results": access.query(
            payload["text"],
            purpose=MemoryPurpose.LOCAL_MODEL_CONTEXT,
            memory_type=payload.get("type"),
            top_k=payload.get("top_k", 5),
            min_sensitivity=payload.get("min_sensitivity", 0),
        )
        }

    def on_list(payload: dict) -> dict:
        # 用户可查看任意记忆（§5.4）：list/get 不按敏感度过滤，仅检索路径过滤。
        return {"results": access.list(
            purpose=MemoryPurpose.USER_EXPLICIT_VIEW,
            memory_type=payload.get("type"),
            min_sensitivity=payload.get("min_sensitivity", 0),
        )
        }


    def on_get(payload: dict) -> dict:
        return _require(
            access.get(payload["id"], purpose=MemoryPurpose.USER_EXPLICIT_VIEW),
            "memory not found",
        )
    bus.respond("memory/write", on_write)
    bus.respond("memory/query", on_query)
    bus.respond("memory/list", on_list)
    bus.respond("memory/get", on_get)
    bus.respond("memory/delete", lambda p: {"deleted": manager.delete(p["id"])})
    bus.respond("memory/strengthen", lambda p: {"ok": manager.strengthen(p["id"])})
    bus.respond("memory/wipe", lambda p: {"deleted_count": manager.wipe()})
