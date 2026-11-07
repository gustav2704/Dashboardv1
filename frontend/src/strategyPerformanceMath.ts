import type { EquityPoint, Trade } from './types'

export type DirectionFilter = 'total' | 'buy' | 'sell'
export type MonthlyRow = { year: number; months: number[]; total: number; tradeCounts: number[] }
export type EquityPeakPoint = { time_msc: number; equity: number; peak: number; underwater: number; net_profit: number }
export type DirectionStats = { trades: number; profit: number; wins: number; winRate: number }
export type HourBucket = { hour: number; label: string; profit: number; trades: number }

function utcDate(msc: number) { return new Date(msc) }

export function isBuy(direction: string) { return /^(long|buy)$/i.test(direction) }
export function isSell(direction: string) { return /^(short|sell)$/i.test(direction) }

export function matchesDirection(trade: Pick<Trade, 'direction'>, filter: DirectionFilter) {
  return filter === 'total' || (filter === 'buy' ? isBuy(trade.direction) : isSell(trade.direction))
}

/** Profit per calendar month (UTC), newest year first. */
export function monthlyProfit(curve: Pick<EquityPoint, 'time_msc' | 'net_profit'>[]): MonthlyRow[] {
  const years = new Map<number, MonthlyRow>()
  for (const point of curve) {
    const date = utcDate(point.time_msc)
    const year = date.getUTCFullYear()
    const row = years.get(year) || { year, months: Array(12).fill(0), total: 0, tradeCounts: Array(12).fill(0) }
    row.months[date.getUTCMonth()] += point.net_profit
    row.tradeCounts[date.getUTCMonth()] += 1
    row.total += point.net_profit
    years.set(year, row)
  }
  return [...years.values()].sort((a, b) => b.year - a.year)
}

/** Profit per UTC day for a YYYY-MM month (same day bucketing as /daily-pnl). */
export function dailyProfit(curve: Pick<EquityPoint, 'time_msc' | 'net_profit'>[], month: string) {
  const days: Record<string, number> = {}
  for (const point of curve) {
    const key = utcDate(point.time_msc).toISOString().slice(0, 10)
    if (!key.startsWith(month)) continue
    days[key] = (days[key] || 0) + point.net_profit
  }
  return days
}

/** Cumulative curve starting at 0 with running peak and distance below it. */
export function equityWithPeaks(curve: Pick<EquityPoint, 'time_msc' | 'net_profit'>[]) {
  let equity = 0
  let peak = 0
  let maxDrawdown = 0
  const points: EquityPeakPoint[] = []
  if (curve.length) points.push({ time_msc: curve[0].time_msc, equity: 0, peak: 0, underwater: 0, net_profit: 0 })
  for (const point of curve) {
    equity += point.net_profit
    peak = Math.max(peak, equity)
    maxDrawdown = Math.max(maxDrawdown, peak - equity)
    points.push({ time_msc: point.time_msc, equity, peak, underwater: peak - equity, net_profit: point.net_profit })
  }
  const maxDrawdownPct = peak > 0 ? maxDrawdown / peak * 100 : null
  return { points, maxDrawdown, maxDrawdownPct, finalEquity: equity }
}

export function curveFromTrades(trades: Trade[], filter: DirectionFilter): EquityPoint[] {
  let equity = 0
  return [...trades]
    .filter(trade => matchesDirection(trade, filter))
    .sort((a, b) => a.close_time_msc - b.close_time_msc)
    .map(trade => {
      equity += trade.net_profit
      return { time_msc: trade.close_time_msc, equity, net_profit: trade.net_profit }
    })
}

export function directionSplit(trades: Trade[]): { buy: DirectionStats; sell: DirectionStats } {
  const stats = (filter: DirectionFilter): DirectionStats => {
    const rows = trades.filter(trade => matchesDirection(trade, filter))
    const wins = rows.filter(trade => trade.net_profit > 0).length
    return { trades: rows.length, profit: rows.reduce((sum, trade) => sum + trade.net_profit, 0), wins, winRate: rows.length ? wins / rows.length : 0 }
  }
  return { buy: stats('buy'), sell: stats('sell') }
}

/** Net profit grouped by UTC opening hour. */
export function hourlyProfit(trades: Trade[]): HourBucket[] {
  const buckets: HourBucket[] = Array.from({ length: 24 }, (_, hour) => ({ hour, label: String(hour), profit: 0, trades: 0 }))
  for (const trade of trades) {
    const bucket = buckets[utcDate(trade.open_time_msc || trade.close_time_msc).getUTCHours()]
    bucket.profit += trade.net_profit
    bucket.trades += 1
  }
  return buckets
}

function csvCell(value: unknown) {
  const text = value == null ? '' : String(value)
  return /[",\n;]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text
}

export function tradesToCsv(trades: Trade[]) {
  const header = ['ticket', 'symbol', 'type', 'volume', 'open_time', 'open_price', 'close_time', 'close_price', 'swap', 'commission', 'profit', 'net_profit', 'magic', 'comment', 'account']
  const iso = (msc: number) => msc ? new Date(msc).toISOString() : ''
  const lines = trades.map(trade => [
    trade.deal_ticket, trade.symbol, isBuy(trade.direction) ? 'BUY' : isSell(trade.direction) ? 'SELL' : trade.direction,
    trade.volume, iso(trade.open_time_msc), trade.open_price, iso(trade.close_time_msc), trade.close_price,
    trade.swap, trade.commission, trade.profit, trade.net_profit, trade.magic, trade.comment, trade.source_account || '',
  ].map(csvCell).join(','))
  return [header.join(','), ...lines].join('\n')
}
