"""Regenerate the dashboard inventory of bots that lack a valid backtest.

The report deliberately separates the user-authorized forensic campaign from
identity blockers and bots that simply have not been put in a campaign.  This
makes a status report safe to refresh while the isolated MT5 tester is active.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.backtest_batches import (  # noqa: E402
    EXCLUDED_BACKTEST_ACCOUNTS,
    EXCLUDED_BACKTEST_STRATEGY_IDS,
)
from app.db import session  # noqa: E402


CAMPAIGN_CHUNKS = (
    (50, (431, 432, 433, 435, 497, 509, 514, 515)),
    (None, (391, 406, 575, 576, 577, 580, 583, 584, 585)),
    (None, (586, 592, 593, 594, 598, 599, 600, 601)),
)
FORENSIC_BLOCKERS = {
    517: "EX5/perfil localizado, pero EURUSD.sa no tiene equivalencia Darwinex verificada",
    522: "EX5/perfil localizado, pero XAUUSD.sa no tiene equivalencia Darwinex verificada",
    527: "dos identidades VPS completas con hashes EX5 distintos; no se adivina cuál es la correcta",
    528: "EX5/perfil localizado, pero XAUUSD.sa no tiene equivalencia Darwinex verificada",
    572: "no se encontró EX5 ni perfil de gráfico tras la búsqueda profunda en las VPS",
    573: "no se encontró EX5 ni perfil de gráfico tras la búsqueda profunda en las VPS",
    574: "no se encontró EX5 ni perfil de gráfico tras la búsqueda profunda en las VPS",
    578: "no se encontró EX5 ni perfil de gráfico tras la búsqueda profunda en las VPS",
}


def esc(value: object) -> str:
    return str(value or "—").replace("|", "\\|").replace("\n", " ")


def main() -> None:
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    campaign_ids = {strategy_id for _, ids in CAMPAIGN_CHUNKS for strategy_id in ids}
    with session() as conn:
        pending = conn.execute(
            """SELECT s.id,s.sqx_name,s.symbol,s.account_login
                 FROM strategies s
                 WHERE s.retired=0 AND s.archived_at IS NULL
                   AND NOT EXISTS(
                     SELECT 1 FROM backtest_runs r JOIN backtest_metrics m ON m.run_id=r.id
                     WHERE r.strategy_id=s.id AND r.status='completed'
                   )
                 ORDER BY s.id"""
        ).fetchall()
        excluded = [
            row for row in pending
            if row["id"] in EXCLUDED_BACKTEST_STRATEGY_IDS
            or (row["account_login"] or "") in EXCLUDED_BACKTEST_ACCOUNTS
        ]
        real_pending = [row for row in pending if row not in excluded]
        item_rows = conn.execute(
            """SELECT i.strategy_id,i.status AS item_status,i.run_id,b.id AS batch_id,
                       b.status AS batch_status,r.status AS run_status
                 FROM backtest_batch_items i
                 JOIN backtest_batches b ON b.id=i.batch_id
                 LEFT JOIN backtest_runs r ON r.id=i.run_id
                 WHERE b.id>=50"""
        ).fetchall()
        campaign_results = conn.execute(
            """SELECT i.strategy_id,i.batch_id,i.run_id,r.status AS run_status
                 FROM backtest_batch_items i
                 JOIN backtest_runs r ON r.id=i.run_id
                 JOIN backtest_metrics m ON m.run_id=r.id
                 WHERE i.strategy_id IN ({}) AND r.status='completed'
                 ORDER BY i.strategy_id""".format(
                ",".join("?" for _ in campaign_ids)
            ),
            tuple(sorted(campaign_ids)),
        ).fetchall()
    by_strategy = {row["strategy_id"]: row for row in item_rows}

    completed_campaign = {row["strategy_id"]: row for row in campaign_results}
    lines = [
        "# Inventario de bots sin backtest válido",
        "",
        f"Actualizado: {generated}.",
        "",
        "Un backtest válido requiere un `backtest_run` `completed` con métricas. "
        "Solo incluye estrategias activas (`retired = 0`, no archivadas).",
        "",
        f"Resumen: **{len(real_pending)} pendientes reales** y **{len(excluded)} excluidos deliberadamente**. "
        f"De la campaña original de 25 bots, **{len(completed_campaign)} ya están validados** y "
        f"**{len(campaign_ids - set(completed_campaign))} siguen en ejecución/cola**. "
        "El histórico es Darwinex-Demo aislado, del 2017-01-01 al 2026-07-27, un tester a la vez.",
        "",
        f"## Resultados ya validados de la campaña ({len(completed_campaign)})",
        "",
        "| ID | Lote | Run | Estado |",
        "|---:|---:|---:|---|",
    ]
    for strategy_id in sorted(completed_campaign):
        result = completed_campaign[strategy_id]
        lines.append(f"| {strategy_id} | {result['batch_id']} | {result['run_id']} | {result['run_status']} |")

    lines += [
        "",
        f"## Campaña forense aún en ejecución ({len(campaign_ids - set(completed_campaign))})",
        "",
        "| ID | Bot | Símbolo | Cuenta | Estado actual |",
        "|---:|---|---|---|---|",
    ]
    for row in real_pending:
        strategy_id = row["id"]
        if strategy_id not in campaign_ids:
            continue
        active = by_strategy.get(strategy_id)
        if active:
            state = f"lote {active['batch_id']}: {active['item_status']}"
            if active["run_status"]:
                state += f" (run {active['run_id']}: {active['run_status']})"
        else:
            chunk = next(index + 1 for index, (_, ids) in enumerate(CAMPAIGN_CHUNKS) if strategy_id in ids)
            state = f"programado: bloque {chunk}, tras completar los anteriores"
        lines.append(f"| {strategy_id} | {esc(row['sqx_name'])} | {esc(row['symbol'])} | {esc(row['account_login'])} | {state} |")

    lines += [
        "",
        "## Pendientes bloqueados por identidad/artifacto (8)",
        "",
        "| ID | Bot | Motivo verificable |",
        "|---:|---|---|",
    ]
    for row in real_pending:
        if row["id"] in FORENSIC_BLOCKERS:
            lines.append(f"| {row['id']} | {esc(row['sqx_name'])} | {FORENSIC_BLOCKERS[row['id']]} |")

    lines += [
        "",
        "## Pendientes fuera de la campaña solicitada (20)",
        "",
        "No se han lanzado automáticamente: no formaban parte de la lista de 33 IDs autorizada.",
        "",
        "| ID | Bot | Símbolo | Cuenta |",
        "|---:|---|---|---|",
    ]
    for row in real_pending:
        if row["id"] not in campaign_ids and row["id"] not in FORENSIC_BLOCKERS:
            lines.append(f"| {row['id']} | {esc(row['sqx_name'])} | {esc(row['symbol'])} | {esc(row['account_login'])} |")

    lines += [
        "",
        "## Excluidos intencionalmente (34)",
        "",
        "| ID | Bot | Símbolo | Cuenta | Motivo |",
        "|---:|---|---|---|---|",
    ]
    for row in excluded:
        reasons = []
        if row["id"] in EXCLUDED_BACKTEST_STRATEGY_IDS:
            reasons.append("ID en exclusión")
        if (row["account_login"] or "") in EXCLUDED_BACKTEST_ACCOUNTS:
            reasons.append("cuenta excluida")
        lines.append(f"| {row['id']} | {esc(row['sqx_name'])} | {esc(row['symbol'])} | {esc(row['account_login'])} | {' y '.join(reasons)} |")

    output = ROOT / "reports" / "inventario_bots_sin_backtest_2026-09-06.md"
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(output)
    print(f"real_pending={len(real_pending)} excluded={len(excluded)} campaign={len(campaign_ids)}")


if __name__ == "__main__":
    main()
