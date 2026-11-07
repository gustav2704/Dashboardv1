import type { JournalAnalysisData, JournalTrade } from './journalTypes'

export type TemporalMetric = 'winRate' | 'expectancy' | 'profitFactor'

export type TemporalBucket = {
  key: string
  label: string
  count: number
  winRate: number | null
  expectancy: number | null
  profitFactor: number | null
  lowSample: boolean
}

export type DrawdownEpisode = {
  start_msc: number
  trough_msc: number
  recovery_msc: number | null
  depth: number
  depth_pct: number | null
  days: number
  ongoing: boolean
}

const OFFSET_MSC = 2 * 60 * 60 * 1000
const DAY_MSC = 24 * 60 * 60 * 1000
const CAPITAL_FRESHNESS_MSC = 15 * 60 * 1000
const CAPITAL_GAP_MSC = 36 * 60 * 60 * 1000
const LOW_SAMPLE = 10

function shiftedDate(ms: number) {
  return new Date(ms + OFFSET_MSC)
}

function valueStats(values: number[]) {
  const wins = values.filter(value => value > 0)
  const losses = values.filter(value => value < 0)
  const grossProfit = wins.reduce((sum, value) => sum + value, 0)
  const grossLoss = Math.abs(losses.reduce((sum, value) => sum + value, 0))
  return {
    count: values.length,
    winRate: values.length ? wins.length / values.length * 100 : null,
    expectancy: values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null,
    profitFactor: grossLoss > 0 ? grossProfit / grossLoss : null,
  }
}

function temporalBucket(key: string, label: string, trades: JournalTrade[]): TemporalBucket {
  const stats = valueStats(trades.map(trade => trade.net_profit))
  return { key, label, ...stats, lowSample: trades.length < LOW_SAMPLE }
}

function groupTemporal(trades: JournalTrade[]) {
  const hours = Array.from({ length: 24 }, () => [] as JournalTrade[])
  const weekdays = Array.from({ length: 7 }, () => [] as JournalTrade[])
  const months = Array.from({ length: 12 }, () => [] as JournalTrade[])
  for (const trade of trades) {
    const date = shiftedDate(trade.close_time_msc)
    hours[date.getUTCHours()].push(trade)
    weekdays[(date.getUTCDay() + 6) % 7].push(trade)
    months[date.getUTCMonth()].push(trade)
  }
  const dayLabels = ['Lun', 'Mar', 'Mié', 'Jue', 'Vie', 'Sáb', 'Dom']
  const monthLabels = ['Ene', 'Feb', 'Mar', 'Abr', 'May', 'Jun', 'Jul', 'Ago', 'Sep', 'Oct', 'Nov', 'Dic']
  return {
    hours: hours.map((items, hour) => temporalBucket(String(hour), `${String(hour).padStart(2, '0')}:00`, items)),
    weekdays: weekdays.map((items, day) => temporalBucket(String(day), dayLabels[day], items)),
    months: months.map((items, month) => temporalBucket(String(month), monthLabels[month], items)),
  }
}

function exposurePercent(trades: JournalTrade[], rangeStart: number, rangeEnd: number) {
  if (rangeEnd <= rangeStart) return null
  const intervals = trades
    .map(trade => [Math.max(rangeStart, trade.open_time_msc), Math.min(rangeEnd, trade.close_time_msc)] as const)
    .filter(([start, end]) => end > start)
    .sort((left, right) => left[0] - right[0])
  let exposed = 0
  let activeStart = 0
  let activeEnd = 0
  for (const [start, end] of intervals) {
    if (activeEnd === 0) {
      activeStart = start
      activeEnd = end
    } else if (start <= activeEnd) {
      activeEnd = Math.max(activeEnd, end)
    } else {
      exposed += activeEnd - activeStart
      activeStart = start
      activeEnd = end
    }
  }
  if (activeEnd) exposed += activeEnd - activeStart
  return exposed / (rangeEnd - rangeStart) * 100
}

function maxStagnationDays(trades: JournalTrade[], rangeStart: number, rangeEnd: number) {
  let equity = 0
  let peak = 0
  let peakAt = rangeStart
  let maximum = 0
  for (const trade of [...trades].sort((a, b) => a.close_time_msc - b.close_time_msc)) {
    equity += trade.net_profit
    if (equity >= peak) {
      maximum = Math.max(maximum, trade.close_time_msc - peakAt)
      peak = equity
      peakAt = trade.close_time_msc
    }
  }
  maximum = Math.max(maximum, rangeEnd - peakAt)
  return Math.max(0, maximum / DAY_MSC)
}

function normalCdf(value: number) {
  const sign = value < 0 ? -1 : 1
  const x = Math.abs(value) / Math.sqrt(2)
  const t = 1 / (1 + 0.3275911 * x)
  const erf = sign * (1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * Math.exp(-x * x))
  return (1 + erf) / 2
}

export function runsTest(trades: JournalTrade[]) {
  const sequence = trades
    .filter(trade => trade.net_profit !== 0)
    .sort((left, right) => left.close_time_msc - right.close_time_msc)
    .map(trade => trade.net_profit > 0)
  const wins = sequence.filter(Boolean).length
  const losses = sequence.length - wins
  if (sequence.length < 10 || !wins || !losses) return { zScore: null, zProbability: null, count: sequence.length }
  let runs = 1
  for (let index = 1; index < sequence.length; index++) if (sequence[index] !== sequence[index - 1]) runs++
  const total = sequence.length
  const expected = 1 + 2 * wins * losses / total
  const variance = 2 * wins * losses * (2 * wins * losses - total) / (total * total * (total - 1))
  if (variance <= 0) return { zScore: null, zProbability: null, count: sequence.length }
  const zScore = (runs - expected) / Math.sqrt(variance)
  const pValue = 2 * (1 - normalCdf(Math.abs(zScore)))
  return { zScore, zProbability: Math.max(0, Math.min(100, (1 - pValue) * 100)), count: sequence.length }
}

/** Net deposits/withdrawals with from < time_msc <= to. */
export function capitalFlowBetween(data: JournalAnalysisData, from: number, to: number, account?: string) {
  return (data.capital_movements || [])
    .filter(item => item.time_msc > from && item.time_msc <= to && (account == null || item.account === account))
    .reduce((total, item) => total + item.amount, 0)
}

type EquityEvent = { time_msc: number; amount: number; flow: boolean }

/** Trades and capital movements in time order; flows sort before trades at the same instant. */
export function equityEvents(data: JournalAnalysisData): EquityEvent[] {
  return [
    ...data.trades.map(trade => ({ time_msc: trade.close_time_msc, amount: trade.net_profit, flow: false })),
    ...(data.capital_movements || []).map(item => ({ time_msc: item.time_msc, amount: item.amount, flow: true })),
  ].sort((left, right) => left.time_msc - right.time_msc || Number(right.flow) - Number(left.flow))
}

function capitalValidation(data: JournalAnalysisData, eligible: boolean) {
  if (!eligible) return { valid: false, reason: 'Los filtros parciales no representan el capital completo de la cuenta.' }
  const coverage = data.capital_coverage
  if (!coverage?.accounts.length || !coverage.start.complete || !coverage.end.complete) {
    return { valid: false, reason: 'Faltan snapshots de balance en uno de los límites.' }
  }
  if ((coverage.start.max_age_msc ?? Infinity) > CAPITAL_FRESHNESS_MSC || (coverage.end.max_age_msc ?? Infinity) > CAPITAL_FRESHNESS_MSC) {
    return { valid: false, reason: 'Los snapshots fronterizos están a más de 15 minutos del período.' }
  }
  for (const account of coverage.accounts) {
    const start = coverage.start.accounts[account]
    const end = coverage.end.accounts[account]
    if (!start || !end || end.time_msc <= start.time_msc) return { valid: false, reason: `Cobertura incompleta para la cuenta ${account}.` }
    const pnl = data.trades
      .filter(trade => trade.account === account && trade.close_time_msc > start.time_msc && trade.close_time_msc <= end.time_msc)
      .reduce((sum, trade) => sum + trade.net_profit, 0)
    const delta = end.balance - start.balance - capitalFlowBetween(data, start.time_msc, end.time_msc, account)
    if (Math.abs(delta - pnl) > Math.max(.05, Math.abs(pnl) * 1e-6)) {
      return { valid: false, reason: `El balance de ${account} no reconcilia con el P/L; puede haber movimientos de capital o trades filtrados.` }
    }
  }
  return { valid: true, reason: '' }
}

function capitalMetrics(data: JournalAnalysisData, eligible: boolean) {
  const validation = capitalValidation(data, eligible)
  if (!validation.valid) return { cagr: null, sharpe: null, startBalance: null, reason: validation.reason }
  const start = data.capital_coverage.start
  const end = data.capital_coverage.end
  const startBalance = start.total_balance as number
  const endBalance = end.total_balance as number
  const elapsedDays = ((end.time_msc as number) - (start.time_msc as number)) / DAY_MSC
  // Deposits/withdrawals are not performance: compare against invested capital.
  const invested = startBalance + capitalFlowBetween(data, start.time_msc as number, end.time_msc as number)
  const cagr = elapsedDays >= 30 && invested > 0 && endBalance > 0
    ? (Math.pow(endBalance / invested, 365.2425 / elapsedDays) - 1) * 100
    : null
  const complete = data.daily_balances.filter(point => point.complete).sort((left, right) => left.time_msc - right.time_msc)
  const returns: number[] = []
  let previous = { time_msc: start.time_msc as number, total_balance: startBalance }
  let sharpeValid = true
  for (const point of complete) {
    if (point.time_msc <= previous.time_msc) continue
    if (point.time_msc - previous.time_msc > CAPITAL_GAP_MSC) {
      sharpeValid = false
      break
    }
    const pnl = data.trades
      .filter(trade => trade.close_time_msc > previous.time_msc && trade.close_time_msc <= point.time_msc)
      .reduce((sum, trade) => sum + trade.net_profit, 0)
    const flow = capitalFlowBetween(data, previous.time_msc, point.time_msc)
    if (Math.abs((point.total_balance - previous.total_balance - flow) - pnl) > Math.max(.05, Math.abs(pnl) * 1e-6) || previous.total_balance <= 0) {
      sharpeValid = false
      break
    }
    returns.push(pnl / previous.total_balance)
    previous = point
  }
  let sharpe: number | null = null
  if (sharpeValid && returns.length >= 20) {
    const mean = returns.reduce((sum, value) => sum + value, 0) / returns.length
    const variance = returns.reduce((sum, value) => sum + Math.pow(value - mean, 2), 0) / (returns.length - 1)
    if (variance > 0) sharpe = mean / Math.sqrt(variance) * Math.sqrt(252)
  }
  const reasons = [
    cagr == null ? 'CAGR requiere al menos 30 días y balances positivos.' : '',
    sharpe == null ? 'Sharpe requiere 20 retornos diarios reconciliados y sin huecos.' : '',
  ].filter(Boolean)
  return { cagr, sharpe, startBalance, reason: reasons.join(' ') }
}

function drawdownEpisodes(data: JournalAnalysisData, startBalance: number | null) {
  let equity = startBalance ?? 0
  let peak = equity
  let peakAt = data.range_start_msc
  let active: { start: number; trough: number; troughAt: number; peak: number } | null = null
  const result: DrawdownEpisode[] = []
  for (const event of equityEvents(data)) {
    equity += event.amount
    if (event.flow) {
      // A deposit/withdrawal shifts the whole equity level; it is neither a
      // new high nor a drawdown.
      peak += event.amount
      if (active) {
        active.peak += event.amount
        active.trough += event.amount
      }
      continue
    }
    if (equity < peak) {
      if (!active) active = { start: peakAt, trough: equity, troughAt: event.time_msc, peak }
      if (equity < active.trough) {
        active.trough = equity
        active.troughAt = event.time_msc
      }
    } else {
      if (active) {
        const depth = active.peak - active.trough
        result.push({
          start_msc: active.start, trough_msc: active.troughAt, recovery_msc: event.time_msc,
          depth, depth_pct: startBalance != null && active.peak > 0 ? depth / active.peak * 100 : null,
          days: (event.time_msc - active.start) / DAY_MSC, ongoing: false,
        })
        active = null
      }
      peak = equity
      peakAt = event.time_msc
    }
  }
  if (active) {
    const depth = active.peak - active.trough
    result.push({
      start_msc: active.start, trough_msc: active.troughAt, recovery_msc: null,
      depth, depth_pct: startBalance != null && active.peak > 0 ? depth / active.peak * 100 : null,
      days: (data.range_end_msc - active.start) / DAY_MSC, ongoing: true,
    })
  }
  return result.sort((left, right) => right.depth - left.depth)
}

function distribution<T extends string>(items: T[]) {
  const counts = new Map<T, number>()
  for (const item of items) counts.set(item, (counts.get(item) || 0) + 1)
  return [...counts].map(([label, count]) => ({ label, count, pct: items.length ? count / items.length * 100 : 0 }))
    .sort((left, right) => right.count - left.count)
}

function durationLabel(trade: JournalTrade) {
  const hours = Math.max(0, trade.close_time_msc - trade.open_time_msc) / 3_600_000
  if (hours < 1) return '< 1h'
  if (hours < 4) return '1–4h'
  if (hours < 12) return '4–12h'
  if (hours < 24) return '12–24h'
  if (hours < 72) return '1–3d'
  if (hours < 168) return '3–7d'
  if (hours < 720) return '7–30d'
  return '> 30d'
}

export function temporalValue(bucket: TemporalBucket, metric: TemporalMetric) {
  return bucket[metric]
}

export function buildAccountAnalytics(data: JournalAnalysisData, capitalEligible = true) {
  const capital = capitalMetrics(data, capitalEligible)
  const z = runsTest(data.trades)
  const direction = distribution(data.trades.map(trade => {
    const value = trade.direction.toLowerCase()
    return value === 'long' || value === 'buy' ? 'BUY' : value === 'short' || value === 'sell' ? 'SELL' : 'N/D'
  }))
  return {
    exposure: exposurePercent(data.trades, data.range_start_msc, data.range_end_msc),
    stagnationDays: maxStagnationDays(data.trades, data.range_start_msc, data.range_end_msc),
    z,
    capital,
    temporal: groupTemporal(data.trades),
    symbols: distribution(data.trades.map(trade => trade.symbol || 'N/D')),
    directions: direction,
    durations: distribution(data.trades.map(durationLabel)),
    drawdowns: drawdownEpisodes(data, capital.startBalance),
  }
}
