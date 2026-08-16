import { describe, expect, it } from 'vitest'
import { buildMagicNumberPerformancePoints } from './App'
import { buildBotRankings } from './JournalAnalysis'
import type { JournalTrade } from './journalTypes'

function trade(index: number, overrides: Partial<JournalTrade> = {}): JournalTrade {
  return {
    strategy_id: 1,
    strategy_name: 'Bot 1',
    primary_magic_number: 111,
    magic_numbers: [111],
    historical_magic_numbers: [],
    symbol: 'XAUUSD',
    account: '100',
    source_role: 'live',
    is_stopped: false,
    magic_number: 111,
    open_time_msc: index * 3_600_000,
    close_time_msc: (index + 1) * 3_600_000,
    direction: 'Long',
    volume: .1,
    profit: index % 2 ? -5 : 10,
    commission: 0,
    swap: 0,
    net_profit: index % 2 ? -5 : 10,
    ...overrides,
  }
}

describe('Magic Number performance grouping', () => {
  it('ranks eligible MN samples and excludes samples below 10 trades', () => {
    const eligible = Array.from({ length: 10 }, (_, index) => trade(index))
    const insufficient = Array.from({ length: 9 }, (_, index) => trade(index + 20, { magic_number: 222 }))

    expect(buildMagicNumberPerformancePoints([...eligible, ...insufficient])).toHaveLength(1)
    expect(buildMagicNumberPerformancePoints([...eligible, ...insufficient])[0]).toMatchObject({
      id: 1, name: 'MN 111', magicNumber: 111, trades: 10,
    })
  })

  it('keeps different bots separate when they share a Magic Number', () => {
    const firstBot = Array.from({ length: 10 }, (_, index) => trade(index, { strategy_id: 1, strategy_name: 'Bot 1' }))
    const secondBot = Array.from({ length: 10 }, (_, index) => trade(index + 20, { strategy_id: 2, strategy_name: 'Bot 2', account: '200' }))
    const points = buildMagicNumberPerformancePoints([...firstBot, ...secondBot])
    const rankings = buildBotRankings([...firstBot, ...secondBot], 'mn')

    expect(points).toHaveLength(2)
    expect(points.map(point => point.name)).toEqual(['MN 111', 'MN 111'])
    expect(new Set(points.map(point => point.key)).size).toBe(2)
    expect(rankings.map(row => [row.name, row.strategyName, row.account])).toEqual([
      ['MN 111', 'Bot 1', '100'],
      ['MN 111', 'Bot 2', '200'],
    ])
  })
})
