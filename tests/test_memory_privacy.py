from yuki.memory.manager import MemoryManager
from yuki.memory.privacy import MemoryAccess, MemoryPrivacyPolicy, MemoryPurpose
from yuki.memory.store import MemoryStore

from tests.fakes import mark_automatically_strengthened


def test_privacy_policy_is_purpose_aware() -> None:
    policy = MemoryPrivacyPolicy()
    public = {"sensitivity": 0}
    private = {"sensitivity": 1}
    high = {"sensitivity": 2}

    assert policy.allows(public, MemoryPurpose.CLOUD_MODEL_CONTEXT)
    assert not policy.allows(private, MemoryPurpose.CLOUD_MODEL_CONTEXT)
    assert not policy.allows(high, MemoryPurpose.CLOUD_MODEL_CONTEXT)

    assert policy.allows(private, MemoryPurpose.LOCAL_MODEL_CONTEXT)
    assert not policy.allows(high, MemoryPurpose.LOCAL_MODEL_CONTEXT)

    assert policy.allows(high, MemoryPurpose.USER_EXPLICIT_VIEW)


def test_memory_access_filters_by_purpose(tmp_path) -> None:
    manager = MemoryManager(MemoryStore(tmp_path / "m.db"))
    manager.write("preference", "安静公共偏好", sensitivity=0)
    private_id = manager.write("preference", "安静私密偏好", sensitivity=1)
    high_id = manager.write("personal", "安静高敏资料", sensitivity=2)

    access = MemoryAccess(manager)

    cloud = access.query("安静", purpose=MemoryPurpose.CLOUD_MODEL_CONTEXT, top_k=10)
    local = access.query("安静", purpose=MemoryPurpose.LOCAL_MODEL_CONTEXT, top_k=10)
    user_list = access.list(purpose=MemoryPurpose.USER_EXPLICIT_VIEW)

    assert [m["content"] for m in cloud] == ["安静公共偏好"]
    assert {m["content"] for m in local} == {"安静公共偏好", "安静私密偏好"}
    assert {m["content"] for m in user_list} == {
        "安静公共偏好",
        "安静私密偏好",
        "安静高敏资料",
    }
    assert access.get(private_id, purpose=MemoryPurpose.CLOUD_MODEL_CONTEXT) is None
    assert access.get(high_id, purpose=MemoryPurpose.USER_EXPLICIT_VIEW)["sensitivity"] == 2


def test_memory_access_query_only_touches_filtered_results(tmp_path) -> None:
    manager = MemoryManager(MemoryStore(tmp_path / "m.db"))
    public_id = manager.write("preference", "xy public", sensitivity=0)
    private_id = manager.write("preference", "xy private", sensitivity=1)
    high_id = manager.write("personal", "xy high", sensitivity=2)

    access = MemoryAccess(manager)
    results = access.query("xy", purpose=MemoryPurpose.CLOUD_MODEL_CONTEXT, top_k=1)

    assert [m["id"] for m in results] == [public_id]
    assert manager._store.get(public_id)["access_count"] == 1
    assert manager._store.get(private_id)["access_count"] == 0
    assert manager._store.get(high_id)["access_count"] == 0


def test_personality_evidence_requires_automatic_strengthening(tmp_path) -> None:
    manager = MemoryManager(MemoryStore(tmp_path / "m.db"))
    manager.write("preference", "普通活跃偏好", sensitivity=0)
    manual_id = manager.write("preference", "人工强化偏好", sensitivity=0)
    manager.strengthen(manual_id)
    automatic_id = manager.write(
        "preference",
        "自动成熟偏好",
        sensitivity=0,
    )
    private_id = manager.write(
        "preference",
        "私密自动成熟偏好",
        sensitivity=1,
    )
    scenario_id = manager.write(
        "scenario",
        "自动强化场景",
        sensitivity=0,
    )
    mark_automatically_strengthened(
        tmp_path / "m.db", automatic_id, private_id, scenario_id
    )

    evidence = MemoryAccess(manager).personality_evidence()

    assert [item["content"] for item in evidence] == ["自动成熟偏好"]
    manager.close()
