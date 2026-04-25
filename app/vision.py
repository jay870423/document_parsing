from __future__ import annotations

import base64
import mimetypes
import time

import requests

from app.config import Settings


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


class DoubaoVisionClient:
    def __init__(self, settings: Settings):
        self.api_key = settings.ark_api_key
        self.model = settings.doubao_vision_model
        self.base_url = settings.ark_base_url.rstrip("/") + "/chat/completions"
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
                        {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_base64}"}},
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

        for i in range(self.max_retries):
            try:
                resp = requests.post(self.base_url, headers=headers, json=payload, timeout=self.timeout)
                _raise_for_status_with_details(resp)
                data = resp.json()
                time.sleep(1 / self.qps)
                return data["choices"][0]["message"]["content"]
            except Exception:
                if i == self.max_retries - 1:
                    return None
                time.sleep(i + 1)
        return None
