from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from .config import SQX_DIR, SQX_EXTRACTOR
from .db import session, utcnow
from .mapping import normalize


class SQXUnavailable(RuntimeError):
    pass


def _run(*args: str, timeout: int = 60) -> Any:
    if not SQX_EXTRACTOR.is_file():
        raise SQXUnavailable(f"No se encontró el extractor SQX: {SQX_EXTRACTOR}")
    command = [
        sys.executable,
        str(SQX_EXTRACTOR),
        "--sqx-dir",
        str(SQX_DIR),
        "--format",
        "json",
        *args,
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if completed.returncode:
        message = (completed.stderr or completed.stdout or "SQX no disponible").strip()
        raise SQXUnavailable(message[:1000])
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SQXUnavailable("El extractor SQX devolvió una respuesta no JSON") from exc


def status() -> dict[str, Any]:
    try:
        result = _run("status", timeout=15)
        return {"available": True, "details": result}
    except (SQXUnavailable, subprocess.TimeoutExpired) as exc:
        return {"available": False, "message": str(exc)}


def _items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("strategies", "items", "rows", "data", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        if payload and all(isinstance(v, dict) for v in payload.values()):
            return list(payload.values())
    return []


def sync(project: str, databank: str) -> dict[str, Any]:
    if not project or not databank:
        raise ValueError("project y databank son obligatorios")
    payload = _run("bulk", "--project", project, "--databank", databank, timeout=180)
    incoming = _items(payload)
    imported = unmatched = 0
    with session() as conn:
        strategies = conn.execute("SELECT * FROM strategies").fetchall()
        by_name = {normalize(row["sqx_name"]): row for row in strategies}
        by_alt = {normalize(row["mql5_name"] or ""): row for row in strategies if row["mql5_name"]}
        for item in incoming:
            identity = item.get("identity", item)
            name = str(identity.get("name") or item.get("name") or item.get("strategy") or "")
            strategy = by_name.get(normalize(name)) or by_alt.get(normalize(name))
            if not strategy:
                unmatched += 1
                continue
            stats = item.get("stats", {})
            for sample in ("full", "is", "oos"):
                metrics = stats.get(sample)
                if not isinstance(metrics, dict) or not metrics:
                    continue
                conn.execute(
                    """INSERT INTO baseline_snapshots(
                         strategy_id,source,project,databank,sample_type,metrics_json,orders_json,synced_at
                       ) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        strategy["id"],
                        "sqx",
                        project,
                        databank,
                        sample,
                        json.dumps(metrics, ensure_ascii=False),
                        json.dumps(item.get("orders"), ensure_ascii=False) if item.get("orders") else None,
                        utcnow(),
                    ),
                )
            imported += 1
    return {"project": project, "databank": databank, "received": len(incoming), "imported": imported, "unmatched": unmatched}

