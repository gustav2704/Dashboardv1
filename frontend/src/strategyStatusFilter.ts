export type StrategyStatus = '' | 'live' | 'monitor' | 'candidate_live' | 'axi_live' | 'favorite' | 'to_modify'
export type StrategyStatusFilter = 'all' | StrategyStatus | ''

type StatusStrategy = { strategy_statuses: readonly StrategyStatus[] }

export function matchesStrategyStatus<T extends StatusStrategy>(strategy: T, filter: StrategyStatusFilter) {
  return filter === 'all' || (filter === '' ? strategy.strategy_statuses.length === 0 : strategy.strategy_statuses.includes(filter))
}

export function withStrategyStatuses<T extends { id: number; strategy_statuses: readonly StrategyStatus[] }>(strategies: T[], strategyId: number, statuses: StrategyStatus[]) {
  return strategies.map(strategy => strategy.id === strategyId ? { ...strategy, strategy_statuses: statuses } : strategy)
}
