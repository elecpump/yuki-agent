import pytest

from yuki.cognition.l2.client import CloudClient, CloudError


def test_chat_posts_correct_request():
    captured = {}

    def fake_post(url, headers, payload, timeout):
        captured.update(url=url, headers=headers, payload=payload, timeout=timeout)
        return {"choices": [{"message": {"content": "hi"}}]}

    client = CloudClient("https://api.example.com/v1", "m1", api_key="k", timeout_s=5.0, post=fake_post)
    result = client.chat([{"role": "user", "content": "x"}], tools=[{"type": "function"}])
    assert result["choices"][0]["message"]["content"] == "hi"
    assert captured["url"] == "https://api.example.com/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer k"
    assert captured["payload"]["model"] == "m1"
    assert captured["payload"]["tools"] == [{"type": "function"}]
    assert captured["timeout"] == 5.0


def test_chat_without_api_key_omits_auth():
    captured = {}

    def fake_post(url, headers, payload, timeout):
        captured["headers"] = headers
        return {"choices": [{"message": {"content": "x"}}]}

    client = CloudClient("https://api.example.com/v1", "m1", post=fake_post)
    client.chat([{"role": "user", "content": "x"}])
    assert "Authorization" not in captured["headers"]


def test_chat_propagates_cloud_error():
    def fake_post(url, headers, payload, timeout):
        raise CloudError("HTTP 429")

    client = CloudClient("https://api.example.com/v1", "m1", post=fake_post)
    with pytest.raises(CloudError, match="429"):
        client.chat([])


def test_chat_maps_network_error_to_cloud_error():
    def fake_post(url, headers, payload, timeout):
        raise TimeoutError("timed out")

    client = CloudClient("https://api.example.com/v1", "m1", post=fake_post)
    with pytest.raises(CloudError):
        client.chat([])


def test_chat_rejects_missing_choices():
    def fake_post(url, headers, payload, timeout):
        return {}

    client = CloudClient("https://api.example.com/v1", "m1", post=fake_post)
    with pytest.raises(CloudError, match="choices"):
        client.chat([])
