import { describe, expect, it } from 'vitest'
import { bestAndWorst, monthlyProfitColor, selectedCalendarTotal, temporalMetricValue } from './analyticsPanelMath'
import type { StrategyTemporalBucket } from './types'

const bucket = (label: string, value: number | null): StrategyTemporalBucket => ({
  key: label, label, count: value == null ? 0 : 2, win_rate: value,
  expectancy: value, profit_factor: value, low_sample: true,
})

describe('analytics panel helpers', () => {
  it('finds the best and worst populated temporal buckets', () => {
    const result = bestAndWorst([bucket('A', 20), bucket('B', null), bucket('C', 80)], 'win_rate')
    expect(result.best?.label).toBe('C')
    expect(result.worst?.label).toBe('A')
  })

  it('treats a profit factor without losses as unbounded, not missing', () => {
    const allWins: StrategyTemporalBucket = { key: 'W', label: 'W', count: 2, win_rate: 100, expectancy: 5, profit_factor: null, low_sample: true }
    const empty: StrategyTemporalBucket = { key: 'E', label: 'E', count: 0, win_rate: null, expectancy: null, profit_factor: null, low_sample: true }
    const losing: StrategyTemporalBucket = { key: 'L', label: 'L', count: 3, win_rate: 0, expectancy: -4, profit_factor: 0, low_sample: true }
    expect(temporalMetricValue(allWins, 'profit_factor')).toBe(Infinity)
    expect(temporalMetricValue(empty, 'profit_factor')).toBeNull()
    const result = bestAndWorst([allWins, empty, losing], 'profit_factor')
    expect(result.best?.label).toBe('W')
    expect(result.worst?.label).toBe('L')
  })

  it('uses YTD for the current year and the full total for past years', () => {
    const now = new Date(2026, 2, 10)
    expect(selectedCalendarTotal(2026, [1, 2, 3, 100], now)).toBe(6)
    expect(selectedCalendarTotal(2025, [1, 2, 3, 100], now)).toBe(106)
  })

  it('returns distinct positive, negative and neutral heat colors', () => {
    expect(monthlyProfitColor(10, 10)).toContain('16, 185, 129')
    expect(monthlyProfitColor(-10, 10)).toContain('244, 63, 94')
    expect(monthlyProfitColor(0, 10)).toContain('30, 52, 66')
  })
})
