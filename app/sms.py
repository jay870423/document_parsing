from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any

import requests

from app.config import Settings


@dataclass
class SmsSendResult:
    ok: bool
    provider_message_id: str | None = None
    error: str | None = None
    provider_error: str | None = None
    raw: dict[str, Any] | None = None


def friendly_sms_error(message: str | None) -> str:
    text = (message or "").strip()
    lowered = text.lower()
    if not text:
        return "验证码发送失败，请稍后重试"
    if "single mobile number" in lowered and "upper limit" in lowered:
        return "该手机号今日获取验证码次数已达上限，请明天再试或更换手机号"
    if "frequency" in lowered or "too many" in lowered or "upper limit" in lowered:
        return "验证码请求过于频繁，请稍后再试"
    if "signature" in lowered and ("approved" in lowered or "format" in lowered):
        return "短信服务签名暂未通过审核，请联系客服处理"
    if "template" in lowered and "match" in lowered:
        return "验证码发送失败，请稍后重试"
    if "phone" in lowered and ("format" in lowered or "invalid" in lowered):
        return "手机号格式不正确，请检查后重新输入"
    if text.isascii():
        return "验证码发送失败，请稍后重试"
    return text


class TencentSmsClient:
    endpoint = "https://sms.tencentcloudapi.com"
    host = "sms.tencentcloudapi.com"
    service = "sms"
    version = "2021-01-11"
    region = "ap-guangzhou"
    algorithm = "TC3-HMAC-SHA256"

    def __init__(self, settings: Settings):
        self.secret_id = settings.app_sms_secret_id
        self.secret_key = settings.app_sms_secret_key
        self.sdk_app_id = settings.app_sms_sdk_app_id
        self.sign_name = settings.app_sms_sign_name.strip().strip("【】[]")
        self.template_id = settings.app_sms_template_id

    @staticmethod
    def _sign(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    def _authorization(self, payload: str, timestamp: int) -> str:
        date = dt.datetime.utcfromtimestamp(timestamp).strftime("%Y-%m-%d")
        http_request_method = "POST"
        canonical_uri = "/"
        canonical_querystring = ""
        canonical_headers = f"content-type:application/json; charset=utf-8\nhost:{self.host}\n"
        signed_headers = "content-type;host"
        hashed_request_payload = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        canonical_request = "\n".join(
            [
                http_request_method,
                canonical_uri,
                canonical_querystring,
                canonical_headers,
                signed_headers,
                hashed_request_payload,
            ]
        )

        credential_scope = f"{date}/{self.service}/tc3_request"
        hashed_canonical_request = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
        string_to_sign = "\n".join(
            [
                self.algorithm,
                str(timestamp),
                credential_scope,
                hashed_canonical_request,
            ]
        )

        secret_date = self._sign(("TC3" + self.secret_key).encode("utf-8"), date)
        secret_service = self._sign(secret_date, self.service)
        secret_signing = self._sign(secret_service, "tc3_request")
        signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

        return (
            f"{self.algorithm} "
            f"Credential={self.secret_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, "
            f"Signature={signature}"
        )

    def _send_with_params(self, phone: str, params: list[str]) -> SmsSendResult:
        body = {
            "SmsSdkAppId": self.sdk_app_id,
            "SignName": self.sign_name,
            "TemplateId": self.template_id,
            "TemplateParamSet": params,
            "PhoneNumberSet": [f"+86{phone}"],
        }
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        timestamp = int(time.time())
        headers = {
            "Authorization": self._authorization(payload, timestamp),
            "Content-Type": "application/json; charset=utf-8",
            "Host": self.host,
            "X-TC-Action": "SendSms",
            "X-TC-Timestamp": str(timestamp),
            "X-TC-Version": self.version,
            "X-TC-Region": self.region,
        }
        try:
            resp = requests.post(self.endpoint, headers=headers, data=payload.encode("utf-8"), timeout=12)
            data = resp.json()
        except Exception as exc:
            return SmsSendResult(ok=False, error=f"短信请求失败：{exc}")

        if not resp.ok:
            return SmsSendResult(ok=False, error=f"短信接口 HTTP {resp.status_code}", raw=data)

        send_set = data.get("Response", {}).get("SendStatusSet", [])
        first = send_set[0] if send_set else {}
        code_value = str(first.get("Code", ""))
        provider_message = str(first.get("Message") or data)
        ok = code_value.lower() == "ok"
        return SmsSendResult(
            ok=ok,
            provider_message_id=str(first.get("SerialNo") or data.get("Response", {}).get("RequestId") or ""),
            error=None if ok else friendly_sms_error(provider_message),
            provider_error=None if ok else provider_message,
            raw=data,
        )

    def send_code(self, phone: str, code: str, expire_minutes: int = 5) -> SmsSendResult:
        missing = [
            name
            for name, value in {
                "APP_SMS_SECRET_ID": self.secret_id,
                "APP_SMS_SECRET_KEY": self.secret_key,
                "APP_SMS_SDK_APP_ID": self.sdk_app_id,
                "APP_SMS_SIGN_NAME": self.sign_name,
                "APP_SMS_TEMPLATE_ID": self.template_id,
            }.items()
            if not value
        ]
        if missing:
            return SmsSendResult(ok=False, error=f"短信配置缺失：{', '.join(missing)}")

        result = self._send_with_params(phone, [code, str(expire_minutes)])
        if result.ok:
            return result

        error = (result.provider_error or result.error or "").lower()
        if "template" in error and "match" in error:
            retry = self._send_with_params(phone, [code])
            if retry.ok:
                return retry
            if retry.raw is not None:
                retry.raw["_first_attempt"] = result.raw
            return retry
        return result
