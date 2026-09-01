# Estado SQX · Edge y EGT

Actualizado: 31 de agosto de 2026, tras reiniciar SQX y sincronizar `Retester / Results`.

## Resumen

| Métrica | Bots |
|---|---:|
| Filas actualmente presentes en SQX Results | 64 |
| Filas sincronizadas desde la grilla actual | 64 |
| EGT disponible en esas filas | 64 |
| EGT pendiente en esas filas | 0 |
| Snapshots históricos almacenados en Dashboard | 91 |
| Snapshots con EGT almacenado | 91 |
| Snapshots históricos con Edge disponible | 27 |

Las estrategias que hoy no están en Results no se clasifican como “sin EGT”;
conservan el último snapshot hasta que vuelvan a cargarse.

## Resultado de la sincronización

La grilla de SQX confirmó y guardó, para las 64 filas actuales, el símbolo, la
temporalidad, las métricas IS/OOS y el valor EGT que muestra SQX. No se marcó
ninguna estrategia como ausente.

El endpoint SQX `bulk` continúa sin ser fiable tras un retest grande. Esta
sincronización usa la grilla rápida de Results y no inspecciona estrategias una
a una. La grilla no contiene las órdenes necesarias para recalcular Edge; en
las 64 filas recién actualizadas Edge se marca como pendiente, en vez de
conservar un valor antiguo como si fuese actual.

## Protección de sincronización aplicada

Una respuesta vacía o parcial de SQX no puede marcar bots como ausentes ni
reemplazar una muestra OOS válida. La sincronización desde grilla solo actualiza
lo que SQX expone de forma verificable.
