from __future__ import annotations

import base64

import pytest
import requests

from app.config import Settings
from app.minimax_client import MiniMaxChatClient, MiniMaxVisionClient


FAKE_API_KEY = "offline-test-key"
MESSAGES_URL = "https://api.minimaxi.com/anthropic/v1/messages"


class MiniMaxSettings(Settings):
    """Test-only stand-in until the main integration adds these Settings fields."""

    anthropic_api_key: str = ""
    anthropic_base_url: str = "https://api.minimaxi.com/anthropic"
    minimax_model: str = "MiniMax-M3"


def make_settings(**updates) -> Settings:
    # model_construct keeps this test independent of the concurrently added
    # Settings fields and, importantly, never loads a developer's real .env.
    values = {
        "anthropic_api_key": FAKE_API_KEY,
        "anthropic_base_url": "https://api.minimaxi.com/anthropic",
        "minimax_model": "MiniMax-M3",
        "vision_max_retries": 3,
        "vision_timeout_seconds": 17,
        "vision_temperature": 0.1,
        "vision_max_tokens": 2048,
        "vision_qps": 2.0,
        "rag_answer_max_tokens": 1200,
    }
    values.update(updates)
    return MiniMaxSettings.model_construct(**values)


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 400
        self.url = MESSAGES_URL
        self.text = ""

    def json(self) -> dict:
        return self.payload

    def raise_for_status(self) -> None:
        if not self.ok:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def assert_anthropic_headers(headers: dict) -> None:
    assert headers == {
        "x-api-key": FAKE_API_KEY,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    assert "Authorization" not in headers


def test_chat_request_and_text_blocks_are_doubao_compatible(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse(
            {
                "content": [
                    {"type": "thinking", "thinking": "不得出现在答案中"},
                    {"type": "text", "text": "结论：ENABLE=1 [1]\n"},
                    {"type": "thinking", "text": "也不能拼接这个字段"},
                    {"type": "text", "text": "依据：寄存器表 [1]"},
                ],
                "usage": {"input_tokens": 31, "output_tokens": 9},
            }
        )

    monkeypatch.setattr("app.minimax_client.requests.post", fake_post)
    client = MiniMaxChatClient(make_settings())
    hits = [
        {
            "source_filename": "芯片.pdf",
            "source_file": "/docs/芯片.pdf",
            "chunk_index": 4,
            "score": 0.875,
            "text": "ENABLE 位写入 1 时启用。",
        }
    ]

    answer = client.answer_with_context("如何启用？", hits)

    assert isinstance(answer, str)
    assert answer == "结论：ENABLE=1 [1]\n依据：寄存器表 [1]"
    assert "thinking" not in answer
    assert "不得出现" not in answer
    assert len(calls) == 1
    url, request = calls[0]
    assert url == MESSAGES_URL
    assert request["timeout"] == 90
    assert_anthropic_headers(request["headers"])
    payload = request["json"]
    assert payload["model"] == "MiniMax-M3"
    assert payload["max_tokens"] == 1200
    assert payload["temperature"] == 0.2
    assert payload["system"].startswith("你是芯片手册解析助手")
    assert payload["messages"][0]["role"] == "user"
    assert "问题：如何启用？" in payload["messages"][0]["content"]
    assert "[1] score=0.8750" in payload["messages"][0]["content"]
    assert all(message["role"] != "system" for message in payload["messages"])


def test_vision_request_base64_image_text_filter_usage_and_qps(monkeypatch, tmp_path):
    image_path = tmp_path / "register.png"
    image_bytes = b"\x89PNG\r\noffline-image"
    image_path.write_bytes(image_bytes)
    calls = []
    sleeps = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse(
            {
                "content": [
                    {"type": "thinking", "thinking": "内部推理"},
                    {"type": "text", "text": "| 字段 | 值 |\n"},
                    {"type": "text", "text": "| ENABLE | 1 |"},
                ],
                "usage": {"input_tokens": 100, "output_tokens": 12},
            }
        )

    monkeypatch.setattr("app.minimax_client.requests.post", fake_post)
    monkeypatch.setattr("app.minimax_client.time.sleep", sleeps.append)
    client = MiniMaxVisionClient(make_settings())

    result = client.analyze_image(str(image_path), "提取表格")

    assert result == "| 字段 | 值 |\n| ENABLE | 1 |"
    assert sleeps == [0.5]
    assert len(calls) == 1
    url, request = calls[0]
    assert url == MESSAGES_URL
    assert request["timeout"] == 17
    assert_anthropic_headers(request["headers"])
    payload = request["json"]
    assert payload["model"] == "MiniMax-M3"
    assert payload["max_tokens"] == 2048
    assert payload["temperature"] == 0.1
    content = payload["messages"][0]["content"]
    assert content[0] == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(image_bytes).decode("utf-8"),
        },
    }
    assert content[1] == {"type": "text", "text": "提取表格"}


@pytest.mark.parametrize(
    "content",
    [
        [],
        [{"type": "thinking", "thinking": "只有推理，没有正文"}],
        [{"type": "text", "text": " \n\t"}],
    ],
)
def test_chat_rejects_empty_text_body(monkeypatch, content):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse({"content": content, "usage": {"input_tokens": 3, "output_tokens": 1}})

    monkeypatch.setattr("app.minimax_client.requests.post", fake_post)
    client = MiniMaxChatClient(make_settings())

    with pytest.raises(RuntimeError, match="no text content"):
        client.answer_with_context("问题", [])
    assert len(calls) == 1


def test_vision_retries_http_error_then_succeeds_without_returning_usage(monkeypatch, tmp_path):
    image_path = tmp_path / "register.jpg"
    image_path.write_bytes(b"jpeg-offline")
    calls = []
    sleeps = []
    responses = iter(
        [
            FakeResponse({"error": {"message": "temporary offline error"}}, status_code=500),
            FakeResponse(
                {
                    "content": [{"type": "text", "text": "重试后的正文"}],
                    "usage": {"input_tokens": 8, "output_tokens": 4},
                }
            ),
        ]
    )

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return next(responses)

    monkeypatch.setattr("app.minimax_client.requests.post", fake_post)
    monkeypatch.setattr("app.minimax_client.time.sleep", sleeps.append)
    client = MiniMaxVisionClient(make_settings(vision_qps=4.0))

    result = client.analyze_image(str(image_path), "识别")

    assert result == "重试后的正文"
    assert isinstance(result, str)
    assert len(calls) == 2
    assert sleeps == [1, 0.25]


def test_vision_returns_none_after_retry_exhaustion(monkeypatch, tmp_path):
    image_path = tmp_path / "register.png"
    image_path.write_bytes(b"offline")
    calls = []
    sleeps = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        raise requests.Timeout("offline timeout")

    monkeypatch.setattr("app.minimax_client.requests.post", fake_post)
    monkeypatch.setattr("app.minimax_client.time.sleep", sleeps.append)
    client = MiniMaxVisionClient(make_settings(vision_max_retries=2))

    assert client.analyze_image(str(image_path), "识别") is None
    assert len(calls) == 2
    assert sleeps == [1]


def test_missing_key_preserves_existing_client_behavior(monkeypatch, tmp_path):
    def unexpected_post(*args, **kwargs):
        raise AssertionError("requests.post must not be called without an API key")

    monkeypatch.setattr("app.minimax_client.requests.post", unexpected_post)
    settings = make_settings(anthropic_api_key="")
    image_path = tmp_path / "register.png"
    image_path.write_bytes(b"offline")

    assert MiniMaxVisionClient(settings).analyze_image(str(image_path), "识别") is None
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        MiniMaxChatClient(settings).answer_with_context("问题", [])


def test_document_pipeline_routes_text_and_vision_to_minimax(monkeypatch):
    from app.pipeline import DocumentPipeline

    monkeypatch.setattr("app.pipeline.DoubaoEmbeddingClient", lambda settings: object())
    monkeypatch.setattr("app.pipeline.QdrantIndexer", lambda settings: object())
    pipeline = DocumentPipeline(make_settings(ai_provider="minimax"))

    assert isinstance(pipeline.vision, MiniMaxVisionClient)
    assert isinstance(pipeline.chat, MiniMaxChatClient)
    assert pipeline.vision.model == "MiniMax-M3"
    assert pipeline.chat.model == "MiniMax-M3"
