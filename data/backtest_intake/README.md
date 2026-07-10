# Entrega de material para backtests incompletos

Coloca los archivos de cada bot dentro de su carpeta correspondiente sin
renombrarlos. Se aceptan:

- El `.ex5` exacto que se ejecuta en MT5.
- El `.mq5` o `.sqx`, si está disponible.
- El `.set` cuando el EA no usa sus parámetros predeterminados.
- Un informe MT5 `.htm` o `.html`, si ya existe.

Completa también el archivo `backtest-info.json` de la carpeta. Los nombres de
símbolo deben ser los del broker FPM. No incluyas contraseñas, credenciales ni
copias completas del directorio del terminal.

El lote automático queda pausado hasta que el terminal de backtests pueda
utilizarse con seguridad. Nunca se cerrarán posiciones ni se eliminarán órdenes.
