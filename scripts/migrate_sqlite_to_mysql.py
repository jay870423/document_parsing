#!/usr/bin/env python3
"""Offline SQLite -> MySQL migration; dry-run is the default."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", help="SQLite source; defaults to SQLITE_SOURCE_PATH")
    parser.add_argument("--env-file", help="MySQL settings env file; defaults to APP_ENV_FILE")
    parser.add_argument("--apply", action="store_true", help="Apply migration; without this flag only dry-run")
    parser.add_argument("--resume", action="store_true", help="Allow a non-empty target; verify every source row")
    parser.add_argument("--report", help="Optional JSON verification report path")
    args = parser.parse_args()
    if args.resume and not args.apply:
        parser.error("--resume requires --apply")
    if args.env_file:
        os.environ["APP_ENV_FILE"] = str(Path(args.env_file).resolve())

    from app.config import get_settings
    from app.sqlite_migration import MigrationError, migrate_sqlite

    settings = get_settings()
    source = args.sqlite or settings.sqlite_source_path
    try:
        report = migrate_sqlite(source, settings, apply=args.apply, resume=args.resume)
    except MigrationError as exc:
        print(f"MIGRATION FAILED: {exc}", file=sys.stderr)
        return 1
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.report:
        target = Path(args.report).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(encoded, encoding="utf-8")
        temporary.replace(target)
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
