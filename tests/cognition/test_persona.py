from yuki.cognition.brain.persona import format_preferences, format_soul_params, generate


def test_format_preferences_empty():
    assert format_preferences([]) == ""


def test_format_preferences_templated():
    out = format_preferences([{"content": "用户喜欢安静"}, {"content": "回复要简短"}])
    assert "用户偏好：" in out
    assert "- 用户喜欢安静" in out
    assert "- 回复要简短" in out


def test_format_soul_params_empty():
    assert format_soul_params({}) == ""


def test_format_soul_params_templated():
    out = format_soul_params({"humor": "high"})
    assert "humor: high" in out


def test_generate_assembles_sections():
    out = generate("yuki", [{"content": "喜欢猫"}], {"cooldown": 120},
                   base_prompt="你是{persona},温柔。")
    assert out.startswith("你是yuki,温柔。")
    assert "用户偏好：" in out
    assert "参数说明：" in out


def test_generate_omits_empty_sections():
    out = generate("yuki", [], {}, base_prompt="你是{persona}。")
    assert "用户偏好" not in out
    assert "参数说明" not in out


def test_generate_does_not_duplicate_baked_soul_sections():
    description = (
        "你是yuki,一个温柔的中文语音陪伴 agent。\n\n"
        "人格内核：\n"
        "- [binding] 安全优先。\n\n"
        "性格参数：保持均衡、自然、贴近当下语境。"
    )
    soul = {
        "personality_description": description,
        "core_values": [{"role": "binding", "text": "安全优先。"}],
        "personality_traits": {"warmth": 0.5},
    }
    out = generate("yuki", [], {}, soul=soul)
    assert out.count("人格内核") == 1
    assert out.count("性格参数") == 1


def test_generate_refine_success():
    out = generate("yuki", [], {}, base_prompt="base", refine=lambda text: "精修后的文本")
    assert out == "精修后的文本"


def test_generate_refine_failure_falls_back():
    def boom(text):
        raise RuntimeError("down")

    out = generate("yuki", [], {}, base_prompt="base", refine=boom)
    assert out == "base"
