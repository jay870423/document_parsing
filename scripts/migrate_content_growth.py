#!/usr/bin/env python3
"""One-off, idempotent content-growth schema migration; never runs at app import."""
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.content_growth_store import ContentGrowthStore


REQUIRED_TABLES = {
    "growth_profiles", "growth_own_articles", "growth_briefs",
    "growth_drafts", "growth_metrics", "growth_operations",
}


def main() -> None:
    store = ContentGrowthStore(auto_migrate=False)
    if "--check" not in sys.argv[1:]:
        store.init_db()
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT table_name AS name FROM information_schema.tables WHERE table_schema = ?",
            (store.settings.mysql_database,),
        ).fetchall()
    existing = {str(row["name"]) for row in rows}
    missing = sorted(REQUIRED_TABLES - existing)
    if missing:
        raise SystemExit(f"content-growth schema incomplete: {', '.join(missing)}")
    print(
        f"content-growth schema ready: {store.settings.mysql_database}; "
        f"tables={len(REQUIRED_TABLES)}; check={'read-only' if '--check' in sys.argv[1:] else 'migrated'}"
    )


if __name__ == "__main__":
    main()