#!/usr/bin/env python3
"""Build evidence-backed Magic Number candidates for BotInventoryReport.

The tool never changes a terminal or its catalog.  It reads the inventory CSV
and searches the terminal's local MQ5 sources and saved SET files.  A source
default is useful evidence, but is deliberately not treated as proof of the
value configured on a live chart.
"""

from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path


MAGIC_INPUT = re.compile(
    r"^\s*(?:input|extern)\s+(?:long|int|uint|ulong)\s+([A-Za-z_]\w*)\s*=\s*(-?\d+)\b",
    re.IGNORECASE | re.MULTILINE,
)
SET_MAGIC = re.compile(r"^\s*([A-Za-z_]\w*magic\w*)\s*=\s*(-?\d+)\s*$", re.IGNORECASE | re.MULTILINE)


def read_text(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeError:
            continue
    return ""


def source_magic(path: Path) -> tuple[str, str] | None:
    for variable, value in MAGIC_INPUT.findall(read_text(path)):
        if "magic" in variable.lower():
            return variable, value
    return None


def set_magic(path: Path) -> tuple[str, str] | None:
    matches = SET_MAGIC.findall(read_text(path))
    return matches[0] if len(matches) == 1 else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True, help="BotInventory_<...>.csv (main report)")
    parser.add_argument("--terminal-data-dir", type=Path, required=True, help="MT5 Data Folder, containing MQL5")
    parser.add_argument("--output", type=Path, help="CSV destination (default: next to inventory)")
    args = parser.parse_args()

    inventory = args.inventory.resolve()
    data_dir = args.terminal_data_dir.resolve()
    if not inventory.is_file():
        parser.error(f"inventory does not exist: {inventory}")
    experts = data_dir / "MQL5" / "Experts"
    if not experts.is_dir():
        parser.error(f"not an MT5 Data Folder (MQL5/Experts missing): {data_dir}")

    output = args.output or inventory.with_name(inventory.stem + "_MagicCandidates.csv")
    source_index = {path.stem.lower(): path for path in experts.rglob("*.mq5")}
    set_files = list((data_dir / "MQL5").rglob("*.set"))

    with inventory.open(newline="", encoding="utf-8-sig") as stream:
        unknown = [
            row
            for row in csv.DictReader(stream)
            if row.get("attached") == "SI" and row.get("trade_status") == "MN_DESCONOCIDO"
        ]

    rows: list[dict[str, str]] = []
    for item in unknown:
        ex5 = item["ex5_name"]
        stem = Path(ex5).stem
        if not stem:
            rows.append({"ex5_name": ex5, "magic_number": "", "evidence": "no_expert_name", "confidence": "none", "path": "", "note": "MT5 did not expose the EA name for this chart."})
            continue

        exact_sets = [path for path in set_files if path.stem.lower() == stem.lower()]
        set_candidates = [(path, set_magic(path)) for path in exact_sets]
        set_candidates = [(path, result) for path, result in set_candidates if result]
        if len(set_candidates) == 1:
            path, (variable, value) = set_candidates[0]
            rows.append({"ex5_name": ex5, "magic_number": value, "evidence": f"saved_set:{variable}", "confidence": "candidate", "path": str(path), "note": "Saved SET found. Confirm it is the SET loaded by the active chart before adding it to the catalog."})
            continue

        source = source_index.get(stem.lower())
        value = source_magic(source) if source else None
        if value:
            variable, magic = value
            rows.append({"ex5_name": ex5, "magic_number": magic, "evidence": f"source_default:{variable}", "confidence": "candidate", "path": str(source), "note": "MQ5 default only; an active chart may override this input."})
        else:
            rows.append({"ex5_name": ex5, "magic_number": "", "evidence": "not_found", "confidence": "none", "path": "", "note": "No matching local MQ5 source or unambiguous SET file was found."})

    with output.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["ex5_name", "magic_number", "evidence", "confidence", "path", "note"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} candidates: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
