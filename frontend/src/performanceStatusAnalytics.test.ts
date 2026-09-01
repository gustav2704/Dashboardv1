import { describe, expect, it } from 'vitest'
import { buildMagicNumberPerformancePoints, buildPerformancePoints } from './App'
import { buildBotRankings } from './JournalAnalysis'
import type { JournalTrade } from './journalTypes'
import type { Strategy } from './types'

function strategy(statuses: Strategy['strategy_statuses']): Strategy {
  return {
    id: 7,
    selection: false,
    strategy_statuses: statuses,
    display_name: 'Bot destacado',
    mql5_name: 'Bot destacado',
    sqx_name: 'Bot destacado',
    account_login: '100',
    primary_magic_number: 700,
    symbol: 'XAUUSD',
    metrics: { trades: 12, trade_edge: 1, monthly_sqn: 2, performance_trades_per_month: 4, performance_months: 3, profit_factor: 1.5 },
    lifetime_metrics: null,
  } as unknown as Strategy
}

function trade(index: number): JournalTrade {
  return {
    strategy_id: 7, strategy_name: 'Bot destacado', primary_magic_number: 700,
    magic_numbers: [700], historical_magic_numbers: [], symbol: 'XAUUSD', account: '100',
    source_role: 'live', is_stopped: false, magic_number: 700,
    open_time_msc: index * 3_600_000, close_time_msc: (index + 1) * 3_600_000,
    direction: 'Long', volume: .1, profit: index % 2 ? -2 : 5, commission: 0, swap: 0,
    net_profit: index % 2 ? -2 : 5,
  }
}

describe('performance status indicators', () => {
  it('keeps Live and Candidata live through bot and MN ranking data', () => {
    const trades = Array.from({ length: 10 }, (_, index) => trade(index))

    expect(buildPerformancePoints([strategy(['live', 'axi_live'])])[0].strategyStatuses).toEqual(['live', 'axi_live'])
    expect(buildMagicNumberPerformancePoints(trades, { 7: ['candidate_live'] })[0].strategyStatuses).toEqual(['candidate_live'])
    expect(buildBotRankings(trades, 'mn', { 7: 'live' })[0].strategyStatus).toBe('live')
  })

  it('keeps uncategorized bots without a performance indicator status', () => {
    const trades = Array.from({ length: 10 }, (_, index) => trade(index))

    expect(buildPerformancePoints([strategy([])])[0].strategyStatuses).toEqual([])
    expect(buildMagicNumberPerformancePoints(trades)[0].strategyStatuses).toEqual([])
    expect(buildBotRankings(trades, 'mn')[0].strategyStatus).toBe('')
  })
})
