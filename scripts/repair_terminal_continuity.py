#!/usr/bin/env python3
"""Preview or apply active/archive terminal mapping continuity repairs."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.terminal_continuity import repair


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "dashboard.db")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(repair(args.db.resolve(), apply=args.apply), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
