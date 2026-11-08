/**
 * Minimum-trades filter for the Overview table: hides strategies whose trade
 * count is too small for their metrics to be trusted.
 */
export function parseMinTrades(input: string): number | null {
  const value = Number.parseInt(input.trim(), 10)
  return Number.isFinite(value) && value > 0 ? value : null
}

export function meetsMinTrades(trades: number | null | undefined, min: number | null) {
  if (min == null) return true
  return (trades ?? 0) >= min
}
