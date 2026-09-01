# EGT de un único bot SQX

## Regla de tiempo

Cuando falta el EGT de una fila concreta, **nunca** se ejecuta la
sincronización global de `Retester / Results`. La operación dirigida tiene un
presupuesto total de **20 segundos**:

1. Hasta 5 segundos para confirmar el nombre exacto en la cuadrícula de SQX.
2. Hasta 15 segundos para inspeccionar solo esa estrategia y recuperar sus
   métricas EGT.

No se ejecuta el análisis Doctor (órdenes) en esta ruta. No se hacen reintentos
automáticos ni se cambia a un lote completo si SQX se demora.

## Operación

Use el identificador de la fila del Dashboard:

```json
POST /api/sqx/sync
{
  "project": "Retester",
  "databank": "Results",
  "strategy_id": 453
}
```

La actualización se vincula solo si el nombre SQX encontrado coincide de forma
única con la estrategia. Si se completa, persiste los snapshots SQX, incluido
el EGT, para que el Dashboard lo muestre en la misma identidad desplegada.

## Si no responde

Un timeout significa que SQX no respondió dentro del presupuesto, no que el
bot carezca de EGT. Mantenga el valor existente sin modificar, informe el
timeout y deje pendiente la actualización. Nunca se debe usar el EGT de otra
estrategia ni una coincidencia aproximada.

La sincronización global solo se usa para un refresco explícito de todo el
banco, fuera de una investigación de una fila individual.
