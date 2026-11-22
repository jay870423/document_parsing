from __future__ import annotations

import json
from datetime import timedelta

import pytest

from app.billing import BillingStore, utc_now


def test_email_auth_password_and_session_roundtrip(mysql_settings):
    store = BillingStore(mysql_settings)
    user = store.create_user("Person@Example.com", "old-password-123")

    assert user["email"] == "person@example.com"
    assert user["phone"] is None
    assert store.authenticate("person@example.com", "wrong") is None
    assert store.authenticate("PERSON@example.com", "old-password-123")["id"] == user["id"]
    assert store.get_active_subscription(user["id"])["remaining_calls"] == 3

    token = store.create_session(user["id"])
    assert store.get_user_by_session(token).id == user["id"]
    store.delete_session(token)
    assert store.get_user_by_session(token) is None

    store.change_password(user["id"], "old-password-123", "new-password-456")
    assert store.authenticate("person@example.com", "old-password-123") is None
    assert store.authenticate("person@example.com", "new-password-456")["id"] == user["id"]
    with pytest.raises(ValueError, match="已注册"):
        store.create_user("person@example.com", "another-password")


def test_phone_auth_bootstrap_admin_and_agreement(mysql_settings):
    store = BillingStore(mysql_settings)
    store.bootstrap_admin("admin@example.com", "admin-password")
    admin = store.authenticate("admin@example.com", "admin-password")
    assert admin["is_admin"] is True
    store.bootstrap_admin("other@example.com", "other-password")
    assert store.authenticate("other@example.com", "other-password") is None

    user = store.create_user_by_phone("+86 138 0013 8000", "phone-password")
    assert user["email"] == ""
    assert user["phone"] == "13800138000"
    assert user["phone_masked"] == "138****8000"
    assert store.authenticate("13800138000", "phone-password")["id"] == user["id"]
    with pytest.raises(ValueError, match="已注册"):
        store.create_user_by_phone("13800138000", "other-password")

    store.record_agreement_consent(user["id"], "register", "v1", "v1", "hash", "127.0.0.1", "pytest")
    with store.connect() as conn:
        row = conn.execute("SELECT * FROM agreement_consents WHERE user_id=?", (user["id"],)).fetchone()
        assert row["action"] == "register"
        assert row["document_hash"] == "hash"


def test_api_key_is_reused_and_resolves_identity(mysql_settings):
    store = BillingStore(mysql_settings)
    user = store.create_user("keys@example.com", "password-123")
    raw, public = store.create_api_key(user["id"], "服务器")
    raw_again, _ = store.create_api_key(user["id"], "另一个名称")

    assert raw_again == raw
    assert public["api_key"] == raw
    assert public["can_copy"] is True
    context = store.get_api_key_context(raw)
    assert context.user_id == user["id"]
    assert context.email == user["email"]
    assert context.id == public["id"]
    assert len(store.list_api_keys(user["id"])) == 1
    assert store.get_api_key_context("dp_not-found") is None


def test_knowledge_bases_documents_and_tenant_isolation(mysql_settings):
    store = BillingStore(mysql_settings)
    owner = store.create_user("owner@example.com", "password-123")
    other = store.create_user("other@example.com", "password-123")

    default = store.ensure_default_knowledge_base(owner["id"])
    assert store.ensure_default_knowledge_base(owner["id"])["id"] == default["id"]
    second = store.create_knowledge_base(owner["id"], "芯片资料", "寄存器与手册")
    with pytest.raises(ValueError, match="无权访问"):
        store.get_knowledge_base_for_user(other["id"], second["id"])

    document = store.record_knowledge_document(
        owner["id"], second["id"], "run-1", "中文手册.pdf", "pdf", "completed", 12, 8, 2, "artifacts/run-1"
    )
    assert document["vector_count"] == 12
    assert store.list_knowledge_documents(owner["id"], second["id"])[0]["source_filename"] == "中文手册.pdf"
    bases = store.list_knowledge_bases(owner["id"])
    target = next(item for item in bases if item["id"] == second["id"])
    assert target["document_count"] == 1
    assert target["vector_count"] == 12
    assert isinstance(target["vector_count"], int)
    with pytest.raises(ValueError, match="无权访问"):
        store.get_knowledge_document_for_user(other["id"], second["id"], document["id"])
    assert store.delete_knowledge_document(owner["id"], second["id"], document["id"])["id"] == document["id"]
    assert store.list_knowledge_documents(owner["id"], second["id"]) == []


def test_rag_log_citations_feedback_and_permissions(mysql_settings):
    store = BillingStore(mysql_settings)
    owner = store.create_user("rag@example.com", "password-123")
    other = store.create_user("rag-other@example.com", "password-123")
    kb = store.ensure_default_knowledge_base(owner["id"])
    log = store.record_rag_query_log(
        owner["id"], None, kb["id"], "如何配置？", "答案", "hybrid", 6, "手册.pdf", 2,
        [{"text": "依据", "page": 3}],
    )
    assert log["citations"] == [{"text": "依据", "page": 3}]
    assert log["citation_count"] == 1
    updated = store.update_rag_feedback(owner["id"], log["id"], "up", "有帮助")
    assert updated["feedback_rating"] == "up"
    assert updated["feedback_note"] == "有帮助"
    with pytest.raises(ValueError, match="无权访问"):
        store.update_rag_feedback(other["id"], log["id"], "down")
    with pytest.raises(ValueError, match="反馈类型"):
        store.update_rag_feedback(owner["id"], log["id"], "bad")


def test_subscription_quota_usage_summary_and_purchase(mysql_settings):
    store = BillingStore(mysql_settings)
    user = store.create_user("billing@example.com", "password-123")
    plans = store.list_plans()
    assert {item["code"] for item in plans} >= {"free_trial", "personal_light"}
    assert all(isinstance(item["price_cents"], int) for item in plans)

    active = store.ensure_quota(user["id"])
    store.consume_call(user["id"], None, "/api/rag/query", "POST", 200, 15, 20, 30, {"source": "test"})
    store.consume_call(user["id"], None, "/api/rag/query", "POST", 500, 20, 10, 0)
    after = store.get_active_subscription(user["id"])
    assert after["id"] == active["id"]
    assert after["used_calls"] == 1
    assert after["remaining_calls"] == active["call_quota"] - 1

    summary = store.usage_summary(user["id"])
    assert summary["totals"]["calls"] == 2
    assert summary["totals"]["input_tokens"] == 30
    assert summary["totals"]["output_tokens"] == 30
    assert summary["endpoints"][0]["requests"] == 2
    json.dumps(summary, ensure_ascii=False)

    purchase = store.create_purchase_intent(user["id"], "personal_light", "微信联系")
    assert purchase["status"] == "pending"
    assert purchase["price_yuan"] == 9.9
    with pytest.raises(ValueError, match="免费体验包"):
        store.create_purchase_intent(user["id"], "free_trial")
    granted = store.grant_subscription(user["id"], "personal_light", call_quota=7)
    assert granted["call_quota"] == 7
    assert granted["remaining_calls"] == 7
    listed = store.list_users()
    assert isinstance(listed[0]["total_quota"], int)
    json.dumps(listed, ensure_ascii=False)


def test_slider_challenge_single_use_and_ip_binding(mysql_settings):
    store = BillingStore(mysql_settings)
    store.create_slider_challenge("challenge-1", "target", utc_now() + timedelta(minutes=5), "127.0.0.1")
    assert not store.verify_slider_challenge("challenge-1", ["target"], "127.0.0.2")
    assert not store.verify_slider_challenge("challenge-1", ["wrong"], "127.0.0.1")
    assert store.verify_slider_challenge("challenge-1", ["wrong", "target"], "127.0.0.1")
    assert not store.verify_slider_challenge("challenge-1", ["target"], "127.0.0.1")


def test_sms_rate_limit_code_expiry_and_lock(mysql_settings):
    store = BillingStore(mysql_settings)
    phone = "13900139000"
    assert store.sms_send_allowed(phone, "127.0.0.1", 2, 3) == (True, "")
    store.store_sms_code(phone, "123456", "login", utc_now() + timedelta(minutes=5), "127.0.0.1", "msg-1")
    assert store.sms_send_allowed(phone, "127.0.0.1", 2, 3)[0] is False
    for attempt in range(1, 6):
        ok, message = store.verify_sms_code(phone, "000000", "login", 900)
        assert not ok
        if attempt < 5:
            assert str(5 - attempt) in message
    assert "锁定" in message
    assert store.verify_sms_code(phone, "123456", "login", 900)[0] is False

    other = "13700137000"
    store.store_sms_code(other, "654321", "login", utc_now() + timedelta(minutes=5), "127.0.0.2")
    assert store.verify_sms_code(other, "654321", "login", 900) == (True, "")
    assert store.verify_sms_code(other, "654321", "login", 900)[0] is False


def test_failed_connection_rolls_back_and_integrity_error_is_value_error(mysql_settings):
    store = BillingStore(mysql_settings)
    store.create_user("duplicate@example.com", "password-123")
    with pytest.raises(ValueError):
        store.create_user("duplicate@example.com", "password-456")
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        assert conn.execute("SELECT 1").fetchone()[0] == 1
