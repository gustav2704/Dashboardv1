export type StrategyStatus = '' | 'live' | 'monitor' | 'candidate_live' | 'axi_live'
export type StrategyStatusFilter = 'all' | StrategyStatus

type StatusStrategy = { strategy_status: StrategyStatus }

export function matchesStrategyStatus<T extends StatusStrategy>(strategy: T, filter: StrategyStatusFilter) {
  return filter === 'all' || strategy.strategy_status === filter
}

export function withStrategyStatus<T extends { id: number; strategy_status: StrategyStatus }>(strategies: T[], strategyId: number, status: StrategyStatus) {
  return strategies.map(strategy => strategy.id === strategyId ? { ...strategy, strategy_status: status } : strategy)
}
