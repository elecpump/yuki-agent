import pytest

from yuki.memory.manager import MemoryManager
from yuki.memory.service import register_memory_services
from yuki.memory.store import MemoryError, MemoryStore

from tests.fakes import FakeBus


@pytest.fixture()
def bus_and_manager(tmp_path):
    bus = FakeBus()
    manager = MemoryManager(MemoryStore(tmp_path / "mem.db"))
    register_memory_services(bus, manager)
    yield bus, manager
    manager.close()


def test_write_then_query(bus_and_manager):
    bus, _ = bus_and_manager
    rid = bus.request("memory/write", {"memory_type": "preference", "content": "用户喜欢猫"})["id"]
    assert rid > 0
    results = bus.request("memory/query", {"text": "猫", "top_k": 3})["results"]
    assert results[0]["content"] == "用户喜欢猫"
    assert "score" in results[0]


def test_list_and_get(bus_and_manager):
    bus, _ = bus_and_manager
    rid = bus.request("memory/write", {"memory_type": "scenario", "content": "在读某文"})["id"]
    listed = bus.request("memory/list", {})["results"]
    assert len(listed) == 1
    got = bus.request("memory/get", {"id": rid})["memory"]
    assert got["content"] == "在读某文"


def test_get_missing_raises_memory_error(bus_and_manager):
    bus, _ = bus_and_manager
    with pytest.raises(MemoryError):
        bus.request("memory/get", {"id": 999})


def test_delete_strengthen_wipe(bus_and_manager):
    bus, _ = bus_and_manager
    rid = bus.request("memory/write", {"memory_type": "personal", "content": "小羽"})["id"]
    assert bus.request("memory/strengthen", {"id": rid})["ok"] is True
    assert bus.request("memory/delete", {"id": rid})["deleted"] is True
    rid2 = bus.request("memory/write", {"memory_type": "preference", "content": "x"})["id"]
    assert bus.request("memory/wipe", {})["deleted_count"] == 1

def test_read_services_strip_high_sensitivity(bus_and_manager):
    bus, _ = bus_and_manager
    rid = bus.request("memory/write", {
        "memory_type": "personal", "content": "银行卡密码",
        "sensitivity": 2,
    })["id"]
    assert bus.request("memory/query", {"text": "银行卡", "top_k": 5})["results"] == []
    assert bus.request("memory/list", {})["results"] == []
    assert bus.request("memory/get", {"id": rid})["memory"] is None
