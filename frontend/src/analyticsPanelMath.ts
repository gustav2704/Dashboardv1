import type { StrategyTemporalBucket } from './types'

export type StrategyTemporalMetric = 'win_rate' | 'expectancy' | 'profit_factor'

export function temporalMetricValue(bucket: StrategyTemporalBucket, metric: StrategyTemporalMetric) {
  return bucket[metric]
}

export function bestAndWorst(rows: StrategyTemporalBucket[], metric: StrategyTemporalMetric) {
  const eligible = rows.filter(row => row.count > 0 && temporalMetricValue(row, metric) != null)
  if (!eligible.length) return { best: null, worst: null }
  const sorted = [...eligible].sort((left, right) =>
    (temporalMetricValue(left, metric) as number) - (temporalMetricValue(right, metric) as number))
  return { worst: sorted[0], best: sorted[sorted.length - 1] }
}

export function selectedCalendarTotal(year: number, months: number[], now = new Date()) {
  return year === now.getFullYear()
    ? months.slice(0, now.getMonth() + 1).reduce((sum, value) => sum + value, 0)
    : months.reduce((sum, value) => sum + value, 0)
}

export function monthlyProfitColor(value: number, maximum: number) {
  if (value === 0 || maximum <= 0) return 'rgba(30, 52, 66, .55)'
  const intensity = .18 + Math.min(1, Math.abs(value) / maximum) * .42
  return value > 0 ? `rgba(16, 185, 129, ${intensity})` : `rgba(244, 63, 94, ${intensity})`
}
