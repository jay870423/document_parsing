from __future__ import annotations

import base64
import mimetypes
import time

import requests

from app.config import Settings


_ANTHROPIC_VERSION = "2023-06-01"
_EMPTY_TEXT_ERROR = "MiniMax response contained no text content."


def _messages_url(base_url: str) -> str:
    """Accept either an Anthropic base URL or a full Messages endpoint."""
    root = base_url.rstrip("/")
    if root.endswith("/messages"):
        return root
    if root.endswith("/v1"):
        return root + "/messages"
    return root + "/v1/messages"


def _headers(api_key: str) -> dict[str, str]:
    return {
        "x-api-key": api_key,
        "anthropic-version": _ANTHROPIC_VERSION,
        "Content-Type": "application/json",
    }


def _raise_for_status_with_details(resp: requests.Response) -> None:
    if resp.ok:
        return
    detail = ""
    try:
        data = resp.json()
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                detail = str(err.get("message") or err.get("code") or "")
            else:
                detail = str(data)
    except Exception:
        detail = (resp.text or "").strip()
    if detail:
        raise RuntimeError(f"HTTP {resp.status_code} calling {resp.url}: {detail}")
    resp.raise_for_status()


def _extract_text(data: dict) -> str:
    """Concatenate text blocks only; thinking and other blocks are not answers."""
    content = data.get("content") if isinstance(data, dict) else None
    if not isinstance(content, list):
        raise RuntimeError(_EMPTY_TEXT_ERROR)
    text = "".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    )
    if not text.strip():
        raise RuntimeError(_EMPTY_TEXT_ERROR)
    return text


class MiniMaxVisionClient:
    """Drop-in Anthropic-protocol replacement for DoubaoVisionClient."""

    def __init__(self, settings: Settings):
        self.api_key = settings.anthropic_api_key
        self.model = settings.minimax_model
        self.base_url = _messages_url(settings.anthropic_base_url)
        self.max_retries = settings.vision_max_retries
        self.timeout = settings.vision_timeout_seconds
        self.temperature = settings.vision_temperature
        self.max_tokens = settings.vision_max_tokens
        self.qps = max(settings.vision_qps, 0.1)

    @staticmethod
    def _encode(image_path: str) -> tuple[str, str]:
        with open(image_path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode("utf-8")
        mime_type, _ = mimetypes.guess_type(image_path)
        return image_b64, (mime_type or "image/png")

    def analyze_image(self, image_path: str, prompt: str) -> str | None:
        if not self.api_key:
            return None
        image_base64, mime_type = self._encode(image_path)
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime_type,
                                "data": image_base64,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

        for i in range(self.max_retries):
            try:
                resp = requests.post(
                    self.base_url,
                    headers=_headers(self.api_key),
                    json=payload,
                    timeout=self.timeout,
                )
                _raise_for_status_with_details(resp)
                data = resp.json()
                time.sleep(1 / self.qps)
                return _extract_text(data)
            except Exception:
                if i == self.max_retries - 1:
                    return None
                time.sleep(i + 1)
        return None


class MiniMaxChatClient:
    """Drop-in Anthropic-protocol replacement for DoubaoChatClient."""

    def __init__(self, settings: Settings):
        self.api_key = settings.anthropic_api_key
        self.model = settings.minimax_model
        self.base_url = _messages_url(settings.anthropic_base_url)
        self.max_tokens = settings.rag_answer_max_tokens

    def answer_with_context(self, question: str, hits: list[dict]) -> str:
        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY is required for answer generation.")

        context_parts: list[str] = []
        for idx, hit in enumerate(hits, start=1):
            context_parts.append(
                "\n".join(
                    [
                        f"[{idx}] source_filename={hit.get('source_filename', '')}",
                        f"[{idx}] source_file={hit.get('source_file', '')}",
                        f"[{idx}] chunk_index={hit.get('chunk_index', -1)}",
                        f"[{idx}] score={hit.get('score', 0.0):.4f}",
                        f"[{idx}] text={hit.get('text', '')}",
                    ]
                )
            )
        context_text = "\n\n".join(context_parts)

        system_prompt = (
            "你是芯片手册解析助手。请基于给定检索片段回答问题。"
            "如果信息不足，请明确说“资料不足”。"
            "回答中涉及事实结论时，必须在句末标注来源编号，如 [1]、[2]。"
            "不要编造不存在的寄存器字段或数值。"
        )
        user_prompt = (
            f"问题：{question}\n\n"
            "请根据下列检索片段回答：\n"
            f"{context_text}\n\n"
            "输出要求：\n"
            "1) 先给“结论”段（简明）\n"
            "2) 再给“依据”段（按要点）\n"
            "3) 仅使用提供片段中的信息"
        )
        payload = {
            "model": self.model,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
            "temperature": 0.2,
            "max_tokens": self.max_tokens,
        }
        resp = requests.post(
            self.base_url,
            headers=_headers(self.api_key),
            json=payload,
            timeout=90,
        )
        _raise_for_status_with_details(resp)
        return _extract_text(resp.json())
