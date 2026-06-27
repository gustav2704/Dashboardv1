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
- Exportación: `GET /api/catalog/export` crea una copia actualizada en `data\exports` con una hoja `Dashboard MT5`.
- SQLite: `data\dashboard.db`.
- Cola MT5: `<DataDir>\MQL5\Files\Dashboardv1`.
- SQX: sincronización read-only cuando SQX está abierto; los snapshots quedan persistidos.
- Refresco: 5 minutos. Una terminal queda desconectada tras 10 minutos sin respuesta.

## API

- `GET /api/dashboard`, `GET /api/strategies/{id}`
- `GET|POST /api/terminals`, `POST /api/terminals/{id}/sync`
- `GET /api/chart/{strategy_id}`
- `POST /api/catalog/import`, `GET /api/catalog/export`
- `GET /api/mappings/suggestions`, `POST /api/mappings/confirm`
- `GET /api/sqx/status`, `POST /api/sqx/sync`
- `GET|PUT /api/alerts`

La aplicación escucha únicamente en `127.0.0.1`.
