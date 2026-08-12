#!/usr/bin/env python3
"""Dry-run or apply the EX5-based operational identity backfill."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.db import init_db  # noqa: E402
from app.expert_identity_migration import repair  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "dashboard.db")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    db_path = args.db.resolve()
    init_db(db_path)
    print(json.dumps(repair(db_path, apply=args.apply), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
