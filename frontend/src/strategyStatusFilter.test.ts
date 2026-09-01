import { describe, expect, it } from 'vitest'
import { matchesStrategyStatus, withStrategyStatuses } from './strategyStatusFilter'

describe('manual strategy status', () => {
  const strategies = [
    { id: 1, strategy_statuses: ['live'] as const },
    { id: 2, strategy_statuses: ['monitor', 'axi_live'] as const },
    { id: 3, strategy_statuses: ['candidate_live'] as const },
    { id: 4, strategy_statuses: [] as const },
  ]

  it('filters every manual category, including strategies without one', () => {
    expect(strategies.filter(strategy => matchesStrategyStatus(strategy, 'all')).map(item => item.id)).toEqual([1, 2, 3, 4])
    expect(strategies.filter(strategy => matchesStrategyStatus(strategy, 'live')).map(item => item.id)).toEqual([1])
    expect(strategies.filter(strategy => matchesStrategyStatus(strategy, 'monitor')).map(item => item.id)).toEqual([2])
    expect(strategies.filter(strategy => matchesStrategyStatus(strategy, 'axi_live')).map(item => item.id)).toEqual([2])
    expect(strategies.filter(strategy => matchesStrategyStatus(strategy, 'candidate_live')).map(item => item.id)).toEqual([3])
    expect(strategies.filter(strategy => matchesStrategyStatus(strategy, '')).map(item => item.id)).toEqual([4])
  })

  it('updates all selected categories for an optimistic save or rollback', () => {
    const optimistic = withStrategyStatuses(strategies, 2, ['live', 'axi_live'])
    expect(optimistic[1].strategy_statuses).toEqual(['live', 'axi_live'])
    expect(withStrategyStatuses(optimistic, 2, ['monitor', 'axi_live'])[1].strategy_statuses).toEqual(['monitor', 'axi_live'])
  })
})
