import json

import pytest

from yuki.cognition.brain.soul import (
    SoulConflictError,
    SoulRestoreError,
    SoulStore,
    SoulValidationError,
)


def test_update_applies_partial_traits_clamps_and_skips_noop(tmp_path):
    store = SoulStore(tmp_path / "soul.json", "yuki", min_snapshot_interval_s=0)
    store.ensure()

    result = store.update(traits={"warmth": 2.0}, source="realtime")

    assert result == {
        "changed": True,
        "revision": 1,
        "changed_fields": ["personality_traits"],
    }
    assert store.load()["personality_traits"] == {
        "warmth": 1.0,
        "humor": 0.5,
        "directness": 0.5,
        "proactiveness": 0.5,
        "empathy": 0.5,
    }
    assert store.update(traits={"warmth": 1.0}, source="realtime")["changed"] is False
    assert store.load()["revision"] == 1


def test_update_rejects_unknown_trait_without_writing(tmp_path):
    path = tmp_path / "soul.json"
    store = SoulStore(path, "yuki")
    store.ensure()
    before = path.read_text(encoding="utf-8")

    with pytest.raises(SoulValidationError, match="unknown trait"):
        store.update(traits={"patience": 0.7}, source="realtime")

    assert path.read_text(encoding="utf-8") == before


@pytest.mark.parametrize(
    "values, message",
    [
        ([], "non-empty"),
        ([{"id": "", "text": "x", "role": "guiding"}], "id must be non-empty"),
        ([{"id": "a", "text": " ", "role": "guiding"}], "text must be non-empty"),
        ([{"id": "a", "text": "x", "role": "other"}], "invalid role"),
        (
            [
                {"id": "a", "text": "x", "role": "guiding"},
                {"id": "a", "text": "y", "role": "binding"},
            ],
            "duplicate",
        ),
    ],
)
def test_core_values_are_validated_as_one_atomic_replacement(tmp_path, values, message):
    store = SoulStore(tmp_path / "soul.json", "yuki")
    original = store.ensure()["core_values"]

    with pytest.raises(SoulValidationError, match=message):
        store.update(core_values=values, source="periodic")

    assert store.load()["core_values"] == original
    assert store.load()["revision"] == 0


def test_expected_revision_rejects_stale_reflection(tmp_path):
    store = SoulStore(tmp_path / "soul.json", "yuki")
    store.ensure()
    base_revision = store.load()["revision"]
    store.update(description="实时路径的新描述", source="realtime")

    with pytest.raises(SoulConflictError, match="stale soul revision"):
        store.update(
            description="旧反思生成的描述",
            source="periodic",
            expected_revision=base_revision,
        )

    assert store.load()["personality_description"] == "实时路径的新描述"


def test_snapshots_coalesce_and_restore_creates_new_revision(tmp_path):
    now = [100.0]
    store = SoulStore(
        tmp_path / "soul.json",
        "yuki",
        snapshots_dir=tmp_path / "snapshots",
        min_snapshot_interval_s=60,
        clock=lambda: now[0],
    )
    store.ensure()
    store.update(traits={"warmth": 0.8}, source="realtime")
    now[0] += 10
    store.update(traits={"humor": 0.9}, source="realtime")

    names = sorted(path.name for path in (tmp_path / "snapshots").iterdir())
    assert names == ["soul_snapshot_r000000.json", "soul_snapshot_r000002.json"]

    result = store.restore(0)

    assert result == {"changed": True, "revision": 3, "restored_revision": 0}
    restored = store.load()
    assert restored["revision"] == 3
    assert restored["personality_traits"]["warmth"] == pytest.approx(0.5)
    assert "prefs_since_regen" not in json.loads(
        (tmp_path / "soul.json").read_text(encoding="utf-8")
    )


def test_list_revisions_exposes_only_restorable_committed_versions(tmp_path):
    store = SoulStore(
        tmp_path / "soul.json",
        "yuki",
        snapshots_dir=tmp_path / "snapshots",
        min_snapshot_interval_s=0,
    )
    store.ensure()
    assert store.list_revisions() == []
    store.update(traits={"warmth": 0.8}, source="realtime")
    assert store.list_revisions() == [0, 1]


def test_update_and_restore_notify_runtime_prompt_refresh(tmp_path):
    refreshed = []
    store = SoulStore(
        tmp_path / "soul.json",
        "yuki",
        snapshots_dir=tmp_path / "snapshots",
        min_snapshot_interval_s=0,
    )
    store.set_on_updated(lambda: refreshed.append(store.load_or_default()["revision"]))
    store.ensure()

    store.update(traits={"warmth": 0.8}, source="realtime")
    store.update(traits={"warmth": 0.8}, source="realtime")
    store.restore(0)

    assert refreshed == [1, 2]


def test_description_limit_is_enforced(tmp_path):
    store = SoulStore(tmp_path / "soul.json", "yuki", max_description_chars=5)
    with pytest.raises(SoulValidationError, match="exceeds"):
        store.update(description="123456", source="realtime")


def test_restore_rejects_staged_revision_that_never_committed(tmp_path):
    snapshots = tmp_path / "snapshots"
    store = SoulStore(tmp_path / "soul.json", "yuki", snapshots_dir=snapshots)
    current = store.ensure()
    orphan = {**current, "revision": 1, "personality_description": "从未提交"}
    snapshots.mkdir()
    (snapshots / "soul_snapshot_r000001.json").write_text(
        json.dumps({"saved_at": 1.0, "soul": orphan}),
        encoding="utf-8",
    )

    with pytest.raises(SoulRestoreError, match="uncommitted"):
        store.restore(1)

    assert store.load()["personality_description"] != "从未提交"


def test_max_versions_one_is_honored_without_internal_clamp(tmp_path):
    snapshots = tmp_path / "snapshots"
    store = SoulStore(
        tmp_path / "soul.json",
        "yuki",
        snapshots_dir=snapshots,
        max_versions=1,
        min_snapshot_interval_s=0,
    )
    store.ensure()
    store.update(traits={"warmth": 0.7}, source="realtime")
    store.update(traits={"humor": 0.8}, source="realtime")

    assert [path.name for path in snapshots.iterdir()] == [
        "soul_snapshot_r000002.json"
    ]
