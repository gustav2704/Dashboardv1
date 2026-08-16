import { describe, expect, it } from 'vitest'
import { buildBotPerformancePoints, buildMagicNumberPerformancePoints } from './App'
import { buildBotRankings, buildTopTenRankingRows } from './JournalAnalysis'
import type { JournalTrade } from './journalTypes'

function trade(index: number, overrides: Partial<JournalTrade> = {}): JournalTrade {
  return {
    strategy_id: 1,
    strategy_name: 'Bot 1',
    primary_magic_number: 101,
    magic_numbers: [101],
    historical_magic_numbers: [],
    symbol: 'XAUUSD',
    account: '7396577',
    source_role: 'live',
    is_stopped: false,
    magic_number: 101,
    open_time_msc: index * 86_400_000,
    close_time_msc: (index + 1) * 86_400_000,
    direction: 'Long',
    volume: .1,
    profit: 10,
    commission: 0,
    swap: 0,
    net_profit: 10,
    ...overrides,
  }
}

function sample(strategyId: number, wins: number, count = 10): JournalTrade[] {
  return Array.from({ length: count }, (_, index) => trade(strategyId * 100 + index, {
    strategy_id: strategyId,
    strategy_name: `Bot ${strategyId}`,
    primary_magic_number: strategyId,
    magic_numbers: [strategyId],
    magic_number: strategyId,
    net_profit: index < wins ? 10 : -5,
  }))
}

describe('performance ranking audit', () => {
  it('selects the ten highest win rates, not the ten lowest', () => {
    const rows = Array.from({ length: 11 }, (_, index) => sample(index + 1, index))
    const ranking = buildTopTenRankingRows(rows.flat())

    expect(ranking.winRate).toHaveLength(10)
    expect(ranking.winRate[0]).toMatchObject({ id: 11, winRate: 100 })
    expect(ranking.winRate.at(-1)).toMatchObject({ id: 2, winRate: 10 })
    expect(ranking.winRate.some(row => row.id === 1)).toBe(false)
  })

  it('uses net P/L for profit factor and ignores ineligible samples when normalising scores', () => {
    const eligible = sample(1, 5).map((item, index) => ({
      ...item,
      profit: index < 5 ? 100 : -1,
      net_profit: index < 5 ? 1 : -10,
    }))
    const shortOutlier = sample(2, 9, 9).map(item => ({ ...item, net_profit: 1_000_000 }))
    const zeroVolumeOutlier = sample(3, 10, 20).map(item => ({ ...item, volume: 0 }))

    const baseline = buildBotRankings(eligible)[0]
    const withOutlier = buildBotRankings([...eligible, ...shortOutlier]).find(row => row.id === 1)
    const withZeroVolumeOutlier = buildBotRankings([...eligible, ...zeroVolumeOutlier]).find(row => row.id === 1)

    expect(baseline.pf).toBeCloseTo(.1)
    expect(withOutlier?.score).toBeCloseTo(baseline.score)
    expect(withZeroVolumeOutlier?.efficiencyScore).toBeCloseTo(baseline.efficiencyScore)
  })

  it('builds the Monthly SQN bot ranking only from the filtered journal trades', () => {
    const active = sample(1, 7)
    const stopped = sample(2, 10).map(item => ({ ...item, is_stopped: true }))
    const unassigned = sample(0, 10).map(item => ({ ...item, strategy_id: 0 }))

    expect(buildBotPerformancePoints([...active, ...stopped, ...unassigned]).map(point => point.id)).toEqual([1])
  })

  it('keeps the Monthly SQN, efficiency, composite, and removal rankings in their stated order', () => {
    const stronger = sample(1, 8)
    const weaker = sample(2, 3)
    const loss = sample(3, 2).map(item => ({ ...item, net_profit: -10 }))
    const all = [...stronger, ...weaker, ...loss]
    const topTen = buildTopTenRankingRows(all)

    expect(buildBotPerformancePoints(all).map(point => point.id)).toEqual([1, 2])
    expect(buildMagicNumberPerformancePoints(all).map(point => point.id)).toEqual([1, 2])
    expect(topTen.efficiency[0].id).toBe(1)
    expect(topTen.composite[0].id).toBe(1)
    expect(topTen.removal.map(row => row.id)).toEqual([3, 2])
  })
})
