"""套餐权益在采集源、后台任务和图片检索中的端到端约束。"""
from __future__ import annotations

import logging
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.billing import BillingStore, UserContext
from app.wewe_api import build_wewe_router
from app.wewe_runner import WeWeCollectionRunner, settle_wewe_call
from app.wewe_scheduler import WeWeScheduler
from app.wewe_store import WeWeStore

from test_wewe_api import FakeClient, FakePipeline, FakeVectorIndex


@pytest.fixture()
def entitled_api(wewe_settings):
    settings = wewe_settings.model_copy(update={
        "wewe_enabled": True,
        "wewe_base_url": "http://upstream:4000",
        "wewe_image_vector_enabled": True,
    })
    store = WeWeStore(settings)
    store.bind_upstream_feed(1, "MP_1")
    pipeline = FakePipeline()
    index = FakeVectorIndex()
    upstream = FakeClient()

    def current_user():
        return UserContext(id=1, email="u@example.com", is_admin=False, is_active=True)

    def resolve_kb(user_id, knowledge_base_id):
        return {"id": int(knowledge_base_id or 1), "name": "默认知识库"}

    app = FastAPI()
    app.include_router(build_wewe_router(
        current_user=current_user,
        store=store,
        pipeline=pipeline,
        vector_index=index,
        client=upstream,
        settings=settings,
        resolve_kb=resolve_kb,
    ))
    return TestClient(app), store, pipeline, upstream, BillingStore(settings)


def wait_for_terminal_task(client: TestClient, task_id: int) -> dict:
    for _ in range(100):
        task = next(
            item for item in client.get("/api/wewe/tasks").json()["tasks"]
            if item["id"] == task_id
        )
        if task["status"] != "running":
            return task
        time.sleep(0.02)
    raise AssertionError("collection task did not finish")


def exhaust_quota(billing: BillingStore) -> None:
    subscription = billing.get_active_subscription(1)
    billing.reserve_call(1, int(subscription["remaining_calls"]))


def test_status_exposes_shared_document_and_collection_entitlement(entitled_api):
    client, _store, _pipeline, _upstream, _billing = entitled_api

    response = client.get("/api/wewe/status")

    assert response.status_code == 200
    entitlement = response.json()["plan_entitlement"]
    assert entitlement["active"] is True
    assert entitlement["plan_code"] == "free_trial"
    assert entitlement["call_quota"] == 3
    assert entitlement["remaining_calls"] == 3
    assert entitlement["can_collect"] is True
    assert entitlement["shared_quota"] is True
    assert entitlement["quota_scope"] == "documents_and_collection"


def test_plan_catalog_describes_collection_capabilities_and_refund_rule(entitled_api):
    _client, _store, _pipeline, _upstream, billing = entitled_api

    plans = billing.list_plans()

    assert plans
    for plan in plans:
        assert plan["collection_enabled"] is True
        assert any("采集源" in item for item in plan["capabilities"])
        assert plan["quota_scope"] == "documents_and_collection"
        assert any("失败" in item and "退回" in item for item in plan["billing_rules"])


def test_manual_collection_is_rejected_before_task_or_pipeline_when_quota_is_empty(entitled_api):
    client, store, pipeline, _upstream, billing = entitled_api
    source_id = store.add_source(
        user_id=1, knowledge_base_id=1, source_type="mp", name="测试来源", upstream_feed_id="MP_1"
    )
    exhaust_quota(billing)

    response = client.post(f"/api/wewe/sources/{source_id}/collect", json={})

    assert response.status_code == 402
    assert "次数" in response.json()["detail"]
    assert pipeline.calls == []
    assert store.list_tasks(1) == []


def test_failed_collection_refunds_and_success_consumes_exactly_one_call(entitled_api, monkeypatch):
    client, store, pipeline, _upstream, billing = entitled_api
    source_id = store.add_source(
        user_id=1, knowledge_base_id=1, source_type="mp", name="测试来源", upstream_feed_id="MP_1"
    )

    def failed_collect(**kwargs):
        return {
            "task_id": kwargs["task_id"], "status": "failed", "article_count": 0,
            "image_count": 0, "indexed_count": 0, "error": "upstream failed",
        }

    monkeypatch.setattr(pipeline, "collect_source", failed_collect)
    failed_response = client.post(f"/api/wewe/sources/{source_id}/collect", json={})
    assert failed_response.status_code == 202
    assert wait_for_terminal_task(client, failed_response.json()["task_id"])["status"] == "failed"
    assert billing.get_active_subscription(1)["used_calls"] == 0
    assert billing.get_active_subscription(1)["remaining_calls"] == 3

    def successful_collect(**kwargs):
        return {
            "task_id": kwargs["task_id"], "status": "success", "article_count": 1,
            "image_count": 0, "indexed_count": 1, "error": "",
        }

    monkeypatch.setattr(pipeline, "collect_source", successful_collect)
    success_response = client.post(f"/api/wewe/sources/{source_id}/collect", json={})
    assert success_response.status_code == 202
    assert wait_for_terminal_task(client, success_response.json()["task_id"])["status"] == "success"
    assert billing.get_active_subscription(1)["used_calls"] == 1
    assert billing.get_active_subscription(1)["remaining_calls"] == 2


def test_subscription_quota_is_reserved_before_upstream_lookup(entitled_api, monkeypatch):
    client, store, _pipeline, upstream, billing = entitled_api
    exhaust_quota(billing)
    upstream_called = False

    def should_not_call_upstream(_share_link):
        nonlocal upstream_called
        upstream_called = True
        raise AssertionError("upstream must not be called without quota")

    monkeypatch.setattr(upstream, "get_mp_info", should_not_call_upstream)

    response = client.post("/api/wewe/subscriptions", json={
        "share_link": "https://mp.weixin.qq.com/s/article-token", "knowledge_base_id": 1,
    })

    assert response.status_code == 402
    assert upstream_called is False
    assert store.list_sources(1) == []


def test_image_search_failure_refunds_and_success_consumes_one_call(entitled_api, monkeypatch):
    client, _store, pipeline, _upstream, billing = entitled_api

    def failing_embedding(_query):
        raise RuntimeError("embedding unavailable")

    monkeypatch.setattr(pipeline.image_embedding, "embed_text", failing_embedding)
    failed = client.post("/api/wewe/images/search", json={"query": "曲线"})
    assert failed.status_code == 502
    assert billing.get_active_subscription(1)["remaining_calls"] == 3

    monkeypatch.setattr(pipeline.image_embedding, "embed_text", lambda _query: [0.1, 0.2])
    succeeded = client.post("/api/wewe/images/search", json={"query": "曲线"})
    assert succeeded.status_code == 200
    assert billing.get_active_subscription(1)["used_calls"] == 1
    assert billing.get_active_subscription(1)["remaining_calls"] == 2


def test_login_and_new_source_require_an_active_subscription(entitled_api):
    client, store, _pipeline, _upstream, billing = entitled_api
    with billing.connect() as conn:
        conn.execute("UPDATE subscriptions SET status = 'expired' WHERE user_id = 1")

    login = client.post("/api/wewe/accounts/login-sessions", json={})
    source = client.post("/api/wewe/sources", json={
        "source_type": "web", "target_url": "https://example.com/feed.xml",
    })

    assert login.status_code == 402
    assert source.status_code == 402
    assert store.list_sources(1) == []


def test_scheduler_skips_exhausted_user_without_starting_pipeline(wewe_settings):
    settings = wewe_settings.model_copy(update={
        "wewe_enabled": True,
        "wewe_scheduler_interval_seconds": 0,
    })
    store = WeWeStore(settings)
    source_id = store.add_source(
        user_id=1, knowledge_base_id=1, source_type="web", name="自动采集来源",
        target_url="https://example.com/feed.xml", subscribed=True,
    )
    pipeline = FakePipeline()
    billing = BillingStore(settings)
    exhaust_quota(billing)

    results = WeWeScheduler(settings, store, pipeline, billing_store=billing).run_once()

    assert results == [{"source_id": source_id, "status": "skipped", "error": "套餐剩余调用次数不足"}]
    assert pipeline.calls == []
    assert store.list_tasks(1) == []
    assert billing.get_active_subscription(1)["remaining_calls"] == 0


def test_scheduler_does_not_refund_after_a_successful_settlement_write_raises(
    wewe_settings, monkeypatch
):
    settings = wewe_settings.model_copy(update={"wewe_enabled": True})
    store = WeWeStore(settings)
    source_id = store.add_source(
        user_id=1, knowledge_base_id=1, source_type="web", name="自动采集来源",
        target_url="https://example.com/feed.xml", subscribed=True,
    )
    pipeline = FakePipeline()
    settlements = []

    def flaky_settlement(*_args, **kwargs):
        successful = kwargs["successful"]
        settlements.append(successful)
        if successful:
            raise RuntimeError("billing acknowledgement lost after commit")

    monkeypatch.setattr("app.wewe_scheduler.settle_wewe_call", flaky_settlement)
    results = WeWeScheduler(settings, store, pipeline, billing_store=None).run_once()

    assert len(pipeline.calls) == 1
    assert settlements == [True], "a settlement error must not trigger a second, refund settlement"
    assert pipeline.calls[0]["source_id"] == source_id
    assert results[0]["status"] == "success"
    assert store.list_tasks(1)[0]["status"] == "success"


def test_scheduler_upstream_permission_error_fails_task_and_refunds_immediately(wewe_settings):
    settings = wewe_settings.model_copy(update={"wewe_enabled": True})
    store = WeWeStore(settings)
    store.add_source(
        user_id=1, knowledge_base_id=1, source_type="web", name="自动采集来源",
        target_url="https://example.com/feed.xml", subscribed=True,
    )

    class DeniedPipeline(FakePipeline):
        def collect_source(self, **kwargs):
            super().collect_source(**kwargs)
            raise PermissionError("upstream denied")

    pipeline = DeniedPipeline()
    billing = BillingStore(settings)
    before = billing.get_active_subscription(1)["remaining_calls"]
    results = WeWeScheduler(settings, store, pipeline, billing_store=billing).run_once()

    assert results == []
    assert len(pipeline.calls) == 1
    assert store.list_tasks(1)[0]["status"] == "failed"
    assert "upstream denied" in store.list_tasks(1)[0]["error"]
    assert billing.get_active_subscription(1)["remaining_calls"] == before
    assert billing.get_active_subscription(1)["used_calls"] == 0


def get_reservation(billing: BillingStore, reservation_id: str) -> dict:
    with billing.connect() as conn:
        row = conn.execute(
            "SELECT * FROM call_reservations WHERE reservation_id = ?",
            (reservation_id,),
        ).fetchone()
    assert row is not None
    return dict(row)


def test_active_task_reservation_survives_generic_ttl_and_success_is_consumed(wewe_settings):
    billing = BillingStore(wewe_settings)
    store = WeWeStore(wewe_settings, worker_id="active-long-task")
    source_id = store.add_source(
        1, 1, "web", "长任务", target_url="https://example.com/long.xml", subscribed=False,
    )
    reservation = billing.reserve_call(1)
    task_id = store.create_task(1, source_id, reservation=reservation)
    with billing.connect() as conn:
        conn.execute(
            "UPDATE call_reservations SET created_at = '2000-01-01T00:00:00+00:00' "
            "WHERE reservation_id = ?",
            (reservation.reservation_id,),
        )

    assert billing.reconcile_abandoned_reservations(older_than_seconds=0) == 0
    assert next(task for task in store.list_tasks(1) if task["id"] == task_id)["status"] == "running"
    assert get_reservation(billing, reservation.reservation_id)["state"] == "reserved"

    assert store.finish_task(task_id, "success") is True
    assert billing.reconcile_wewe_task_reservations() == 1
    settled = get_reservation(billing, reservation.reservation_id)
    assert settled["state"] == "succeeded"
    assert settled["resource_type"] == "wewe_collect_task"
    assert settled["resource_id"] == str(task_id)
    assert billing.get_active_subscription(1)["used_calls"] == 1
    assert billing.get_active_subscription(1)["remaining_calls"] == 2


def test_crashed_running_task_is_refunded_during_scheduler_recovery(wewe_settings):
    settings = wewe_settings.model_copy(update={"wewe_collect_enabled": True})
    billing = BillingStore(settings)
    crashed = WeWeStore(settings, worker_id="crashed-before-finish", lease_seconds=30)
    source_id = crashed.add_source(
        1, 1, "web", "崩溃任务", target_url="https://example.com/crash.xml", subscribed=False,
    )
    reservation = billing.reserve_call(1)
    task_id = crashed.create_task(1, source_id, reservation=reservation)
    with crashed.connect() as conn:
        conn.execute(
            "UPDATE wewe_collect_tasks SET lease_expires_at = '2000-01-01T00:00:00+00:00' "
            "WHERE id = ?",
            (task_id,),
        )

    recovery = WeWeStore(settings, worker_id="recovery-worker", lease_seconds=30)
    assert WeWeScheduler(settings, recovery, FakePipeline(), billing_store=billing).run_once() == []

    task = next(item for item in recovery.list_tasks(1) if item["id"] == task_id)
    assert task["status"] == "failed"
    assert get_reservation(billing, reservation.reservation_id)["state"] == "failed"
    assert billing.get_active_subscription(1)["used_calls"] == 0
    assert billing.get_active_subscription(1)["remaining_calls"] == 3


def test_terminal_success_before_crash_is_consumed_on_runner_startup(wewe_settings):
    billing = BillingStore(wewe_settings)
    store = WeWeStore(wewe_settings, worker_id="crashed-after-success")
    source_id = store.add_source(
        1, 1, "web", "崩溃前已成功", target_url="https://example.com/success.xml", subscribed=False,
    )
    reservation = billing.reserve_call(1)
    task_id = store.create_task(1, source_id, reservation=reservation)
    assert store.finish_task(task_id, "success", article_count=1, indexed_count=1) is True

    runner = WeWeCollectionRunner(store, FakePipeline(), billing_store=billing)
    runner.close()

    assert get_reservation(billing, reservation.reservation_id)["state"] == "succeeded"
    assert billing.get_active_subscription(1)["used_calls"] == 1
    assert billing.get_active_subscription(1)["remaining_calls"] == 2


def test_runner_finish_confirmation_error_does_not_refund_terminal_success(
    wewe_settings, monkeypatch
):
    billing = BillingStore(wewe_settings)
    store = WeWeStore(wewe_settings, worker_id="finish-confirmation")
    source_id = store.add_source(
        1, 1, "web", "确认异常", target_url="https://example.com/confirm.xml", subscribed=False,
    )
    original_finish = store.finish_task
    finish_calls = 0

    def flaky_finish(*args, **kwargs):
        nonlocal finish_calls
        finish_calls += 1
        if finish_calls == 2:
            raise RuntimeError("finish acknowledgement lost after commit")
        return original_finish(*args, **kwargs)

    class AlreadyFinishedPipeline:
        def collect_source(self, **kwargs):
            store.finish_task(kwargs["task_id"], "success", article_count=1, indexed_count=1)
            return {"status": "success", "article_count": 1, "indexed_count": 1}

    monkeypatch.setattr(store, "finish_task", flaky_finish)
    runner = WeWeCollectionRunner(store, AlreadyFinishedPipeline(), billing_store=billing)
    reservation = billing.reserve_call(1)
    accepted = runner.start(1, source_id, 1, None, True, reservation=reservation)
    runner.close()

    task = next(item for item in store.list_tasks(1) if item["id"] == accepted["task_id"])
    assert task["status"] == "success"
    assert finish_calls == 2, "a confirmation error must not enter the pipeline failure handler"
    assert get_reservation(billing, reservation.reservation_id)["state"] == "succeeded"
    assert billing.get_active_subscription(1)["used_calls"] == 1
    assert billing.get_active_subscription(1)["remaining_calls"] == 2


def test_runner_logs_background_settlement_error_and_leaves_work_for_reconciliation(
    wewe_settings, monkeypatch, caplog
):
    billing = BillingStore(wewe_settings)
    store = WeWeStore(wewe_settings, worker_id="settlement-logging")
    source_id = store.add_source(
        1, 1, "web", "后台结算异常", target_url="https://example.com/settle.xml", subscribed=False,
    )
    original_consume = billing.consume_call

    def fail_consume(*_args, **_kwargs):
        raise RuntimeError("billing unavailable")

    monkeypatch.setattr(billing, "consume_call", fail_consume)
    runner = WeWeCollectionRunner(store, FakePipeline(), billing_store=billing)
    reservation = billing.reserve_call(1)
    accepted = runner.start(1, source_id, 1, None, True, reservation=reservation)
    with caplog.at_level(logging.ERROR, logger="wewe.collection"):
        runner.close()

    assert str(accepted["task_id"]) in caplog.text
    assert "settlement" in caplog.text.lower()
    assert get_reservation(billing, reservation.reservation_id)["state"] == "reserved"
    monkeypatch.setattr(billing, "consume_call", original_consume)
    assert billing.reconcile_wewe_task_reservations() == 1
    assert get_reservation(billing, reservation.reservation_id)["state"] == "succeeded"


def test_scheduler_settlement_failure_does_not_block_later_sources(wewe_settings, monkeypatch):
    settings = wewe_settings.model_copy(update={"wewe_collect_enabled": True})
    billing = BillingStore(settings)
    store = WeWeStore(settings, worker_id="scheduler-isolation")
    first = store.add_source(
        1, 1, "web", "第一个来源", target_url="https://example.com/first.xml", subscribed=True,
    )
    second = store.add_source(
        1, 1, "web", "第二个来源", target_url="https://example.com/second.xml", subscribed=True,
    )
    sources = store.list_subscribed_sources()
    sources.sort(key=lambda source: int(source["id"]))
    monkeypatch.setattr(store, "list_subscribed_sources", lambda: sources)

    class Pipeline:
        def __init__(self):
            self.calls = []

        def collect_source(self, **kwargs):
            self.calls.append(kwargs["source_id"])
            if kwargs["source_id"] == first:
                raise RuntimeError("first source failed")
            return {"status": "success", "source_id": second, "article_count": 1}

    def flaky_settlement(*args, **kwargs):
        if kwargs["meta"]["source_id"] == first:
            raise RuntimeError("first settlement unavailable")
        return settle_wewe_call(*args, **kwargs)

    monkeypatch.setattr("app.wewe_scheduler.settle_wewe_call", flaky_settlement)
    pipeline = Pipeline()
    results = WeWeScheduler(settings, store, pipeline, billing_store=billing).run_once()

    assert pipeline.calls == [first, second]
    assert [result["source_id"] for result in results] == [second]
    tasks = store.list_tasks(1)
    assert len(tasks) == 2
    assert {task["status"] for task in tasks} == {"failed", "success"}
    assert billing.reconcile_wewe_task_reservations() == 1
    assert billing.get_active_subscription(1)["used_calls"] == 1
    assert billing.get_active_subscription(1)["remaining_calls"] == 2
