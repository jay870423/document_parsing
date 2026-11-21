"""SSRF 防护的 HTTP 抓取器。

用于网站来源采集与文章图片下载：
- 仅允许 http/https；
- 逐跳校验 DNS 解析结果，拒绝内网/回环/链路本地/保留地址；
- 手动跟随重定向并对每一跳重新校验；
- 限制响应体大小与超时；
- 图片额外校验 MIME 与文件魔数，拒绝 SVG 等可执行内容。
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

import requests

ALLOWED_SCHEMES = {"http", "https"}
MAX_REDIRECTS = 3

IMAGE_SIGNATURES = (
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
)


class FetchError(RuntimeError):
    """抓取失败（网络错误、SSRF 拦截、超限等）。"""


@dataclass
class FetchResult:
    url: str
    status_code: int
    content_type: str
    content: bytes

    @property
    def text(self) -> str:
        charset = "utf-8"
        if "charset=" in self.content_type.lower():
            charset = self.content_type.lower().split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
        try:
            return self.content.decode(charset, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")


def _is_blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def resolve_public_ips(host: str, port: int = 80) -> list[str]:
    """解析域名并返回全部公网 IP；只要有一个解析到内网就整体拒绝。"""

    if not host:
        raise FetchError("empty host")
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise FetchError("dns resolution failed: " + str(exc)) from exc

    ips: list[str] = []
    for info in infos:
        addr = info[4][0]
        if addr not in ips:
            ips.append(addr)
    if not ips:
        raise FetchError("dns resolution returned no address")

    for raw in ips:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            raise FetchError("invalid resolved address: " + raw)
        if _is_blocked_ip(ip):
            raise FetchError("resolved address is not public: " + raw)
    return ips


def validate_url(url: str) -> tuple[str, int]:
    """校验 URL 协议与目标主机，返回 (host, port)。"""

    parsed = urlparse((url or "").strip())
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise FetchError("unsupported scheme: " + str(parsed.scheme))
    host = parsed.hostname or ""
    if not host:
        raise FetchError("missing host")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if _is_blocked_ip(ip):
            raise FetchError("target address is not public")
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        return host, port
    resolve_public_ips(host, parsed.port or (443 if parsed.scheme.lower() == "https" else 80))
    return host, parsed.port or (443 if parsed.scheme.lower() == "https" else 80)


class SafeFetcher:
    """带 SSRF 防护的最小 HTTP 客户端。"""

    def __init__(self, timeout: int = 20, max_bytes: int = 5_000_000, session: requests.Session | None = None):
        self.timeout = max(1, int(timeout))
        self.max_bytes = max(1024, int(max_bytes))
        self.session = session or requests.Session()

    def get(
        self,
        url: str,
        headers: dict | None = None,
        max_bytes: int | None = None,
        timeout: int | None = None,
    ) -> FetchResult:
        limit = max_bytes or self.max_bytes
        current = (url or "").strip()
        for _ in range(MAX_REDIRECTS + 1):
            validate_url(current)
            try:
                resp = self.session.get(
                    current,
                    headers=headers or {},
                    timeout=timeout or self.timeout,
                    allow_redirects=False,
                    stream=True,
                )
            except FetchError:
                raise
            except Exception as exc:
                raise FetchError("request failed: " + str(exc)) from exc

            if resp.is_redirect or resp.is_permanent_redirect:
                location = resp.headers.get("Location") or ""
                resp.close()
                if not location:
                    raise FetchError("redirect without location")
                from urllib.parse import urljoin

                current = urljoin(current, location)
                continue

            content_type = (resp.headers.get("Content-Type") or "").lower()
            declared = resp.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > limit:
                resp.close()
                raise FetchError("response too large")

            buffer = bytearray()
            try:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    buffer.extend(chunk)
                    if len(buffer) > limit:
                        raise FetchError("response too large")
            except FetchError:
                resp.close()
                raise
            except Exception as exc:
                resp.close()
                raise FetchError("read failed: " + str(exc)) from exc

            status_code = int(resp.status_code)
            resp.close()
            if status_code >= 400:
                raise FetchError("upstream returned status %d" % status_code)
            return FetchResult(
                url=current,
                status_code=status_code,
                content_type=content_type,
                content=bytes(buffer),
            )

        raise FetchError("too many redirects")


def sniff_image(data: bytes) -> tuple[str, str] | None:
    """按魔数识别图片类型，返回 (mime, suffix)；非图片返回 None。"""

    for signature, mime, suffix in IMAGE_SIGNATURES:
        if data.startswith(signature):
            return mime, suffix
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return None


def download_image(fetcher: SafeFetcher, url: str) -> tuple[bytes, str, str]:
    """下载并校验图片，返回 (数据, mime, 后缀)。"""

    result = fetcher.get(url, headers={"Accept": "image/*"})
    sniffed = sniff_image(result.content)
    if not sniffed:
        raise FetchError("not a supported image payload")
    mime, suffix = sniffed
    return result.content, mime, suffix
