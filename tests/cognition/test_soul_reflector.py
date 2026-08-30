import json

import pytest

from yuki.cognition.brain.soul import SoulStore
from yuki.cognition.brain.soul_reflector import SoulReflector
from yuki.cognition.l2.client import CloudClient, CloudError
from yuki.memory.manager import MemoryManager
from yuki.memory.provenance import AUTOMATIC_STRENGTHENER
from yuki.memory.store import MemoryStore


class FakeClient:
    def __init__(self, content: str):
        self.content = content
        self.calls = []

    def chat(self, messages, tools=None, timeout_s=None):
        self.calls.append({"messages": messages, "tools": tools, "timeout_s": timeout_s})
        return {"choices": [{"message": {"content": self.content}}]}


def test_reflector_uses_only_automatic_stable_preferences_and_commits_candidate(tmp_path):
    memory = MemoryManager(MemoryStore(tmp_path / "memory.db"))
    memory.write("preference", "普通公开偏好", sensitivity=0)
    manual_id = memory.write("preference", "人工强化偏好", sensitivity=0)
    memory.strengthen(manual_id)
    stable_id = memory.write(
        "preference",
        "自动成熟偏好",
        sensitivity=0,
        metadata={"strengthened_by": AUTOMATIC_STRENGTHENER},
    )
    memory.strengthen(stable_id)
    memory.write("preference", "私密偏好", sensitivity=1)
    store = SoulStore(tmp_path / "soul.json", "yuki", min_snapshot_interval_s=0)
    store.ensure()
    client = FakeClient('```json\n{"traits":{"warmth":0.9}}\n```')
    refreshed = []
    reflector = SoulReflector(
        client,
        store,
        memory,
        on_updated=lambda: refreshed.append(True),
        timeout_s=3.0,
    )

    assert reflector.reflect() is True

    assert store.load()["personality_traits"]["warmth"] == pytest.approx(0.9)
    assert refreshed == [True]
    call = client.calls[0]
    assert call["tools"] is None
    assert call["timeout_s"] == 3.0
    user_content = call["messages"][1]["content"]
    payload = json.loads(
        user_content.removeprefix("<reflection-data>\n").removesuffix(
            "\n</reflection-data>"
        )
    )
    assert [item["content"] for item in payload["preferences"]] == ["自动成熟偏好"]
    assert "recent_turns" not in payload
    memory.close()


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        '{"unknown": 1}',
        "{}",
    ],
)
def test_reflector_skips_invalid_or_empty_candidate(tmp_path, content):
    memory = MemoryManager(MemoryStore(tmp_path / "memory.db"))
    store = SoulStore(tmp_path / "soul.json", "yuki")
    store.ensure()
    reflector = SoulReflector(
        FakeClient(content),
        store,
        memory,
    )

    assert reflector.reflect() is False
    assert store.load()["revision"] == 0
    memory.close()


def test_reflector_skips_cloud_failure(tmp_path):
    class FailingClient:
        def chat(self, messages, tools=None, timeout_s=None):
            raise CloudError("down")

    memory = MemoryManager(MemoryStore(tmp_path / "memory.db"))
    store = SoulStore(tmp_path / "soul.json", "yuki")
    reflector = SoulReflector(
        FailingClient(),
        store,
        memory,
    )

    assert reflector.reflect() is False
    memory.close()


def test_reflector_skips_cloud_timeout(tmp_path):
    def timeout_post(url, headers, payload, timeout):
        raise TimeoutError("timed out")

    memory = MemoryManager(MemoryStore(tmp_path / "memory.db"))
    store = SoulStore(tmp_path / "soul.json", "yuki")
    client = CloudClient(
        "http://cloud.invalid",
        "model",
        timeout_s=0.1,
        post=timeout_post,
    )
    reflector = SoulReflector(
        client,
        store,
        memory,
        timeout_s=0.1,
    )

    assert reflector.reflect() is False
    assert store.load_or_default()["revision"] == 0
    memory.close()


def test_reflector_rejects_candidate_generated_from_stale_revision(tmp_path):
    memory = MemoryManager(MemoryStore(tmp_path / "memory.db"))
    store = SoulStore(tmp_path / "soul.json", "yuki")
    store.ensure()

    class RacingClient:
        def chat(self, messages, tools=None, timeout_s=None):
            store.update(description="实时更新", source="realtime")
            return {"choices": [{"message": {"content": '{"description":"旧反思"}'}}]}

    reflector = SoulReflector(
        RacingClient(),
        store,
        memory,
    )

    assert reflector.reflect() is False
    assert store.load()["personality_description"] == "实时更新"
    memory.close()


def test_reflector_honors_cancellation_before_commit(tmp_path):
    memory = MemoryManager(MemoryStore(tmp_path / "memory.db"))
    store = SoulStore(tmp_path / "soul.json", "yuki")
    reflector = SoulReflector(
        FakeClient('{"description":"不应提交"}'),
        store,
        memory,
    )

    assert reflector.reflect(cancelled=lambda: True) is False
    assert store.load_or_default()["revision"] == 0
    memory.close()
