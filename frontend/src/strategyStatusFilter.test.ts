import { describe, expect, it } from 'vitest'
import { matchesStrategyStatus, withStrategyStatus } from './strategyStatusFilter'

describe('manual strategy status', () => {
  const strategies = [
    { id: 1, strategy_status: 'live' as const },
    { id: 2, strategy_status: 'monitor' as const },
    { id: 3, strategy_status: 'candidate_live' as const },
    { id: 4, strategy_status: '' as const },
  ]

  it('filters every manual category, including strategies without one', () => {
    expect(strategies.filter(strategy => matchesStrategyStatus(strategy, 'all')).map(item => item.id)).toEqual([1, 2, 3, 4])
    expect(strategies.filter(strategy => matchesStrategyStatus(strategy, 'live')).map(item => item.id)).toEqual([1])
    expect(strategies.filter(strategy => matchesStrategyStatus(strategy, 'monitor')).map(item => item.id)).toEqual([2])
    expect(strategies.filter(strategy => matchesStrategyStatus(strategy, 'candidate_live')).map(item => item.id)).toEqual([3])
    expect(strategies.filter(strategy => matchesStrategyStatus(strategy, '')).map(item => item.id)).toEqual([4])
  })

  it('replaces one state and restores the previous state after a failed save', () => {
    const optimistic = withStrategyStatus(strategies, 2, 'candidate_live')
    expect(optimistic[1].strategy_status).toBe('candidate_live')
    expect(withStrategyStatus(optimistic, 2, 'monitor')[1].strategy_status).toBe('monitor')
  })
})
