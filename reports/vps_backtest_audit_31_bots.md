# VPS MT5 backtest — resultado de la auditoría (31 bots sin backtest)

Generado: 2026-08-18. Mecanismo = el mismo que los backtests MT5 previos:
`remote_backtests.audit_candidates()` sobre el runner aislado de Darwinex-Demo
(terminal 210, VPS 145.63.135.196).

## Resultado general

- Runner aislado: **autorizado y operativo** (42 GB libres, instalado).
- La auditoría descubrió **40 candidatos** sin backtest completado.
- **Eligibles: 1** (ID 430 — NO está en los 31 del informe original).
- **De los 31 originales: NINGUNO es elegible.** 29 están bloqueados y 2 (137, 406)
  ya no aparecen porque tienen un run completado en otra vía.

## Motivos de bloqueo (29 de los 31)

| Motivo | Cuenta(s) | # bots |
|---|---|---|
| No SSH inventory source is registered for this account | 3000097316 (Darwinex-Demo), 4000094894 (Darwinex-Live) | 14 |
| No exact chart profile matches EX5, symbol, Magic and comment | 10025867 (Axi), 7396577 (FPM) | 14 |
| Several different EX5 builds have the same confirmed chart identity | 7396577 | 1 |

## Tabla compacta (31)

| ID | Bot | Cuenta | MN | Estado VPS | Motivo |
|---:|---|---|---:|---|---|
| 134 | WF_Matrix_Strategy_3_10_85_XAU | 4000094894 | 78 | blocked | No SSH inventory source |
| 360 | AltisimaPrecision | 4000094894 | 40155527 | blocked | No SSH inventory source |
| 514 | XAU_M15_3_69_opt | 4000094894 | 369193 | blocked | No SSH inventory source |
| 140 | ORB_American_EMA9 | 3000097316 | 4 | blocked | No SSH inventory source |
| 146 | MomentA BuyStop | 3000097316 | 3 | blocked | No SSH inventory source |
| 149 | TemaEA_V2 | 3000097316 | 1 | blocked | No SSH inventory source |
| 157 | WF_Matrix_Strategy_4_7_21_2 | 3000097316 | 47212 | blocked | No SSH inventory source |
| 242 | AltisimaPrecision | 3000097316 | 2 | blocked | No SSH inventory source |
| 246 | MomentA BuyStop | 3000097316 | 7 | blocked | No SSH inventory source |
| 248 | GannPullbackBuyStop | 3000097316 | 8 | blocked | No SSH inventory source |
| 249 | VWAP | 3000097316 | 9 | blocked | No SSH inventory source |
| 391 | ULTRADAX_D1 | 3000097316 | 41500119 | blocked | No SSH inventory source |
| 509 | SencillayEstupida | 3000097316 | 104701 | blocked | No SSH inventory source |
| 515 | USDJPY_M30_1139 | 3000097316 | 1139 | blocked | No SSH inventory source |
| 144 | AltisimaPrecision | 10025867 | 3 | blocked | No exact chart profile |
| 150 | VX | 10025867 | 6 | blocked | No exact chart profile |
| 152 | CCI_USDJPY | 10025867 | 3 | blocked | No exact chart profile |
| 274 | AltisimaPrecision · BTCUSD · MN4 | 10025867 | 4 | blocked | No exact chart profile |
| 277 | AltisimaPrecision · XAUUSD · MN216062026 | 10025867 | 216062026 | blocked | No exact chart profile |
| 286 | Hobbiecode_ORB_Strategy | 10025867 | 5 | blocked | No exact chart profile |
| 287 | TemaEA_V2 | 10025867 | 1 | blocked | No exact chart profile |
| 288 | ParabolicDailyHL | 10025867 | 116062026 | blocked | No exact chart profile |
| 295 | GBPAUD_Domingo_PASSEDStrategy_ | 10025867 | 265160 | blocked | No exact chart profile |
| 296 | WF_Matrix_Strategy_3_10_85_XAU | 10025867 | 31085 | blocked | No exact chart profile |
| 358 | PPM | 10025867 | 61 | blocked | No exact chart profile |
| 508 | AltisimaPrecision | 10025867 | 1 | blocked | No exact chart profile |
| 511 | MomentA BuyStop | 10025867 | 5487 | blocked | No exact chart profile |
| 472 | BotPulse Connector PRO | 7396577 | — | blocked | Varios EX5 iguales (ambiguo) |
| 406 | XAU_Strategy 1.4.61 | (sin) | — | no auditado | ya tiene run (resolvable FPM) |
| 137 | GBPAUD_Domingo_2_65_160_REAL | 4000094894 | 265160 | no auditado | ya tiene run FPM 126 (curva generada) |

## Para desbloquearlas (necesario decidir)

1. **Darwinex (14 bots)**: registrar un **SSH inventory source** (`terminal_log_sources`)
   para las cuentas `3000097316` y `4000094894` en la VPS (ahora no tienen fuente
   configurada, por eso la auditoría no puede escanearlas).
2. **Axi/FPM (14 bots)**: los EX5 **sí están** en la VPS, pero falta un **chart profile
   (.chr)** que case símbolo+magic+comment. Habría que confirmar/crear el perfil para
   cada uno, o usar `USER_CONFIRMED_EXPERTS`/`USER_CONFIRMED_CONFIGS` (método que ya se
   usó en batches previos como FPM) en lugar del perfil exacto.
3. **ID 472**: resolver la ambigüedad (varios EX5 con misma identidad confirmada).

## Elegible ahora mismo
- **ID 430** (WF Matrix - NAQStrategy 1.11.24dwnx, cuenta 7396577, US100 H1, MN 1,
  terminal 216) — es el único que la auditoría puede lanzar automáticamente, pero **no
  está en los 31**.
