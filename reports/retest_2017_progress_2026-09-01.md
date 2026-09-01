# Reporte de progreso — retest histórico desde 2017

**Corte:** 1 de septiembre de 2026

## Resumen ejecutivo

El lote **49** continúa en estado `running`, contra el runner aislado **Darwinex Demo por SSH**. No se ha usado ningún terminal operativo ni se han tocado posiciones u órdenes reales.

| Estado | Cantidad |
|---|---:|
| Retests validados y completados | 10 |
| Retest actualmente en ejecución | 1 |
| En cola | 31 |
| Fallido por validación técnica | 1 |
| Alcance del lote | 43 |

Avance validado: **10/43 (23,3 %)**. El porcentaje excluye el test actualmente en curso.

## Trabajo en curso

- **Estrategia 349:** `DosVelas_EURUSD_m15`
- **Ejecución:** 525
- **Mercado y marco temporal:** EURUSD, M15
- **Periodo:** 2017-01-01 a 2026-07-27
- **Estado:** `running`

La prueba lleva más tiempo que las anteriores, pero el runner informa exactamente **un** proceso `terminal64.exe` y su diario continúa registrando operaciones y eventos del Strategy Tester. No hay una firma de error ni una segunda instancia concurrente. Por ello se mantiene la ejecución y no se relanza.

## Salud del runner

| Control | Estado |
|---|---|
| Entorno | Darwinex Demo aislado (`DarwinexDemoSSH`) |
| Procesos de tester | 1 |
| Espacio libre | 54,73 GB |
| Mínimo requerido | 5 GB |
| Seguridad para ejecutar | Aprobada para runner aislado |
| Conexión del terminal remoto | Conectada |

## Incidencia preservada

La estrategia **2** (ejecución 515) figura como `validation_failed` únicamente por una carrera de planificación: se intentó arrancar cuando el tester aislado ya tenía un proceso. No es un resultado de trading ni un error de datos. Se conserva el intento y se reintentará sólo de forma secuencial al finalizar la cola activa.

## Próximos pasos operativos

1. Esperar a que finalice la ejecución 525 y validar su informe/métricas.
2. Procesar los 31 elementos restantes, uno a uno.
3. Reintentar la estrategia 2 en exclusividad.
4. Tratar en un lote separado las 14 estrategias aún pendientes de resolver por identidad/artefacto.
