# Auditoría de duplicados operativos — 13-08-2026

## Resultado ejecutivo

- **58 fichas secundarias** detectadas para conciliación.
- La evidencia usa la clave operativa: **servidor + cuenta + símbolo + MagicNumber + comentario**.
- Todas las fusiones propuestas por identidad operativa exacta fueron completadas el 13-08-2026: 58 fichas secundarias en total (ocho colisiones entre mapeos y 50 fichas secundarias de despliegue). No quedan conflictos entre identidades canónicas activas.
- El patrón coincide con el caso ya corregido `450 → 340`: una importación creó una ficha con el nombre físico del EX5 aunque ya existía una ficha canónica asociada al mismo bot operativo.

## Caso solicitado: 336 y 463

| Campo | Bot canónico | Ficha secundaria |
| --- | --- | --- |
| ID | 336 | 463 |
| Nombre | `EURUSD_M5_231132` | `WF Optimization - EURUSD M5 SH JULStrategy 2.31.132` |
| Servidor / cuenta | Darwinex-Demo / 3000097316 | Darwinex-Demo / 3000097316 |
| Símbolo / periodo | EURUSD / M5 | EURUSD / M5 |
| Magic | 231132 | 231132 |
| Comentario confirmado | `EURUSD_M5_231132` | `EURUSD_M5_231132` |
| Evidencia histórica | Backtest Darwinex completado (run 282) | Sólo despliegue importado (ID 64) |

**Conclusión:** son el mismo bot. El comentario es decisivo: conserva la identidad lógica usada por el EA aunque el EX5 se llame de otro modo. Propuesta: `463 → 336`.

## Colisiones operativas exactas

Estas ocho parejas tenían dos fichas activas con exactamente el mismo servidor, cuenta, símbolo, Magic y comentario. Fueron fusionadas: la ficha de menor ID se conservó como canónica y la secundaria quedó archivada con trazabilidad.

| Canónico | Secundario | Servidor/cuenta | Símbolo | Magic | Comentario |
| ---: | ---: | --- | --- | ---: | --- |
| 135 | 436 | Axi-US50-Demo / 10025867 | XAUUSD | 31146 | `XAUWF_Matrix_Strategy_3_11_46d` |
| 135 | 432 | FirstPrudentialMarkets-Demo / 7396577 | XAUUSD.cyr | 4 | `XAUWF_Matrix_Strategy_3_11_46d` |
| 136 | 437 | Axi-US50-Demo / 10025867 | XAUUSD | 31339 | `XAUWF_Matrix_Strategy_3_13_39d` |
| 136 | 435 | Darwinex-Demo / 3000097316 | XAUUSD | 4 | `XAUWF_Matrix_Strategy_3_13_39d` |
| 136 | 433 | FirstPrudentialMarkets-Demo / 7396577 | XAUUSD.cyr | 5 | `XAUWF_Matrix_Strategy_3_13_39d` |
| 289 | 434 | FirstPrudentialMarkets-Demo / 7396577 | XAUUSD.cyr | 51471 | `WF_Matrix_Strategy_5_14_71` |
| 292 | 431 | FirstPrudentialMarkets-Demo / 7396577 | XAUUSD.cyr | 11111 | `XAU_M15_GWF_Opt._Strategy_3_36` |
| 375 | 430 | FirstPrudentialMarkets-Demo / 7396577 | US100 | 1 | `WF_Matrix_NAQStrategy_1_11_24d` |

## Despliegues que apuntan a una ficha secundaria, por bróker de origen

El bróker indicado es el del despliegue importado (la ficha secundaria). Un bot canónico puede tener mapeos en otros brókers, pero no cambia el origen de esa ficha secundaria.

### Darwinex-Demo — cuenta 3000097316

`451 → 338`, `456 → 334`, `457 → 333`, `458 → 339`, `459 → 335`, `460 → 363`, `461 → 345`, `462 → 346`, **`463 → 336`**, `464 → 341`, `465 → 342`, `466 → 376`.

### Darwinex-Live — cuenta 4000094894

`439 → 135`, `440 → 136`, `441 → 31`, `442 → 292`, `443 → 290`, `449 → 375`.

### FirstPrudentialMarkets-Demo (FPM) — cuenta 7396577

`434 → 289`, `467 → 375`, `468 → 278`, `469 → 260`, `471 → 256`, `474 → 31`, `477 → 390`, `478 → 364` (dos despliegues/Magic), `479 → 331`, `480 → 297`, `482 → 135`, `483 → 136`, `485 → 33`, `486 → 34`, `487 → 365`, `488 → 115`, `489 → 36`, `490 → 292`, `491 → 37`, `492 → 38`, `493 → 106`, `494 → 107`, `495 → 108`, `496 → 356`, `498 → 392`, `499 → 402`, `500 → 397`, `501 → 394`, `502 → 398`, `503 → 393`, `504 → 395`, `505 → 396`, `506 → 399`.

## Cómo se tratará cada fusión, tras confirmación

1. Crear una copia de seguridad de la base de datos.
2. Mover el despliegue confirmado y su alias de EX5 al bot canónico.
3. Mantener íntegros los backtests, métricas y mapeos del canónico.
4. Archivar la ficha secundaria, sin eliminarla, con razón de auditoría y referencia al ID canónico.
5. Verificar que queda un único dueño activo de cada identidad operativa.

## Por qué el comentario importa

El nombre de archivo EX5 puede variar por exportación, ruta, versión o convención del VPS. El comentario configurado por el EA es estable y, junto al MagicNumber y al símbolo, identifica la instancia que produce órdenes e historial. Por eso se usa como prueba fuerte, no como coincidencia de texto aislada.

En los 58 casos listados, el comentario coincide con el mapeo confirmado del bot canónico dentro del mismo servidor, cuenta y símbolo.
