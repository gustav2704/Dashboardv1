# Backtest VPS — reporte de bots bloqueados/fallidos

Generado: 2026-08-18.

## VPS con conexión y capaces de backtest

Todos los backtests MT5 se ejecutan en el **runner aislado de Darwinex-Demo**:
- **terminal 210 · `145.63.135.196`** (Darwinex-Demo, cuenta 3000097316) — autorizado y operativo.

Tengo conexión SSH a (usados como *fuente* de expertos, no para ejecutar el test):
- `145.63.135.196` → terminales 208 (Darwinex-Live), 210 (Darwinex-Demo runner), 211 (Axi-US50-Demo), 212 (FPM-Demo)
- `100.107.77.50` → terminal 216 (FP Markets CloudTrading) y hermanos

## Resultado de la prueba (ID 430)

- Retry con símbolo corregido **NDX** → **run 410 COMPLETED** → equity curve generada (`.html` en `data/backtests/410/`). ✅

## Excluidos (22) — confirmados, NO se backtestean

Exclusión expresa (`EXCLUDED_BACKTEST_STRATEGY_IDS`) y/o cuenta `10025867` (`EXCLUDED_BACKTEST_ACCOUNTS`):
`134, 144, 146, 150, 152, 242, 249, 274, 277, 286, 287, 288, 295, 296, 358, 360, 436, 437, 472, 508, 510, 511`

## Resto — bloqueados (18), tabla compacta

| ID | Nombre | Cuenta | VPS (fuente expertos) | Estado | Motivo |
|---:|---|---|---|---|---|
| 140 | ORB_American_EMA9 | 3000097316 | 145.63.135.196 | blocked | No SSH inventory source |
| 149 | TemaEA_V2 | 3000097316 | 145.63.135.196 | blocked | No SSH inventory source |
| 157 | WF_Matrix_Strategy_4_7_21_2 | 3000097316 | 145.63.135.196 | blocked | No SSH inventory source |
| 246 | MomentA BuyStop | 3000097316 | 145.63.135.196 | blocked | No SSH inventory source |
| 248 | GannPullbackBuyStop | 3000097316 | 145.63.135.196 | blocked | No SSH inventory source |
| 391 | ULTRADAX_D1 | 3000097316 | 145.63.135.196 | blocked | No SSH inventory source |
| 435 | XAUWF Matrix - 3.13.39d | 3000097316 | 145.63.135.196 | blocked | No SSH inventory source |
| 509 | SencillayEstupida | 3000097316 | 145.63.135.196 | blocked | No SSH inventory source |
| 515 | USDJPY_M30_1139 | 3000097316 | 145.63.135.196 | blocked | No SSH inventory source |
| 512 | USDJPY_M15_3_7_47_Real | 4000094894 | 145.63.135.196 | blocked | No SSH inventory source |
| 513 | US30v4 BUY | 4000094894 | 145.63.135.196 | blocked | No SSH inventory source |
| 514 | XAU_M15_3_69_opt | 4000094894 | 145.63.135.196 | blocked | No SSH inventory source |
| 431 | XAU_M15_GWF_Opt._3_36 | 7396577 | 100.107.77.50 | blocked | No exact chart profile |
| 432 | XAUWF_Matrix_3_11_46d | 7396577 | 100.107.77.50 | blocked | No exact chart profile |
| 433 | XAUWF_Matrix_3_13_39d | 7396577 | 100.107.77.50 | blocked | No exact chart profile |
| 434 | WF_Matrix_Strategy_5_14_71 | 7396577 | 100.107.77.50 | blocked | No exact chart profile |
| 497 | XAU_7_24_currenthour_1.4.61 | 7396577 | 100.107.77.50 | blocked | No exact chart profile |
| 430 | WF_Matrix_NAQStrategy_1_11_24d | 7396577 | 100.107.77.50 | **eligible** | — (ya completado run 410) |

**Motivos principales:**
- **No SSH inventory source** (12) → cuentas `3000097316` y `4000094894` no tienen `terminal_log_sources` configurado en la VPS; hay que registrarlo.
- **No exact chart profile** (5) → el EX5 está en la VPS pero falta un perfil `.chr` que case símbolo+magic+comment.
- **Elegible** (1) → ID 430, ya backtesteado.
