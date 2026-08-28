import json

from yuki.cognition.brain.soul_versions import SoulVersionStore


def test_list_revisions_returns_only_valid_committed_snapshots(tmp_path):
    versions = SoulVersionStore(
        tmp_path,
        max_versions=10,
        min_snapshot_interval_s=0,
        clock=lambda: 1.0,
    )
    versions.ensure_baseline({"revision": 0, "personality_description": "base"})
    _, saved_at = versions.stage(
        {"revision": 1, "personality_description": "current"}
    )
    versions.finalize(1, saved_at)
    (tmp_path / "soul_snapshot_r000002.json").write_text(
        "not json",
        encoding="utf-8",
    )
    (tmp_path / "soul_snapshot_r000003.json").write_text(
        json.dumps({"saved_at": 2.0, "soul": {"revision": 3}}),
        encoding="utf-8",
    )

    assert versions.list_revisions(current_revision=1) == [0, 1]
