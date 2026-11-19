export type StrategyStatus = '' | 'live' | 'monitor' | 'candidate_live' | 'axi_live' | 'favorite' | 'to_modify'
export type StrategyStatusFilter = 'all' | StrategyStatus | '' | 'candidate_live_only'

type StatusStrategy = { strategy_statuses: readonly StrategyStatus[] }

export function matchesStrategyStatus<T extends StatusStrategy>(strategy: T, filter: StrategyStatusFilter) {
  if (filter === 'all') return true
  if (filter === '') return strategy.strategy_statuses.length === 0
  // Only the "candidata live" mark, with no other mark alongside it.
  if (filter === 'candidate_live_only') return strategy.strategy_statuses.length > 0 && strategy.strategy_statuses.every(status => status === 'candidate_live')
  return strategy.strategy_statuses.includes(filter)
}

export function withStrategyStatuses<T extends { id: number; strategy_statuses: readonly StrategyStatus[] }>(strategies: T[], strategyId: number, statuses: StrategyStatus[]) {
  return strategies.map(strategy => strategy.id === strategyId ? { ...strategy, strategy_statuses: statuses } : strategy)
}
