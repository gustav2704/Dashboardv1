// Pattern classification for the strategy-diagnosis event view.  It mirrors the
// backend classifier additions (NoMM / denied-entry lines) and groups events so
// the panel can show what kind of operational trouble a strategy is in.
export type StrategyEventCategory = 'no_mm' | 'invalid_stops' | 'denied' | 'other'

const NO_MM_RE = /\b(?:no\s*mm|sin\s*mm|no\s+money\s+management|sin\s+gesti[oó]n\s+de\s+riesgo)\b/i
const INVALID_STOPS_RE = /\b(?:invalid\s+(?:price|stops?)|min\s+distance|incorrect\s+price|too\s+(?:tight|close)|ceñid[oa]|stop\s+muy\s+ceñido|stop\s+loss\s+too)\b/i
const DENIED_RE = /\b(?:denegad\w*|rechazad\w*|entrada\s+negada|failed|order\s+rejected|retcode\s*:\s*1001[0-9]|not\s+enough|requote|off\s+quotes?)\b/i

export function classifyStrategyEvent(message: string, context = ''): StrategyEventCategory {
  const text = `${message} ${context}`
  if (NO_MM_RE.test(text)) return 'no_mm'
  if (INVALID_STOPS_RE.test(text)) return 'invalid_stops'
  if (DENIED_RE.test(text)) return 'denied'
  return 'other'
}

export const strategyEventCategoryLabels: Record<StrategyEventCategory, string> = {
  no_mm: 'Entradas con NoMM / sin gestión',
  invalid_stops: 'Stops / precio inválido (SL ceñido)',
  denied: 'Entrada denegada / orden fallida',
  other: 'Otros',
}

export function summarizeStrategyEvents(events: Array<{ message: string; context: string }>): Record<StrategyEventCategory, number> {
  const summary: Record<StrategyEventCategory, number> = { no_mm: 0, invalid_stops: 0, denied: 0, other: 0 }
  for (const event of events) summary[classifyStrategyEvent(event.message, event.context)] += 1
  return summary
}
