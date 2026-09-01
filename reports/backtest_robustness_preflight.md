# Pre-flight de robustez para backtests MT5

> **Objetivo:** que la próxima corrida de backtest sea más rápida, autónoma y
> con menos bloqueos manuales.  Este documento captura las lecciones de la
> corrida anterior y establece una lista de verificación previa que valida
> todas las dependencias antes de crear un batch.

---

## 1. Lecciones de la corrida anterior (After-Action Review)

| Problema | Impacto | Solución aplicada | Previene |
|---|---|---|---|
| SQX requerido incluso para items que no necesitan código fuente MQ5 | Batch 42 pausado `"SQX debe estar abierto en Retester → Results"` aunque el item (ID 157) tenía EX5 confirmado y run FPM de referencia | `_needs_sqx()`: solo pide SQX cuando `resolution_method == "generated_from_sqx"` | Bloques futuros por SQX no disponible para items ya resueltos |
| Batch 27 atascado en `paused` con todos sus items completados | Imposible crear nuevo batch, requirió cancelación manual | `recover_batches()` auto-finaliza batches paused sin items activos | Estancamiento silencioso sin intervención |
| Cuentas Darwinex-Live sin log sources registrados | 14 bots bloqueados: `"No SSH inventory source"` | `ensure_log_sources()` ahora registra DataDirs compañeros para **cualquier** terminal SSH Darwinex en la misma VPS | Descubrimiento automático sin registros manuales |
| Riesgo auditado pero no actionable desde dashboard | `risk_exceeded` existe en BD pero no se refleja en el worker | *(documentado en sección 5 — pendiente de integrar en UI)* | — |

---

## 2. Pre-flight checklist

Ejecutar **antes de** `create_batch()`:

### 2.1 Verificar terminales SSH

```sql
SELECT id, name, account_login, transport, ssh_host, remote_data_dir
FROM terminals WHERE transport='ssh';
```

**Criterio de aceptación:** cada cuenta en `SOURCE_ACCOUNTS = ("10025867", "3000097316", "4000094894", "7396577")` debe tener **al menos** una terminal con `transport='ssh'` y `remote_data_dir` poblado.

### 2.2 Verificar log sources

```sql
SELECT t.account_login, COUNT(s.id) AS sources
FROM terminals t
LEFT JOIN terminal_log_sources s ON s.terminal_id = t.id AND s.enabled = 1
WHERE t.transport = 'ssh'
GROUP BY t.account_login;
```

**Criterio:** cada cuenta debe tener `sources > 0`. Si no, ejecutar `terminal_events.ensure_log_sources()`.

### 2.3 Verificar SQX solo si es necesario

Revisar los candidatos del discovery:

```python
from app import backtest_batches
result = backtest_batches.discover_candidates('Darwinex')
needs_sqx = [
    c for c in result['candidates']
    if c.get('resolution_method') == 'generated_from_sqx'
]
print(f"{len(needs_sqx)} candidatos requieren SQX abierto")
```

Si `needs_sqx` está vacío, **no necesitas abrir SQX** — el batch correrá autónomamente.

### 2.4 Verificar symbol mappings

```sql
SELECT source_symbol, target_symbol FROM symbol_mappings WHERE broker='Darwinex';
```

**Criterio:** cada símbolo de origen (`XAUUSD`, `US100`, `WS30`, `GDAXI`, etc.) debe tener un mapping. Los más comunes:

| Source | Target Darwinex |
|---|---|
| `xauusd` | `XAUUSD_M1` |
| `naq` / `us100` / `nas100` | `NDX_M1` |
| `ws30` / `us30` | `WS30_DK_2016` |
| `dax` / `ger40` / `gdaxi` | `GDAXI_M1` |
| `sp500` | `SP500_tick_2025_26jun` |

### 2.5 Verificar exclusiones

```python
from app.backtest_batches import EXCLUDED_BACKTEST_STRATEGY_IDS, EXCLUDED_BACKTEST_ACCOUNTS
```

Confirmar que las exclusiones siguen siendo las deseadas.  Recordar que `EXCLUDED_BACKTEST_ACCOUNTS={"10025867"}` elimina **todos** los bots de esa cuenta.

---

## 3. Estados del batch y cómo recuperarse

| Estado batch | Significado | Acción |
|---|---|---|
| `resolving` | Resolviendo EX5/config para items pendientes | El worker lo procesa automáticamente |
| `queued` | Items listos para testear | El worker lo procesa automáticamente |
| `running` | Un test está ejecutándose | Esperar; `cancel_batch()` si es necesario |
| `waiting_terminal` | Esperando que terminal esté seguro (sin posiciones/órdenes abiertas) | Cerrar posiciones manualmente o esperar |
| `paused` | Pausado por error (SQX no disponible o error transitorio) | Ver `error` en BD; corregir causa y `resume_batch()` |
| `completed` | Todos los items terminados | Normal |
| `cancelled` | Cancelado por usuario | Crear nuevo batch |

### Auto-recuperación desde worker

El worker llama `recover_batches()` al iniciar y ante pausas.  Desde la v3.2:

- **Batches paused sin items activos** → se auto-finalizan como `completed`.
- **Items running al recover** → se re-marcan `queued` para re-ejecución.

---

## 4. Bloqueadores comunes y sus resoluciones

### 4.1 "No SSH inventory source" (antes: 14 bots)

**Causa:** la cuenta no tiene `terminal_log_sources` habilitados en la VPS.

**Resolución desde v3.2:** `ensure_log_sources()` registra automáticamente los DataDirs compañeros de cualquier terminal SSH Darwinex que tenga uno de los folders conocidos (`6C3C6A11…`, `2F12A47F…`, etc.).

**Si persiste:**
1. Identificar el folder remoto: `ssh VPS "powershell Get-ChildItem 'C:\Users\Administrator\AppData\Roaming\MetaQuotes\Terminal' -Directory"`
2. Insertar source manual:
   ```sql
   INSERT INTO terminal_log_sources(terminal_id,label,data_dir,account_login,created_at)
   VALUES(terminal_id,'Manual fix','C:\Users\...\Terminal\FOLDER_HASH','account_login',datetime('now'));
   ```

### 4.2 "No exact chart profile" (antes: 14 bots)

**Causa:** el EX5 está en la VPS pero no hay un perfil `.chr` que case symbol+magic+comment.

**Resolución:**
1. Opción A — crear perfil `.chr` desde MT5 manualmente.
2. Opción B — usar `USER_CONFIRMED_EXPERTS[strategy_id]` y `USER_CONFIRMED_CONFIGS[strategy_id]` en `backtest_batches.py`.
3. Opción C — añadir mapping en `mappings` table:
   ```sql
   INSERT INTO mappings(strategy_id, magic, symbol, comment_pattern, confirmed)
   VALUES(strategy_id, 12345, 'XAUUSD', 'MyComment', 1);
   ```
   Luego re-auditar con `audit_candidates(refresh=True)`.

### 4.3 "No Darwinex symbol mapping" (bloqueo intermitente)

**Causa:** el símbolo del bot no tiene equivalente en Darwinex.

**Resolución:**
1. Añadir mapping:
   ```sql
   INSERT INTO symbol_mappings(source_symbol, target_symbol, broker)
   VALUES('oldsymbol', 'newsymbol_M1', 'Darwinex');
   ```

### 4.4 "No SQX configuration is linked" (bloqueo frecuente)

**Causa:** el bot no tiene `sqx_strategy_links` (project/databank/strategy_name).

**Resolución:**
1. Abrir SQX → localizar la estrategia en Retester → Results.
2. Insertar link:
   ```sql
   INSERT INTO sqx_strategy_links(strategy_id, project, databank, strategy_name, symbol, timeframe)
   VALUES(id, 'Retester', 'Results', 'Strategy Name', 'XAU', 'H1');
   ```

### 4.5 "Several EX5 builds match" (ambigüedad, raro)

**Causa:** dos EX5 distintos (diferente hash) tienen el mismo nombre limpio.

**Resolución:** eliminar el duplicado de los `EXPERT_SEARCH_ROOTS` o añadir la ruta exacta a `USER_CONFIRMED_EXPERTS`.

---

## 5. Risk audit — integración con backtest

`backtest_risk.py` audita cada reporte de MT5:

- **`validated`** → pérdida máxima dentro del riesgo declarado.
- **`risk_exceeded`** → pérdida máxima supera el riesgo declarado en más del 10%.
- **`no_mm`** → el EA no usa `UseMoneyManagement=true`.
- **`all_entries_at_minimum`** → todas las entradas usaron el lote mínimo.

El worker registra el audit en `backtest_metrics.raw_metrics_json` pero **no bloquea** el batch por riesgo.  El dashboard expone `risk_alert` y `risk_alert_accepted` para revisión humana.

**Próximo paso recomendado:** añadir una validación en `process_once()` que detecte `risk_exceeded` y pause el batch (opt-in via batch policy) para evitar que un EA arriesgue más de lo declarado sin revisión.

---

## 6. Flujo típico para próxima corrida

```mermaid
flowchart TD
    A[Pre-flight checklist] --> B[ensure_log_sources()]
    B --> C{¿Hay candidatos sin SQX?}
    C -- No --> D[create_batch broker='Darwinex']
    C -- Sí --> E[Abrir SQX Retester→Results]
    E --> D
    D --> F{Worker loop}
    F --> G[Recover batches+paused]
    G --> H[Resolve items sin SQX primero]
    H --> I[Ejecutar tests en runner aislado]
    I --> J[Auditar riesgo en cada reporte]
    J --> F
```

---

## 7. Referencias

| Archivo | Propósito |
|---|---|
| `backend/app/backtest_batches.py` | Orquestación de batches (discovery, resolve, execute) |
| `backend/app/remote_backtests.py` | Backtest remoto vía SSH (runner Darwinex-Demo) |
| `backend/app/terminal_events.py` | Registro automático de log sources VPS |
| `backend/app/backtest_risk.py` | Auditoría de riesgo post-backtest |
| `backend/app/mt5_backtests.py` | Parsing de reportes HTML, ejecución local |
| `data/backtest_intake/manifest.json` | Intake manual de bots para backtest |

---

*Generado: 2026-08-18 · Versión de robustez: v3.2*