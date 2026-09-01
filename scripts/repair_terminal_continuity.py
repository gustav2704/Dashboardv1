#!/usr/bin/env python3
"""Preview or apply active/archive terminal mapping continuity repairs."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.terminal_continuity import repair, repair_same_login_server_archives


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "dashboard.db")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--same-login-server-archives", action="store_true")
    parser.add_argument("--account-login")
    args = parser.parse_args()
    action = repair_same_login_server_archives if args.same_login_server_archives else repair
    kwargs = {"apply": args.apply}
    if args.same_login_server_archives:
        kwargs["account_login"] = args.account_login
    print(json.dumps(action(args.db.resolve(), **kwargs), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
