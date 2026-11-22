from __future__ import annotations

import threading
import time

import pytest

from app.wewe_client import FeedItem
from app.wewe_scheduler import WeWeScheduler
from app.wewe_runner import WeWeCollectionRunner
from app.wewe_store import WeWeStore
from test_wewe_pipeline import _add_source, _pipeline


def get_task(store: WeWeStore, task_id: int):
    return next(item for item in store.list_tasks(1) if int(item["id"]) == task_id)


def test_recovery_preserves_other_workers_live_task(wewe_settings):
    owner = WeWeStore(wewe_settings, worker_id="worker-a", lease_seconds=30)
    other = WeWeStore(wewe_settings, worker_id="worker-b", lease_seconds=30)
    source_id = _add_source(owner)
    task_id = owner.create_task(1, source_id=source_id)

    assert other.recover_interrupted_tasks() == 0
    task = get_task(owner, task_id)
    assert task["status"] == "running"
    assert task["worker_id"] == "worker-a"


def test_heartbeat_context_prevents_expiry_and_stops_cleanly(wewe_settings):
    owner = WeWeStore(wewe_settings, worker_id="worker-heartbeat", lease_seconds=0.3)
    other = WeWeStore(wewe_settings, worker_id="worker-other", lease_seconds=30)
    source_id = _add_source(owner)
    task_id = owner.create_task(1, source_id=source_id)

    with owner.task_heartbeat(task_id):
        time.sleep(0.55)
        assert other.recover_interrupted_tasks() == 0
        assert get_task(owner, task_id)["status"] == "running"

    owner.finish_task(task_id, "success", 0, 0, 0, "")
    assert get_task(owner, task_id)["status"] == "success"


def test_only_expired_lease_can_be_recovered(wewe_settings):
    owner = WeWeStore(wewe_settings, worker_id="worker-dead", lease_seconds=30)
    other = WeWeStore(wewe_settings, worker_id="worker-recovery", lease_seconds=30)
    source_id = _add_source(owner)
    task_id = owner.create_task(1, source_id=source_id)
    with owner.connect() as conn:
        conn.execute(
            "UPDATE wewe_collect_tasks SET lease_expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", task_id),
        )

    assert other.recover_interrupted_tasks() == 1
    task = get_task(other, task_id)
    assert task["status"] == "failed"
    assert "lease" in task["error"].lower()
    assert other.recover_interrupted_tasks() == 0


def test_existing_task_table_receives_lease_columns(wewe_settings):
    original = WeWeStore(wewe_settings, worker_id="worker-schema")
    with original.connect() as conn:
        conn.execute(
            """ALTER TABLE wewe_collect_tasks
               DROP COLUMN worker_id, DROP COLUMN heartbeat_at, DROP COLUMN lease_expires_at"""
        )

    upgraded = WeWeStore(wewe_settings, worker_id="worker-upgraded")
    with upgraded.connect() as conn:
        rows = conn.execute(
            """SELECT column_name AS name FROM information_schema.columns
               WHERE table_schema = ? AND table_name = 'wewe_collect_tasks'""",
            (wewe_settings.mysql_database,),
        ).fetchall()
    columns = {row["name"] for row in rows}
    assert {"worker_id", "heartbeat_at", "lease_expires_at"} <= columns
    source_id = _add_source(upgraded)
    task_id = upgraded.create_task(1, source_id)
    assert get_task(upgraded, task_id)["worker_id"] == "worker-upgraded"

def test_heartbeat_exposes_lease_loss_to_collection(wewe_settings):
    owner = WeWeStore(wewe_settings, worker_id="worker-lost", lease_seconds=0.2)
    other = WeWeStore(wewe_settings, worker_id="worker-recover", lease_seconds=30)
    source_id = _add_source(owner)
    task_id = owner.create_task(1, source_id=source_id)

    with owner.task_heartbeat(task_id) as lease:
        with owner.connect() as conn:
            conn.execute(
                "UPDATE wewe_collect_tasks SET lease_expires_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00+00:00", task_id),
            )
        assert other.recover_interrupted_tasks() == 1
        time.sleep(0.3)
        assert lease.lost.is_set()
        with pytest.raises(RuntimeError, match="lease"):
            owner.assert_task_lease(task_id)


def test_recovered_worker_cannot_publish_stale_article(wewe_settings):
    owner = WeWeStore(wewe_settings, worker_id="worker-old", lease_seconds=30)
    other = WeWeStore(wewe_settings, worker_id="worker-new", lease_seconds=30)
    source_id = _add_source(owner)
    article_id, _ = owner.upsert_article(
        user_id=1,
        source_id=source_id,
        knowledge_base_id=1,
        upstream_article_id="fenced-article",
        title="published",
    )
    task_id = owner.create_task(1, source_id=source_id)
    with owner.connect() as conn:
        conn.execute(
            "UPDATE wewe_collect_tasks SET lease_expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", task_id),
        )
    assert other.recover_interrupted_tasks() == 1

    with pytest.raises(RuntimeError, match="lease"):
        owner.update_article(article_id, title="stale", task_id=task_id)
    assert owner.get_article(1, article_id)["title"] == "published"


def test_pipeline_treats_rejected_task_finalization_as_failure(tmp_path, wewe_settings, monkeypatch):
    store = WeWeStore(wewe_settings, worker_id="worker-finish")
    pipeline, _index, _vision, _image_embedding, _session = _pipeline(
        tmp_path,
        store,
        [FeedItem(
            article_id="finish-guard",
            title="title",
            url="https://example.com/finish-guard",
            content_html="<p>valid article body</p>",
            published_at="",
        )],
    )
    source_id = _add_source(store)
    monkeypatch.setattr(store, "finish_task", lambda *args, **kwargs: False)

    result = pipeline.collect_source(user_id=1, source_id=source_id)
    assert result["status"] == "failed"
    assert "lease" in result["error"].lower()


def test_live_scheduler_recovers_expired_peer_without_restart(wewe_settings):
    crashed = WeWeStore(wewe_settings, worker_id="worker-crashed", lease_seconds=30)
    live = WeWeStore(wewe_settings, worker_id="worker-live", lease_seconds=30)
    source_id = _add_source(crashed)
    old_task_id = crashed.create_task(1, source_id=source_id, kind="scheduled")
    with crashed.connect() as conn:
        conn.execute(
            "UPDATE wewe_collect_tasks SET lease_expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", old_task_id),
        )

    class Pipeline:
        def collect_source(self, **kwargs):
            return {"status": "success", "article_count": 0, "image_count": 0, "indexed_count": 0}

    settings = wewe_settings.model_copy(update={"wewe_collect_enabled": True})
    results = WeWeScheduler(settings, live, Pipeline()).run_once()
    assert [item["status"] for item in results] == ["success"]
    tasks = live.list_tasks(1)
    assert len(tasks) == 2
    assert get_task(live, old_task_id)["status"] == "failed"
    assert next(task for task in tasks if task["id"] != old_task_id)["status"] == "success"

def test_heartbeat_database_outage_eventually_cancels_collection(wewe_settings, monkeypatch):
    owner = WeWeStore(wewe_settings, worker_id="worker-db-outage", lease_seconds=0.15)
    source_id = _add_source(owner)
    task_id = owner.create_task(1, source_id=source_id)

    with owner.task_heartbeat(task_id) as lease:
        def fail_heartbeat(_task_id):
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(owner, "heartbeat_task", fail_heartbeat)
        time.sleep(0.35)
        assert lease.lost.is_set()
        with pytest.raises(RuntimeError, match="lease"):
            owner.assert_task_lease(task_id)


def test_manual_runner_recovers_expired_peer_without_restart(wewe_settings):
    crashed = WeWeStore(wewe_settings, worker_id="worker-manual-crashed", lease_seconds=30)
    live = WeWeStore(wewe_settings, worker_id="worker-manual-live", lease_seconds=30)
    source_id = _add_source(crashed)
    old_task_id = crashed.create_task(1, source_id=source_id, kind="manual")
    with crashed.connect() as conn:
        conn.execute(
            "UPDATE wewe_collect_tasks SET lease_expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", old_task_id),
        )

    class Pipeline:
        def collect_source(self, **kwargs):
            return {"status": "success", "article_count": 0, "image_count": 0, "indexed_count": 0}

    runner = WeWeCollectionRunner(live, Pipeline(), max_workers=1)
    try:
        accepted = runner.start(1, source_id, 1, None, True)
        assert accepted["status"] == "running"
    finally:
        runner.close()
    tasks = live.list_tasks(1)
    assert len(tasks) == 2
    assert get_task(live, old_task_id)["status"] == "failed"
    assert next(task for task in tasks if task["id"] != old_task_id)["status"] == "success"


def test_external_write_guard_blocks_recovery_until_old_write_finishes(wewe_settings):
    owner = WeWeStore(wewe_settings, worker_id="worker-old-vector", lease_seconds=0.15)
    other = WeWeStore(wewe_settings, worker_id="worker-new-vector", lease_seconds=30)
    source_id = _add_source(owner)
    task_id = owner.create_task(1, source_id=source_id)
    entered = threading.Event()
    release = threading.Event()
    events = []
    errors = []
    recovered = []

    def old_external_write():
        try:
            with owner.task_write_guard(task_id):
                entered.set()
                assert release.wait(timeout=2)
                events.append("old-vector-write-complete")
        except Exception as exc:  # surface thread failures in the main assertion
            errors.append(exc)
            entered.set()

    def recover_expired():
        try:
            recovered.append(other.recover_interrupted_tasks(source_id=source_id))
            events.append("recovery-complete")
        except Exception as exc:
            errors.append(exc)

    old_thread = threading.Thread(target=old_external_write)
    old_thread.start()
    assert entered.wait(timeout=2)
    time.sleep(0.25)  # lease expires while the external write holds the task row
    recovery_thread = threading.Thread(target=recover_expired)
    recovery_thread.start()
    time.sleep(0.15)
    assert recovery_thread.is_alive(), "recovery overtook the in-flight external write"
    release.set()
    old_thread.join(timeout=2)
    recovery_thread.join(timeout=2)
    assert not old_thread.is_alive() and not recovery_thread.is_alive()
    assert not errors
    assert recovered == [1]
    assert events == ["old-vector-write-complete", "recovery-complete"]


def test_manual_runner_heartbeats_while_task_waits_in_executor_queue(wewe_settings):
    store = WeWeStore(wewe_settings, worker_id="queued-heartbeat", lease_seconds=0.2)
    first_source = store.add_source(1, 1, "mp", "占位来源", upstream_feed_id="MP_BUSY")
    queued_source = store.add_source(1, 1, "mp", "排队来源", upstream_feed_id="MP_QUEUED")
    started = threading.Event()
    release = threading.Event()

    class Pipeline:
        def collect_source(self, **kwargs):
            if kwargs["source_id"] == first_source:
                started.set()
                assert release.wait(timeout=3)
            return {"status": "success", "article_count": 0, "image_count": 0, "indexed_count": 0}

    runner = WeWeCollectionRunner(store, Pipeline(), max_workers=1)
    try:
        first = runner.start(1, first_source, 1, None, True)
        assert started.wait(timeout=2)
        queued = runner.start(1, queued_source, 1, None, True)
        time.sleep(0.45)
        assert get_task(store, queued["task_id"])["status"] == "running"
        assert store.recover_interrupted_tasks(source_id=queued_source) == 0
        assert get_task(store, first["task_id"])["status"] == "running"
    finally:
        release.set()
        runner.close()
    assert get_task(store, queued["task_id"])["status"] == "success"
