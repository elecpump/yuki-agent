import pytest

from yuki.cognition.context.snapshot import ContextSnapshot
from yuki.cognition.l2.client import CloudError
from yuki.cognition.l2.proactive import ProactiveAgent


class FakeClient:
    def __init__(self, content=None, error=None):
        self.content = content
        self.error = error
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.error:
            raise self.error
        return {"choices": [{"message": {"content": self.content}}]}


@pytest.mark.parametrize(
    ("raw", "action"),
    [
        ('{"action":"silent","text":"","reason":"quiet"}', "silent"),
        ('```json\n{"action":"speak","text":"你好","reason":"opening"}\n```', "speak"),
    ],
)
def test_parses_json_and_code_fences(raw, action):
    decision = ProactiveAgent(FakeClient(raw)).decide(ContextSnapshot(), {})
    assert decision.action == action


@pytest.mark.parametrize(
    "raw",
    ["not-json", '{"action":"speak","text":"","reason":"x"}', '{"text":"hi"}'],
)
def test_invalid_output_is_parse_error(raw):
    decision = ProactiveAgent(FakeClient(raw)).decide(ContextSnapshot(), {})
    assert decision.action == "silent"
    assert decision.reason == "parse_error"


def test_cloud_failure_and_prompt_contract():
    failing = ProactiveAgent(FakeClient(error=CloudError("offline")))
    assert failing.decide(ContextSnapshot(), {}).reason == "cloud_error"

    client = FakeClient('{"action":"silent","text":"","reason":"quiet"}')
    ProactiveAgent(client).decide(ContextSnapshot(situation={"topic": "文章"}), {})
    messages, kwargs = client.calls[0]
    assert any('"action":"silent"' in message["content"] for message in messages)
    system = messages[0]["content"]
    assert "【主动开口准则】" in system
    assert "silent 是完全正常且受鼓励的输出" in system
    assert "不确定时倾向 silent" in system
    assert "不评价用户" in system
    assert kwargs == {"timeout_s": 5.0, "temperature": 0.5, "max_tokens": 100}


def test_custom_system_prompt_and_optional_soul():
    client = FakeClient('{"action":"silent","text":"","reason":"quiet"}')
    ProactiveAgent(client, system_prompt="自定义决策角色").decide(ContextSnapshot())
    assert "自定义决策角色" in client.calls[0][0][0]["content"]


def test_speak_text_is_truncated():
    raw = '{"action":"speak","text":"123456","reason":"x"}'
    decision = ProactiveAgent(FakeClient(raw), max_chars=4).decide(ContextSnapshot(), {})
    assert decision.text == "1234"
