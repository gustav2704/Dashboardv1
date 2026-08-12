import type { JournalAnalysisData } from './journalTypes'

type IdentifiedStrategy = { id: number }

export function parseStrategyIds(query: string) {
  return new Set(
    query
      .split(/[\s,]+/)
      .filter(part => /^\d+$/.test(part))
      .map(Number)
  )
}

export function filterStrategiesByIds<T extends IdentifiedStrategy>(strategies: T[], strategyIds: Set<number>) {
  if (strategyIds.size === 0) return strategies
  return strategies.filter(strategy => strategyIds.has(strategy.id))
}

export function filterJournalByStrategyIds(data: JournalAnalysisData | null, strategyIds: Set<number>) {
  if (!data || strategyIds.size === 0) return data

  const trades = data.trades.filter(trade => strategyIds.has(trade.strategy_id))
  const attributedTrades = trades.filter(trade => trade.source_role !== 'unassigned').length

  return {
    ...data,
    trades,
    reconciliation: {
      raw_closed_trades: trades.length,
      attributed_trades: attributedTrades,
      unassigned_trades: trades.length - attributedTrades,
      displayed_trades: trades.length,
      status: 'reconciled' as const,
    },
  }
}
