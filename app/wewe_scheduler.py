"""定时采集调度（轻量线程实现，无额外依赖）。

默认关闭（WEWE_COLLECT_ENABLED=false）。开启后按固定间隔为所有
"已订阅 + 启用" 的来源触发一次采集；单个来源失败只记录错误，不影响其他来源。
"""

from __future__ import annotations

import logging
import threading

from app.config import Settings
from app.wewe_pipeline import WeWeIngestPipeline
from app.wewe_store import WeWeStore

logger = logging.getLogger("wewe.scheduler")


class WeWeScheduler:
    def __init__(self, settings: Settings, store: WeWeStore, pipeline: WeWeIngestPipeline):
        self.settings = settings
        self.store = store
        self.pipeline = pipeline
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
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
            self.run_once()

    def run_once(self) -> list[dict]:
        results: list[dict] = []
        for source in self.store.list_subscribed_sources():
            try:
                results.append(
                    self.pipeline.collect_source(
                        user_id=int(source["user_id"]),
                        source_id=int(source["id"]),
                        knowledge_base_id=int(source.get("knowledge_base_id") or 0) or None,
                        refresh=True,
                        kind="scheduled",
                    )
                )
            except Exception as exc:
                logger.warning("scheduled collect failed for source %s: %s", source.get("id"), exc)
        return results

