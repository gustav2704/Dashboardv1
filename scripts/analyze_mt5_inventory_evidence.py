#!/usr/bin/env python3
"""Resolve MT5 bot activity from a collector evidence ZIP.

MT5 chart profiles store the configuration of attached EAs, including the
actual MagicNumber and CustomComment. This tool joins that configuration with
BotInventory history without modifying the terminal or its catalog.
"""

from __future__ import annotations

import argparse
import csv
import re
import tempfile
import zipfile
from collections import Counter, defaultdict
from pathlib import Path


UTF16_TEXT = re.compile(rb"(?:[ -~]\x00){4,}")


def profile_fields(path: Path) -> dict[str, str]:
    strings = [chunk.decode("utf-16le", "ignore") for chunk in UTF16_TEXT.findall(path.read_bytes())]

    def get(name: str) -> str:
        prefix = f"{name}="
        return next((value[len(prefix) :].strip() for value in strings if value.startswith(prefix)), "")

    return {
        "symbol": get("symbol"),
        "name": get("name"),
        "expert_path": get("path"),
        "magic_number": get("MagicNumber") or get("MyNumber"),
        "expected_comment": get("CustomComment"),
    }


def write_csv(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-zip", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, help="Default: directory containing the ZIP")
    args = parser.parse_args()

    evidence_zip = args.evidence_zip.resolve()
    if not evidence_zip.is_file():
        parser.error(f"ZIP not found: {evidence_zip}")
    output_dir = (args.output_dir or evidence_zip.parent).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="mt5-inventory-evidence-") as temporary:
        extracted = Path(temporary)
        with zipfile.ZipFile(evidence_zip) as archive:
            archive.extractall(extracted)

        manifest = {}
        with (extracted / "manifest.csv").open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                manifest[row["archive_path"].replace("\\", "/")] = row["source_relative_path"]

        history_file = max((extracted / "inventory").glob("*_History.csv"), key=lambda path: path.name)
        with history_file.open(encoding="utf-8-sig", newline="") as stream:
            history = {
                (row["symbol"].lower(), row["magic_number"]): row
                for row in csv.DictReader(stream)
            }

        resolved: list[dict[str, str]] = []
        for profile in sorted((extracted / "chart_profiles").glob("*.chr")):
            config = profile_fields(profile)
            if not config["expert_path"] or not config["magic_number"]:
                continue
            ex5_name = config["expert_path"].replace("/", "\\").rsplit("\\", 1)[-1]
            statistics = history.get((config["symbol"].lower(), config["magic_number"]), {})
            entries = int(statistics.get("entry_deals", "0")) if statistics else 0
            orders = int(statistics.get("orders", "0")) if statistics else 0
            activity = "CON_OPERACIONES" if entries else "SOLO_ORDENES" if orders else "NUNCA_OPERO"
            archive_path = str(profile.relative_to(extracted)).replace("\\", "/")
            resolved.append(
                {
                    "profile_source": manifest.get(archive_path, archive_path),
                    "symbol": config["symbol"],
                    "ex5_name": ex5_name,
                    "magic_number": config["magic_number"],
                    "expected_comment": config["expected_comment"],
                    "activity_status": activity,
                    "entry_deals": str(entries),
                    "orders": str(orders),
                    "first_trade": statistics.get("first_trade", ""),
                    "last_trade": statistics.get("last_trade", ""),
                    "first_order": statistics.get("first_order", ""),
                    "last_order": statistics.get("last_order", ""),
                }
            )

    resolved.sort(key=lambda row: (row["symbol"], row["ex5_name"], int(row["magic_number"])))
    resolved_path = output_dir / "BotInventoryProfileResolved.csv"
    fields = [
        "profile_source", "symbol", "ex5_name", "magic_number", "expected_comment", "activity_status",
        "entry_deals", "orders", "first_trade", "last_trade", "first_order", "last_order",
    ]
    write_csv(resolved_path, resolved, fields)

    by_ex5: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in resolved:
        by_ex5[row["ex5_name"].lower()].append(row)
    duplicates = [row for values in by_ex5.values() if len(values) > 1 for row in values]
    seed_rows = [
        {"ex5_name": row["ex5_name"], "magic_number": row["magic_number"], "expected_comment": row["expected_comment"]}
        for values in by_ex5.values()
        if len(values) == 1
        for row in values
    ]
    seed_rows.sort(key=lambda row: row["ex5_name"].lower())
    seed_path = output_dir / "BotInventoryCatalogProfileSeed.csv"
    write_csv(seed_path, seed_rows, ["ex5_name", "magic_number", "expected_comment"])

    duplicates_path = output_dir / "BotInventoryProfileDuplicates.csv"
    write_csv(duplicates_path, duplicates, fields)

    summary = Counter(row["activity_status"] for row in resolved)
    print(f"Resolved charts: {len(resolved)}")
    print(" · ".join(f"{key}: {value}" for key, value in sorted(summary.items())))
    print(f"Resolved report: {resolved_path}")
    print(f"Safe catalog seed: {seed_path}")
    print(f"Duplicate EX5 instances excluded from seed: {len(duplicates)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
