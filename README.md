# Dashboardv1 — EA Observatory

Dashboard web local para comparar EAs ejecutados en MetaTrader 5 con sus métricas históricas de StrategyQuant X.

## Inicio rápido

1. Instalar y compilar el servicio de sólo lectura:

   ```powershell
   .\install-mt5-service.ps1 -DataDir 'C:\ruta\al\DataDir' -InstallBotInventory
   ```

2. En MT5, iniciar `Navegador > Servicios > Dashboardv1 > DashboardBridge`.
   El bridge es estrictamente de lectura: historial, cuenta, posiciones, ordenes
   pendientes y velas; no envia, modifica ni cierra ordenes.
3. Opcionalmente, ejecutar una vez `Navegador > Scripts > Dashboardv1 >
   BotInventoryReport` en un chart para importar los EAs adjuntos y sus Magic
   Numbers. El script tambien es de solo lectura.
4. Ejecutar `.\start-dashboard.ps1` y abrir `http://127.0.0.1:8765`.

La primera ejecución crea `.venv`, instala dependencias y compila el frontend. El servicio MQL5 sólo lee historial, posiciones y velas; no contiene ninguna llamada de envío o modificación de órdenes.

## Datos

- Fuente operativa: MT5. La identidad desplegada es servidor + cuenta + activo normalizado + nombre EX5 normalizado. Terminal y Magic Number son atributos; las observaciones sin EX5 o alias confirmado se conservan, pero no crean estrategias ni alimentan métricas.
- El overview sólo publica estrategias con evidencia confirmada de terminal o inventario VPS. SQX y backtests pueden enriquecerlas, pero no crean filas operativas.
- SQLite: `data\dashboard.db`.
- Cola MT5: `<DataDir>\MQL5\Files\Dashboardv1`.
- SQX: sincronización read-only cuando SQX está abierto; los snapshots quedan persistidos. Para recuperar el EGT de un único bot, use la actualización dirigida por `strategy_id`: dedica como máximo 5 segundos a localizarlo y 15 a inspeccionarlo (20 segundos en total); no lanza el lote completo ni el análisis Doctor.

La regla operativa y el tratamiento de timeouts están documentados en
[docs/SQX_EGT_SINGLE_BOT_RUNBOOK.md](docs/SQX_EGT_SINGLE_BOT_RUNBOOK.md).
- Refresco: 5 minutos. Una terminal queda desconectada tras 10 minutos sin respuesta.

### Terminal remota por SSH

El Dashboard puede registrar una carpeta de datos MT5 de una VPS mediante
**SSH/SFTP**. Esto elimina la dependencia de que la carpeta del terminal esté
montada o instalada localmente. En `Settings > Register terminal`, selecciona
`SSH / SFTP VPS` e indica host, puerto, usuario, ruta de clave privada local,
huella SHA-256 del servidor y el `DataDir` remoto.

La huella se valida en cada conexión; no se aceptan claves de host nuevas de
forma automática. La sincronización lee y archiva los JSON de
`MQL5\Files\Dashboardv1` por SFTP y escribe solicitudes de sincronización o
velas en esa misma cola. También existe un diagnóstico SSH de solo lectura con
una lista cerrada de comandos.

SSH reemplaza el **transporte**, no la fuente MT5: `DashboardBridge` debe seguir
activo dentro de cada terminal para producir el estado live de cuenta,
posiciones, órdenes, historial y velas. Un script de Windows ejecutado por SSH
no puede obtener ese estado interno de MT5 de forma fiable sin un componente
que se ejecute dentro de la terminal.

La guía operativa completa y validada para preparar una nueva VPS Windows con
clave ED25519, permisos de `Administrator`, huella de host y diagnóstico está
en [docs/SSH_VPS_MT5_RUNBOOK.md](docs/SSH_VPS_MT5_RUNBOOK.md). La excepción es
`GuardianCuenta`: se registra como fuente observadora SFTP y no utiliza
`DashboardBridge`.

## Inventarios de VPS

Los CSV principales generados por `BotInventoryReport` en una terminal real se
copian en `data\imports\bot_inventory\vps\<cuenta>`. El Dashboard los importa al
arrancar y en cada refresco. Los inventarios prueban que un EA está adjunto, pero
un despliegue sin Magic Number requiere confirmación manual en `Settings` y no se
usa para atribuir operaciones hasta conocer un magic fiable.

## API

- `GET /api/dashboard`, `GET /api/strategies/{id}`, `GET /api/strategies/{id}/analytics`
- `GET|POST /api/terminals`, `POST /api/terminals/{id}/sync`
- `GET /api/chart/{strategy_id}`
- `GET /api/strategy-identities/conflicts`
- `POST /api/strategy-identities/merge` (`dry_run=true` por defecto)
- `GET /api/mappings/suggestions`, `POST /api/mappings/confirm`
- `GET /api/sqx/status`, `POST /api/sqx/sync`
  - Lote completo: `{ "project": "Retester", "databank": "Results" }`.
  - Un solo bot/EGT: `{ "project": "Retester", "databank": "Results", "strategy_id": 453 }`. Esta es la opción obligatoria para investigar una fila concreta; falla rápido si SQX no responde, en vez de recorrer todas las estrategias.
- `GET|PUT /api/alerts`
- `GET|POST /api/backtests`, `GET /api/backtests/{id}`
- `POST /api/backtests/{id}/cancel`, `POST /api/backtests/{id}/retry`
- `GET /api/strategies/{id}/backtests`, `GET /api/strategies/{id}/backtest-defaults`
- `GET /api/backtests/candidates`
- `GET|POST /api/backtests/batches`
- `GET /api/backtests/batches/{id}`, `POST /api/backtests/batches/{id}/pause|resume|cancel`
- `GET /api/backtests/remote/status`, `GET /api/backtests/remote/audit?refresh=true`
- `POST /api/backtests/remote/setup`, `POST /api/backtests/remote/batches`

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

### Runner Darwinex Demo por SSH

El flujo remoto instala una copia portatil aislada en
`C:\MT5Backtest\DarwinexDemoRunner` y ejecuta exclusivamente la cuenta
`3000097316 / Darwinex-Demo`, con periodo `2016-01-01` a `2024-12-31` y modelo
OHLC M1. `remote/audit` compara perfiles, EX5, Magic, comentario, simbolo y
timeframe de las cuentas VPS; la respuesta se conserva cinco minutos para que
la creacion del batch no repita la lectura SSH.

Cada ejecucion descarga informe, set, INI, log y manifiesto SHA-256 a
`data\backtests\<run_id>`. El directorio temporal remoto solo se elimina
despues de validar hashes, informe y KPIs locales. El primer elemento es un
piloto obligatorio; si falla, el lote queda pausado. El runner no arranca si
el snapshot de seguridad Live/Demo no esta fresco o contiene posiciones u
ordenes pendientes.

### Axi 10025867

La instalacion local espejo de Axi usa el DataDir
`C:\Users\Admin\AppData\Roaming\MetaQuotes\Terminal\91667A77170349F91845E5FC96BD9A8C`
(`10025867 / Axi-US50-Demo`). El valor por defecto del instalador apunta a esta
terminal; use `-DataDir` para otra instalacion. El historial importado queda
separado de la sesion live y solo los mapeos confirmados atribuyen operaciones
nuevas a una estrategia.

La aplicación escucha únicamente en `127.0.0.1`.
