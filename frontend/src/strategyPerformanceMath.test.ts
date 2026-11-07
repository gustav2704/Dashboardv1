import { describe, expect, it } from 'vitest'
import { curveFromTrades, dailyProfit, directionSplit, equityWithPeaks, hourlyProfit, monthlyProfit, tradesToCsv } from './strategyPerformanceMath'
import type { Trade } from './types'

const msc = (iso: string) => Date.parse(iso)
const trade = (overrides: Partial<Trade>): Trade => ({
  trade_key: 'k', terminal_id: 1, position_id: 1, deal_ticket: 1, symbol: 'CADJPY', direction: 'Short',
  open_time_msc: msc('2026-09-01T10:00:00Z'), close_time_msc: msc('2026-09-01T12:00:00Z'), open_price: 1, close_price: 1,
  volume: 1, magic: 5655, comment: '', exit_comment: '', profit: 0, commission: 0, swap: 0, net_profit: 0, status: 'closed',
  ...overrides,
})

describe('strategy performance helpers', () => {
  it('groups profit by UTC month and year', () => {
    const rows = monthlyProfit([
      { time_msc: msc('2025-12-31T23:00:00Z'), net_profit: 5 },
      { time_msc: msc('2026-08-07T10:00:00Z'), net_profit: -10 },
      { time_msc: msc('2026-09-03T10:00:00Z'), net_profit: 30 },
      { time_msc: msc('2026-09-04T10:00:00Z'), net_profit: 20 },
    ])
    expect(rows.map(row => row.year)).toEqual([2026, 2025])
    expect(rows[0].months[7]).toBe(-10)
    expect(rows[0].months[8]).toBe(50)
    expect(rows[0].tradeCounts[8]).toBe(2)
    expect(rows[0].total).toBe(40)
  })

  it('buckets daily profit inside the requested month only', () => {
    const days = dailyProfit([
      { time_msc: msc('2026-09-03T10:00:00Z'), net_profit: 3 },
      { time_msc: msc('2026-09-03T20:00:00Z'), net_profit: 2 },
      { time_msc: msc('2026-10-01T00:30:00Z'), net_profit: 9 },
    ], '2026-09')
    expect(days).toEqual({ '2026-09-03': 5 })
  })

  it('tracks peak and max drawdown from a zero start', () => {
    const result = equityWithPeaks([
      { time_msc: 1, net_profit: 100 },
      { time_msc: 2, net_profit: -60 },
      { time_msc: 3, net_profit: 20 },
    ])
    expect(result.points[0].equity).toBe(0)
    expect(result.points.at(-1)).toMatchObject({ equity: 60, peak: 100, underwater: 40 })
    expect(result.maxDrawdown).toBe(60)
    expect(result.maxDrawdownPct).toBe(60)
  })

  it('splits buy and sell trades and filters curves by direction', () => {
    const trades = [
      trade({ direction: 'Long', net_profit: 10 }),
      trade({ direction: 'Short', net_profit: -5, close_time_msc: 2 }),
      trade({ direction: 'Short', net_profit: 15, close_time_msc: 3 }),
    ]
    const split = directionSplit(trades)
    expect(split.buy).toMatchObject({ trades: 1, profit: 10, winRate: 1 })
    expect(split.sell).toMatchObject({ trades: 2, profit: 10, winRate: .5 })
    expect(curveFromTrades(trades, 'sell').map(point => point.equity)).toEqual([-5, 10])
  })

  it('groups trades by opening hour and exports CSV', () => {
    const rows = [trade({ net_profit: 7, comment: 'a,b' })]
    expect(hourlyProfit(rows)[10]).toMatchObject({ profit: 7, trades: 1 })
    const csv = tradesToCsv(rows).split('\n')
    expect(csv[0].startsWith('ticket,symbol,type')).toBe(true)
    expect(csv[1]).toContain('SELL')
    expect(csv[1]).toContain('"a,b"')
  })
})
