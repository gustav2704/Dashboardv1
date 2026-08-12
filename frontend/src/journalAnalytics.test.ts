import { describe, expect, it } from 'vitest'
import { buildAccountAnalytics, runsTest, type TemporalBucket } from './journalAnalytics'
import { buildBotEquityCurves, buildBotRankings, buildRemovalCandidates, temporalDomain, temporalVisualValue } from './JournalAnalysis'
import type { JournalAnalysisData, JournalTrade } from './journalTypes'

const DAY = 86_400_000

function trade(overrides: Partial<JournalTrade> = {}): JournalTrade {
  return {
    strategy_id: 1,
    strategy_name: 'Bot',
    primary_magic_number: 101,
    magic_numbers: [101],
    historical_magic_numbers: [],
    symbol: 'XAUUSD',
    account: '100',
    source_role: 'live',
    is_stopped: false,
    magic_number: 101,
    open_time_msc: 0,
    close_time_msc: 3_600_000,
    direction: 'Long',
    volume: .1,
    profit: 10,
    commission: 0,
    swap: 0,
    net_profit: 10,
    ...overrides,
  }
}

function journal(trades: JournalTrade[], overrides: Partial<JournalAnalysisData> = {}): JournalAnalysisData {
  return {
    generated_at: '2026-01-31T00:00:00Z',
    window: 'all',
    account: '100',
    range_start_msc: 0,
    range_end_msc: 40 * DAY,
    balances: { '100': 1000 },
    trades,
    daily_balances: [],
    capital_coverage: {
      accounts: ['100'],
      start: { time_msc: 0, total_balance: 1000, accounts: { '100': { time_msc: 0, balance: 1000 } }, complete: true, max_age_msc: 0 },
      end: { time_msc: 40 * DAY, total_balance: 1000, accounts: { '100': { time_msc: 40 * DAY, balance: 1000 } }, complete: true, max_age_msc: 0 },
    },
    reconciliation: { raw_closed_trades: trades.length, attributed_trades: trades.length, unassigned_trades: 0, displayed_trades: trades.length, status: 'reconciled' },
    ...overrides,
  }
}

describe('account analytics', () => {
  it('builds individual equity only at each active bot closing trade', () => {
    const curves = buildBotEquityCurves([
      trade({ strategy_id: 1, strategy_name: 'Bot 1', close_time_msc: 1, net_profit: 10 }),
      trade({ strategy_id: 2, strategy_name: 'Bot 2', close_time_msc: 2, net_profit: 20 }),
      trade({ strategy_id: 1, strategy_name: 'Bot 1', close_time_msc: 3, net_profit: -4 }),
    ])

    expect(curves.series.map(series => series.id)).toEqual([1, 2])
    expect(curves.points).toMatchObject([
      { timestamp: 1, bot_1: 10 },
      { timestamp: 2, bot_2: 20 },
      { timestamp: 3, bot_1: 6 },
    ])
    expect(curves.points[1].bot_1).toBeUndefined()
    expect(curves.points[2].bot_2).toBeUndefined()
  })

  it('excludes stopped bots only from individual equity curves', () => {
    const stopped = trade({ strategy_id: 4, strategy_name: 'Stopped', is_stopped: true })
    const active = trade({ strategy_id: 5, strategy_name: 'Active', close_time_msc: 2 })

    const curves = buildBotEquityCurves([stopped, active])

    expect(curves.series.map(series => series.id)).toEqual([5])
    expect(curves.points).toHaveLength(1)
    expect(stopped.is_stopped).toBe(true)
  })

  it('excludes stopped bots from performance rankings and removal candidates', () => {
    const stopped = Array.from({ length: 10 }, (_, index) => trade({ strategy_id: 4, strategy_name: 'Stopped', is_stopped: true, net_profit: -10, close_time_msc: index + 1 }))
    const active = Array.from({ length: 10 }, (_, index) => trade({ strategy_id: 5, strategy_name: 'Active', net_profit: -2, close_time_msc: index + 20 }))

    expect(buildBotRankings([...stopped, ...active]).map(row => row.id)).toEqual([5])
    expect(buildRemovalCandidates([...stopped, ...active]).map(row => row.id)).toEqual([5])
  })

  it('keeps strategy identifiers and handles missing magic numbers in bot rankings', () => {
    const identified = Array.from({ length: 10 }, (_, index) => trade({ strategy_id: 254, strategy_name: 'US30v4 BUY', primary_magic_number: 857, magic_numbers: [857], close_time_msc: index + 1 }))
    const withoutMagic = Array.from({ length: 10 }, (_, index) => trade({ strategy_id: 2, strategy_name: 'No magic', primary_magic_number: null, magic_numbers: [], historical_magic_numbers: [], close_time_msc: index + 20 }))
    const rows = buildRemovalCandidates([...identified.map(item => ({ ...item, net_profit: -1 })), ...withoutMagic.map(item => ({ ...item, net_profit: -2 }))])

    expect(rows.find(row => row.id === 254)).toMatchObject({ name: 'US30v4 BUY', primaryMagicNumber: 857, magicNumbers: [857], historicalMagicNumbers: [] })
    expect(rows.find(row => row.id === 2)).toMatchObject({ primaryMagicNumber: null, magicNumbers: [], historicalMagicNumbers: [] })
  })

  it('selects only confirmed loss-making bots as removal candidates, worst first', () => {
    const losses = Array.from({ length: 10 }, (_, index) => trade({ strategy_id: 1, strategy_name: 'Worst loss', net_profit: -10, close_time_msc: index + 1 }))
    const smallerLosses = Array.from({ length: 10 }, (_, index) => trade({ strategy_id: 2, strategy_name: 'Smaller loss', net_profit: -4, close_time_msc: index + 20 }))
    const profitable = Array.from({ length: 10 }, (_, index) => trade({ strategy_id: 3, strategy_name: 'Profitable', net_profit: 3, close_time_msc: index + 40 }))
    const insufficient = Array.from({ length: 9 }, (_, index) => trade({ strategy_id: 4, strategy_name: 'Short sample', net_profit: -20, close_time_msc: index + 60 }))

    expect(buildRemovalCandidates([...losses, ...smallerLosses, ...profitable, ...insufficient]).map(row => [row.name, row.net])).toEqual([
      ['Worst loss', -100],
      ['Smaller loss', -40],
    ])
  })

  it('merges overlapping exposure intervals', () => {
    const data = journal([
      trade({ open_time_msc: 0, close_time_msc: 4 * 3_600_000 }),
      trade({ open_time_msc: 2 * 3_600_000, close_time_msc: 6 * 3_600_000 }),
    ], { range_end_msc: 12 * 3_600_000 })
    expect(buildAccountAnalytics(data).exposure).toBeCloseTo(50)
  })

  it('uses fixed UTC+02 for hourly buckets around midnight', () => {
    const at2330Utc = Date.UTC(2026, 0, 1, 23, 30)
    const result = buildAccountAnalytics(journal([trade({ close_time_msc: at2330Utc })]))
    expect(result.temporal.hours[1].count).toBe(1)
    expect(result.temporal.hours[23].count).toBe(0)
  })

  it('keeps profit factor unavailable when a bucket has no losses', () => {
    const result = buildAccountAnalytics(journal([trade({ net_profit: 10 }), trade({ net_profit: 5, close_time_msc: 4_000_000 })]))
    expect(result.temporal.hours[1].profitFactor).toBeNull()
  })

  it('builds completed and ongoing drawdown episodes', () => {
    const data = journal([
      trade({ net_profit: 20, close_time_msc: DAY }),
      trade({ net_profit: -10, close_time_msc: 2 * DAY }),
      trade({ net_profit: 15, close_time_msc: 3 * DAY }),
      trade({ net_profit: -5, close_time_msc: 4 * DAY }),
    ], {
      capital_coverage: {
        accounts: ['100'],
        start: { time_msc: 0, total_balance: 1000, accounts: { '100': { time_msc: 0, balance: 1000 } }, complete: true, max_age_msc: 0 },
        end: { time_msc: 40 * DAY, total_balance: 1020, accounts: { '100': { time_msc: 40 * DAY, balance: 1020 } }, complete: true, max_age_msc: 0 },
      },
    })
    const result = buildAccountAnalytics(data)
    expect(result.drawdowns).toHaveLength(2)
    expect(result.drawdowns.some(item => !item.ongoing && item.depth === 10)).toBe(true)
    expect(result.drawdowns.some(item => item.ongoing && item.depth === 5)).toBe(true)
  })

  it('rejects capital metrics when account balance does not reconcile', () => {
    const data = journal([trade({ net_profit: 10, close_time_msc: DAY })])
    const result = buildAccountAnalytics(data)
    expect(result.capital.cagr).toBeNull()
    expect(result.capital.reason).toContain('no reconcilia')
  })

  it('rejects capital metrics for partial filters', () => {
    const result = buildAccountAnalytics(journal([]), false)
    expect(result.capital.cagr).toBeNull()
    expect(result.capital.reason).toContain('filtros parciales')
  })

  it('calculates exact CAGR and daily Sharpe from reconciled snapshots', () => {
    let balance = 1000
    const trades: JournalTrade[] = []
    const daily: JournalAnalysisData['daily_balances'] = []
    for (let day = 1; day <= 40; day++) {
      const pnl = day % 2 ? 10 : -4
      balance += pnl
      trades.push(trade({ open_time_msc: day * DAY - 3_600_000, close_time_msc: day * DAY, net_profit: pnl, profit: pnl }))
      daily.push({ day: `day-${day}`, time_msc: day * DAY, total_balance: balance, accounts: { '100': { time_msc: day * DAY, balance } }, complete: true })
    }
    const data = journal(trades, {
      daily_balances: daily,
      capital_coverage: {
        accounts: ['100'],
        start: { time_msc: 0, total_balance: 1000, accounts: { '100': { time_msc: 0, balance: 1000 } }, complete: true, max_age_msc: 0 },
        end: { time_msc: 40 * DAY, total_balance: balance, accounts: { '100': { time_msc: 40 * DAY, balance } }, complete: true, max_age_msc: 0 },
      },
    })
    const result = buildAccountAnalytics(data)
    expect(result.capital.cagr).not.toBeNull()
    expect(result.capital.sharpe).not.toBeNull()
  })

  it('assigns every requested duration band', () => {
    const hours = [.5, 2, 6, 18, 48, 100, 300, 800]
    const result = buildAccountAnalytics(journal(hours.map((duration, index) => trade({
      close_time_msc: (index + 1) * 1_000_000_000,
      open_time_msc: (index + 1) * 1_000_000_000 - duration * 3_600_000,
    }))))
    expect(result.durations.map(item => item.label)).toEqual(expect.arrayContaining(['< 1h', '1–4h', '4–12h', '12–24h', '1–3d', '3–7d', '7–30d', '> 30d']))
  })

  it('returns unavailable runs metrics for one-sided or short samples', () => {
    expect(runsTest(Array.from({ length: 12 }, (_, index) => trade({ close_time_msc: index, net_profit: 1 }))).zScore).toBeNull()
    expect(runsTest([trade({ net_profit: 1 }), trade({ net_profit: -1, close_time_msc: 2 })]).zScore).toBeNull()
  })

  it('calculates a finite runs score for a mixed sequence', () => {
    const trades = Array.from({ length: 20 }, (_, index) => trade({ close_time_msc: index + 1, net_profit: index % 2 ? -1 : 1 }))
    const result = runsTest(trades)
    expect(result.zScore).not.toBeNull()
    expect(result.zProbability).toBeGreaterThan(0)
  })

  it('uses the requested stable domains for temporal bars', () => {
    const analytics = buildAccountAnalytics(journal([
      trade({ net_profit: 15, close_time_msc: 1 }),
      trade({ net_profit: -7, close_time_msc: 2 }),
    ])).temporal
    expect(temporalDomain(analytics.hours, 'winRate')).toEqual([0, 100])
    expect(temporalDomain(analytics.hours, 'profitFactor')[0]).toBe(-temporalDomain(analytics.hours, 'profitFactor')[1])
    const expectancy = temporalDomain(analytics.hours, 'expectancy')
    expect(expectancy[0]).toBe(-expectancy[1])
  })

  it('uses PF minus one only for the signed temporal representation', () => {
    const positive: TemporalBucket = { key: 'a', label: 'A', count: 2, winRate: 50, expectancy: 0, profitFactor: 2.55, lowSample: true }
    const negative: TemporalBucket = { ...positive, key: 'b', profitFactor: .19 }
    const breakeven: TemporalBucket = { ...positive, key: 'c', profitFactor: 1 }
    expect(temporalVisualValue(positive, 'profitFactor')).toBeCloseTo(1.55)
    expect(temporalVisualValue(negative, 'profitFactor')).toBeCloseTo(-.81)
    expect(temporalVisualValue(breakeven, 'profitFactor')).toBe(0)
    expect(positive.profitFactor).toBe(2.55)
  })
})
