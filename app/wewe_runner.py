"""Background execution for manually triggered WeWe collection tasks."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from app.wewe_pipeline import WeWeIngestPipeline
from app.wewe_store import CollectionAlreadyRunning, WeWeStore

logger = logging.getLogger("wewe.collection")


class WeWeCollectionRunner:
    """Run collection outside the HTTP worker; MySQL owns cross-process locking."""

    def __init__(self, store: WeWeStore, pipeline: WeWeIngestPipeline, max_workers: int = 4):
        self.store = store
        self.pipeline = pipeline
        self.executor = ThreadPoolExecutor(
            max_workers=max(1, int(max_workers)), thread_name_prefix="wewe-collect"
        )

    def start(
        self,
        user_id: int,
        source_id: int,
        knowledge_base_id: int,
        limit: int | None,
        refresh: bool,
    ) -> dict:
        try:
            task_id = self.store.create_task(user_id, source_id, kind="manual")
        except CollectionAlreadyRunning:
            # A live peer may recover a crashed worker's expired lock without
            # requiring a process restart. Never override a valid lock.
            if self.store.recover_interrupted_tasks(source_id=source_id) != 1:
                raise
            task_id = self.store.create_task(user_id, source_id, kind="manual")
        heartbeat = self.store.task_heartbeat(task_id)
        entered = False
        try:
            # Renew while waiting in the executor queue, not only after a worker starts.
            heartbeat.__enter__()
            entered = True
            self.executor.submit(
                self._run, task_id, user_id, source_id, knowledge_base_id, limit, refresh, heartbeat
            )
        except Exception:
            if entered:
                heartbeat.__exit__(None, None, None)
            self.store.finish_task(task_id, "failed", error="failed to schedule collection")
            raise
        return {"task_id": task_id, "source_id": source_id, "status": "running"}

    def _run(
        self,
        task_id: int,
        user_id: int,
        source_id: int,
        knowledge_base_id: int,
        limit: int | None,
        refresh: bool,
        heartbeat,
    ) -> None:
        try:
            # The queue heartbeat may have observed a real lease loss while waiting.
            # Revalidate before invoking even lightweight/custom pipeline implementations.
            self.store.assert_task_lease(task_id)
            result = self.pipeline.collect_source(
                user_id=user_id,
                source_id=source_id,
                knowledge_base_id=knowledge_base_id,
                limit=limit,
                refresh=refresh,
                task_id=task_id,
            )
            # Idempotent finalization also covers lightweight/custom pipelines.
            self.store.finish_task(
                task_id,
                status=str(result.get("status") or "failed"),
                article_count=int(result.get("article_count") or 0),
                image_count=int(result.get("image_count") or 0),
                indexed_count=int(result.get("indexed_count") or 0),
                error=str(result.get("error") or ""),
            )
        except Exception as exc:
            logger.exception("background collection failed for task %s", task_id)
            self.store.finish_task(task_id, "failed", error=str(exc)[:300])
        finally:
            heartbeat.__exit__(None, None, None)

    def close(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=False)
