import type { JournalAnalysisData, JournalTrade } from './journalTypes'
import { capitalFlowBetween, equityEvents, runsTest } from './journalAnalytics'

const OFFSET = 2 * 60 * 60 * 1000
const DAY = 24 * 60 * 60 * 1000

export type PeriodKey = 'today' | 'week' | 'month' | 'year'
export type PerformancePeriod = {
  key: PeriodKey; label: string; profit: number; profitDifference: number;
  gainPct: number | null; gainDifference: number | null; trades: number; tradesDifference: number;
  lots: number; lotsDifference: number; winRate: number | null; winRateDifference: number | null;
  movement: number | null; movementDifference: number | null; movementLabel: string;
}
export type PerformanceAssetRow = {
  symbol: string; trades: number; wins: number; losses: number; winRate: number;
  profit: number; lots: number; movement: number | null; movementLabel: string;
  longs: { trades: number; movement: number | null; profit: number };
  shorts: { trades: number; movement: number | null; profit: number };
}

function sum(values: number[]) { return values.reduce((total, value) => total + value, 0) }
function mean(values: number[]) { return values.length ? sum(values) / values.length : 0 }
function sampleDeviation(values: number[]) {
  if (values.length < 2) return null
  const average = mean(values)
  return Math.sqrt(sum(values.map(value => (value - average) ** 2)) / (values.length - 1))
}
function isLong(trade: JournalTrade) { return /^(long|buy)$/i.test(trade.direction) }
function canonicalSymbol(symbol: string) { return symbol.toUpperCase().split('.')[0].replace(/[^A-Z0-9]/g, '') }
const currencies = new Set(['USD','EUR','JPY','GBP','AUD','CAD','CHF','NZD'])

export function tradeMovement(trade: JournalTrade) {
  const symbol = canonicalSymbol(trade.symbol)
  const open = Number(trade.open_price)
  const close = Number(trade.close_price)
  if (!Number.isFinite(open) || !Number.isFinite(close) || (!open && !close)) return { value: null, label: 'Pips / puntos' }
  const direction = isLong(trade) ? 1 : -1
  const isFx = symbol.length === 6 && currencies.has(symbol.slice(0, 3)) && currencies.has(symbol.slice(3))
  const divisor = isFx ? (symbol.endsWith('JPY') ? .01 : .0001) : 1
  return { value: direction * (close - open) / divisor, label: isFx ? 'Pips' : 'Puntos' }
}

function movementFor(trades: JournalTrade[]) {
  const units = new Set<string>()
  let total = 0
  for (const trade of trades) {
    const movement = tradeMovement(trade)
    if (movement.value == null) continue
    units.add(movement.label)
    total += movement.value
  }
  if (!units.size || units.size > 1) return { value: null, label: units.size > 1 ? 'Unidades mixtas' : 'Pips / puntos' }
  return { value: total, label: [...units][0] }
}

function initialCapital(data: JournalAnalysisData) {
  const accounts = Object.keys(data.balances || {})
  const configured = data.initial_capitals || {}
  if (accounts.length && accounts.every(account => Number(configured[account]) > 0)) {
    return sum(accounts.map(account => Number(configured[account])))
  }
  const first = data.real_equity_curve?.[0]
  if (first) return first.equity - (Number((first as { net_profit?: number }).net_profit) || 0)
  const current = sum(Object.values(data.balances || {}))
  return current ? current - sum(data.trades.map(trade => trade.net_profit)) : null
}

function rangeFor(key: PeriodKey, now: number): [number, number, number, number] {
  const shifted = new Date(now + OFFSET)
  const year = shifted.getUTCFullYear(), month = shifted.getUTCMonth(), day = shifted.getUTCDate()
  const unshift = (value: number) => value - OFFSET
  let start = 0, previousStart = 0, end = now + 1
  if (key === 'today') {
    start = unshift(Date.UTC(year, month, day))
    previousStart = unshift(Date.UTC(year, month, day - 1))
  }
  if (key === 'week') {
    const weekday = (shifted.getUTCDay() + 6) % 7
    start = unshift(Date.UTC(year, month, day - weekday))
    previousStart = unshift(Date.UTC(year, month, day - weekday - 7))
  }
  if (key === 'month') {
    start = unshift(Date.UTC(year, month, 1))
    previousStart = unshift(Date.UTC(year, month - 1, 1))
  }
  if (key === 'year') {
    start = unshift(Date.UTC(year, 0, 1))
    previousStart = unshift(Date.UTC(year - 1, 0, 1))
  }
  return [start, end, previousStart, start]
}

function periodStats(trades: JournalTrade[], start: number, end: number, capital: number | null) {
  const scoped = trades.filter(trade => trade.close_time_msc >= start && trade.close_time_msc < end)
  const profit = sum(scoped.map(trade => trade.net_profit))
  const wins = scoped.filter(trade => trade.net_profit > 0).length
  const movement = movementFor(scoped)
  return { profit, gain: capital && capital > 0 ? profit / capital * 100 : null, trades: scoped.length,
    lots: sum(scoped.map(trade => trade.volume)), winRate: scoped.length ? wins / scoped.length * 100 : null, movement }
}

function periods(data: JournalAnalysisData, capital: number | null): PerformancePeriod[] {
  const labels: Record<PeriodKey, string> = { today: 'Hoy', week: 'Esta semana', month: 'Este mes', year: 'Este año' }
  return (Object.keys(labels) as PeriodKey[]).map(key => {
    const [start, end, previousStart, previousEnd] = rangeFor(key, data.range_end_msc || Date.now())
    // Gain % is measured against the capital invested when each period began.
    const base = (from: number) => capital == null ? null : capital + capitalFlowBetween(data, -Infinity, from)
    const current = periodStats(data.trades, start, end, base(start))
    const previous = periodStats(data.trades, previousStart, previousEnd, base(previousStart))
    return { key, label: labels[key], profit: current.profit, profitDifference: current.profit - previous.profit,
      gainPct: current.gain, gainDifference: current.gain == null || previous.gain == null ? null : current.gain - previous.gain,
      trades: current.trades, tradesDifference: current.trades - previous.trades, lots: current.lots,
      lotsDifference: current.lots - previous.lots, winRate: current.winRate,
      winRateDifference: current.winRate == null || previous.winRate == null ? null : current.winRate - previous.winRate,
      movement: current.movement.value, movementDifference: current.movement.value == null || previous.movement.value == null ? null : current.movement.value - previous.movement.value,
      movementLabel: current.movement.label }
  })
}

function assetRows(trades: JournalTrade[]): PerformanceAssetRow[] {
  const groups = new Map<string, JournalTrade[]>()
  for (const trade of trades) groups.set(trade.symbol || 'N/D', [...(groups.get(trade.symbol || 'N/D') || []), trade])
  return [...groups.entries()].map(([symbol, items]) => {
    const longs = items.filter(isLong), shorts = items.filter(trade => !isLong(trade))
    const side = (part: JournalTrade[]) => ({ trades: part.length, movement: movementFor(part).value, profit: sum(part.map(trade => trade.net_profit)) })
    const wins = items.filter(trade => trade.net_profit > 0).length
    const movement = movementFor(items)
    return { symbol, trades: items.length, wins, losses: items.filter(trade => trade.net_profit < 0).length,
      winRate: items.length ? wins / items.length * 100 : 0, profit: sum(items.map(trade => trade.net_profit)),
      lots: sum(items.map(trade => trade.volume)), movement: movement.value, movementLabel: movement.label,
      longs: side(longs), shorts: side(shorts) }
  }).sort((left, right) => right.trades - left.trades)
}

function temporal(trades: JournalTrade[]) {
  const hours = Array.from({ length: 24 }, (_, hour) => ({ key: String(hour), label: String(hour).padStart(2, '0'), wins: 0, losses: 0, total: 0 }))
  const labels = ['Lun','Mar','Mié','Jue','Vie','Sáb','Dom']
  const weekdays = labels.map((label, day) => ({ key: String(day), label, wins: 0, losses: 0, total: 0 }))
  for (const trade of trades) {
    const date = new Date(trade.close_time_msc + OFFSET)
    const targets = [hours[date.getUTCHours()], weekdays[(date.getUTCDay() + 6) % 7]]
    for (const target of targets) { if (trade.net_profit > 0) target.wins++; else if (trade.net_profit < 0) target.losses++; target.total++ }
  }
  return { hours, weekdays }
}

function durationLabel(seconds: number) {
  if (seconds < 3600) return '< 1 h'; if (seconds < 4 * 3600) return '1–4 h'; if (seconds < 12 * 3600) return '4–12 h'
  if (seconds < DAY / 1000) return '12–24 h'; if (seconds < 3 * DAY / 1000) return '1–3 d'; if (seconds < 7 * DAY / 1000) return '3–7 d'
  return '> 7 d'
}

function accountHistory(data: JournalAnalysisData) {
  const histories = data.account_snapshot_history || {}
  const accounts = Object.keys(histories)
  const events = accounts.flatMap(account => histories[account].map(point => ({ account, ...point }))).sort((a, b) => a.time_msc - b.time_msc)
  const latest = new Map<string, typeof events[number]>()
  const result: Array<{ time_msc: number; balance: number; equity: number; margin: number; free_margin: number; margin_level: number | null; filtered_floating_profit: number }> = []
  for (const event of events) {
    latest.set(event.account, event)
    if (latest.size !== accounts.length) continue
    const values = [...latest.values()]
    const balance = sum(values.map(value => value.balance || 0)), equity = sum(values.map(value => value.equity || value.balance || 0))
    const margin = sum(values.map(value => value.margin || 0)), free = sum(values.map(value => value.free_margin || 0))
    result.push({ time_msc: event.time_msc, balance, equity, margin, free_margin: free,
      margin_level: margin > 0 ? equity / margin * 100 : null,
      filtered_floating_profit: sum(values.map(value => value.filtered_floating_profit || 0)) })
  }
  return result
}

function profitCurve(data: JournalAnalysisData, capital: number | null) {
  let profit = 0, invested = capital || 0, peak = invested
  const curve: Array<{ time_msc: number; profit: number; growth: number | null; drawdown: number; drawdown_pct: number | null }> = []
  for (const event of equityEvents(data)) {
    if (event.flow) {
      // Deposits/withdrawals move invested capital and the peak together.
      invested += event.amount
      peak += event.amount
      continue
    }
    profit += event.amount
    const equity = invested + profit
    peak = Math.max(peak, equity)
    const drawdown = Math.max(0, peak - equity)
    curve.push({ time_msc: event.time_msc, profit, growth: capital && invested > 0 ? profit / invested * 100 : null,
      drawdown, drawdown_pct: peak > 0 ? drawdown / peak * 100 : null })
  }
  return curve
}

export function buildAccountPerformance(data: JournalAnalysisData) {
  const values = data.trades.map(trade => trade.net_profit)
  const wins = data.trades.filter(trade => trade.net_profit > 0), losses = data.trades.filter(trade => trade.net_profit < 0)
  const longs = data.trades.filter(isLong), shorts = data.trades.filter(trade => !isLong(trade))
  const grossProfit = sum(wins.map(trade => trade.net_profit)), grossLoss = Math.abs(sum(losses.map(trade => trade.net_profit)))
  const deviation = sampleDeviation(values), average = mean(values), capital = initialCapital(data)
  const netDeposits = sum((data.capital_movements || []).map(item => item.amount))
  const invested = capital == null ? null : capital + netDeposits
  const capitalConfigured = Object.keys(data.balances || {}).length > 0
    && Object.keys(data.balances || {}).every(account => Number(data.initial_capitals?.[account]) > 0)
  const durations = data.trades.map(trade => Math.max(0, trade.close_time_msc - trade.open_time_msc) / 1000).sort((a, b) => a - b)
  const medianDuration = durations.length
    ? durations.length % 2
      ? durations[Math.floor(durations.length / 2)]
      : (durations[durations.length / 2 - 1] + durations[durations.length / 2]) / 2
    : 0
  const durationGroups = new Map<string, number>()
  for (const seconds of durations) durationGroups.set(durationLabel(seconds), (durationGroups.get(durationLabel(seconds)) || 0) + 1)
  const latest = Object.values(data.account_snapshots || {})
  const movement = movementFor(data.trades), curve = profitCurve(data, capital)
  const run = runsTest(data.trades)
  return {
    capital,
    netDeposits,
    capitalConfigured,
    account: { balance: sum(latest.map(item => item.balance)), equity: sum(latest.map(item => item.equity ?? item.balance)),
      margin: sum(latest.map(item => item.margin || 0)), freeMargin: sum(latest.map(item => item.free_margin || 0)),
      filteredFloating: sum(latest.map(item => item.filtered_floating_profit || 0)),
      capturedAt: latest.map(item => item.captured_at).sort().at(0) || null, accounts: latest.length },
    overview: { profit: sum(values), gainPct: invested && invested > 0 ? sum(values) / invested * 100 : null, trades: values.length,
      wins: wins.length, losses: losses.length, lots: sum(data.trades.map(trade => trade.volume)), movement: movement.value,
      movementLabel: movement.label, maxDrawdown: Math.max(0, ...curve.map(point => point.drawdown)),
      maxDrawdownPct: Math.max(0, ...curve.map(point => point.drawdown_pct || 0)), commissions: sum(data.trades.map(trade => trade.commission)), swaps: sum(data.trades.map(trade => trade.swap)) },
    periods: periods(data, capital), assets: assetRows(data.trades), temporal: temporal(data.trades),
    advanced: { trades: values.length, wins: wins.length, losses: losses.length, longTrades: longs.length,
      longWins: longs.filter(trade => trade.net_profit > 0).length, shortTrades: shorts.length, shortWins: shorts.filter(trade => trade.net_profit > 0).length,
      profitFactor: grossLoss ? grossProfit / grossLoss : grossProfit ? 999 : null, deviation, expectancy: average,
      tradeSharpe: deviation ? average / deviation : null, zScore: run.zScore, zProbability: run.zProbability,
      averageWin: mean(wins.map(trade => trade.net_profit)), averageLoss: mean(losses.map(trade => trade.net_profit)),
      bestTrade: values.length ? Math.max(...values) : null, worstTrade: values.length ? Math.min(...values) : null,
      averageDuration: mean(durations), medianDuration },
    durations: [...durationGroups].map(([label, count]) => ({ label, count, pct: durations.length ? count / durations.length * 100 : 0 })),
    curves: { profit: curve, account: accountHistory(data) },
  }
}
