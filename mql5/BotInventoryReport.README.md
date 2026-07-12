# BotInventoryReport

Script MQL5 de solo lectura que inventaria los EAs adjuntos a todos los charts
abiertos del perfil actual y cruza sus nombres con Magic Numbers catalogados.

Si no hay coincidencia exacta en el catalogo, intenta asociar el EA con el
historial por simbolo y por la firma numerica del nombre/comentario, por ejemplo
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
5. En MT5, arrastrar `Scripts > Dashboardv1 > BotInventoryReport` a cualquier
   chart del perfil que se desea inspeccionar.

Los archivos se crean en `<DataDir>\MQL5\Files\BotInventoryReport`.

- `BotInventory_<cuenta>_<fecha>.csv`: inventario principal por chart/EA/MN.
- `BotInventory_<cuenta>_<fecha>.html`: reporte visual con diagnostico.
- `BotInventory_<cuenta>_<fecha>_History.csv`: historial agrupado por simbolo y MN.
- `BotInventory_<cuenta>_<fecha>_CatalogPending.csv`: sugerencias para completar
  el catalogo cuando hubo asociacion automatica o MN desconocido.
- `BotInventory_<cuenta>_<fecha>_Diagnostics.csv`: ruta del catalogo, filas leidas
  y contadores de coincidencias.

## Interpretacion

- `ACTIVO`: el EA esta adjunto, el terminal esta conectado, Algo Trading esta
  habilitado y la cuenta permite operar mediante EAs.
- `INACTIVO`: el EA esta adjunto pero falla al menos una de esas condiciones.
- `NUNCA_OPERO`: el Magic Number catalogado no tiene entradas BUY/SELL en todo
  el historial disponible de la cuenta actual.
- `MN_DESCONOCIDO`: el EA no coincide con ninguna fila del catalogo; no se
  concluye si opero o no.
- `HISTORIAL_AUTO`: el catalogo no coincidio, pero el historial tenia un unico MN
  compatible por simbolo y firma de estrategia. Revisar y, si es correcto, pasar
  esa fila desde `CatalogPending.csv` al catalogo oficial.
- `NO_DETERMINABLE`: no hay MN fiable para ese EA; por tanto no se puede afirmar
  que nunca opero.

El estado de Algo Trading se obtiene a nivel global. MQL5 no permite que un
script inspeccione los parametros internos ni el permiso individual de otro EA.
El chart desde donde se ejecuta el script se excluye del inventario para no
contaminar el reporte con un falso `.ex5`.
