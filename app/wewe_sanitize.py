"""HTML 清洗与图文拆分。

只依赖标准库 html.parser，不引入新的第三方依赖，避免供应链风险。
清洗目标：移除脚本/样式/可执行属性与危险协议，保留文章正文结构，
并把图片地址单独抽取出来，供图片下载与视觉描述链路使用。
"""

from __future__ import annotations

import re
from html import escape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

ALLOWED_TAGS = {
    "p", "br", "hr",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li",
    "strong", "b", "em", "i", "u", "s", "strike",
    "blockquote", "code", "pre",
    "table", "thead", "tbody", "tfoot", "tr", "td", "th",
    "figure", "figcaption",
    "div", "span", "section", "article",
    "a", "img",
}

# 标签连同其内容一起丢弃（避免脚本/样式文本进入正文）。
DROP_WITH_CONTENT = {"script", "style", "noscript", "iframe", "object", "embed", "template", "svg", "canvas", "form"}

VOID_TAGS = {"br", "hr", "img"}

ALLOWED_ATTRS = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title", "width", "height"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
    "blockquote": {"cite"},
}

SAFE_URL_SCHEMES = {"http", "https", "mailto"}
SAFE_DATA_IMAGE_RE = re.compile(r"^data:image/(png|jpe?g|gif|webp);base64,[A-Za-z0-9+/=\s]+$", re.IGNORECASE)

_BLOCK_TAGS = {
    "p", "div", "section", "article", "li", "tr", "blockquote", "pre",
    "h1", "h2", "h3", "h4", "h5", "h6", "figure", "figcaption", "table",
}
_DROP_START_RE = re.compile(
    r"(?is)<(" + "|".join(sorted(DROP_WITH_CONTENT)) + r")\b[^>]*>"
)


def _is_safe_url(value: str, allow_data_image: bool = False) -> bool:
    raw = (value or "").strip()
    if not raw:
        return False
    lowered = raw.lower()
    if lowered.startswith("data:"):
        return bool(allow_data_image and SAFE_DATA_IMAGE_RE.match(raw) and len(raw) <= 2_000_000)
    if lowered.startswith("//"):
        return True
    parsed = urlparse(raw)
    if not parsed.scheme:
        # 相对地址由调用方用 base_url 补全后再次校验
        return True
    return parsed.scheme.lower() in SAFE_URL_SCHEMES


class _Sanitizer(HTMLParser):
    def __init__(self, base_url: str = "") -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url or ""
        self.output: list[str] = []
        self.text_parts: list[str] = []
        self.images: list[dict] = []
        self.dropped_parts: list[str] = []
        self._skip_depth = 0
        self._skip_tag: str | None = None

    # -- helpers -------------------------------------------------
    def _resolve(self, url: str) -> str:
        raw = (url or "").strip()
        if not raw:
            return ""
        if raw.lower().startswith("data:"):
            return raw
        if self.base_url:
            try:
                return urljoin(self.base_url, raw)
            except Exception:
                return raw
        return raw

    def _clean_attrs(self, tag: str, attrs) -> str:
        allowed = ALLOWED_ATTRS.get(tag, set())
        parts: list[str] = []
        for name, value in attrs:
            lname = (name or "").lower()
            if lname not in allowed:
                continue
            if lname.startswith("on"):
                continue
            val = value or ""
            if lname in {"href", "src", "cite"}:
                if val.lower().startswith("data:"):
                    # 内联 data URI 图片不进入资产链路，避免正文体积膨胀
                    continue
                if not _is_safe_url(val, allow_data_image=(lname == "src" and tag == "img")):
                    continue
                val = self._resolve(val)
            parts.append(" " + lname + "=\"" + escape(val, quote=True) + "\"")
        return "".join(parts)

    def _register_image(self, url: str, alt: str) -> None:
        if not url:
            return
        if url.lower().startswith("data:"):
            return
        for item in self.images:
            if item["url"] == url:
                if alt and not item.get("alt"):
                    item["alt"] = alt
                return
        self.images.append({"url": url, "alt": alt or ""})

    def _flush_text(self, tag: str) -> None:
        if tag in _BLOCK_TAGS:
            self.text_parts.append("\n")

    # -- HTMLParser API ------------------------------------------
    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if self._skip_depth:
            if tag in DROP_WITH_CONTENT and tag == self._skip_tag:
                self._skip_depth += 1
            return
        if tag in DROP_WITH_CONTENT:
            self._skip_depth = 1
            self._skip_tag = tag
            return
        if tag not in ALLOWED_TAGS:
            return
        if tag == "img":
            alt = ""
            src = ""
            lazy_src = ""
            normalized_attrs = []
            for name, value in attrs:
                lname = (name or "").lower()
                if lname == "alt":
                    alt = value or ""
                if lname == "src":
                    src = value or ""
                    continue
                if lname in {"data-src", "data-original"}:
                    lazy_src = value or lazy_src
                    continue
                normalized_attrs.append((name, value))
            # 微信正文常用 data-src；占位 data URI 不能覆盖真实图片地址。
            if lazy_src and (not src or src.lower().startswith("data:")):
                src = lazy_src
            if src:
                normalized_attrs.append(("src", src))
            attr_str = self._clean_attrs(tag, normalized_attrs)
            if src and not src.lower().startswith("data:") and _is_safe_url(src):
                self._register_image(self._resolve(src), alt)
            if ' src="' in attr_str:
                self.output.append("<img" + attr_str + " />")
            return
        attr_str = self._clean_attrs(tag, attrs)
        self.output.append("<" + tag + attr_str + ">")

    def handle_startendtag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if self._skip_depth or tag in DROP_WITH_CONTENT or tag not in ALLOWED_TAGS:
            return
        if tag == "img":
            self.handle_starttag(tag, attrs)
            return
        attr_str = self._clean_attrs(tag, attrs)
        self.output.append("<" + tag + attr_str + " />")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._skip_depth:
            if tag == self._skip_tag:
                self._skip_depth -= 1
                if self._skip_depth == 0:
                    self._skip_tag = None
            return
        if tag not in ALLOWED_TAGS or tag in VOID_TAGS:
            return
        self.output.append("</" + tag + ">")
        self._flush_text(tag)

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            self.dropped_parts.append(data or "")
            return
        text = (data or "").replace("\u00a0", " ").strip()
        if not text:
            return
        self.output.append(escape(text, quote=False))
        self.text_parts.append(text)


def _repair_unclosed_drop_elements(raw: str) -> str:
    """Fail closed at an unclosed script/style-like element.

    A block-looking tag inside JavaScript is data, not an HTML recovery point.
    Discarding the malformed tail is safer than admitting code into RAG/feed text.
    """
    offset = 0
    for _ in range(raw.count("<") + 1):
        start = _DROP_START_RE.search(raw, offset)
        if start is None:
            break
        tag = start.group(1).lower()
        close = re.search(r"(?is)</\s*" + re.escape(tag) + r"\s*>", raw[start.end():])
        if close is None:
            return raw[:start.start()]
        offset = start.end() + close.end()
    return raw


def sanitize_html(html: str, base_url: str = "") -> dict:
    """清洗 HTML，返回清洗后 HTML、纯文本正文与图片列表。"""

    raw = _repair_unclosed_drop_elements(html or "")
    parser = _Sanitizer(base_url=base_url)
    try:
        parser.feed(raw)
        parser.close()
    except Exception:
        # 解析失败时退化为纯文本提取，避免整篇内容丢失
        parser.output = []
        parser.text_parts = []
        parser.images = []

    text = re.sub(r"\n{3,}", "\n\n", "".join(parser.text_parts))
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    clean_html = "".join(parser.output).strip()
    return {
        "html": clean_html,
        "text": text,
        "images": parser.images,
    }


def strip_tags(html: str) -> str:
    """快速提取纯文本（不做完整清洗，用于标题/摘要场景）。"""

    raw = _repair_unclosed_drop_elements(html or "")
    cleaned = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    cleaned = re.sub(r"(?s)<[^>]+>", " ", cleaned)
    cleaned = re.sub(r"&nbsp;?", " ", cleaned)
    cleaned = re.sub(r"&amp;", "&", cleaned)
    cleaned = re.sub(r"&lt;", "<", cleaned)
    cleaned = re.sub(r"&gt;", ">", cleaned)
    cleaned = re.sub(r"&quot;", '"', cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()
