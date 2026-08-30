import json

import pytest

from yuki.cognition.context.sediment import (
    CandidateValidationError,
    Sedimenter,
    normalize_canonical_key,
    validate_candidates,
)


def _turn(turn_id: int, content: str, *, role: str = "user") -> dict:
    return {"id": turn_id, "role": role, "kind": role, "content": content}


def test_normalize_canonical_key_handles_unicode_case_space_and_stopwords():
    assert normalize_canonical_key(" ＲＰＧ 游戏 偏好！ ") == "rpg"


def test_validator_accepts_normalized_quote_substring():
    candidates = [
        {
            "draft_key": "rpg-like",
            "proposed_op": "add",
            "memory_type": "preference",
            "canonical_key": "RPG 游戏偏好",
            "content": "用户喜欢 RPG 游戏",
            "confidence": 0.92,
            "sensitivity": 0,
            "evidence": [{"turn_id": 7, "quote": "我 喜欢RPG游戏!"}],
            "metadata": {},
        }
    ]

    validated = validate_candidates(
        candidates,
        turns=[_turn(7, "嗯，我喜欢 RPG 游戏！")],
        related=[],
    )

    assert validated[0].canonical_key_norm == "rpg"


def test_validator_strips_system_owned_provenance_from_llm_metadata():
    candidate = {
        "draft_key": "tea",
        "proposed_op": "add",
        "memory_type": "preference",
        "canonical_key": "饮料偏好",
        "content": "用户喜欢喝茶",
        "confidence": 0.95,
        "sensitivity": 0,
        "evidence": [{"turn_id": 7, "quote": "我喜欢喝茶"}],
        "metadata": {
            "strengthened_by": "memory_evolver",
            "strengthened_episode_count": 99,
            "source_hint": "explicit",
        },
    }

    validated = validate_candidates(
        [candidate],
        turns=[_turn(7, "我喜欢喝茶")],
        related=[],
    )

    assert validated[0].metadata == {"source_hint": "explicit"}


def test_validator_rejects_paraphrased_or_foreign_evidence():
    candidate = {
        "draft_key": "bad-evidence",
        "proposed_op": "add",
        "memory_type": "preference",
        "canonical_key": "饮料偏好",
        "content": "用户爱喝茶",
        "confidence": 0.8,
        "sensitivity": 0,
        "evidence": [{"turn_id": 8, "quote": "用户很爱喝茶"}],
        "metadata": {},
    }

    with pytest.raises(CandidateValidationError, match="evidence turn"):
        validate_candidates([candidate], turns=[_turn(7, "我喜欢喝茶")], related=[])
    candidate["evidence"] = [{"turn_id": 7, "quote": "用户很爱喝茶"}]
    with pytest.raises(CandidateValidationError, match="quote"):
        validate_candidates([candidate], turns=[_turn(7, "我喜欢喝茶")], related=[])


def test_validator_rejects_candidate_inferred_only_from_agent_reply():
    candidate = {
        "draft_key": "agent-inference",
        "proposed_op": "add",
        "memory_type": "preference",
        "canonical_key": "饮料偏好",
        "content": "用户喜欢喝茶",
        "confidence": 0.99,
        "sensitivity": 0,
        "evidence": [{"turn_id": 8, "quote": "你喜欢喝茶"}],
        "metadata": {},
    }

    with pytest.raises(CandidateValidationError, match="direct user evidence"):
        validate_candidates(
            [candidate],
            turns=[_turn(8, "你喜欢喝茶", role="agent")],
            related=[],
        )


def test_validator_rejects_update_target_outside_related_or_wrong_revision():
    candidate = {
        "draft_key": "update-drink",
        "proposed_op": "update",
        "memory_type": "preference",
        "canonical_key": "饮料偏好",
        "content": "用户现在更喜欢咖啡",
        "confidence": 0.95,
        "sensitivity": 0,
        "target_id": 11,
        "target_revision": 1,
        "evidence": [{"turn_id": 7, "quote": "我现在更喜欢咖啡"}],
        "metadata": {},
    }
    turns = [_turn(7, "我现在更喜欢咖啡")]

    with pytest.raises(CandidateValidationError, match="related"):
        validate_candidates([candidate], turns=turns, related=[])
    with pytest.raises(CandidateValidationError, match="revision"):
        validate_candidates(
            [candidate],
            turns=turns,
            related=[{"id": 11, "revision": 2, "memory_type": "preference"}],
        )


def test_sedimenter_parses_strict_candidate_array_and_wraps_history():
    captured = {}

    class Client:
        def chat(self, messages, **kwargs):
            captured["messages"] = messages
            captured["kwargs"] = kwargs
            content = json.dumps(
                {
                    "candidates": [
                        {
                            "draft_key": "tea",
                            "proposed_op": "add",
                            "memory_type": "preference",
                            "canonical_key": "饮料偏好",
                            "content": "用户喜欢喝茶",
                            "confidence": 0.95,
                            "sensitivity": 0,
                            "evidence": [{"turn_id": 3, "quote": "我喜欢喝茶"}],
                            "metadata": {},
                        }
                    ]
                },
                ensure_ascii=False,
            )
            return {"choices": [{"message": {"content": content}}]}

    sedimenter = Sedimenter(Client(), timeout_s=4.0)
    candidates = sedimenter.consolidate([_turn(3, "我喜欢喝茶")], [])

    assert candidates[0].content == "用户喜欢喝茶"
    assert "<episode_turns>" in captured["messages"][1]["content"]
    assert captured["kwargs"]["timeout_s"] == 4.0
