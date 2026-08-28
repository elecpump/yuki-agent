import json
import urllib.error
import urllib.request
from typing import Callable


class CloudError(Exception):
    """云端调用失败（网络/超时/HTTP/解析/空响应）。"""


def _default_post(url: str, headers: dict, payload: dict, timeout: float) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise CloudError(f"HTTP {exc.code}") from exc
    except json.JSONDecodeError as exc:
        raise CloudError(f"invalid JSON response: {exc}") from exc


class CloudClient:
    """OpenAI 兼容 chat/completions 客户端。post 可注入以便测试。"""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout_s: float = 10.0,
        post: Callable[[str, dict, dict, float], dict] | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._timeout = timeout_s
        self._post = post or _default_post

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        timeout_s: float | None = None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        payload: dict = {"model": self._model, "messages": messages}
        if tools:
            payload["tools"] = tools
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        timeout = self._timeout if timeout_s is None else timeout_s
        try:
            raw = self._post(f"{self._base}/chat/completions", headers, payload, timeout)
        except CloudError:
            raise
        except Exception as exc:
            raise CloudError(f"network error: {exc}") from exc
        if not isinstance(raw, dict) or not raw.get("choices"):
            raise CloudError("invalid response: no choices")
        return raw
