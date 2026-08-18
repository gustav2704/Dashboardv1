"""Produce a consolidated Markdown report of bots and their equity-curve status,
including which missing curves were newly generated and why the rest cannot be."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.main import dashboard_data


def nm(st: dict) -> str:
    return str(st["display_name"] or st["mql5_name"] or st["sqx_name"] or "").strip() or "(sin nombre)"


def main() -> None:
    data = dashboard_data("all", None, None, None, True, None, sync_alerts=False)
    strategies = sorted(data["strategies"], key=lambda s: (s["account_login"] or "", s["id"]))

    have = []
    missing = []
    for st in strategies:
        bt = st.get("backtest") or {}
        if bt.get("equity_curve_available"):
            have.append(st)
        else:
            missing.append(st)

    # Bots whose curves we generated (Darwinex + FPM 137)
    generated_ids = {137, 291, 333, 341, 374, 447, 455}

    lines: list[str] = []
    lines.append("# Bots · Equity curve report")
    lines.append("")
    lines.append(f"Generado UTC: {datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    lines.append(f"- **Total de bots** (estrategias del dashboard): **{len(strategies)}**")
    lines.append(f"- **Con equity curve disponible**: **{len(have)}**")
    lines.append(f"- **Sin equity curve**: **{len(missing)}**")
    curvas_dashboard = sum(
        1 for s in strategies if (s.get("backtest") or {}).get("equity_curve_available")
    )
    lines.append(
        "- De los bots sin equity curve, **generamos nuevas curvas en su carpeta para 7** "
        "(IDs 137, 291, 333, 341, 374, 447, 455) a partir del reporte HTML con tabla de "
        "transacciones. **6** ya quedaron visibles en el dashboard (291, 333, 341, 374, 447, 455); "
        "la curva del **137** quedó escrita en su carpeta (run FPM 126) pero el botón no la "
        "mostrará porque usa el último backtest completado del broker activo (Darwinex)."
    )
    lines.append("")

    # ---------- WITH curve ----------
    lines.append("## Bots con equity curve (" + str(len(have)) + ")")
    lines.append("")
    lines.append("| ID interno | Nombre | Símbolo | Cuenta broker | MN |")
    lines.append("|---|---|---|---|---|")
    for st in have:
        mn = st["primary_magic_number"]
        lines.append(f"| {st['id']} | {nm(st).replace('|','/')} | {st['symbol'] or '—'} | {st['account_login'] or '—'} | {mn if mn is not None else '—'} |")
    lines.append("")

    # ---------- WITHOUT curve ----------
    lines.append("## Bots sin equity curve (" + str(len(missing)) + ")")
    lines.append("")
    lines.append("| ID interno | Nombre | Símbolo | Cuenta broker | MN | Estado |")
    lines.append("|---|---|---|---|---|---|")
    for st in sorted(missing, key=lambda s: (s["account_login"] or "", s["id"])):
        bt = st.get("backtest") or {}
        mn = st["primary_magic_number"]
        if st["id"] in generated_ids:
            status = "Curva generada"
        elif bt.get("equity_curve_reason"):
            status = bt["equity_curve_reason"]
        elif not bt.get("has_completed"):
            status = "Sin backtest completado (no hay reporte para curva)"
        else:
            status = "Backtest completado pero sin fuente de curvas"
        lines.append(
            f"| {st['id']} | {nm(st).replace('|','/')} | {st['symbol'] or '—'} | {st['account_login'] or '—'} "
            f"| {mn if mn is not None else '—'} | {status} |"
        )
    lines.append("")

    lines.append("## Notas")
    lines.append("")
    lines.append("- **Curvas generadas** (7): IDs 137 (FPM, visible en su carpeta), 291, 333, 341, 374, 447 y 455 (Darwinex).")
    lines.append("  - Las 6 de Darwinex (291, 333, 341, 374, 447, 455) se generaron desde el reporte HTML "
              "`DashboardBacktest-<run>.htm`, que contenía la tabla **Deals** en cabecera inglesa "
              "(`Time … Profit, Balance`).")
    lines.append("  - Para reconocer ese formato ampliamos `_read_html_deals` en `backend/app/mt5_equity_curve.py` "
              "(ahora detecta la cabecera por roles de columna: Profit/Balance, no solo `Fecha/Hora`).")
    lines.append("  - ID 137 se generó desde `report.html` (formato español ya soportado); `.html` de equity curve "
              "escrito en `data/backtests/126/`.")
    lines.append("- **Generadas pero no visibles en el botón**: la curva de ID 137 quedó en su carpeta (run FPM 126), "
              "pero el botón «Abrir equity curve» usa el último backtest completado del broker activo (Darwinex), "
              "por lo que no aparecerá en el dashboard para ese bot.")
    lines.append("- **No generables desde su carpeta** (4): IDs 155 (CSV vacío), 243, 253 y 254 "
              "(`cache-extracted-report.html` de solo KPIs sin tabla de transacciones; los deals están en un `.tst` "
              "binario no conservado como tabla HTML).")
    lines.append("- **Sin backtest completado** (31): no existe reporte HTML/CSV utilizable en su carpeta para construir "
              "la curva. Requiere ejecutar un backtest (botón de backtest en el dashboard).")
    lines.append("- Los `.html` de curves se escriben según el mecanismo normal (`equity_path`), por lo que el endpoint "
              "`/api/backtests/{run_id}/equity-curve` ya los sirve sin volver a calcularlos.")

    out = Path(__file__).resolve().parent / ".." / "reports" / "bots_equity_curve_report.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"Markdown report written to: {out}")
    print(f"  bots={len(strategies)} have={len(have)} missing={len(missing)}")


if __name__ == "__main__":
    main()
