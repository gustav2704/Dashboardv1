# SymbolSpecProbe

Script MT5 de solo lectura para la auditoría HTML FPM vs. Darwinex. Consulta
propiedades `SymbolInfo*` y escribe un JSON local; no llama a ninguna función
de trading y no modifica posiciones u órdenes.

## Ejecución única

En cada terminal, abre `Navigator > Scripts > ContractRiskAudit` y arrastra
`SymbolSpecProbe` a cualquier gráfico. Acepta los valores por defecto y pulsa
`OK`.

| Terminal | Archivo de salida |
|---|---|
| FPM | `MQL5\Files\ContractRiskAudit\symbol_specs_FPM.json` |
| Darwinex Strategy Tester | `MQL5\Files\ContractRiskAudit\symbol_specs_DarwinexCustom.json` |
| Darwinex Live | `MQL5\Files\ContractRiskAudit\symbol_specs_DarwinexLive.json` |

Los símbolos de cada fuente ya están preconfigurados en
`MQL5\Files\ContractRiskAudit\SymbolSpecProbeConfig.csv`.

Después de las tres ejecuciones, genera el artefacto independiente:

```powershell
& .\.venv\Scripts\python.exe .\scripts\build_contract_risk_report.py
```

El HTML y CSV se guardan en `data\exports`. No se modifica el dashboard, la
base de datos, la API ni los EAs de estrategia.
