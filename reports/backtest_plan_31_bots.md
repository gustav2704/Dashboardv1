# Plan de backtest · 31 bots sin backtest completado

Generado: 2026-08-18.

## Cómo se prueban los backtests

- Broker activo de backtest: **Darwinex**.
- Todos los backtests se lanzan como *batch* sobre el **runner dedicado de backtest**: terminal SSH **210 – "Darwinex Demo VPS"** en `145.63.135.196` (cuenta darwinex-demo `3000097316`), aislado y restringido a Demo.
- **El backtest NO corre en la VPS de trading donde vive cada bot.** La VPS live de cada bot solo sirve de origen/"identidad" del EA. El test siempre va al runner de backtest de Darwinex-Demo.
- Un bot solo se descubre como candidato si: no está excluido, tiene enlace SQX (config con timeframe), mapping de símbolo al broker en uso, y (para Darwinex) un run FPM de referencia que clonar.

## Resultado: de los 31 bots, actualmente NINGUNO es backtest-eligible por el flujo de batch

Estado por el discovery (activo = Darwinex): **13 exclusiones explícitas + 14 cuenta Axi excluida + 4 bloqueados sin descubrir/blocked**. Un batch `create_batch(broker='Darwinex', only_missing=True)` devolvería **ningún candidato** para estos 31.

| ID | Bot | Símbolo | Cuenta | MN | VPS live (donde existe) | ¿Descubierto en Darwinex? | Motivo no-backtest |
|---:|---|---|---:|---:|---|---|---|
| 134 | WF_Matrix_Strategy_3_10_85_XAU | XAUUSD | 4000094894 | 78 | Darwinex Live VPS (`145.63.135.196`) | excluido | Exclusión expresa (user) |
| 137 | GBPAUD_Domingo_2_65_160_REAL | GBPAUD | 4000094894 | 265160 | Darwinex Live VPS | blocked | Sin mapping de símbolo Darwinex |
| 360 | AltisimaPrecision | XAUUSD | 4000094894 | 40155527 | Darwinex Live VPS | excluido | Exclusión expresa (user) |
| 514 | XAU_M15_3_69_opt | XAUUSD | 4000094894 | 369193 | Darwinex Live VPS | no descubierto | Sin run FPM referencia + sin SQX link |
| 144 | AltisimaPrecision | BTCUSD | 10025867 | 3 | Axi Demo VPS (`145.63.135.196`) | excluido | Cuenta Axi 10025867 excluida |
| 150 | VX | XAGUSD | 10025867 | 6 | Axi Demo VPS | excluido | Cuenta Axi excluida |
| 152 | CCI_USDJPY | USDJPY | 10025867 | 3 | Axi Demo VPS | excluido | Cuenta Axi excluida |
| 274 | AltisimaPrecision · BTCUSD · MN4 | BTCUSD | 10025867 | 4 | Axi Demo VPS | no descubierto | Sin SQX link |
| 277 | AltisimaPrecision · XAUUSD · MN216062026 | XAUUSD | 10025867 | 216062026 | Axi Demo VPS | excluido | Cuenta Axi excluida |
| 286 | Hobbiecode_ORB_Strategy | EURUSD | 10025867 | 5 | Axi Demo VPS | no descubierto | EA confirmado pero sin mapping FPM |
| 287 | TemaEA_V2 | XAUUSD | 10025867 | 1 | Axi Demo VPS | excluido | Cuenta Axi excluida |
| 288 | ParabolicDailyHL | USDJPY | 10025867 | 116062026 | Axi Demo VPS | no descubierto | EA confirmado pero sin mapping FPM |
| 295 | GBPAUD_Domingo_PASSEDStrategy_ | GBPAUD | 10025867 | 265160 | Axi Demo VPS | excluido | Cuenta Axi excluida |
| 296 | WF_Matrix_Strategy_3_10_85_XAU | XAUUSD | 10025867 | 31085 | Axi Demo VPS | excluido | Cuenta Axi excluida |
| 358 | PPM | USTECH | 10025867 | 61 | Axi Demo VPS | no descubierto | Sin SQX link |
| 508 | AltisimaPrecision | EURUSD | 10025867 | 1 | Axi Demo VPS | no descubierto | Sin SQX link |
| 511 | MomentA BuyStop | XAUUSD | 10025867 | 5487 | Axi Demo VPS | no descubierto | Sin SQX link |
| 140 | ORB_American_EMA9 | NDX | 3000097316 | 4 | Darwinex Demo VPS (runner) | no descubierto | Sin SQX link |
| 146 | MomentA BuyStop | XAUUSD | 3000097316 | 3 | Darwinex Demo VPS (runner) | excluido | Exclusión expresa (user) |
| 149 | TemaEA_V2 | XAUUSD | 3000097316 | 1 | Darwinex Demo VPS (runner) | no descubierto | Sin SQX link |
| 157 | WF_Matrix_Strategy_4_7_21_2 | WS30 | 3000097316 | 47212 | Darwinex Demo VPS (runner) | no descubierto | Elegible FPM, pero sin run FPM ref para Darwinex |
| 242 | AltisimaPrecision | EURUSD | 3000097316 | 2 | Darwinex Demo VPS (runner) | excluido | Exclusión expresa (user) |
| 246 | MomentA BuyStop | USDJPY | 3000097316 | 7 | Darwinex Demo VPS (runner) | no descubierto | Sin SQX link |
| 248 | GannPullbackBuyStop | XAUUSD | 3000097316 | 8 | Darwinex Demo VPS (runner) | no descubierto | Sin SQX link |
| 249 | VWAP | XAUUSD | 3000097316 | 9 | Darwinex Demo VPS (runner) | excluido | Exclusión expresa (user) |
| 391 | ULTRADAX_D1 | GDAXI | 3000097316 | 41500119 | Darwinex Demo VPS (runner) | no descubierto | Sin SQX link |
| 509 | SencillayEstupida | NDX | 3000097316 | 104701 | Darwinex Demo VPS (runner) | no descubierto | Sin SQX link |
| 515 | USDJPY_M30_1139 | USDJPY | 3000097316 | 1139 | Darwinex Demo VPS (runner) | no descubierto | Sin SQX link |
| 406 | XAU_Strategy 1.4.61 | XAU | (sin cuenta) | — | — | no descubierto | Sin account / sin SQX link |
| 472 | BotPulse Connector PRO | BTCUSD | 7396577 | — | CloudTrading SRV FP Markets (`100.107.77.50`) | excluido | Exclusión expresa (user) |
| 497 | XAU_7_24_currenthour_1.4.61 | XAUUSD.cyr | 7396577 | 1461 | CloudTrading SRV FP Markets (`100.107.77.50`) | no descubierto | Sin SQX link |

## Resumen

- **13** → exclusión expresa en `EXCLUDED_BACKTEST_STRATEGY_IDS`: 134, 144, 146, 150, 152, 242, 249, 277, 287, 295, 296, 360, 472.
- **13** → cuenta **10025867** en `EXCLUDED_BACKTEST_ACCOUNTS` ("cuenta excluida por petición del usuario"): 144, 150, 152, 274, 277, 286, 287, 288, 295, 296, 358, 508, 511. (Se solapan con las expuras del punto anterior: 144, 150, 152, 277, 287, 295, 296.)
- Resto → **bloqueados** porque `discover_candidates('Darwinex')` no los descubre: requieren run FPM de referencia **y** enlace SQX **y** mapping de símbolo.
- Sólo **2** tienen EA confirmado (`user_confirmed_ex5`) pero aún así bloqueados por falta de mapping de símbolo FPM: 286 (`Hobbiecode_ORB_Strategy`) y 288 (`ParabolicDailyHL`).
- Sólo **2** alcanzan estado `eligible`/`resolvable` en **FPM**: 157 (`WF_Matrix_Strategy_4_7_21_2`, eligible) y 406 (`XAU_Strategy 1.4.61`, resolvable vía SQX).

## Progreso (18/08): foco inicial = 157 y 406 (FPM)

- Elegido: solo los 2 elegibles/resolvibles en FPM. Batch **42 (FPM)** creado para **157**.
- Bloqueado ahora mismo por **SQX no disponible**: el worker de batch requiere SQX abierto en *Retester → Results* antes de resolver cualquier item (aunque 157 no lo necesite; 406 sí).
- Batch 27 (FPM) estaba atascado en `paused` con todos sus items terminados; se marcó **cancelled** para desbloquear.
- **Acción pendiente del usuario**: abrir SQX (`D:\SQX_144`) en Retester → Results. Luego se reanuda el batch 42 (157) y se añade 406.
- Estado batch 42: `paused` (esperando SQX).

## Para que se puedan backtestar de verdad necesitas elegir (o aceptar):

1. **Desbloquear cuentas/estrategias**: quitar de `EXCLUDED_BACKTEST_STRATEGY_IDS` y/o `EXCLUDED_BACKTEST_ACCOUNTS` (p.ej. 10025867) los bots que sí quieres probar.
2. **Dispone de un run FPM de referencia** para los que no lo tienen (requisito del pipeline Darwinex), o añadirlos a `EXPLICIT_DARWINEX_RERUN_STRATEGY_IDS`.
3. **Enlace SQX** (project/databank) o aportar MQ5 para los que aparecen "sin SQX link".
4. **Mapping de símbolo** al broker para los bloqueados por "No ... symbol mapping".

Dime qué bots quieres habilitar (o si quieres que los desbloquee todos y genere el mapping/-SQX necesario) y con qué configuración, y lanzo el batch sobre el runner de backtest de Darwinex-Demo.
