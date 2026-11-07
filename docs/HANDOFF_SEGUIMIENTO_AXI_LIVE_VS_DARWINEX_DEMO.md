# Handoff: seguimiento de bots Axi live frente a Darwinex demo

Preparado el 27 de octubre de 2026. Este documento se entrega a otro agente/harness para desarrollar un proyecto independiente. Dashboardv1 es una referencia de acceso y protocolo; no es el lugar donde implementar el proyecto nuevo.

## 1. Encargo

El usuario selecciona los mejores bots de una cuenta demo y los despliega en una cuenta real. Quiere seguir los bots que están en la real, compararlos con sus equivalentes en demo e investigar diferencias de comportamiento en ambos sentidos: bots que empeoran o mejoran en real. La diferencia de broker es una hipótesis a contrastar.

Primera entrega del nuevo proyecto: conexión observadora a las dos fuentes, extracción verificable del historial y un mapa de equivalencias entre bots. Después: comparación de operaciones y explicación de diferencias respaldada por datos.

## 2. Conexiones actuales

Fuente: lectura de `data/dashboard.db`, tabla `terminals`, registros 267 y 210. Los IDs pertenecen únicamente a Dashboardv1; no son números de cuenta ni identificadores universales.

| Campo | Cuenta real objetivo | Cuenta demo de referencia |
|---|---|---|
| Nombre | Axi Live 60366027 VPS (Tailscale) | Darwinex Demo VPS |
| ID en Dashboardv1 | 267 | 210 |
| Login MT5 | `60366027` | `3000097316` |
| Servidor del broker | `Axi-US51-Live` | `Darwinex-Demo` |
| Host SSH | `100.107.77.50` | `145.63.130.53` |
| Puerto SSH | `22` | `22` |
| Usuario Windows/SSH | `Administrador` | `Administrator` |
| Huella SSH registrada | `SHA256:odtfgtI5dkXYMdQo+S6PvcuU8EGcmv/ydIJg+UGXYrA` | `SHA256:befdzdGXzUTgWGQeGiSdNKJequtSn7/rlm9OgxFpSZU` |
| Transporte | SSH/SFTP a través de Tailscale | SSH/SFTP a IP pública |

**DataDir remoto de Axi live:**

```text
C:\Users\Administrador\AppData\Roaming\MetaQuotes\Terminal\6FBEE76C719DC78AB2AE839B5A0C7442
```

**DataDir remoto de Darwinex demo:**

```text
C:\Users\Administrator\AppData\Roaming\MetaQuotes\Terminal\6C3C6A11D1C3791DD4DBF45421BF8028
```

La máquina que ejecute el nuevo harness necesita conectividad a la misma red Tailscale para alcanzar `100.107.77.50`. Disponer de la clave SSH no incorpora una máquina nueva a Tailscale: esa incorporación requiere autorización de red por separado. Si el harness corre en este mismo PC y ve esa red, puede aprovechar la conectividad existente. No se ha identificado una IP pública alternativa vigente para Axi live.

No confundir Axi live con **Axi Demo**, cuenta `10025867`, servidor `Axi-US50-Demo`, terminal 211 en `145.63.130.53`. Ambas instalaciones tienen un DataDir con el mismo último componente `6FBEE76C719DC78AB2AE839B5A0C7442`: host y usuario forman parte de la identidad de la ruta. Tampoco confundir Darwinex demo con Darwinex Live, cuenta `4000094894`.

`ssh_access/access_ssh.txt` contiene la IP antigua `145.63.135.196` y material RSA histórico. No usarlo como configuración vigente de estas dos fuentes. La configuración actual de Darwinex demo apunta a `145.63.130.53`.

## 3. Claves, contraseñas y reutilización

Raíz local de referencia:

```text
D:\08_proyectos_claudecode\06_sqx_mcp\Dashboardv1
```

Las dos terminales tienen configurada **la misma clave privada ED25519**:

```text
D:\08_proyectos_claudecode\06_sqx_mcp\Dashboardv1\ssh_access\codex_vps_ed25519
```

Su clave pública está junto a ella:

```text
D:\08_proyectos_claudecode\06_sqx_mcp\Dashboardv1\ssh_access\codex_vps_ed25519.pub
```

Se ha comprobado localmente que Paramiko carga la clave privada sin passphrase. No hay una contraseña que introducir para descifrar ese archivo. El conector actual usa autenticación por clave, no contraseña Windows ni contraseña MT5.

**Reutilización:** el cambio de proyecto/harness no obliga a generar otra clave ni a cambiar contraseñas. En este mismo PC, el nuevo agente puede referenciar el archivo existente si sus permisos lo permiten. En otra máquina o contenedor, esa ruta no existirá necesariamente: habrá que facilitar acceso seguro a la clave, por ejemplo como secreto montado en un archivo privado, y configurar su nueva ruta. La clave pública `.pub` por sí sola no permite autenticar.

La aceptación remota de la clave depende de que siga autorizada en cada VPS; comprobarla con el diagnóstico antes de dar por operativo el acceso. No rotar ni sobrescribir la clave actual durante el handoff: Dashboardv1 también depende de ella. Una clave nueva solo es necesaria si no se puede proporcionar la existente al nuevo entorno, se ha revocado o se decide separar accesos.

**Validación realizada durante este handoff:** el conector existente autenticó con esa clave y comprobó la huella registrada en ambas VPS. `whoami` devolvió `srv\administrador` en Axi y `prime-intel-de0\administrator` en Darwinex. Se leyeron por SFTP `origin.txt` y las carpetas de respuestas/archivo de ambas terminales. En ese momento las respuestas estaban archivadas, sin un `sync.response.json` en la carpeta principal. No se escribieron solicitudes ni se movieron archivos. `origin.txt` identifica las instalaciones `C:\Program Files\Axi MetaTrader 5 Terminal` y `C:\Program Files\Darwinex MetaTrader 5`, respectivamente; para los datos usar los DataDir de la sección 2, no estas carpetas de programa.

En Windows OpenSSH, las claves públicas de usuarios administradores suelen autorizarse en:

```text
C:\ProgramData\ssh\administrators_authorized_keys
```

Esa ubicación procede del runbook existente; no se ha verificado en esta entrega leyendo el archivo de autorización de ambas VPS. No modificarlo para una simple reutilización. Nunca subir la clave privada a la VPS; solo se autoriza allí la pública si fuera necesario crear un acceso distinto.

**Dónde están las contraseñas:** no se ha identificado un almacén de contraseñas Windows/RDP o de los brokers para estas cuentas en los archivos de conexión y el código revisados. `ssh_access/access_ssh.txt` contiene una clave privada RSA histórica, no una contraseña vigente verificada. Este handoff no permite afirmar cuáles son las contraseñas Windows o MT5. Para leer los archivos que generan las terminales ya conectadas no se necesitan esas contraseñas. Si el nuevo proyecto pretende abrir una terminal nueva y autenticarse directamente contra el broker, necesitará que el usuario facilite las credenciales MT5 apropiadas; la clave SSH no las sustituye.

`ssh_access/` está excluido por `.gitignore`: clonar el repositorio o entregar solo este documento no transfiere la clave. El documento contiene localizadores, no secretos privados. Dar al otro agente acceso al archivo mediante el mecanismo de secretos de su harness. Los permisos del sandbox del nuevo agente son independientes de que la clave sea técnicamente reutilizable.

## 4. Diagnóstico reproducible sin modificar la VPS

Se entrega junto a este documento `handoff_mt5_ssh_probe.py`. Requiere Python y `paramiko`; no requiere Dashboardv1 ni su base de datos. Verifica la huella del servidor **antes de autenticarse**, usa únicamente la clave indicada y comprueba por SFTP la existencia del DataDir y de las carpetas de respuestas. No solicita contraseñas ni acepta automáticamente otra huella. No escribe solicitudes ni archiva respuestas.

Desde este PC:

El script adjunto también se ejecutó satisfactoriamente contra ambas VPS: huellas verificadas, autenticación aceptada y las tres rutas comprobadas existentes. En el entorno restringido del agente, la primera ejecución no pudo leer la clave (`PermissionError`); al ejecutarlo con los permisos de acceso necesarios funcionó sin cambiar claves ni contraseñas. Si el nuevo harness reproduce ese error, revisar sus permisos de lectura antes de generar credenciales nuevas.

```powershell
& 'D:\08_proyectos_claudecode\06_sqx_mcp\Dashboardv1\.venv\Scripts\python.exe' `
  'D:\08_proyectos_claudecode\06_sqx_mcp\Dashboardv1\docs\handoff_mt5_ssh_probe.py' `
  --key 'D:\08_proyectos_claudecode\06_sqx_mcp\Dashboardv1\ssh_access\codex_vps_ed25519'
```

En otro entorno: instalar `paramiko` según las convenciones de ese proyecto y pasar la ruta donde se haya montado la misma clave. Un timeout hacia Axi puede ser un problema de Tailscale/ruta/ACL de red; no demuestra una contraseña incorrecta. Un rechazo `publickey` requiere revisar usuario y autorización de la clave. Si cambia la huella, verificarla por un canal independiente antes de actualizarla.

## 5. Cómo se obtiene el historial

SSH es el transporte. La fuente de historial y estado interno es MT5, mediante un servicio que se ejecuta dentro de cada terminal: `DashboardBridge`. Una sesión PowerShell por SSH no equivale a una conexión a la API de MT5. El paquete Python `MetaTrader5` tampoco se conecta por SSH por sí solo.

Archivos de referencia locales:

```text
backend\app\ssh_bridge.py          Transporte SSH/SFTP y comprobación de huella
backend\app\mt5_bridge.py          Solicitudes, ingestión y cursores
mql5\DashboardBridge.mq5           Productor dentro de MT5
mql5\BotInventoryReport.mq5        Inventario de EAs adjuntos
mql5\BotInventoryReport.README.md   Uso y formato del inventario
mql5\SymbolSpecProbe.mq5           Extracción ampliada de especificaciones
docs\SSH_VPS_MT5_RUNBOOK.md         Preparación Windows/OpenSSH
```

En cada DataDir remoto:

```text
MQL5\Files\Dashboardv1\Requests\sync.request.json
MQL5\Files\Dashboardv1\Responses\sync.response.json
MQL5\Files\Dashboardv1\Responses\Archive\
MQL5\Files\Dashboardv1\Requests\chart.request.json
MQL5\Files\Dashboardv1\Responses\chart.response.json
MQL5\Profiles\Charts\
MQL5\Experts\
MQL5\Logs\
Logs\
```

El Dashboard existente solicita sincronizaciones y mueve respuestas a `Archive`. La ausencia puntual de `sync.response.json` puede ser normal. Primero aprovechar lecturas de archivos existentes y copias de datos; no iniciar un segundo consumidor que mueva o borre respuestas.

El protocolo actual tiene nombres únicos compartidos por terminal: dos consumidores independientes pueden sobrescribir solicitudes o consumir respuestas ajenas. Para el proyecto nuevo elegir una coordinación explícita: un único recolector con distribución de datos, una exportación desde el existente, o un productor y carpetas propios. Preparar ese cambio antes de activar polling independiente. No cambiar el servicio ni reiniciar terminales live durante una prueba de conectividad.

Una solicitud completa según el código local es:

```json
{"request_id":"seguimiento-inicial","command":"sync","since_msc":0,"capital_since_msc":0,"requested_at":"FECHA_UTC_ISO8601"}
```

Es una referencia de protocolo, no una instrucción para escribir ahora en la cola compartida. Con `since_msc=0`, el servicio consulta desde el inicio el historial que MT5 tenga disponible. Verificar cobertura, primera/última fecha y conteos; no prometer que contenga todo el historial del broker. El código local emite `schema_version=4`; comprobar la versión realmente desplegada.

La respuesta incluye identidad de cuenta/servidor, estado de conexión, balance/equity/margen, deals, movimientos de capital, posiciones, órdenes pendientes y especificaciones básicas. Los deals incluyen ticket, `position_id`, `time_msc`, símbolo, tipo/entrada, volumen, precio, beneficio, comisión, swap, magic y comentario. El bridge local no exporta por esta vía todo el historial de órdenes ni toda la información de ejecución. Las especificaciones básicas actuales son volumen mínimo y paso de volumen; para una auditoría de broker hacen falta datos adicionales.

Validar siempre que el JSON corresponde al login y servidor esperados antes de importar. Persistir originales y deduplicar deals por servidor + cuenta + ticket; reconstruir posiciones y cierres parciales usando `position_id` dentro de esa identidad. No usar el cursor del Dashboard como inicio de una importación nueva: omitiría operaciones antiguas. Usar cursores propios, solapamiento temporal y deduplicación para lecturas incrementales.

`generated_at` se genera en UTC, pero no asumir sin validación que todos los tiempos históricos/velas del broker estén ya normalizados a UTC: conservar valores originales y verificar horario de servidor y cambios estacionales.

El Dashboard local usa `http://127.0.0.1:8765` y tiene `GET /api/terminals`. Su SQLite está en `data\dashboard.db`. Son referencias locales, no servicios públicos accesibles desde otro harness. No copiar toda la base como si cada terminal fuese una cuenta distinta: contiene espejos, archivos e importaciones históricas. Para una copia consistente de SQLite en uso, usar backup de SQLite, no copiar solamente el `.db` ignorando su posible WAL.

## 6. Comparación que debe construir el nuevo agente

1. Empezar por los EAs desplegados en Axi live y encontrar su equivalente en Darwinex demo. Conservar EX5/nombre/hash cuando sea accesible, parámetros, timeframe, símbolo exacto, magic, comentario y fechas de despliegue. Un magic o nombre coincidente no demuestra equivalencia de versiones.
2. Comparar solo períodos en que el bot estuvo activo en ambas cuentas. Separar operaciones ausentes, desfases de señal, diferencias de entrada/salida y diferencias de tamaño/riesgo/costes. Mantener sin emparejar los casos ambiguos.
3. Validar equivalencias de símbolos y contratos: tamaño de contrato, tick size/value, divisa, volumen mínimo/paso, sesiones, zona horaria, stops mínimos, ejecución y sufijos. El mismo lote puede representar riesgos distintos.
4. Mostrar beneficio bruto y neto, comisiones, swap, retorno normalizado por riesgo/capital, cantidad de operaciones y drawdown observado. El drawdown de balance reconstruido desde cierres no equivale al drawdown intradía de equity.
5. Para explicar causalmente al broker: cotejar precios/velas, spreads disponibles, horarios, rechazos y registros de EA/terminal. El slippage exige precio solicitado frente a ejecutado; no se puede deducir solo de dos precios de ejecución en brokers distintos. La latencia exige evidencia temporal apropiada.
6. El resultado por bot debe señalar si va mejor, peor o parecido en real, tamaño de la muestra, evidencia de diferencias y grado de confianza. Clasificar como causa confirmada, hipótesis o dato faltante. Dos cuentas distintas confunden efecto broker y efecto demo/real; sus resultados por sí solos no aíslan causalidad.

## 7. Límites operativos y pendientes

El nuevo proyecto es observador. No abrir/cerrar/modificar operaciones, cambiar inputs de EAs, reiniciar terminales, ejecutar testers en instalaciones operativas ni tocar la configuración del Dashboard existente. No usar `C:\MT5Backtest\DarwinexDemoRunner` como fuente operativa: es un runner aislado de backtesting.

Pendientes que debe resolver el nuevo agente al comenzar: acceso de su harness al archivo privado, conectividad Tailscale, comprobación SSH con huella fijada, cobertura efectiva del historial, inventario de bots y arquitectura de recolección que no compita por la cola compartida. Estas verificaciones no requieren crear contraseñas nuevas por defecto.
