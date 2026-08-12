# BotInventoryReport

Script MQL5 de solo lectura que inventaria los EAs adjuntos a todos los charts
abiertos del perfil actual y cruza sus nombres con Magic Numbers catalogados.

Si no hay coincidencia exacta en el catalogo, intenta asociar el EA primero con
deals ejecutados y despues con ordenes historicas o pendientes actuales, siempre
por simbolo y firma numerica del nombre/comentario. Por ejemplo,
`Strategy 4.7.21(2)` contra `US30_Strategy_4_7_21_2`.

## Instalacion

1. Copiar `BotInventoryReport.mq5` a
   `<DataDir>\MQL5\Scripts\Dashboardv1\BotInventoryReport.mq5` y compilarlo.
2. Copiar `BotInventoryCatalog.csv` a
   `<DataDir>\MQL5\Files\BotInventoryReport\BotInventoryCatalog.csv`.
3. Opcionalmente, para compartir un solo catalogo entre terminales del mismo
   Windows/VPS, copiarlo a la carpeta comun de MetaTrader:
   `<CommonDataDir>\Files\BotInventoryReport\BotInventoryCatalog.csv`.
   El script busca primero en el `DataDir` del terminal y luego en `FILE_COMMON`.
4. Editar el catalogo con una fila por combinacion EX5/Magic Number. El archivo
   usa comas y admite campos entre comillas cuando un comentario contiene comas.
   Con `AutoSeedCatalogFromHistory=true` (valor por defecto), el script agrega
   automaticamente al catalogo local las coincidencias unicas basadas en deals
   ejecutados. Nunca inventa un Magic Number para un EA sin historial.
5. En MT5, arrastrar `Scripts > Dashboardv1 > BotInventoryReport` a cualquier
   chart del perfil que se desea inspeccionar.

Los archivos se crean en `<DataDir>\MQL5\Files\BotInventoryReport`.

- `BotInventory_<cuenta>_<fecha>.csv`: inventario principal por chart/EA/MN.
- `BotInventory_<cuenta>_<fecha>.html`: reporte visual con diagnostico.
- `BotInventory_<cuenta>_<fecha>_History.csv`: entradas y ordenes agrupadas por
  simbolo y MN, incluyendo fechas, comentarios y estados de orden.
- `BotInventory_<cuenta>_<fecha>_CatalogPending.csv`: sugerencias para completar
  el catalogo cuando hubo asociacion automatica o MN desconocido.
- `BotInventory_<cuenta>_<fecha>_Diagnostics.csv`: ruta del catalogo, filas leidas
  y contadores de coincidencias.

## Interpretacion

- `ACTIVO`: el EA esta adjunto, el terminal esta conectado, Algo Trading esta
  habilitado y la cuenta permite operar mediante EAs.
- `INACTIVO`: el EA esta adjunto pero falla al menos una de esas condiciones.
- `NUNCA_OPERO`: el Magic Number catalogado no tiene entradas BUY/SELL en todo
  el historial disponible de la cuenta actual. Puede haber colocado ordenes.
- `CON_ENTRADAS`: existe al menos una entrada BUY/SELL ejecutada.
- `SOLO_ORDENES`: el EA coloco una orden historica o mantiene una pendiente,
  pero no existe ninguna entrada BUY/SELL ejecutada en el historial disponible.
- `SIN_ACTIVIDAD`: el MN se conoce por catalogo, pero no hay deals ni ordenes.
- `NO_DETERMINABLE`: no se conoce un MN fiable para clasificar la actividad.
- `MN_DESCONOCIDO`: el EA no coincide con ninguna fila del catalogo; no se
  concluye si opero o no.
- `HISTORIAL_DEALS_AUTO`: asociacion automatica mediante una entrada ejecutada.
- `HISTORIAL_ORDENES_AUTO`: asociacion automatica mediante una orden historica
  o pendiente actual. Revisar y pasar la fila sugerida al catalogo oficial.

El estado de Algo Trading se obtiene a nivel global. MQL5 no permite que un
script inspeccione los parametros internos ni el permiso individual de otro EA.
El chart desde donde se ejecuta el script se excluye del inventario para no
contaminar el reporte con un falso `.ex5`.
