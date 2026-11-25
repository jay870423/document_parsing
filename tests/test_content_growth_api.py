from __future__ import annotations

import hashlib
import json
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.billing import BillingStore, UserContext, utc_now
from app.content_growth_api import build_content_growth_router
from app.content_growth_store import ContentGrowthStore
from app.wewe_store import WeWeStore

STYLE_JSON = json.dumps({
    "voice": "像真人一样真诚", "tone": "温和克制", "sentence_style": "短句为主",
    "structure_preferences": ["真实问题开头", "结尾一个行动建议"],
    "do": ["使用具体家庭场景"], "avoid": ["制造焦虑", "承诺效果"],
    "confidence": "high", "sample_count": 999,
}, ensure_ascii=False)
BRIEF_JSON = json.dumps({
    "summary": "参考内容集中在学习方法和家长陪伴。",
    "content_observations": [{"observation": "常用问题开头", "evidence_refs": ["R1"]}],
    "topics": [{
        "title": "孩子写作业拖拉，家长先改哪一步？", "angle": "给一个可执行的小动作",
        "why_now": "临近期末", "audience_value": "减少亲子冲突",
        "evidence_refs": ["R1"], "risk": "不能承诺短期提分",
    }],
    "data_limitations": ["没有阅读和转化数据，不判断推广效果"],
}, ensure_ascii=False)
DRAFT_JSON = json.dumps({
    "title_options": ["孩子写作业拖拉，先别催", "把陪伴改成一个小动作", "期末陪伴清单"],
    "outline": ["一个真实场景", "可执行动作", "温和收尾"],
    "body_markdown": "写作业拖拉，不一定只是孩子的问题。[R1]\n\n" + "先观察，再调整一个小动作。" * 20,
    "citation_map": [{"claim": "参考文章使用问题式开头", "evidence_refs": ["R1"]}],
    "unresolved_facts": ["涉及个体效果时需要更多事实"],
    "risk_notes": ["不承诺提分"], "suggested_cta": "体验一次学习记录",
}, ensure_ascii=False)


class FakeChat:
    def __init__(self):
        self.responses = []
        self.calls = []
        self.error = None

    def generate_text(self, system_prompt, user_prompt, **kwargs):
        self.calls.append({"system": system_prompt, "user": user_prompt, **kwargs})
        if self.error:
            raise self.error
        if not self.responses:
            raise AssertionError("unexpected paid AI call")
        return self.responses.pop(0)


@pytest.fixture()
def growth_api(wewe_settings):
    billing = BillingStore(wewe_settings)
    billing.grant_subscription(1, "personal_light", call_quota=20, ends_at=utc_now() + timedelta(days=400))
    store = ContentGrowthStore(wewe_settings)
    references = WeWeStore(wewe_settings)
    source_id = references.add_source(
        user_id=1, knowledge_base_id=1, source_type="mp", upstream_feed_id="MP_GROWTH", name="参考公众号",
    )
    references.upsert_article(
        user_id=1, source_id=source_id, knowledge_base_id=1, upstream_article_id="reference-1",
        title="期末陪伴的五个方法", url="https://example.com/reference", author="参考作者",
        published_at="2026-11-20", text_content="家长先观察孩子的困难，再从一个动作开始。" * 30,
        status="indexed",
    )
    chat = FakeChat()
    current = {"user": UserContext(1, "wewe@example.com", False, True)}

    def current_user():
        return current["user"]

    def resolve_kb(user_id, knowledge_base_id):
        return billing.get_knowledge_base_for_user(user_id, knowledge_base_id)

    app = FastAPI()
    app.include_router(build_content_growth_router(
        current_user=current_user, store=store, reference_store=references,
        chat=chat, billing_store=billing, resolve_kb=resolve_kb,
    ))
    return TestClient(app), store, references, chat, billing, current


def add_own_article(client, title="我的陪伴记录"):
    response = client.post("/api/growth/own-articles", json={
        "knowledge_base_id": 1, "title": title, "body": "孩子写作业时，我先观察再记录。" * 40,
        "url": "https://example.com/my-old", "published_at": "2026-11-01",
    })
    assert response.status_code == 200
    return response.json()["article"]["id"]


def activate_style(client, chat):
    chat.responses.append(STYLE_JSON)
    response = client.post("/api/growth/style-profiles/generate", json={"knowledge_base_id": 1, "operation_id": "style-generate-test"})
    assert response.status_code == 200
    assert response.json()["style_profile"]["sample_count"] >= 1
    assert response.json()["activated"] is False
    response = client.post("/api/growth/style-profiles/activate", json={
        "knowledge_base_id": 1, "style_profile": response.json()["style_profile"],
    })
    assert response.status_code == 200


def test_profile_workspace_and_own_article_are_persistent(growth_api):
    client, store, _references, _chat, _billing, _current = growth_api
    response = client.put("/api/growth/profile", json={
        "knowledge_base_id": 1, "audience": "6-10岁孩子家长", "goal": "注册体验",
        "cta": "领取学习记录模板", "forbidden": "保过、提分承诺",
    })
    assert response.status_code == 200
    own_id = add_own_article(client)
    workspace = client.get("/api/growth/workspace?knowledge_base_id=1")
    assert workspace.status_code == 200
    data = workspace.json()
    assert data["user_id"] == 1
    assert data["profile"]["audience"] == "6-10岁孩子家长"
    assert data["own_articles"][0]["id"] == own_id
    assert "body" not in data["own_articles"][0]
    assert data["reference_articles"][0]["title"] == "期末陪伴的五个方法"
    assert "text_content" not in data["reference_articles"][0]
    assert data["billing"]["ai_success_cost"] == 1
    assert data["data_boundaries"]["wechat_read_metrics"] is False


def test_style_brief_draft_edit_export_and_metrics_form_complete_paid_loop(growth_api):
    client, _store, _references, chat, billing, _current = growth_api
    own_id = add_own_article(client)
    activate_style(client, chat)
    assert billing.get_active_subscription(1)["used_calls"] == 1

    chat.responses.append(BRIEF_JSON)
    brief_response = client.post("/api/growth/briefs", json={
        "knowledge_base_id": 1, "focus": "期末亲子陪伴", "analysis_mode": "structure",
        "operation_id": "brief-full-workflow",
    })
    assert brief_response.status_code == 200
    brief = brief_response.json()["brief"]
    assert brief["content"]["topics"][0]["evidence_refs"] == ["R1"]
    assert brief["sources"][0]["ref_id"] == "R1"
    assert billing.get_active_subscription(1)["used_calls"] == 2

    chat.responses.append(DRAFT_JSON)
    draft_response = client.post("/api/growth/drafts", json={
        "knowledge_base_id": 1, "brief_id": brief["id"], "topic": "期末亲子陪伴",
        "target_length": 1000,
        "operation_id": "draft-full-workflow",
    })
    assert draft_response.status_code == 200
    draft = draft_response.json()["draft"]
    assert draft["brief_id"] == brief["id"]
    assert draft["sources"][0]["article_id"]
    assert "R1" in draft["body_markdown"]
    assert billing.get_active_subscription(1)["used_calls"] == 3

    updated = client.patch(f"/api/growth/drafts/{draft['id']}", json={
        "title": "人工修改后的标题", "body_markdown": draft["body_markdown"] + "\n\n人工补充一个标点。", "status": "reviewed",
        "expected_revision": draft["revision"],
    })
    assert updated.status_code == 200
    assert updated.json()["draft"]["status"] == "reviewed"
    assert updated.json()["draft"]["evidence_invalidated"] is True
    assert updated.json()["draft"]["citation_map"] == []
    assert updated.json()["draft"]["unresolved_facts"] == []
    for fmt, expected_type in (("markdown", "text/markdown"), ("txt", "text/plain"), ("html", "text/html")):
        exported = client.get(f"/api/growth/drafts/{draft['id']}/export?format={fmt}")
        assert exported.status_code == 200
        assert exported.headers["content-type"].startswith(expected_type)
        assert "人工定稿正文" not in exported.text
        assert "人工补充一个标点" in exported.text
        assert "[R1]" in exported.text
        assert "期末陪伴的五个方法" in exported.text
        assert "引用尚未重新核验" in exported.text
        assert "历史来源" in exported.text
        assert "涉及个体效果时需要更多事实" not in exported.text
    assert billing.get_active_subscription(1)["used_calls"] == 3

    metric = client.post("/api/growth/metrics", json={
        "knowledge_base_id": 1, "own_article_id": own_id, "metric_date": "2026-11-24",
        "window": "7d", "reads": 1000, "shares": 50, "new_follows": 20,
        "landing_clicks": 100, "registrations": 20, "payments": 5,
        "account_label": "测试公众号", "source_name": "公众号后台手工导出 + 自有订单表",
        "campaign_id": "campaign-2026-11",
    })
    assert metric.status_code == 200
    observed = metric.json()["metric"]["observed_rates"]
    assert observed["share_rate"] == 0.05
    assert observed["payment_per_read"] == 0.005
    assert metric.json()["interpretation"] == "descriptive_only"


def test_invalid_ai_output_is_502_refunded_and_not_saved(growth_api):
    client, store, _references, chat, billing, _current = growth_api
    chat.responses.append('{"topics":[{"title":" truncated"}')
    response = client.post("/api/growth/briefs", json={"knowledge_base_id": 1, "focus": "阅读", "operation_id": "brief-standard-test"})
    assert response.status_code == 502
    assert billing.get_active_subscription(1)["used_calls"] == 0
    assert billing.get_active_subscription(1)["remaining_calls"] == 20
    assert store.list_briefs(1, 1) == []


def test_brief_observation_without_valid_evidence_is_502_refunded(growth_api):
    client, store, _references, chat, billing, _current = growth_api
    chat.responses.append(json.dumps({
        "summary": "测试",
        "content_observations": [{"observation": "没有证据的观察", "evidence_refs": ["R999"]}],
        "topics": [{"title": "合法选题", "evidence_refs": ["R1"]}],
    }, ensure_ascii=False))
    response = client.post("/api/growth/briefs", json={"knowledge_base_id": 1, "focus": "阅读", "operation_id": "brief-standard-test"})
    assert response.status_code == 502
    assert billing.get_active_subscription(1)["used_calls"] == 0
    assert billing.get_active_subscription(1)["remaining_calls"] == 20
    assert store.list_briefs(1, 1) == []


def test_draft_unknown_title_ref_is_502_refunded(growth_api):
    client, store, _references, chat, billing, _current = growth_api
    add_own_article(client)
    activate_style(client, chat)
    assert billing.get_active_subscription(1)["used_calls"] == 1
    contaminated = json.loads(DRAFT_JSON)
    contaminated["title_options"] = ["标题污染[R999]"]
    chat.responses.append(json.dumps(contaminated, ensure_ascii=False))
    response = client.post("/api/growth/drafts", json={
        "knowledge_base_id": 1, "topic": "亲子陪伴", "target_length": 1000,
        "operation_id": "draft-unknown-title-ref",
    })
    assert response.status_code == 502
    assert billing.get_active_subscription(1)["used_calls"] == 1
    assert billing.get_active_subscription(1)["remaining_calls"] == 19
    assert store.list_drafts(1, 1) == []


def test_quota_denied_before_ai_and_success_charges_once(growth_api):
    client, _store, _references, chat, billing, _current = growth_api
    subscription = billing.get_active_subscription(1)
    billing.grant_subscription(1, "personal_light", call_quota=1, ends_at=utc_now() + timedelta(days=500))
    billing.reserve_call(1)  # Hold the final unit: the next paid call must be denied.
    response = client.post("/api/growth/briefs", json={"knowledge_base_id": 1, "focus": "阅读", "operation_id": "brief-standard-test"})
    assert response.status_code == 402
    assert chat.calls == []
    assert subscription["used_calls"] == 0


def test_cross_user_sources_briefs_drafts_and_metrics_fail_closed(growth_api):
    client, store, references, chat, billing, current = growth_api
    other = billing.create_user("other-growth@example.com", "password-123")
    other_kb = billing.ensure_default_knowledge_base(other["id"])
    store.add_own_article(other["id"], other_kb["id"], "他人文章", "秘密" * 100)
    store.upsert_profile(other["id"], other_kb["id"], {"audience": "他人读者"})
    store.activate_style(other["id"], other_kb["id"], {"voice": "秘密风格"})
    other_brief = store.create_brief(other["id"], other_kb["id"], "structure", "秘密", {"topics": []}, [])
    other_draft = store.create_draft(other["id"], other_kb["id"], other_brief, "秘密", "秘密正文", {}, [])
    store.activate_style(1, 1, {"voice": "本人", "tone": "克制", "sentence_style": "短句"})

    assert client.get(f"/api/growth/drafts/{other_draft}").status_code == 404
    assert client.get(f"/api/growth/drafts/{other_draft}/export").status_code == 404
    assert client.patch(
        f"/api/growth/drafts/{other_draft}", json={"title": "越权", "expected_revision": 1},
    ).status_code == 404
    assert client.post("/api/growth/drafts", json={
        "knowledge_base_id": 1, "brief_id": other_brief, "topic": "越权",
        "operation_id": "draft-cross-user",
    }).status_code == 404
    assert chat.calls == []
    current["user"] = UserContext(other["id"], other["email"], False, True)
    assert client.get(f"/api/growth/drafts/{other_draft}").status_code == 200
    current["user"] = UserContext(1, "wewe@example.com", False, True)
    assert client.get(f"/api/growth/drafts/{other_draft}").status_code == 404


def test_reference_selection_cannot_read_other_users_article(growth_api):
    client, _store, references, chat, billing, _current = growth_api
    other = billing.create_user("other-ref@example.com", "password-123")
    other_kb = billing.ensure_default_knowledge_base(other["id"])
    source_id = references.add_source(
        user_id=other["id"], knowledge_base_id=other_kb["id"], source_type="web",
        target_url="https://other.example", name="他人来源",
    )
    article_id, _ = references.upsert_article(
        user_id=other["id"], source_id=source_id, knowledge_base_id=other_kb["id"],
        upstream_article_id="secret", title="秘密参考", text_content="秘密" * 100,
    )
    response = client.post("/api/growth/briefs", json={
        "knowledge_base_id": 1, "focus": "秘密", "reference_article_ids": [article_id],
        "operation_id": "brief-cross-user-reference",
    })
    assert response.status_code == 422
    assert chat.calls == []


def test_style_unknown_reference_is_502_refunded_and_not_saved(growth_api):
    client, store, _references, chat, billing, _current = growth_api
    add_own_article(client)
    contaminated = json.loads(STYLE_JSON)
    contaminated["voice"] = "温和[R999]"
    chat.responses.append(json.dumps(contaminated, ensure_ascii=False))
    response = client.post("/api/growth/style-profiles/generate", json={"knowledge_base_id": 1, "operation_id": "style-generate-test"})
    assert response.status_code == 502
    assert billing.get_active_subscription(1)["used_calls"] == 0
    assert store.get_profile(1, 1)["style_draft"] is None


def test_metric_unknowns_are_null_and_oldest_metric_save_does_not_500(growth_api):
    from datetime import date, timedelta
    client, store, _references, _chat, _billing, _current = growth_api
    own_id = add_own_article(client)
    for index in range(100):
        store.upsert_metric(1, own_id, str(date(2026, 2, 1) + timedelta(days=index)), "24h", {
            "reads": index + 1, "shares": None, "new_follows": None,
            "landing_clicks": None, "registrations": None, "payments": None,
            "account_label": "测试公众号", "source_name": "公众号后台导出", "campaign_id": "",
        })
    payload = {
        "knowledge_base_id": 1, "own_article_id": own_id, "metric_date": "2026-01-01",
        "window": "24h", "reads": None, "shares": 0, "new_follows": None,
        "landing_clicks": None, "registrations": None, "payments": None,
        "account_label": "测试公众号", "source_name": "公众号后台导出（仅分享数字）",
        "campaign_id": "older-than-latest-100",
    }
    response = client.post("/api/growth/metrics", json=payload)
    assert response.status_code == 200
    metric = response.json()["metric"]
    assert metric["reads"] is None
    assert metric["shares"] == 0
    assert metric["observed_rates"]["share_rate"] is None
    assert metric["observed_rates"]["payment_per_read"] is None
    assert "follow_rate" not in metric["observed_rates"]
    assert "跨系统" in metric["rate_note"]
    assert "未提供" in metric["sample_note"]


def test_draft_patch_uses_optimistic_lock_and_noop_save_succeeds(growth_api):
    client, store, _references, _chat, _billing, _current = growth_api
    brief = store.create_brief(1, 1, "structure", "修订测试", {"topics": []}, [])
    draft_id = store.create_draft(1, 1, brief, "V1", "正文" * 80, {}, [])
    first = client.patch(f"/api/growth/drafts/{draft_id}", json={
        "title": "V2", "expected_revision": 1,
    })
    assert first.status_code == 200
    assert first.json()["draft"]["revision"] == 2
    repeated = client.patch(f"/api/growth/drafts/{draft_id}", json={
        "title": "V2", "expected_revision": 2,
    })
    assert repeated.status_code == 200
    assert repeated.json()["draft"]["revision"] == 2
    stale = client.patch(f"/api/growth/drafts/{draft_id}", json={
        "title": "过期页面覆盖", "expected_revision": 1,
    })
    assert stale.status_code == 409
    assert store.get_draft(1, draft_id)["title"] == "V2"


def test_paid_success_replay_does_not_call_ai_or_charge_twice(growth_api):
    client, store, _references, chat, billing, _current = growth_api
    chat.responses.append(BRIEF_JSON)
    payload = {"knowledge_base_id": 1, "focus": "幂等成功", "operation_id": "brief-idem-success"}
    first = client.post("/api/growth/briefs", json=payload)
    replay = client.post("/api/growth/briefs", json=payload)
    assert first.status_code == replay.status_code == 200
    assert first.json()["brief"]["id"] == replay.json()["brief"]["id"]
    assert replay.json()["idempotent_replay"] is True
    assert len(chat.calls) == 1
    assert billing.get_active_subscription(1)["used_calls"] == 1
    assert len(store.list_briefs(1, 1)) == 1


def test_failed_paid_operation_can_retry_same_id_then_replay(growth_api):
    client, store, _references, chat, billing, _current = growth_api
    chat.responses.extend(['{"topics": broken', BRIEF_JSON])
    payload = {"knowledge_base_id": 1, "focus": "失败重试", "operation_id": "brief-idem-retry"}
    failed = client.post("/api/growth/briefs", json=payload)
    assert failed.status_code == 502
    assert billing.get_active_subscription(1)["used_calls"] == 0
    assert store.list_briefs(1, 1) == []
    succeeded = client.post("/api/growth/briefs", json=payload)
    replay = client.post("/api/growth/briefs", json=payload)
    assert succeeded.status_code == replay.status_code == 200
    assert len(chat.calls) == 2
    assert billing.get_active_subscription(1)["used_calls"] == 1
    assert len(store.list_briefs(1, 1)) == 1


def test_same_operation_id_with_different_payload_is_409(growth_api):
    client, _store, _references, chat, billing, _current = growth_api
    chat.responses.append(BRIEF_JSON)
    payload = {"knowledge_base_id": 1, "focus": "原始请求", "operation_id": "brief-idem-hash"}
    assert client.post("/api/growth/briefs", json=payload).status_code == 200
    changed = dict(payload, focus="偷换请求")
    assert client.post("/api/growth/briefs", json=changed).status_code == 409
    assert len(chat.calls) == 1
    assert billing.get_active_subscription(1)["used_calls"] == 1


def test_concurrent_same_operation_is_409_while_first_ai_call_is_running(growth_api):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    client, store, _references, chat, billing, _current = growth_api
    chat.responses.append(BRIEF_JSON)
    entered = threading.Event()
    release = threading.Event()
    original = chat.generate_text

    def blocking_generate(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    chat.generate_text = blocking_generate
    payload = {"knowledge_base_id": 1, "focus": "并发", "operation_id": "brief-idem-concurrent"}
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(client.post, "/api/growth/briefs", json=payload)
        assert entered.wait(5)
        duplicate = client.post("/api/growth/briefs", json=payload)
        assert duplicate.status_code == 409
        release.set()
        first = pending.result(timeout=10)
    assert first.status_code == 200
    assert len(chat.calls) == 1
    assert billing.get_active_subscription(1)["used_calls"] == 1
    assert len(store.list_briefs(1, 1)) == 1


def test_operation_completion_failure_rolls_back_artifact_and_charge(growth_api, monkeypatch):
    client, store, _references, chat, billing, _current = growth_api
    chat.responses.extend([BRIEF_JSON, BRIEF_JSON])
    original = store.complete_operation

    def fail_completion(*args, **kwargs):
        raise RuntimeError("injected completion failure")

    monkeypatch.setattr(store, "complete_operation", fail_completion)
    payload = {"knowledge_base_id": 1, "focus": "原子结算", "operation_id": "brief-idem-atomic"}
    failed = client.post("/api/growth/briefs", json=payload)
    assert failed.status_code == 502
    assert billing.get_active_subscription(1)["used_calls"] == 0
    assert store.list_briefs(1, 1) == []
    monkeypatch.setattr(store, "complete_operation", original)
    succeeded = client.post("/api/growth/briefs", json=payload)
    assert succeeded.status_code == 200
    assert billing.get_active_subscription(1)["used_calls"] == 1
    assert len(store.list_briefs(1, 1)) == 1


def test_admin_failed_final_write_retries_checkpoint_immediately(growth_api, monkeypatch):
    client, store, _references, chat, _billing, current = growth_api
    current["user"] = UserContext(1, "wewe@example.com", True, True)
    chat.responses.append(BRIEF_JSON)
    original = store.complete_operation

    def fail_completion(*args, **kwargs):
        raise RuntimeError("injected admin completion failure")

    monkeypatch.setattr(store, "complete_operation", fail_completion)
    payload = {
        "knowledge_base_id": 1, "focus": "超管最终写入失败恢复",
        "operation_id": "brief-admin-checkpoint-retry",
    }
    failed = client.post("/api/growth/briefs", json=payload)
    assert failed.status_code == 502
    assert len(chat.calls) == 1
    monkeypatch.setattr(store, "complete_operation", original)

    # No clock manipulation: a normally failed admin attempt is immediately retryable.
    recovered = client.post("/api/growth/briefs", json=payload)
    assert recovered.status_code == 200
    assert len(chat.calls) == 1
    assert len(store.list_briefs(1, 1)) == 1


def test_expired_same_operation_recovers_without_another_billing_call(growth_api):
    client, store, _references, chat, billing, _current = growth_api
    request = {
        "knowledge_base_id": 1,
        "focus": "进程崩溃后的同号恢复",
        "analysis_mode": "structure",
        "reference_article_ids": [],
    }
    operation_id = "brief-idem-crash-recovery"
    request_hash = hashlib.sha256(
        json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    begun = store.begin_operation(
        1, "/api/growth/briefs", operation_id, request_hash,
        lambda conn: billing.reserve_call(1, conn=conn),
    )
    old_reservation_id = begun["reservation"].reservation_id
    with store.connect() as conn:
        conn.execute(
            "UPDATE call_reservations SET created_at = ? WHERE reservation_id = ?",
            ("2000-01-01T00:00:00+00:00", old_reservation_id),
        )
        conn.execute(
            "UPDATE growth_operations SET updated_at = ? WHERE operation_id = ?",
            ("2000-01-01T00:00:00+00:00", operation_id),
        )
    assert billing.get_active_subscription(1)["used_calls"] == 1
    chat.responses.append(BRIEF_JSON)
    recovered = client.post("/api/growth/briefs", json=request | {"operation_id": operation_id})
    assert recovered.status_code == 200
    assert len(chat.calls) == 1
    assert billing.get_active_subscription(1)["used_calls"] == 1
    with store.connect() as conn:
        old = conn.execute(
            "SELECT state FROM call_reservations WHERE reservation_id = ?",
            (old_reservation_id,),
        ).fetchone()
        current = conn.execute(
            """SELECT r.state, r.reservation_id FROM growth_operations AS o
               JOIN call_reservations AS r ON r.reservation_id = o.reservation_id
               WHERE o.operation_id = ?""",
            (operation_id,),
        ).fetchone()
    assert old["state"] == "expired"
    assert current["state"] == "succeeded"
    assert current["reservation_id"] != old_reservation_id


def test_success_replay_survives_source_deletion(growth_api):
    client, _store, references, chat, billing, _current = growth_api
    chat.responses.append(BRIEF_JSON)
    payload = {"knowledge_base_id": 1, "focus": "来源删除后重放", "operation_id": "brief-idem-source-delete"}
    first = client.post("/api/growth/briefs", json=payload)
    assert first.status_code == 200
    assert references.delete_source(1, 1) is True
    replay = client.post("/api/growth/briefs", json=payload)
    assert replay.status_code == 200
    assert replay.json()["brief"]["id"] == first.json()["brief"]["id"]
    assert replay.json()["idempotent_replay"] is True
    assert len(chat.calls) == 1
    assert billing.get_active_subscription(1)["used_calls"] == 1


def test_validation_failure_usage_keeps_real_tokens_and_http_status(growth_api):
    client, store, _references, chat, billing, _current = growth_api
    chat.responses.append('{"topics": broken')
    response = client.post("/api/growth/briefs", json={
        "knowledge_base_id": 1, "focus": "失败审计", "operation_id": "brief-usage-audit",
    })
    assert response.status_code == 502
    assert billing.get_active_subscription(1)["used_calls"] == 0
    with store.connect() as conn:
        event = conn.execute(
            """SELECT call_count, input_tokens, output_tokens, status_code
               FROM usage_events WHERE endpoint = ? ORDER BY id DESC LIMIT 1""",
            ("/api/growth/briefs",),
        ).fetchone()
    assert event["call_count"] == 0
    assert event["input_tokens"] > 0
    assert event["output_tokens"] > 0
    assert event["status_code"] == 502


def test_worker_crash_after_ai_uses_checkpoint_without_second_ai_call(growth_api, monkeypatch):
    client, store, _references, chat, billing, _current = growth_api
    chat.responses.append(BRIEF_JSON)
    original_lock_kb = store.lock_kb

    class SimulatedWorkerCrash(BaseException):
        pass

    def crash_after_checkpoint(conn, user_id, knowledge_base_id):
        raise SimulatedWorkerCrash()

    monkeypatch.setattr(store, "lock_kb", crash_after_checkpoint)
    payload = {
        "knowledge_base_id": 1, "focus": "AI返回后进程崩溃",
        "operation_id": "brief-checkpoint-crash",
    }
    with pytest.raises(SimulatedWorkerCrash):
        client.post("/api/growth/briefs", json=payload)
    assert len(chat.calls) == 1
    monkeypatch.setattr(store, "lock_kb", original_lock_kb)
    with store.connect() as conn:
        conn.execute(
            """UPDATE call_reservations SET created_at = ?
               WHERE reservation_id = (SELECT reservation_id FROM growth_operations
                                       WHERE operation_id = ?)""",
            ("2000-01-01T00:00:00+00:00", "brief-checkpoint-crash"),
        )
        conn.execute(
            "UPDATE growth_operations SET updated_at = ? WHERE operation_id = ?",
            ("2000-01-01T00:00:00+00:00", "brief-checkpoint-crash"),
        )
    recovered = client.post("/api/growth/briefs", json=payload)
    assert recovered.status_code == 200
    assert len(chat.calls) == 1
    assert billing.get_active_subscription(1)["used_calls"] == 1
    assert len(store.list_briefs(1, 1)) == 1


def test_metric_rejects_blank_provenance_as_422(growth_api):
    client, _store, _references, _chat, _billing, _current = growth_api
    own_id = add_own_article(client)
    response = client.post("/api/growth/metrics", json={
        "knowledge_base_id": 1, "own_article_id": own_id,
        "metric_date": "2026-11-25", "window": "24h",
        "reads": 10, "account_label": "   ", "source_name": "公众号后台",
    })
    assert response.status_code == 422


def test_kb_deletion_before_final_write_refunds_and_leaves_no_orphan(growth_api, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    client, store, _references, chat, billing, _current = growth_api
    chat.responses.append(BRIEF_JSON)
    original_lock_kb = store.lock_kb
    reached_final_write = threading.Event()
    kb_deleted = threading.Event()

    def pause_before_kb_lock(conn, user_id, knowledge_base_id):
        reached_final_write.set()
        assert kb_deleted.wait(5)
        return original_lock_kb(conn, user_id, knowledge_base_id)

    monkeypatch.setattr(store, "lock_kb", pause_before_kb_lock)
    payload = {
        "knowledge_base_id": 1, "focus": "最终写入与删除竞争",
        "operation_id": "brief-kb-delete-race",
    }
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(client.post, "/api/growth/briefs", json=payload)
        assert reached_final_write.wait(5)
        with store.connect() as conn:
            deleted = conn.execute(
                "DELETE FROM knowledge_bases WHERE id = ? AND user_id = ?", (1, 1),
            )
            assert deleted.rowcount == 1
        kb_deleted.set()
        response = pending.result(timeout=10)
    assert response.status_code == 502
    assert len(chat.calls) == 1
    assert billing.get_active_subscription(1)["used_calls"] == 0
    assert store.list_briefs(1, 1) == []
    with store.connect() as conn:
        operation = conn.execute(
            "SELECT status FROM growth_operations WHERE operation_id = ?",
            ("brief-kb-delete-race",),
        ).fetchone()
    assert operation["status"] == "retryable"
    # The validated AI result remains recoverable, but no orphan can be written
    # after the KB is gone and another retry must neither call AI nor charge.
    monkeypatch.setattr(store, "lock_kb", original_lock_kb)
    retry = client.post("/api/growth/briefs", json=payload)
    assert retry.status_code == 502
    assert len(chat.calls) == 1
    assert billing.get_active_subscription(1)["used_calls"] == 0
    assert store.list_briefs(1, 1) == []
