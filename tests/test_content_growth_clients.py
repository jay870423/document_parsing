from __future__ import annotations

from app.config import Settings
from app.minimax_client import MiniMaxChatClient
from app.rag import DoubaoChatClient


class Response:
    status_code = 200
    ok = True
    text = ""
    url = "https://offline.test"

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload

    def raise_for_status(self):
        return None


def test_doubao_generate_text_supports_custom_prompt_and_token_limit(monkeypatch):
    calls = []
    monkeypatch.setattr("app.rag.requests.post", lambda url, **kwargs: (
        calls.append((url, kwargs)) or Response({"choices": [{"message": {"content": "自定义结果"}}]})
    ))
    settings = Settings.model_construct(
        ark_api_key="offline-key", doubao_chat_model="offline-model",
        ark_base_url="https://offline.test/v1", rag_answer_max_tokens=1000,
    )
    client = DoubaoChatClient(settings)
    result = client.generate_text("自定义系统", "自定义用户", temperature=0.2, max_tokens=2345)
    assert result == "自定义结果"
    payload = calls[0][1]["json"]
    assert payload["messages"] == [
        {"role": "system", "content": "自定义系统"},
        {"role": "user", "content": "自定义用户"},
    ]
    assert payload["max_tokens"] == 2345


def test_minimax_generate_text_supports_custom_prompt_and_token_limit(monkeypatch):
    calls = []
    monkeypatch.setattr("app.minimax_client.requests.post", lambda url, **kwargs: (
        calls.append((url, kwargs)) or Response({"content": [{"type": "text", "text": "自定义结果"}]})
    ))
    settings = Settings.model_construct(
        anthropic_api_key="offline-key", anthropic_base_url="https://offline.test/anthropic",
        minimax_model="offline-model", rag_answer_max_tokens=1000,
    )
    client = MiniMaxChatClient(settings)
    result = client.generate_text("自定义系统", "自定义用户", temperature=0.2, max_tokens=2345)
    assert result == "自定义结果"
    payload = calls[0][1]["json"]
    assert payload["system"] == "自定义系统"
    assert payload["messages"] == [{"role": "user", "content": "自定义用户"}]
    assert payload["max_tokens"] == 2345
