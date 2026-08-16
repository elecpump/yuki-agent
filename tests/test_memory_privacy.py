from yuki.memory.manager import MemoryManager
from yuki.memory.privacy import MemoryAccess, MemoryPrivacyPolicy, MemoryPurpose
from yuki.memory.store import MemoryStore


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
