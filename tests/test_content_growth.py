from __future__ import annotations

import json

import pytest

from app.content_growth import (
    build_source_package,
    normalise_brief,
    normalise_draft,
    normalise_style_profile,
    parse_ai_object,
    render_draft_export,
)
from app.content_growth_store import ContentGrowthStore


def test_parse_ai_object_accepts_fences_prose_and_nested_objects():
    expected = {"topics": [{"title": "选题"}], "meta": {"nested": True}}
    assert parse_ai_object('```json\n{"topics":[{"title":"选题"}],"meta":{"nested":true}}\n```') == expected
    assert parse_ai_object('分析如下：\n{"topics":[{"title":"选题"}],"meta":{"nested":true}}\n以上。') == expected


def test_parse_ai_object_rejects_truncated_or_non_object_payload():
    with pytest.raises(ValueError):
        parse_ai_object('{"topics":[{"title":"未闭合"}')
    with pytest.raises(ValueError):
        parse_ai_object('[{"topics":[]}]')


def test_source_package_has_stable_ids_and_bounded_excerpts():
    package = build_source_package(
        references=[{
            "id": 7, "title": "参考文章", "url": "https://example.com/r",
            "author": "作者", "published_at": "2026-11-01", "text_content": "参考" * 9000,
        }],
        own_articles=[{
            "id": 3, "title": "本人旧文", "url": "https://example.com/o",
            "body": "本人" * 9000, "published_at": "2026-10-01",
        }],
    )
    assert [item["ref_id"] for item in package] == ["R1", "O1"]
    assert package[0]["article_id"] == 7
    assert package[1]["own_article_id"] == 3
    assert all(len(item["excerpt"]) <= 6000 for item in package)
    assert package[0]["url"].startswith("https://")


def test_brief_and_draft_reject_unknown_evidence_refs():
    with pytest.raises(ValueError, match="来源"):
        normalise_brief({
            "summary": "摘要",
            "topics": [{"title": "题", "angle": "角度", "evidence_refs": ["R99"]}],
        }, {"R1"})
    with pytest.raises(ValueError, match="来源"):
        normalise_draft({
            "title_options": ["标题"], "outline": ["开头"],
            "body_markdown": "这是正文 [R9]" + "内容" * 80,
            "citation_map": [], "unresolved_facts": [],
        }, {"R1", "O1"})


def test_brief_rejects_observation_without_valid_evidence():
    with pytest.raises(ValueError, match="观察.*来源"):
        normalise_brief({
            "summary": "摘要",
            "content_observations": [{"observation": "没有证据的观察", "evidence_refs": ["R999"]}],
            "topics": [{"title": "合法选题", "evidence_refs": ["R1"]}],
        }, {"R1"})


def test_brief_accepts_real_minimax_structured_summary_and_limitations_without_losing_refs():
    # Verbatim schema/text excerpts from the real MiniMax production response on 2026-11-25.
    result = normalise_brief({
        "summary": {
            "focus": "期末陪伴",
            "mode": "推广要素观察模式",
            "account_audience": "小学家长",
            "account_goal": "提供可执行陪伴方法",
            "recommended_cta": "收藏并实践",
            "evidence_refs": ["R1", "O1"],
        },
        "content_observations": [{
            "observation": "参考内容标题直接点明“期末陪伴”，并以“五个方法”形成清单式阅读预期，但现有摘录未展示五个方法的具体内容。",
            "evidence_refs": ["R1"],
        }],
        "topics": [{
            "title": "期末陪伴别一上来催：先观察，再从一个小动作开始",
            "angle": "以“先识别孩子当前最难的一点”为入口，提供一个可执行的第一步。",
            "why_now": "现有参考内容聚焦期末陪伴。",
            "audience_value": "帮助小学家长把宽泛的“陪复习”缩小为一次清晰、可落地的陪伴行动。",
            "evidence_refs": ["R1", "O1"],
            "risk": "不得承诺提分或保证学习效果。",
        }],
        "data_limitations": [{
            "limitation": "R1标题虽称“五个方法”，但摘录只明确出现“先观察孩子的困难，再从一个动作开始”，无法还原其余四个方法。",
            "evidence_refs": ["R1"],
        }],
    }, {"R1", "O1"})
    assert result["summary"] == (
        "分析焦点：期末陪伴；分析模式：推广要素观察模式；目标读者：小学家长；"
        "内容目标：提供可执行陪伴方法；建议行动：收藏并实践"
    )
    assert result["data_limitations"] == [
        "R1标题虽称“五个方法”，但摘录只明确出现“先观察孩子的困难，再从一个动作开始”，无法还原其余四个方法。 [R1]"
    ]
    assert result["topics"][0]["evidence_refs"] == ["R1", "O1"]


def test_brief_structured_variants_still_reject_unknown_refs_and_non_text_content():
    base = {
        "summary": {"focus": "期末陪伴", "evidence_refs": ["R1"]},
        "topics": [{"title": "合法选题", "evidence_refs": ["R1"]}],
        "data_limitations": [{"limitation": "只有内容样本", "evidence_refs": ["R1"]}],
    }
    for mutate in (
        lambda value: value["summary"].update(evidence_refs=["R999"]),
        lambda value: value["data_limitations"][0].update(evidence_refs=["R999"]),
        lambda value: value["summary"].update(focus=["不能静默拼接的非文本"]),
    ):
        payload = json.loads(json.dumps(base, ensure_ascii=False))
        mutate(payload)
        with pytest.raises(ValueError, match="来源|字符串"):
            normalise_brief(payload, {"R1"})


def test_brief_validates_all_limitations_before_capping_and_never_splits_refs():
    overflow = [{
        "limitation": "只有内容样本，不能判断真实效果",
        "evidence_refs": ["R1"],
    } for _ in range(20)] + [{
        "limitation": "第二十一条也不能绕过校验",
        "evidence_refs": ["R1", "R999"],
    }]
    with pytest.raises(ValueError, match="来源"):
        normalise_brief({
            "summary": "摘要",
            "topics": [{"title": "合法选题", "evidence_refs": ["R1"]}],
            "data_limitations": overflow,
        }, {"R1"})

    # The appended suffix reserves five characters, so this inline [R1] crosses the text cut.
    with pytest.raises(ValueError, match="截断"):
        normalise_brief({
            "summary": "摘要",
            "topics": [{"title": "合法选题", "evidence_refs": ["R1"]}],
            "data_limitations": [{
                "limitation": "长" * 493 + "[R1]",
                "evidence_refs": ["R1"],
            }],
        }, {"R1"})

    with pytest.raises(ValueError, match="截断"):
        normalise_brief({
            "summary": {"focus": "长" * 1992 + "[R1]"},
            "topics": [{"title": "合法选题", "evidence_refs": ["R1"]}],
        }, {"R1"})


def test_ai_display_fields_reject_unknown_embedded_refs():
    with pytest.raises(ValueError, match="来源"):
        normalise_brief({
            "summary": "摘要夹带[R999]",
            "topics": [{"title": "合法选题", "evidence_refs": ["R1"]}],
        }, {"R1"})
    with pytest.raises(ValueError, match="来源"):
        normalise_draft({
            "title_options": ["标题污染[R999]"],
            "body_markdown": "这里全部是个人观点，不含来源标记。" * 20,
            "citation_map": [], "unresolved_facts": [],
        }, {"R1"})


def test_draft_citation_map_requires_complete_valid_refs_and_style_sample_count_is_server_authoritative():
    style = normalise_style_profile({
        "voice": "真诚", "tone": "克制", "sentence_style": "短句",
        "structure_preferences": ["问题开头"], "do": ["给实例"], "avoid": ["夸大"],
        "confidence": "high", "sample_count": 999,
    }, actual_sample_count=1)
    assert style["sample_count"] == 1
    assert style["confidence"] == "provisional"
    with pytest.raises(ValueError, match="来源"):
        normalise_draft({
            "title_options": ["标题"], "outline": ["开头"],
            "body_markdown": "正文 [R1]" + "内容" * 80,
            "citation_map": [{"claim": "事实", "evidence_refs": ["R1", "R9"]}],
            "unresolved_facts": [],
        }, {"R1"})


def test_draft_accepts_real_minimax_structured_outline_and_unresolved_facts():
    # Verbatim schema/text excerpts from the real MiniMax production response on 2026-11-25.
    result = normalise_draft({
        "title_options": [
            "期末每天二十分钟：家长陪孩子开始，不替他包办",
            "期末复习这样陪：帮启动、让复述、会复盘",
            "每天只陪一小段：给6—10岁孩子的期末复习流程",
        ],
        "outline": [
            {
                "step": 1,
                "heading": "先约定一个小而稳定的时段",
                "key_points": [
                    "把当天复习集中在一个短时段内",
                    "家长帮助孩子确定从哪里开始",
                    "若实际任务量较大，先解决启动问题，不强行卡住固定时长",
                ],
            },
            {
                "step": 2,
                "heading": "让孩子先说出最不确定的题",
                "key_points": [
                    "由孩子选出一道最想弄懂的题",
                    "一起找到对应的课本例题",
                    "让孩子先复述方法，再独立练习",
                ],
            },
        ],
        "body_markdown": (
            "期末复习需要陪伴，但陪伴不等于全程指挥，更不等于替孩子安排每一项任务。"
            "家长可以先做一件小事：帮助孩子启动今天的复习，再把选择和方法留给孩子。\n\n"
            "下面是一套可以按步骤尝试的流程。二十分钟是材料中提出的操作安排，不是适合所有孩子的统一标准；"
            "如果当天任务较多，先帮助孩子开始，比机械限时更重要。[R1][O1]"
        ),
        "citation_map": [],
        "unresolved_facts": [
            {
                "item": "二十分钟是否适合所有6—10岁孩子、各学科及不同任务量",
                "status": "现有材料没有提供适用范围或对照验证。",
            },
            {
                "item": "上述陪伴流程是否能提高成绩、缩短完成时间或减少出错",
                "status": "来源没有提供成绩、完成时长、出错率等变化数据，也没有设置对照组。",
            },
        ],
        "risk_notes": ["不要承诺提分、保过、显著提效或任何未经材料支持的结果。"],
        "suggested_cta": "先收藏这份流程。",
    }, {"R1", "O1"})
    assert result["outline"] == [
        "先约定一个小而稳定的时段：把当天复习集中在一个短时段内；家长帮助孩子确定从哪里开始；若实际任务量较大，先解决启动问题，不强行卡住固定时长",
        "让孩子先说出最不确定的题：由孩子选出一道最想弄懂的题；一起找到对应的课本例题；让孩子先复述方法，再独立练习",
    ]
    assert result["unresolved_facts"] == [
        "二十分钟是否适合所有6—10岁孩子、各学科及不同任务量：现有材料没有提供适用范围或对照验证。",
        "上述陪伴流程是否能提高成绩、缩短完成时间或减少出错：来源没有提供成绩、完成时长、出错率等变化数据，也没有设置对照组。",
    ]


def test_draft_structured_lists_still_reject_non_text_and_validate_before_capping():
    base = {
        "title_options": ["标题"],
        "outline": [{"heading": "合法大纲", "key_points": ["要点"]}],
        "body_markdown": "这是正文 [R1]" + "内容" * 80,
        "citation_map": [],
        "unresolved_facts": [{"item": "待核实", "status": "没有依据"}],
    }
    payload = json.loads(json.dumps(base, ensure_ascii=False))
    payload["outline"][0]["key_points"] = "不能当作一个要点"
    with pytest.raises(ValueError, match="字符串|数组"):
        normalise_draft(payload, {"R1"})

    payload = json.loads(json.dumps(base, ensure_ascii=False))
    payload["unresolved_facts"] = [
        {"item": "前二十项", "status": "无依据"} for _ in range(30)
    ] + [{"item": "第三十一项仍须校验[R999]", "status": "无依据"}]
    with pytest.raises(ValueError, match="来源"):
        normalise_draft(payload, {"R1"})


def test_ai_containers_and_dictionary_keys_cannot_smuggle_unknown_refs():
    for field in ("summary", "topics"):
        payload = {
            "summary": {"[R999]": "伪造来源"} if field == "summary" else "摘要",
            "topics": [{"title": "合法选题", "evidence_refs": ["R1"]}],
        }
        if field == "topics":
            payload["topics"] = [{"[R998]": "伪造标题", "evidence_refs": ["R1"]}]
        with pytest.raises(ValueError, match="来源|字符串"):
            normalise_brief(payload, {"R1"})
    with pytest.raises(ValueError, match="来源"):
        normalise_style_profile({
            "voice": "温和[R999]", "tone": "克制", "sentence_style": "短句",
        }, actual_sample_count=3, allowed_refs={"O1"})
    with pytest.raises(ValueError, match="字符串"):
        normalise_style_profile({
            "voice": "真诚", "tone": "克制", "sentence_style": "短句", "do": [{"bad": "container"}],
        }, actual_sample_count=3)


def test_evidence_ref_lists_reject_mixed_unknown_empty_and_non_string_values():
    for refs in (["R1", "R999"], ["R1", ""], ["R1", 1], "R1"):
        with pytest.raises(ValueError, match="来源"):
            normalise_brief({
                "summary": "摘要", "topics": [{"title": "合法选题", "evidence_refs": refs}],
            }, {"R1"})


def test_html_export_escapes_article_and_urls():
    rendered = render_draft_export(
        {"title": "<script>alert(1)</script>", "body_markdown": "# 标题\n<script>alert(2)</script>"},
        [{"ref_id": "R1", "title": "<img src=x onerror=1>", "url": "https://example.com/?a=1&b=2"}],
        ["待核对 <b>事实</b>"],
        "html",
    )
    assert "<script>" not in rendered
    assert "<img" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "https://example.com/?a=1&amp;b=2" in rendered


def test_store_persists_profile_drafts_metrics_and_scopes_user(wewe_settings):
    from app.billing import BillingStore

    billing = BillingStore(wewe_settings)
    other = billing.create_user("growth-other@example.com", "password-123")
    other_kb = billing.ensure_default_knowledge_base(other["id"])
    store = ContentGrowthStore(wewe_settings)
    store.upsert_profile(1, 1, {"audience": "小学生家长", "goal": "注册", "cta": "体验", "forbidden": "保过"})
    own = store.add_own_article(1, 1, "我的旧文", "正文" * 200, "https://example.com/own", "2026-11-01")
    store.save_style_draft(1, 1, {"voice": "温和"})
    store.activate_style(1, 1, {"voice": "温和", "sample_count": 1})
    brief = store.create_brief(1, 1, "structure", "亲子学习", {"summary": "摘要", "topics": []}, [{"ref_id": "R1"}])
    draft = store.create_draft(
        1, 1, brief, "标题", "正文" * 100,
        {"title_options": ["标题"], "outline": [], "citation_map": [], "unresolved_facts": []},
        [{"ref_id": "R1", "title": "参考"}],
    )
    store.upsert_metric(1, own, "2026-11-24", "7d", {
        "reads": 100, "shares": 5, "new_follows": 2,
        "landing_clicks": 10, "registrations": 3, "payments": 1,
        "account_label": "测试公众号", "source_name": "公众号后台导出 + 自有订单表",
        "campaign_id": "store-test",
    })

    profile = store.get_profile(1, 1)
    assert profile["audience"] == "小学生家长"
    assert profile["style_profile"]["voice"] == "温和"
    assert profile["style_version"] == 1
    assert store.get_own_article(1, own)["features"]["char_count"] == 400
    assert store.get_brief(1, brief)["focus"] == "亲子学习"
    assert store.get_draft(1, draft)["title"] == "标题"
    assert store.list_metrics(1, 1)[0]["reads"] == 100
    assert store.get_profile(other["id"], other_kb["id"]) is None
    assert store.get_own_article(other["id"], own) is None
    assert store.get_brief(other["id"], brief) is None
    assert store.get_draft(other["id"], draft) is None
    assert store.list_metrics(other["id"], other_kb["id"]) == []


def test_metrics_preserve_unknown_and_directly_read_oldest_beyond_100(wewe_settings):
    from datetime import date, timedelta
    store = ContentGrowthStore(wewe_settings)
    own = store.add_own_article(1, 1, "数据文章", "正文" * 100)
    first_id = None
    for index in range(101):
        metric_id = store.upsert_metric(1, own, str(date(2026, 1, 1) + timedelta(days=index)), "24h", {
            "reads": None if index == 0 else index,
            "shares": 0 if index == 0 else 1,
            "new_follows": None,
            "landing_clicks": None,
            "registrations": None,
            "payments": None,
            "source_name": "公众号后台导出 + 自有订单表",
            "account_label": "测试公众号",
            "campaign_id": "camp-2026",
        })
        if first_id is None:
            first_id = metric_id
    assert len(store.list_metrics(1, 1)) == 100
    oldest = store.get_metric(1, 1, first_id)
    assert oldest["reads"] is None
    assert oldest["shares"] == 0
    assert oldest["source_name"] == "公众号后台导出 + 自有订单表"
    assert oldest["account_label"] == "测试公众号"
    assert oldest["campaign_id"] == "camp-2026"


def test_draft_revision_rejects_stale_write_and_noop_does_not_change_revision(wewe_settings):
    from app.content_growth_store import DraftRevisionConflict
    store = ContentGrowthStore(wewe_settings)
    brief = store.create_brief(1, 1, "structure", "测试", {"topics": []}, [])
    draft_id = store.create_draft(1, 1, brief, "旧标题", "正文" * 60, {}, [])
    assert store.update_draft(1, draft_id, {"title": "新标题"}, expected_revision=1) is True
    changed = store.get_draft(1, draft_id)
    assert changed["revision"] == 2
    assert store.update_draft(1, draft_id, {"title": "新标题"}, expected_revision=2) is True
    assert store.get_draft(1, draft_id)["revision"] == 2
    with pytest.raises(DraftRevisionConflict):
        store.update_draft(1, draft_id, {"title": "覆盖别人的修改"}, expected_revision=1)
    assert store.get_draft(1, draft_id)["title"] == "新标题"


def test_own_article_storage_quota_is_enforced(wewe_settings, monkeypatch):
    import app.content_growth_store as module
    monkeypatch.setattr(module, "MAX_OWN_ARTICLES_PER_KB", 2, raising=False)
    monkeypatch.setattr(module, "MAX_OWN_ARTICLE_CHARS_PER_KB", 500, raising=False)
    store = ContentGrowthStore(wewe_settings)
    store.add_own_article(1, 1, "第一篇", "正" * 200)
    store.add_own_article(1, 1, "第二篇", "正" * 200)
    with pytest.raises(ValueError, match="容量|篇"):
        store.add_own_article(1, 1, "第三篇", "正" * 200)


def test_concurrent_own_article_imports_cannot_exceed_storage_quota(wewe_settings, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    import app.content_growth_store as module

    monkeypatch.setattr(module, "MAX_OWN_ARTICLES_PER_KB", 1, raising=False)
    store = ContentGrowthStore(wewe_settings)
    store.upsert_profile(1, 1, {})
    start_together = threading.Barrier(2)

    def import_one(index):
        try:
            start_together.wait(timeout=5)
            store.add_own_article(1, 1, f"并发文章{index}", "正文" * 100)
            return "saved"
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(import_one, (1, 2)))
    assert results.count("saved") == 1
    assert sum("最多保存" in result for result in results) == 1
    assert len(store.list_own_articles(1, 1)) == 1


def test_own_article_acquires_kb_lock_before_profile_lock(wewe_settings, monkeypatch):
    store = ContentGrowthStore(wewe_settings)
    store.upsert_profile(1, 1, {})
    calls = []
    original_lock_kb = store.lock_kb
    original_get_profile = store.get_profile

    def recording_lock_kb(conn, user_id, knowledge_base_id):
        calls.append("kb")
        return original_lock_kb(conn, user_id, knowledge_base_id)

    def recording_get_profile(user_id, knowledge_base_id, *, conn=None):
        if conn is not None:
            calls.append("profile")
        return original_get_profile(user_id, knowledge_base_id, conn=conn)

    monkeypatch.setattr(store, "lock_kb", recording_lock_kb)
    monkeypatch.setattr(store, "get_profile", recording_get_profile)
    store.add_own_article(1, 1, "锁顺序", "正文" * 100)
    assert calls[0] == "kb"
    assert calls[:2] == ["kb", "profile"]


def test_production_store_construction_has_no_schema_side_effects(wewe_settings):
    store = ContentGrowthStore(wewe_settings, auto_migrate=False)
    with store.connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*) FROM information_schema.tables
               WHERE table_schema = ? AND table_name LIKE ?""",
            (wewe_settings.mysql_database, "growth\\_%"),
        ).fetchone()
        assert int(row[0]) == 0
    store.init_db()
    with store.connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*) FROM information_schema.tables
               WHERE table_schema = ? AND table_name LIKE ?""",
            (wewe_settings.mysql_database, "growth\\_%"),
        ).fetchone()
        assert int(row[0]) >= 6
