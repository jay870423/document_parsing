"""Background execution for manually triggered WeWe collection tasks."""
from __future__ import annotations

import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial

from app.billing import BillingStore, CallReservation
from app.wewe_pipeline import WeWeIngestPipeline
from app.wewe_store import CollectionAlreadyRunning, WeWeStore

logger = logging.getLogger("wewe.collection")
CHARGEABLE_TASK_STATES = {"success", "partial"}
TERMINAL_TASK_STATES = CHARGEABLE_TASK_STATES | {"failed"}


def settle_wewe_call(
    billing_store: BillingStore | None,
    reservation: CallReservation | None,
    *,
    user_id: int,
    endpoint: str,
    status_code: int,
    latency_ms: int,
    successful: bool,
    meta: dict | None = None,
) -> None:
    """Settle once; a failed task releases its reservation instead of charging."""
    if billing_store is None or reservation is None:
        return
    billing_store.consume_call(
        user_id=user_id,
        api_key_id=None,
        endpoint=endpoint,
        method="POST",
        status_code=status_code,
        latency_ms=max(0, int(latency_ms)),
        meta=meta or {},
        reservation=reservation,
        call_count=1 if successful else 0,
    )


def settle_wewe_call_safely(
    billing_store: BillingStore | None,
    reservation: CallReservation | None,
    *,
    user_id: int,
    endpoint: str,
    status_code: int,
    latency_ms: int,
    successful: bool,
    meta: dict | None = None,
) -> bool:
    """Log settlement failures and leave the reservation for reconciliation."""
    try:
        settle_wewe_call(
            billing_store,
            reservation,
            user_id=user_id,
            endpoint=endpoint,
            status_code=status_code,
            latency_ms=latency_ms,
            successful=successful,
            meta=meta,
        )
    except Exception:
        details = meta or {}
        logger.exception(
            "collection settlement needs reconciliation: task=%s source=%s",
            details.get("task_id"), details.get("source_id"),
        )
        return False
    return True


def reconcile_wewe_tasks(billing_store: BillingStore | None) -> int:
    """Run task-level reconciliation without disrupting a worker/scheduler."""
    if billing_store is None:
        return 0
    try:
        return billing_store.reconcile_wewe_task_reservations()
    except Exception:
        logger.exception("collection task reconciliation failed")
        return 0


def recover_and_reconcile_wewe_tasks(
    store: WeWeStore,
    billing_store: BillingStore | None,
    source_id: int | None = None,
) -> int:
    """Recover expired leases, then immediately settle their reservations."""
    try:
        recovered = store.recover_interrupted_tasks(source_id=source_id)
    except Exception:
        logger.exception("interrupted collection recovery failed: source=%s", source_id)
        recovered = 0
    reconcile_wewe_tasks(billing_store)
    return recovered


class WeWeCollectionRunner:
    """Run collection outside the HTTP worker; MySQL owns cross-process locking."""

    def __init__(
        self,
        store: WeWeStore,
        pipeline: WeWeIngestPipeline,
        max_workers: int = 4,
        billing_store: BillingStore | None = None,
    ) -> None:
        self.store = store
        self.pipeline = pipeline
        self.billing_store = billing_store
        # A process may have crashed after task finalization but before billing.
        # Recover expired leases and reconcile all already-terminal tasks now.
        recover_and_reconcile_wewe_tasks(self.store, self.billing_store)
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
        *,
        reservation: CallReservation | None = None,
        endpoint: str = "/api/wewe/sources/{id}/collect",
    ) -> dict:
        started = time.perf_counter()
        task_id: int | None = None
        try:
            try:
                task_id = self.store.create_task(
                    user_id, source_id, kind="manual", reservation=reservation
                )
            except CollectionAlreadyRunning:
                # A live peer may recover a crashed worker's expired lock without
                # requiring a process restart. Never override a valid lock.
                if recover_and_reconcile_wewe_tasks(
                    self.store, self.billing_store, source_id=source_id
                ) != 1:
                    raise
                task_id = self.store.create_task(
                    user_id, source_id, kind="manual", reservation=reservation
                )
        except Exception as exc:
            # No task was accepted. create_task rolls back an attached reservation
            # on failure, so normal pre-task/lock failures can refund immediately.
            settle_wewe_call_safely(
                self.billing_store, reservation, user_id=user_id, endpoint=endpoint,
                status_code=getattr(exc, "status_code", 409),
                latency_ms=int((time.perf_counter() - started) * 1000), successful=False,
                meta={"source_id": source_id, "error": str(exc)},
            )
            raise

        heartbeat = self.store.task_heartbeat(task_id)
        entered = False
        try:
            # Renew while waiting in the executor queue, not only after a worker starts.
            heartbeat.__enter__()
            entered = True
            # The worker owns settlement after a successful submit.
            future = self.executor.submit(
                self._run, task_id, user_id, source_id, knowledge_base_id, limit, refresh,
                heartbeat, reservation, endpoint, started,
            )
            future.add_done_callback(partial(self._log_background_failure, task_id=task_id))
        except Exception as exc:
            if entered:
                try:
                    heartbeat.__exit__(None, None, None)
                except Exception:
                    logger.exception("failed to stop heartbeat for task %s", task_id)
            self._fail_and_settle(
                task_id, user_id, source_id, reservation, endpoint, started,
                "failed to schedule collection", exc,
            )
            raise
        return {"task_id": task_id, "source_id": source_id, "status": "running"}

    @staticmethod
    def _log_background_failure(future: Future, *, task_id: int) -> None:
        """A Future must not be the only place where an unexpected error lives."""
        try:
            error = future.exception()
        except Exception:
            logger.exception("background collection future was cancelled: task=%s", task_id)
            return
        if error is not None:
            logger.error(
                "unexpected background collection error escaped worker: task=%s",
                task_id,
                exc_info=(type(error), error, error.__traceback__),
            )

    def _settle_terminal_task(
        self,
        task_id: int,
        user_id: int,
        source_id: int,
        reservation: CallReservation | None,
        endpoint: str,
        started: float,
        result: dict | None = None,
    ) -> bool:
        """Settle from authoritative task state, never from an uncertain acknowledgement."""
        try:
            task = self.store.get_task(task_id)
        except Exception:
            logger.exception("cannot read authoritative state for task %s", task_id)
            return False
        if not task:
            logger.error("collection task %s disappeared before settlement", task_id)
            return False
        status = str(task.get("status") or "")
        if status not in TERMINAL_TASK_STATES:
            logger.warning(
                "collection task %s is not terminal; reservation remains for recovery", task_id
            )
            return False
        chargeable = status in CHARGEABLE_TASK_STATES
        details = result or {}
        return settle_wewe_call_safely(
            self.billing_store,
            reservation,
            user_id=user_id,
            endpoint=endpoint,
            status_code=200 if chargeable else 500,
            latency_ms=int((time.perf_counter() - started) * 1000),
            successful=chargeable,
            meta={
                "source_id": source_id,
                "task_id": task_id,
                "task_status": status,
                "article_count": task.get("article_count", details.get("article_count", 0)),
                "indexed_count": task.get("indexed_count", details.get("indexed_count", 0)),
            },
        )

    def _fail_and_settle(
        self,
        task_id: int,
        user_id: int,
        source_id: int,
        reservation: CallReservation | None,
        endpoint: str,
        started: float,
        error: str,
        exc: Exception,
    ) -> None:
        try:
            self.store.finish_task(task_id, "failed", error=error[:300])
        except Exception:
            logger.exception("failed to finalize collection task %s as failed", task_id)
        # If the pipeline had already committed success/partial, finish_task is a
        # no-op and this read preserves the authoritative chargeable outcome.
        self._settle_terminal_task(
            task_id, user_id, source_id, reservation, endpoint, started,
            {"status": "failed", "error": str(exc)},
        )

    def _run(
        self,
        task_id: int,
        user_id: int,
        source_id: int,
        knowledge_base_id: int,
        limit: int | None,
        refresh: bool,
        heartbeat,
        reservation: CallReservation | None,
        endpoint: str,
        started: float,
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
        except Exception as exc:
            logger.exception("background collection pipeline failed for task %s", task_id)
            self._fail_and_settle(
                task_id, user_id, source_id, reservation, endpoint, started, str(exc), exc
            )
        else:
            try:
                # Idempotent finalization also covers lightweight/custom pipelines.
                # An acknowledgement error here is NOT a pipeline failure: the
                # pipeline may already have committed a chargeable terminal state.
                self.store.finish_task(
                    task_id,
                    status=str(result.get("status") or "failed"),
                    article_count=int(result.get("article_count") or 0),
                    image_count=int(result.get("image_count") or 0),
                    indexed_count=int(result.get("indexed_count") or 0),
                    error=str(result.get("error") or ""),
                )
            except Exception:
                logger.exception("collection finish confirmation failed for task %s", task_id)
            self._settle_terminal_task(
                task_id, user_id, source_id, reservation, endpoint, started, result
            )
        finally:
            try:
                heartbeat.__exit__(None, None, None)
            except Exception:
                logger.exception("background heartbeat cleanup failed for task %s", task_id)

    def close(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=False)
