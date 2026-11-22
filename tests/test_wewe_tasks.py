"""Persistent WeWe collection task locking and crash recovery."""
from __future__ import annotations

import pytest

from app.wewe_store import WeWeStore


def test_running_task_is_unique_per_source_and_released_on_finish(wewe_settings):
    store = WeWeStore(wewe_settings)
    first = store.add_source(1, 1, "mp", "来源一", upstream_feed_id="MP_TASK_1")
    second = store.add_source(1, 1, "mp", "来源二", upstream_feed_id="MP_TASK_2")

    task_id = store.create_task(1, first)
    with pytest.raises(Exception, match="already running"):
        store.create_task(1, first)

    # A different source must not be blocked by the first source's task.
    other_task = store.create_task(1, second, kind="scheduled")
    store.finish_task(other_task, "success")
    store.finish_task(task_id, "success")

    assert store.create_task(1, first) > task_id


def test_interrupted_task_recovery_releases_source(wewe_settings):
    store = WeWeStore(wewe_settings)
    source_id = store.add_source(1, 1, "mp", "来源", upstream_feed_id="MP_RECOVER")
    task_id = store.create_task(1, source_id)
    with store.connect() as conn:
        conn.execute(
            "UPDATE wewe_collect_tasks SET lease_expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", task_id),
        )

    assert store.recover_interrupted_tasks() == 1
    task = next(item for item in store.list_tasks(1) if item["id"] == task_id)
    assert task["status"] == "failed"
    assert task["finished_at"]
    assert "lease" in task["error"]
    assert store.create_task(1, source_id) > task_id
