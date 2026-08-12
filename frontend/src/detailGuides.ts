export type DetailGuideId =
  | 'summary' | 'deployment' | 'comparison' | 'magic-numbers' | 'notes'
  | 'metrics' | 'sqx-analytics' | 'risk-guard' | 'backtest-risk'

export type DetailGuide = {
  title: string
  eyebrow: string
  analyzes: string
  source: string
  attributes: Array<[string, string]>
  interpretation: string
  example: string
  action: string
}

export const detailGuides: Record<DetailGuideId, DetailGuide> = {
  summary: {
    eyebrow: 'LECTURA DEL CUADRO', title: 'Resumen de estrategia',
    analyzes: 'Resume la actividad observada de la estrategia y el estado de salud calculado por el dashboard.',
    source: 'Operaciones importadas de la cuenta y reglas de salud del dashboard; P/L flotante incluye posiciones aún abiertas.',
    attributes: [['P/L realizado', 'Resultado neto de operaciones cerradas.'], ['P/L flotante', 'Resultado no realizado de posiciones abiertas.'], ['Health', 'Alertas activas, no una recomendación de trading.']],
    interpretation: 'Lee P/L y número de operaciones junto con Health: un resultado positivo no elimina una alerta de riesgo o de datos.',
    example: 'Un P/L positivo con Health amarillo puede indicar que la estrategia gana, pero se acerca a un límite o tiene poca muestra.',
    action: 'Investiga las razones de Health y los cuadros de riesgo antes de cambiar la estrategia o su tamaño.',
  },
  deployment: {
    eyebrow: 'LECTURA DEL CUADRO', title: 'VPS Deployment',
    analyzes: 'La evidencia de que el EA está adjunto a una terminal y al gráfico esperado.',
    source: 'Inventario más reciente del VPS/terminal y sus mapeos confirmados.',
    attributes: [['Cuenta y terminal', 'Dónde se detectó el EA.'], ['Chart', 'Símbolo y timeframe observados.'], ['Magic', 'Identificador que vincula operaciones con la estrategia.'], ['Último inventario', 'Momento de la última evidencia, no de la última ejecución.']],
    interpretation: 'Adjunto activo confirma presencia observada; no garantiza que el EA vaya a abrir operaciones.',
    example: '“Observed” sin operaciones recientes requiere comprobar terminal, AutoTrading, inputs y horario del mercado.',
    action: 'Si falta evidencia o el chart no coincide, revisa el despliegue antes de evaluar resultados.',
  },
  comparison: {
    eyebrow: 'LECTURA DEL CUADRO', title: 'Behavior vs. baseline',
    analyzes: 'Compara el comportamiento observado con el baseline seleccionado de SQX o MT5.',
    source: 'Historial operativo actual y el snapshot de baseline elegido en el selector.',
    attributes: [['Profit Factor', 'Beneficio bruto dividido por pérdida bruta.'], ['Expectancy', 'Resultado medio por operación.'], ['Return / DD', 'Retorno relativo al drawdown.'], ['Trades / month', 'Operaciones cerradas divididas por meses observados, con un mínimo de un mes para no extrapolar muestras cortas.'], ['SQN', 'Calidad estadística de la muestra.'], ['Δ', 'Diferencia porcentual frente al baseline.']],
    interpretation: 'Una diferencia aislada no prueba degradación: revisa operaciones, periodo, broker y fuente antes de concluir.',
    example: 'PF menor con pocas operaciones puede ser ruido; con muestra amplia y DD creciente merece revisión.',
    action: 'Confirma que baseline y muestra sean comparables; investiga cambios persistentes, no un único trade.',
  },
  'magic-numbers': {
    eyebrow: 'LECTURA DEL CUADRO', title: 'Magic Numbers',
    analyzes: 'Los identificadores numéricos con los que el mismo EX5 ha operado dentro de su servidor, cuenta y activo.',
    source: 'Mapeos confirmados, inventario VPS e historial importado.',
    attributes: [['Magic number', 'Identificador numérico de órdenes del EA.'], ['Cuenta/símbolo', 'Alcance donde ese magic está mapeado.'], ['Comentario', 'Patrón adicional de identificación cuando existe.']],
    interpretation: 'El Magic Number es evidencia histórica y puede desambiguar aliases conocidos, pero no forma parte de la identidad del bot.',
    example: 'Si el mismo EX5 cambia de 2000 a 8888, ambos aparecen bajo el mismo identificador con sus fechas y actividad.',
    action: 'Confirma o corrige el mapeo antes de usar P/L, rachas o drawdown para tomar decisiones.',
  },
  notes: {
    eyebrow: 'LECTURA DEL CUADRO', title: 'Notas de estrategia',
    analyzes: 'Registro operativo manual de observaciones, decisiones y seguimientos.',
    source: 'Texto guardado por los usuarios del dashboard con su fecha de actualización.',
    attributes: [['Fecha de guardado', 'Última actualización del registro.'], ['Contenido', 'Contexto humano; no modifica el EA ni sus inputs.']],
    interpretation: 'Úsalo para preservar por qué se investigó, pausó o cambió algo.',
    example: '“Revisar slippage tras el backtest Darwinex” deja trazabilidad para el siguiente operador.',
    action: 'Anota decisiones verificables y su motivo; no sustituyas la evidencia de backtests o logs.',
  },
  metrics: {
    eyebrow: 'LECTURA DEL CUADRO', title: 'Métricas compactas',
    analyzes: 'Características de las operaciones cerradas: aciertos, duración, rachas, tamaño de resultados y costes.',
    source: 'Historial combinado atribuido a la estrategia.',
    attributes: [['Win rate', 'Porcentaje de operaciones ganadoras.'], ['Duración', 'Tiempo medio de una operación.'], ['Rachas', 'Máximos consecutivos de ganancias y pérdidas.'], ['Comisión + swap', 'Costes incluidos en el resultado neto.']],
    interpretation: 'No valores una métrica sola: una tasa de acierto baja puede ser válida con ganancias medias mayores que pérdidas.',
    example: 'Un win rate estable con pérdida media que aumenta puede avisar de cambios en ejecución o riesgo.',
    action: 'Compara con baseline y Risk Guard cuando haya suficiente muestra.',
  },
  'sqx-analytics': {
    eyebrow: 'LECTURA DEL CUADRO', title: 'SQX Analytics',
    analyzes: 'Indicadores de calidad/robustez calculados desde la estrategia y datos de StrategyQuant X.',
    source: 'Snapshot SQX más reciente, con proyecto, databank, fuente de histórico y fecha mostrados en el cuadro.',
    attributes: [['Edge Decay', 'Señal de degradación del edge frente a su configuración.'], ['EGT', 'Medida agregada de calidad de la estrategia y su muestra.'], ['Strategy Doctor', 'Diagnóstico reproducible de órdenes históricas, hallazgos y recetas para validar.']],
    interpretation: 'Son análisis SQX; Doctor propone hipótesis basadas en órdenes históricas, no garantiza una mejora ni valida automáticamente un despliegue MT5.',
    example: 'Un Doctor aceptable o un EGT bueno no invalida un Risk Guard rojo si la operativa actual excede el DD histórico.',
    action: 'Actualiza el snapshot SQX si está ausente o antiguo y úsalo junto con métricas de ejecución.',
  },
  'risk-guard': {
    eyebrow: 'CONTROL DE RIESGO', title: 'Risk Guard',
    analyzes: 'Compara el drawdown y las rachas de pérdidas observadas contra los límites históricos IS y OOS de SQX.',
    source: 'Historial operativo combinado y baselines IS/OOS disponibles de SQX.',
    attributes: [['Combined', 'Máximo observado en el historial evaluado.'], ['IS / OOS limit', 'Referencia histórica in-sample u out-of-sample.'], ['Verde / amarillo / rojo', 'Dentro, cerca/en límite, o por encima del límite.']],
    interpretation: 'Mide comportamiento operativo acumulado, no el riesgo declarado de una operación del EA.',
    example: 'DD $678 frente a límite OOS $891 y racha 2 frente a 4 permanece dentro de límites.',
    action: 'Ante amarillo, vigila y revisa muestra; ante rojo, pausa o investiga antes de aumentar exposición. Risk Guard ≠ auditoría de backtest.',
  },
  'backtest-risk': {
    eyebrow: 'CONTROL DE RIESGO', title: 'Backtest Risk Audit',
    analyzes: 'Comprueba si la peor operación del reporte MT5 respeta el riesgo declarado por operación del EA.',
    source: 'Inputs guardados y métricas del último backtest válido de MT5.',
    attributes: [['Riesgo declarado', 'MMRiskedMoney × multiplicador, cuando money management está activo.'], ['Peor pérdida', 'Valor absoluto del peor trade del reporte.'], ['Ratio', 'Peor pérdida / riesgo declarado.'], ['Tolerancia', 'Hasta 10% por encima antes de marcar alerta.']],
    interpretation: 'Una alerta no es un incumplimiento IS/OOS: indica que una operación del backtest superó el riesgo nominal configurado.',
    example: '$200 declarados y $290.37 de pérdida producen ratio 1.45; excede la tolerancia y genera Risk alert.',
    action: 'Revisa lote y SL, spread/slippage/gaps, inputs y consistencia de broker/símbolo antes de modificar o desplegar el EA.',
  },
}
