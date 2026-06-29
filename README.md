# Dashboardv1 — EA Observatory

Dashboard web local para comparar EAs ejecutados en MetaTrader 5 con sus métricas históricas de StrategyQuant X.

## Inicio rápido

1. Instalar y compilar el servicio de sólo lectura:

   ```powershell
   .\install-mt5-service.ps1 -DataDir 'C:\ruta\al\DataDir'
   ```

2. En MT5, iniciar `Navegador > Servicios > Dashboardv1 > DashboardBridge`.
3. Ejecutar `.\start-dashboard.ps1` y abrir `http://127.0.0.1:8765`.

La primera ejecución crea `.venv`, instala dependencias y compila el frontend. El servicio MQL5 sólo lee historial, posiciones y velas; no contiene ninguna llamada de envío o modificación de órdenes.

## Datos

- Fuente operativa: MT5. Cada combinación observada de terminal, cuenta, símbolo, magic y comentario crea o actualiza automáticamente su estrategia.
- Catálogo adicional: `..\EA_track\Track_v1.xlsx`; aporta nombres y KPIs históricos, pero nunca se modifica ni bloquea bots nuevos.
- Exportación: `GET /api/catalog/export` crea en `data\exports` un ranking multifuente con hojas para SQX, backtest MT5, Live MT5, comparativo y metodología; la hoja original se conserva intacta.
- SQLite: `data\dashboard.db`.
- Cola MT5: `<DataDir>\MQL5\Files\Dashboardv1`.
- SQX: sincronización read-only cuando SQX está abierto; los snapshots quedan persistidos.
- Refresco: 5 minutos. Una terminal queda desconectada tras 10 minutos sin respuesta.

## API

- `GET /api/dashboard`, `GET /api/strategies/{id}`
- `GET|POST /api/terminals`, `POST /api/terminals/{id}/sync`
- `GET /api/chart/{strategy_id}`
- `POST /api/catalog/import`, `GET /api/catalog/export`
- `GET /api/strategy-identities/conflicts`
- `POST /api/strategy-identities/merge` (`dry_run=true` por defecto)
- `GET /api/mappings/suggestions`, `POST /api/mappings/confirm`
- `GET /api/sqx/status`, `POST /api/sqx/sync`
- `GET|PUT /api/alerts`
- `GET|POST /api/backtests`, `GET /api/backtests/{id}`
- `POST /api/backtests/{id}/cancel`, `POST /api/backtests/{id}/retry`
- `GET /api/strategies/{id}/backtests`, `GET /api/strategies/{id}/backtest-defaults`
- `GET /api/backtests/candidates`
- `GET|POST /api/backtests/batches`
- `GET /api/backtests/batches/{id}`, `POST /api/backtests/batches/{id}/pause|resume|cancel`

## Backtests MT5

La vista `Backtests` ejecuta el Strategy Tester de FPM con configuracion de
referencia o derivada de SQX. El terminal FPM debe estar cerrado antes de
iniciar una prueba automatizada. Los reportes, logs y archivos INI se guardan
en `data\backtests`; los KPIs normalizados se incorporan como baseline
`mt5_backtest`. Este flujo es independiente del servicio Live y no envia,
modifica ni cierra ordenes.

La opcion `Validate missing` resuelve EX5 de FPM/Darwinex, captura la
configuracion SQX y prepara una cola secuencial reanudable. Cuando se usa la
terminal Live, la cola espera hasta que no existan posiciones ni ordenes
pendientes antes de iniciar el Strategy Tester.

La aplicación escucha únicamente en `127.0.0.1`.
