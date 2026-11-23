"""定时采集调度（轻量线程实现，无额外依赖）。

默认关闭（WEWE_COLLECT_ENABLED=false）。开启后按固定间隔为所有
"已订阅 + 启用" 的来源触发一次采集；单个来源失败只记录错误，不影响其他来源。
定时任务与文档能力共用套餐额度，额度不足时不启动上游采集。
"""

from __future__ import annotations

import logging
import threading
import time

from app.billing import BillingStore, CallReservation
from app.config import Settings
from app.wewe_pipeline import WeWeIngestPipeline
from app.wewe_runner import (
    CHARGEABLE_TASK_STATES,
    TERMINAL_TASK_STATES,
    recover_and_reconcile_wewe_tasks,
    settle_wewe_call,
)
from app.wewe_store import WeWeStore

logger = logging.getLogger("wewe.scheduler")


class WeWeScheduler:
    def __init__(
        self,
        settings: Settings,
        store: WeWeStore,
        pipeline: WeWeIngestPipeline,
        billing_store: BillingStore | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.pipeline = pipeline
        self.billing_store = billing_store
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        # main.py performs its own recovery before scheduler.start(); reconcile
        # here even when periodic collection is disabled so that recovery cannot
        # leave a failed/successful task reservation waiting for the generic TTL.
        recover_and_reconcile_wewe_tasks(self.store, self.billing_store)
        if not self.settings.wewe_collect_enabled:
            return
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="wewe-collect-scheduler", daemon=True)
        self._thread.start()
        logger.info("wewe scheduler started, interval=%s minutes", self.settings.wewe_collect_interval_minutes)

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        interval = max(1, int(self.settings.wewe_collect_interval_minutes)) * 60
        while not self._stop.wait(interval):
            try:
                self.run_once()
            except Exception:
                logger.exception("unexpected scheduled collection pass failure")

    def _settle_safely(
        self,
        reservation: CallReservation | None,
        *,
        user_id: int,
        source_id: int,
        task_id: int | None,
        successful: bool,
        started: float,
        result: dict | None = None,
        error: Exception | None = None,
    ) -> bool:
        details = result or {}
        try:
            settle_wewe_call(
                self.billing_store,
                reservation,
                user_id=user_id,
                endpoint="/api/wewe/scheduler/collect",
                status_code=200 if successful else 500,
                latency_ms=int((time.perf_counter() - started) * 1000),
                successful=successful,
                meta={
                    "source_id": source_id,
                    "task_id": task_id,
                    "trigger": "scheduled",
                    "task_status": details.get("status"),
                    "article_count": details.get("article_count", 0),
                    "indexed_count": details.get("indexed_count", 0),
                    **({"error": str(error)} if error is not None else {}),
                },
            )
        except Exception:
            # Never enter the collection failure handler after a settlement
            # attempt. The original reservation remains available to the
            # idempotent terminal-task reconciler if its state is still reserved.
            logger.exception(
                "scheduled collection settlement needs reconciliation: task=%s source=%s",
                task_id, source_id,
            )
            return False
        return True

    def _settle_terminal_task(
        self,
        task_id: int,
        reservation: CallReservation | None,
        *,
        user_id: int,
        source_id: int,
        started: float,
        result: dict | None = None,
    ) -> bool:
        """Use the committed task state, not a possibly-lost finish acknowledgement."""
        try:
            task = self.store.get_task(task_id)
        except Exception:
            logger.exception("cannot read authoritative scheduled task state: task=%s", task_id)
            return False
        if not task:
            logger.error("scheduled collection task %s disappeared before settlement", task_id)
            return False
        status = str(task.get("status") or "")
        if status not in TERMINAL_TASK_STATES:
            logger.warning(
                "scheduled task %s is not terminal; reservation remains for recovery", task_id
            )
            return False
        details = dict(result or {})
        details.update({
            "status": status,
            "article_count": task.get("article_count", details.get("article_count", 0)),
            "indexed_count": task.get("indexed_count", details.get("indexed_count", 0)),
        })
        return self._settle_safely(
            reservation,
            user_id=user_id,
            source_id=source_id,
            task_id=task_id,
            successful=status in CHARGEABLE_TASK_STATES,
            started=started,
            result=details,
        )

    def _fail_and_settle(
        self,
        task_id: int | None,
        reservation: CallReservation | None,
        *,
        user_id: int,
        source_id: int,
        started: float,
        exc: Exception,
    ) -> None:
        if task_id is None:
            # Reservation/quota refusal before task creation, or a source lock
            # conflict, has no task outcome to reconcile and can refund now.
            self._settle_safely(
                reservation,
                user_id=user_id,
                source_id=source_id,
                task_id=None,
                successful=False,
                started=started,
                result={"status": "failed"},
                error=exc,
            )
            return
        try:
            self.store.finish_task(task_id, "failed", error=str(exc)[:300])
        except Exception:
            logger.exception("failed to finalize scheduled task %s as failed", task_id)
        # A pipeline may have committed success before raising. finish_task will
        # not overwrite a terminal task; authoritative state decides settlement.
        self._settle_terminal_task(
            task_id,
            reservation,
            user_id=user_id,
            source_id=source_id,
            started=started,
            result={"status": "failed"},
        )

    def _collect_source(
        self,
        source: dict,
        task_id: int,
        reservation: CallReservation | None,
        started: float,
    ) -> dict:
        user_id = int(source["user_id"])
        source_id = int(source["id"])
        with self.store.task_heartbeat(task_id):
            result = self.pipeline.collect_source(
                user_id=user_id,
                source_id=source_id,
                knowledge_base_id=int(source.get("knowledge_base_id") or 0) or None,
                refresh=True,
                kind="scheduled",
                task_id=task_id,
            )
            try:
                self.store.finish_task(
                    task_id,
                    status=str(result.get("status") or "failed"),
                    article_count=int(result.get("article_count") or 0),
                    image_count=int(result.get("image_count") or 0),
                    indexed_count=int(result.get("indexed_count") or 0),
                    error=str(result.get("error") or ""),
                )
            except Exception:
                # This second finish is only an idempotent confirmation when the
                # real pipeline already finished the task. Do not refund here or
                # route it through the pipeline failure handler.
                logger.exception("scheduled finish confirmation failed for task %s", task_id)
            self._settle_terminal_task(
                task_id,
                reservation,
                user_id=user_id,
                source_id=source_id,
                started=started,
                result=result,
            )
        return result

    def run_once(self) -> list[dict]:
        # Reconcile on every live scheduler pass so crashed peers do not need
        # this process to restart before their expired locks can be taken over.
        recover_and_reconcile_wewe_tasks(self.store, self.billing_store)
        results: list[dict] = []
        for source in self.store.list_subscribed_sources():
            task_id: int | None = None
            reservation: CallReservation | None = None
            user_id = int(source["user_id"])
            source_id = int(source["id"])
            started = time.perf_counter()
            try:
                if self.billing_store is not None:
                    owner = self.billing_store.get_user_by_id(user_id)
                    if not owner or not owner.get("is_active"):
                        results.append({"source_id": source_id, "status": "skipped", "error": "账号不可用"})
                        continue
                    if not owner.get("is_admin"):
                        # Reserve before creating a task or calling upstream/models.
                        reservation = self.billing_store.reserve_call(user_id)
                task_id = self.store.create_task(
                    user_id, source_id, kind="scheduled", reservation=reservation
                )
                result = self._collect_source(source, task_id, reservation, started)
                results.append(result)
            except PermissionError as exc:
                if task_id is None and reservation is None:
                    # Only a pre-task quota/subscription refusal is an expected
                    # skip. PermissionError from task creation or upstream must
                    # follow the failure/refund path below.
                    logger.info("wewe scheduled collection skipped: user=%s source=%s error=%s", user_id, source_id, exc)
                    results.append({"source_id": source_id, "status": "skipped", "error": str(exc)})
                    continue
                self._fail_and_settle(
                    task_id, reservation,
                    user_id=user_id, source_id=source_id, started=started, exc=exc,
                )
                logger.warning("scheduled collect failed for source %s: %s", source_id, exc)
            except Exception as exc:
                # All finish and settlement helpers are non-throwing, so one
                # broken source cannot prevent subsequent sources from running.
                self._fail_and_settle(
                    task_id, reservation,
                    user_id=user_id, source_id=source_id, started=started, exc=exc,
                )
                logger.warning("scheduled collect failed for source %s: %s", source_id, exc)
        return results
