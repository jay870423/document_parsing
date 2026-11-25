"""Pure helpers for evidence-bound WeChat content analysis and drafting."""
from __future__ import annotations

import html
import json
import re
from typing import Any
from urllib.parse import urlparse

MAX_EXCERPT_CHARS = 6000


def parse_ai_object(text: str) -> dict[str, Any]:
    """Parse one complete JSON object, without accepting a nested salvage as the root."""
    cleaned = str(text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    try:
        value = json.loads(cleaned)
        if isinstance(value, dict):
            return value
        raise ValueError("AI 输出必须是 JSON 对象")
    except json.JSONDecodeError:
        pass

    candidates: list[dict[str, Any]] = []
    index = 0
    while index < len(cleaned):
        start = cleaned.find("{", index)
        if start < 0:
            break
        depth = 0
        in_string = False
        escaped = False
        parsed = False
        for cursor in range(start, len(cleaned)):
            char = cleaned[cursor]
            if escaped:
                escaped = False
            elif in_string:
                if char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(cleaned[start:cursor + 1])
                        if isinstance(value, dict):
                            candidates.append(value)
                    except (json.JSONDecodeError, TypeError):
                        pass
                    index = cursor + 1
                    parsed = True
                    break
        if not parsed:
            index = start + 1
    if not candidates:
        raise ValueError("AI 输出不是完整 JSON 对象")
    # If the response itself begins with an object, never promote a complete
    # nested fragment from an otherwise truncated root object.
    if cleaned.startswith("{"):
        try:
            json.JSONDecoder().raw_decode(cleaned)
        except json.JSONDecodeError as exc:
            raise ValueError("AI 输出的根对象不完整") from exc
    return max(candidates, key=lambda item: len(item))


def _clean_text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("AI 文本字段必须是字符串")
    cleaned = re.sub(r"\s+", " ", value).strip()
    if len(cleaned) > limit:
        for match in re.finditer(r"\[(?:R|O)\d+\]", cleaned):
            if match.start() < limit < match.end():
                raise ValueError("AI 来源编号会被长度限制截断")
    return cleaned[:limit]


def _draft_structured_list(
    value: Any, *, kind: str, limit: int, item_limit: int,
) -> list[str]:
    """Accept MiniMax's object-form outline/facts without dropping their details."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("AI 列表字段必须是字符串或规定结构的对象数组")
    cleaned = []
    # Validate the complete provider response before applying the display cap.
    for item in value:
        if isinstance(item, str):
            text = item
        elif isinstance(item, dict) and kind == "outline":
            heading = item.get("heading") or item.get("title") or item.get("content") or ""
            points = item.get("key_points") if item.get("key_points") is not None else item.get("points", [])
            if (
                not isinstance(heading, str) or not isinstance(points, list)
                or any(not isinstance(point, str) for point in points)
            ):
                raise ValueError("AI 大纲标题和要点必须是字符串或字符串数组")
            heading = heading.strip()
            point_text = "；".join(point.strip() for point in points if point.strip())
            if not heading and not point_text:
                raise ValueError("AI 大纲对象缺少可展示内容")
            if heading and point_text:
                text = f"{heading}：{point_text}"
            else:
                text = heading or point_text
            # The model's `step` is deliberately ignored; array order is authoritative.
        elif isinstance(item, dict) and kind == "unresolved":
            fact = (
                item.get("item") or item.get("fact") or item.get("claim")
                or item.get("text") or ""
            )
            status = item.get("status") or item.get("reason") or item.get("note") or ""
            if not isinstance(fact, str) or not isinstance(status, str):
                raise ValueError("AI 待核实事项和说明必须是字符串")
            fact, status = fact.strip(), status.strip()
            if not fact:
                raise ValueError("AI 待核实对象缺少事项")
            text = f"{fact}：{status}" if status else fact
        else:
            raise ValueError("AI 列表字段包含不支持的结构")
        text = _clean_text(text, item_limit)
        if text:
            cleaned.append(text)
    return cleaned[:limit]


def _string_list(value: Any, *, limit: int = 20, item_limit: int = 500) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("AI 列表字段必须是字符串数组")
    cleaned = []
    for item in value[:limit]:
        if not isinstance(item, str):
            raise ValueError("AI 列表字段必须是字符串数组")
        text = _clean_text(item, item_limit)
        if text:
            cleaned.append(text)
    return cleaned


def _safe_url(value: Any) -> str:
    url = str(value or "").strip()[:2000]
    return url if urlparse(url).scheme in {"http", "https"} and urlparse(url).netloc else ""


def build_source_package(references: list[dict], own_articles: list[dict]) -> list[dict[str, Any]]:
    package: list[dict[str, Any]] = []
    for prefix, rows, own in (("R", references, False), ("O", own_articles, True)):
        for index, row in enumerate(rows, start=1):
            item = {
                "ref_id": f"{prefix}{index}",
                "kind": "own" if own else "reference",
                "title": _clean_text(row.get("title"), 300),
                "url": _safe_url(row.get("url")),
                "author": _clean_text(row.get("author"), 200),
                "published_at": _clean_text(row.get("published_at"), 40),
                "excerpt": str(row.get("body") if own else row.get("text_content") or "")[:MAX_EXCERPT_CHARS],
            }
            if own:
                item["own_article_id"] = int(row["id"])
            else:
                item["article_id"] = int(row["id"])
            package.append(item)
    return package


def source_prompt(package: list[dict]) -> str:
    blocks = []
    for item in package:
        blocks.append(
            f"[{item['ref_id']}] type={item['kind']} title={item['title']}\n"
            f"url={item['url']} author={item['author']} published_at={item['published_at']}\n"
            f"excerpt=\n{item['excerpt']}"
        )
    return "\n\n".join(blocks)


def _valid_refs(value: Any, allowed_refs: set[str]) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError("来源编号必须是非空字符串数组")
    if any(not isinstance(item, str) or not item or item not in allowed_refs for item in value):
        raise ValueError("内容包含不存在的来源编号")
    return list(dict.fromkeys(value))


def _reject_unknown_embedded_refs(value: Any, allowed_refs: set[str]) -> None:
    """Reject source markers hidden in any AI key/value that may be shown or exported."""
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_unknown_embedded_refs(key, allowed_refs)
            _reject_unknown_embedded_refs(item, allowed_refs)
    elif isinstance(value, list):
        for item in value:
            _reject_unknown_embedded_refs(item, allowed_refs)
    elif isinstance(value, str):
        refs = set(re.findall(r"\[((?:R|O)\d+)\]", value))
        if not refs.issubset(allowed_refs):
            raise ValueError("AI 展示内容包含不存在的来源")


_BRIEF_SUMMARY_LABELS = {
    "summary": "摘要", "text": "摘要", "content": "摘要", "overview": "摘要",
    "focus": "分析焦点", "mode": "分析模式", "account_audience": "目标读者",
    "account_goal": "内容目标", "recommended_cta": "建议行动",
}


def _brief_summary(value: Any, allowed_refs: set[str], limit: int = 2000) -> str:
    """Accept MiniMax's labelled summary object without stringifying arbitrary containers."""
    if not isinstance(value, dict):
        return _clean_text(value, limit)
    parts = []
    for key, item in value.items():
        if key == "evidence_refs":
            # Validate summary-level provenance, but do not misattach it to one label/value pair.
            _valid_refs(item, allowed_refs)
            continue
        if not isinstance(key, str) or not isinstance(item, str):
            raise ValueError("AI 摘要对象的标签和内容必须是字符串")
        text = _clean_text(item, limit)
        if not text:
            continue
        label = _BRIEF_SUMMARY_LABELS.get(key, _clean_text(key, 60))
        if not label:
            raise ValueError("AI 摘要对象包含空标签")
        parts.append(f"{label}：{text}")
    return _clean_text("；".join(parts), limit)


def _brief_limitations(value: Any, allowed_refs: set[str], *, limit: int = 20, item_limit: int = 500) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("AI 数据限制必须是数组")
    cleaned = []
    # Validate every model-supplied item before applying the display cap. Otherwise
    # item 21+ could smuggle an unknown bare ref in evidence_refs and be silently dropped.
    for raw in value:
        refs = []
        if isinstance(raw, dict):
            text_key = next((key for key in ("limitation", "text", "content", "description") if key in raw), None)
            if text_key is None:
                raise ValueError("AI 数据限制对象缺少文本")
            text_value = raw[text_key]
            if "evidence_refs" in raw:
                refs = _valid_refs(raw["evidence_refs"], allowed_refs)
        else:
            text_value = raw
        suffix = " " + "".join(f"[{ref}]" for ref in refs) if refs else ""
        # Reserve the complete citation suffix; never truncate a marker into display debris.
        text = _clean_text(text_value, item_limit - len(suffix))
        if text:
            cleaned.append(text + suffix)
    return cleaned[:limit]


def normalise_style_profile(
    data: dict,
    *,
    actual_sample_count: int,
    allowed_refs: set[str] | None = None,
) -> dict[str, Any]:
    allowed_refs = allowed_refs or set()
    _reject_unknown_embedded_refs(data, allowed_refs)
    required = ("voice", "tone", "sentence_style")
    if any(not _clean_text(data.get(key), 1000) for key in required):
        raise ValueError("风格分析缺少 voice/tone/sentence_style")
    raw_confidence = data.get("confidence")
    if raw_confidence is not None and not isinstance(raw_confidence, str):
        raise ValueError("风格置信度必须是字符串")
    confidence = str(raw_confidence or "provisional").lower()
    if confidence not in {"low", "medium", "high", "provisional"}:
        confidence = "provisional"
    if actual_sample_count < 3:
        confidence = "provisional"
    result = {
        "voice": _clean_text(data.get("voice"), 1000),
        "tone": _clean_text(data.get("tone"), 1000),
        "sentence_style": _clean_text(data.get("sentence_style"), 1000),
        "structure_preferences": _string_list(data.get("structure_preferences")),
        "do": _string_list(data.get("do")),
        "avoid": _string_list(data.get("avoid")),
        "confidence": confidence,
        "sample_count": int(actual_sample_count),
    }
    _reject_unknown_embedded_refs(result, allowed_refs)
    return result


def normalise_brief(data: dict, allowed_refs: set[str]) -> dict[str, Any]:
    _reject_unknown_embedded_refs(data, allowed_refs)
    topics = []
    for raw in data.get("topics") if isinstance(data.get("topics"), list) else []:
        if not isinstance(raw, dict):
            continue
        refs = _valid_refs(raw.get("evidence_refs"), allowed_refs)
        if not refs:
            raise ValueError("每个选题必须包含有效来源")
        topics.append({
            "title": _clean_text(raw.get("title"), 300),
            "angle": _clean_text(raw.get("angle"), 1000),
            "why_now": _clean_text(raw.get("why_now"), 500),
            "audience_value": _clean_text(raw.get("audience_value"), 500),
            "evidence_refs": refs,
            "risk": _clean_text(raw.get("risk"), 500),
        })
    topics = [item for item in topics if item["title"]][:8]
    if not topics:
        raise ValueError("选题简报没有有效选题")
    observations = []
    for raw in data.get("content_observations") if isinstance(data.get("content_observations"), list) else []:
        if not isinstance(raw, dict):
            continue
        observation = _clean_text(raw.get("observation"), 1000)
        if not observation:
            continue
        try:
            refs = _valid_refs(raw.get("evidence_refs"), allowed_refs)
        except ValueError as exc:
            raise ValueError("每个观察必须包含有效且完整的来源") from exc
        observations.append({
            "observation": observation,
            "evidence_refs": refs,
        })
    result = {
        "summary": _brief_summary(data.get("summary"), allowed_refs),
        "content_observations": observations[:12],
        "topics": topics,
        "data_limitations": _brief_limitations(data.get("data_limitations"), allowed_refs) or [
            "仅有内容样本，不能据此判断真实阅读、分享、涨粉或支付效果"
        ],
    }
    _reject_unknown_embedded_refs(result, allowed_refs)
    return result


def normalise_draft(data: dict, allowed_refs: set[str]) -> dict[str, Any]:
    _reject_unknown_embedded_refs(data, allowed_refs)
    raw_body = data.get("body_markdown")
    if not isinstance(raw_body, str):
        raise ValueError("成稿正文必须是字符串")
    body = raw_body.strip()
    if len(body) < 100:
        raise ValueError("成稿正文过短")
    used_refs = set(re.findall(r"\[((?:R|O)\d+)\]", body))
    if not used_refs.issubset(allowed_refs):
        raise ValueError("正文包含不存在的来源")
    title_options = _string_list(data.get("title_options"), limit=5, item_limit=120)
    if not title_options:
        raise ValueError("成稿缺少标题")
    citations = []
    for raw in data.get("citation_map") if isinstance(data.get("citation_map"), list) else []:
        if not isinstance(raw, dict):
            continue
        refs = _valid_refs(raw.get("evidence_refs"), allowed_refs)
        claim = _clean_text(raw.get("claim"), 1000)
        if claim and refs:
            citations.append({"claim": claim, "evidence_refs": refs})
    result = {
        "title_options": title_options,
        "outline": _draft_structured_list(
            data.get("outline"), kind="outline", limit=20, item_limit=500,
        ),
        "body_markdown": body[:100_000],
        "citation_map": citations[:30],
        "unresolved_facts": _draft_structured_list(
            data.get("unresolved_facts"), kind="unresolved", limit=30, item_limit=1000,
        ),
        "risk_notes": _string_list(data.get("risk_notes"), limit=20, item_limit=500),
        "suggested_cta": _clean_text(data.get("suggested_cta"), 500),
    }
    _reject_unknown_embedded_refs(result, allowed_refs)
    return result


def _markdown_export(draft: dict, sources: list[dict], unresolved: list[str]) -> str:
    invalidated = bool(draft.get("evidence_invalidated"))
    parts = [f"# {draft.get('title') or '未命名草稿'}", ""]
    if invalidated:
        parts.extend([
            "> 警告：正文已经人工修改，引用尚未重新核验；以下来源仅为历史参考，发布前必须逐条核实。",
            "",
        ])
    parts.append(str(draft.get("body_markdown") or "").strip())
    if sources:
        heading = "## 历史来源（未重新核验）" if invalidated else "## 参考来源"
        parts.extend(["", "---", "", heading])
        for item in sources:
            url = _safe_url(item.get("url"))
            suffix = f"：{url}" if url else ""
            parts.append(f"- [{item.get('ref_id')}] {item.get('title') or '未命名来源'}{suffix}")
    if unresolved:
        parts.extend(["", "## 待核实事实"])
        parts.extend(f"- {item}" for item in unresolved)
    return "\n".join(parts).strip() + "\n"


def _safe_markdown_html(markdown_text: str) -> str:
    """Small allowlisted Markdown renderer; all source text is escaped first."""
    output: list[str] = []
    in_list = False
    for raw_line in markdown_text.splitlines():
        line = raw_line.strip()
        if in_list and not line.startswith(("- ", "* ")):
            output.append("</ul>")
            in_list = False
        if not line:
            continue
        escaped = html.escape(line, quote=True)
        escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
        if line.startswith("### "):
            output.append(f"<h3>{html.escape(line[4:])}</h3>")
        elif line.startswith("## "):
            output.append(f"<h2>{html.escape(line[3:])}</h2>")
        elif line.startswith("# "):
            output.append(f"<h2>{html.escape(line[2:])}</h2>")
        elif line.startswith(("- ", "* ")):
            if not in_list:
                output.append("<ul>")
                in_list = True
            output.append(f"<li>{html.escape(line[2:])}</li>")
        else:
            output.append(f"<p>{escaped}</p>")
    if in_list:
        output.append("</ul>")
    return "\n".join(output)


def render_draft_export(draft: dict, sources: list[dict], unresolved: list[str], fmt: str) -> str:
    markdown_text = _markdown_export(draft, sources, unresolved)
    if fmt in {"markdown", "md", "txt"}:
        return markdown_text
    if fmt != "html":
        raise ValueError("不支持的导出格式")
    title = html.escape(str(draft.get("title") or "未命名草稿"))
    invalidated = bool(draft.get("evidence_invalidated"))
    warning_html = (
        '<aside class="warning"><strong>引用尚未重新核验：</strong>'
        "正文已经人工修改，以下来源仅为历史参考，发布前必须逐条核实。</aside>"
        if invalidated else ""
    )
    body = _safe_markdown_html(str(draft.get("body_markdown") or ""))
    source_rows = []
    for item in sources:
        label = html.escape(f"[{item.get('ref_id')}] {item.get('title') or '未命名来源'}")
        url = _safe_url(item.get("url"))
        source_rows.append(f'<li><a href="{html.escape(url, quote=True)}">{label}</a></li>' if url else f"<li>{label}</li>")
    source_heading = "历史来源（未重新核验）" if invalidated else "参考来源"
    sources_html = f"<h2>{source_heading}</h2><ul>" + "".join(source_rows) + "</ul>" if source_rows else ""
    unresolved_html = ""
    if unresolved:
        unresolved_html = "<h2>待核实事实</h2><ul>" + "".join(f"<li>{html.escape(str(item))}</li>" for item in unresolved) + "</ul>"
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>{title}</title>"
        "<style>body{max-width:760px;margin:32px auto;padding:0 20px;font:17px/1.8 -apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;color:#222}"
        "h1,h2{line-height:1.4}p{margin:1em 0}a{color:#1769aa;overflow-wrap:anywhere}"
        ".warning{padding:12px 16px;border:2px solid #b42318;background:#fff4e5;color:#7a271a}</style></head>"
        f"<body><h1>{title}</h1>{warning_html}{body}{sources_html}{unresolved_html}</body></html>"
    )
