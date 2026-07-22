# Inventarios reales de VPS

Copiar cada CSV principal en una subcarpeta cuyo nombre sea la cuenta MT5:

`data/imports/bot_inventory/vps/<cuenta>/BotInventory_<cuenta>_<fecha>.csv`

Solo esta carpeta se importa automáticamente. Los archivos `_History.csv` y
`_Diagnostics.csv` pueden conservarse junto al principal, pero no son necesarios
para detectar los EAs adjuntos.
