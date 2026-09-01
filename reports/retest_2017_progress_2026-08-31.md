# Progreso del retest histórico — 2017

**Corte:** 31 de agosto de 2026 (lote en ejecución)

## Alcance

- Fecha de inicio: **2017-01-01**.
- Fecha final: **2026-07-27**.
- Tras el piloto, se identificaron **63** estrategias sin cobertura validada desde 2017.
- **49** de ellas tienen una configuración remota previa reutilizable. Se excluyeron 6 casos M5 que ya estaban expresamente fuera de alcance; el lote seguro actual contiene **43 bots**.
- Las **14** restantes necesitan resolver su identidad/artefacto antes de poder retestearse de forma segura.

## Estado del lote 49

| Estado | Bots |
|---|---:|
| Completados | 10 |
| En ejecución | 1 |
| En cola | 31 |
| Fallidos de validación | 1 |
| Total | 43 |

El lote sigue activo y se ejecuta secuencialmente en el terminal aislado Darwinex Demo. Esto equivale a **23,3 % completado**; el bot que está corriendo no se cuenta como completado.

El proceso actual es la estrategia **349 — DosVelas_EURUSD_m15** (ejecución 525). El proceso despachador exclusivo sigue activo, por lo que no se debe iniciar un segundo worker ni lanzar pruebas manuales en paralelo.

## Resultados ya finalizados

| ID | Estrategia | Calidad historial | Beneficio neto | PF | Operaciones |
|---:|---|---:|---:|---:|---:|
| 1 | NAQWF Matrix - NAQStrategy 2.8.22dwnxdwnx | 100 % | 8.480,64 | 1,680 | 536 |
| 148 | Hobbiecode ORB | 100 % | -1.841,79 | 0,973 | 2.471 |
| 155 | ParabolicDailyHL | 100 % | -56,16 | 1,040 | 2.131 |
| 157 | Strategy 4.7.21(2) | 100 % | 1.788,72 | 1,577 | 612 |
| 243 | ST+RSI Buy | 100 % | 2.345,58 | 3,056 | 453 |
| 253 | AltisimaPrecision | 96 % | 451,05 | 9,848 | 22 |
| 257 | USDJPY M5 | 100 % | 7.516,50 | 1,563 | 435 |
| 291 | ParabolicDailyHL | 100 % | -256,54 | 1,039 | 2.131 |
| 297 | GBPAUD Domingo | 100 % | -2.053,46 | 0,938 | 486 |
| 348 | DosVelas NAQ D1 | 100 % | 5.785,16 | 1,456 | 194 |

## Incidencia controlada

La estrategia **2** (ejecución 515) quedó en `validation_failed` porque se intentó iniciar mientras el tester aislado ya estaba ocupado. No indica un fallo de estrategia ni de datos; se reintentará de forma secuencial una vez termine la cola actual, conservando el registro de la incidencia.

## Siguiente ejecución

1. Dejar que finalice la estrategia 349 y las 31 restantes en cola, una por una.
2. Reintentar la estrategia 2 sin concurrencia.
3. Resolver las 14 identidades pendientes y crear un lote separado sólo con aquellas que pasen la validación de artefacto, símbolo y configuración.
4. Consolidar resultados y clasificar los bots por calidad de historial, PF, beneficio, drawdown y número de operaciones.
