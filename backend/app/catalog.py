from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from .db import session, utcnow

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"m": MAIN_NS, "r": REL_NS}


def _column(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref).group(0)
    value = 0
    for char in letters:
        value = value * 26 + ord(char) - 64
    return value - 1


def read_first_sheet(path: Path) -> list[list[object]]:
    """Read values from the first XLSX sheet using only the standard library."""
    with zipfile.ZipFile(path) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root.findall(f"{{{MAIN_NS}}}si"):
                shared.append("".join(node.text or "" for node in item.iter(f"{{{MAIN_NS}}}t")))

        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        first = workbook.find("m:sheets", NS)[0]
        relation_id = first.attrib[f"{{{REL_NS}}}id"]
        relations = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {node.attrib["Id"]: node.attrib["Target"] for node in relations}
        target = targets[relation_id].lstrip("/")
        if not target.startswith("xl/"):
            target = f"xl/{target}"
        sheet = ET.fromstring(archive.read(target))

        result: list[list[object]] = []
        for row in sheet.findall(".//m:sheetData/m:row", NS):
            cells: dict[int, object] = {}
            for cell in row.findall("m:c", NS):
                ref = cell.attrib["r"]
                kind = cell.attrib.get("t")
                value_node = cell.find("m:v", NS)
                inline_node = cell.find("m:is", NS)
                value: object = ""
                if inline_node is not None:
                    value = "".join(n.text or "" for n in inline_node.iter(f"{{{MAIN_NS}}}t"))
                elif value_node is not None:
                    raw = value_node.text or ""
                    if kind == "s" and raw.isdigit():
                        value = shared[int(raw)]
                    elif kind == "b":
                        value = raw == "1"
                    else:
                        try:
                            value = float(raw)
                            if value.is_integer():
                                value = int(value)
                        except ValueError:
                            value = raw
                cells[_column(ref)] = value
            if cells:
                width = max(cells) + 1
                result.append([cells.get(i, "") for i in range(width)])
        return result


def _normalize(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def import_catalog(path: Path) -> dict[str, int]:
    rows = read_first_sheet(path)
    header_index = next(
        i for i, row in enumerate(rows) if row and str(row[0]).strip().lower() == "symbol"
    )
    headers = [str(value).strip() for value in rows[header_index]]
    inserted = updated = 0
    last_symbol = ""
    with session() as conn:
        for source_row, values in enumerate(rows[header_index + 1 :], start=header_index + 2):
            padded = values + [""] * (len(headers) - len(values))
            record = {headers[i]: padded[i] for i in range(len(headers)) if headers[i]}
            symbol = str(record.get("symbol") or "").strip()
            if symbol:
                last_symbol = symbol
            sqx_name = str(record.get("SQX original name") or "").strip()
            if not sqx_name:
                continue
            mql_name = str(record.get("mql5 bot name (alternative)") or "").strip()
            account = str(record.get("demo account number") or "").strip()
            catalog_json = json.dumps(record, ensure_ascii=False)
            existing = conn.execute(
                "SELECT id,origin FROM strategies WHERE sqx_name=? AND account_login=?",
                (sqx_name, account),
            ).fetchone()
            if not existing and mql_name:
                candidates = conn.execute(
                    """SELECT id,origin,sqx_name,mql5_name FROM strategies
                       WHERE account_login=? AND origin IN ('mt5','mt5+excel')""",
                    (account,),
                ).fetchall()
                matching = [
                    candidate
                    for candidate in candidates
                    if _normalize(mql_name)
                    in {
                        _normalize(candidate["sqx_name"]),
                        _normalize(candidate["mql5_name"]),
                    }
                ]
                if len(matching) == 1:
                    existing = matching[0]
            if existing:
                conn.execute(
                    """UPDATE strategies SET symbol=?,sqx_name=?,mql5_name=?,catalog_row=?,
                       catalog_json=?,origin=? WHERE id=?""",
                    (
                        last_symbol,
                        sqx_name,
                        mql_name,
                        source_row,
                        catalog_json,
                        "mt5+excel" if existing["origin"] in ("mt5", "mt5+excel") else "excel",
                        existing["id"],
                    ),
                )
                updated += 1
                strategy_id = existing["id"]
            else:
                cursor = conn.execute(
                    """INSERT INTO strategies(
                         symbol,sqx_name,mql5_name,account_login,origin,catalog_row,catalog_json,created_at
                       ) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        last_symbol,
                        sqx_name,
                        mql_name,
                        account,
                        "excel",
                        source_row,
                        catalog_json,
                        utcnow(),
                    ),
                )
                inserted += 1
                strategy_id = cursor.lastrowid
            edge = record.get("Edge decay analyzer score")
            losses = str(record.get("maximun of losses in is/oos  in a row") or "").split()
            baseline_common = {
                "EdgeScore": edge,
                "AvgTradesPerMonth": record.get("Avg trades per Month"),
                "ReturnDDRatio": record.get("Ret/DD Original"),
                "SQN": record.get("SQN"),
                "MaxDD": record.get("MaxDD"),
            }
            if any(value not in (None, "") for value in baseline_common.values()) or losses:
                conn.execute(
                    "DELETE FROM baseline_snapshots WHERE strategy_id=? AND source='excel'",
                    (strategy_id,),
                )
                full_metrics = {**baseline_common, "MaxConsecLoss": losses[0] if losses else None}
                oos_metrics = {**baseline_common, "MaxConsecLoss": losses[1] if len(losses) > 1 else losses[0] if losses else None}
                for sample_type, metrics in (("full", full_metrics), ("oos", oos_metrics)):
                    conn.execute(
                        """INSERT INTO baseline_snapshots(strategy_id,source,sample_type,metrics_json,synced_at)
                           VALUES(?,?,?,?,?)""",
                        (strategy_id, "excel", sample_type, json.dumps(metrics, ensure_ascii=False), utcnow()),
                    )
    return {"inserted": inserted, "updated": updated, "total": inserted + updated}
