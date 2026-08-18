// Pure math behind the paired-equity sparkline.  Kept free of React so the
// tricky edge cases (an empty main curve, very large series, non-finite
// values) can be unit-tested directly.

export type EquityPointLike = { time_msc: number; equity: number }
export type EquityOverlayLike = { label: string; points: EquityPointLike[] }

export type EquitySeriesResult = {
  min: number
  max: number
  minTime: number | null
  maxTime: number | null
  coords: string
  last: EquityPointLike | null
  lastX: number | null
  lastY: number | null
  overlayCoords: Record<string, string>
  hasTimeAxis: boolean
}

function minMax(values: number[]) {
  let lo = Infinity
  let hi = -Infinity
  for (const value of values) {
    if (Number.isFinite(value)) {
      if (value < lo) lo = value
      if (value > hi) hi = value
    }
  }
  return { min: lo, max: hi }
}

export function buildEquitySeries(
  points: EquityPointLike[],
  overlays: EquityOverlayLike[] = [],
  width = 420,
  height = 118,
  padding = 12,
): EquitySeriesResult {
  const allSeries = [{ label: 'current', points }, ...(overlays.length ? overlays : [])]
  const allTimes: number[] = []
  const allValues: number[] = [0]
  for (const series of allSeries) {
    for (const point of series.points) {
      if (Number.isFinite(point.time_msc)) allTimes.push(point.time_msc)
      if (Number.isFinite(point.equity)) allValues.push(point.equity)
    }
  }
  const timeBounds = minMax(allTimes)
  const minTime = timeBounds.min === Infinity ? null : timeBounds.min
  const maxTime = timeBounds.max === -Infinity ? null : timeBounds.max
  const timeSpan = (maxTime ?? 0) - (minTime ?? 0) || 1
  const valueBounds = minMax(allValues)
  const min = valueBounds.min === Infinity ? 0 : valueBounds.min
  const max = valueBounds.max === -Infinity ? 0 : valueBounds.max
  const range = (max - min) || 1
  const x = (timeMsc: number) => padding + (timeMsc - (minTime ?? 0)) / timeSpan * (width - padding * 2)
  const y = (value: number) => padding + (max - value) / range * (height - padding * 2)
  const coords = points.map(point => `${x(point.time_msc)},${y(point.equity)}`).join(' ')
  const last = points.length ? points[points.length - 1] : null
  const lastX = last ? x(last.time_msc) : null
  const lastY = last ? y(last.equity) : null
  const overlayCoords: Record<string, string> = {}
  for (const overlay of overlays) {
    overlayCoords[overlay.label] = overlay.points
      .map(point => `${x(point.time_msc)},${y(point.equity)}`)
      .join(' ')
  }
  return {
    min,
    max,
    minTime,
    maxTime,
    coords,
    last,
    lastX,
    lastY,
    overlayCoords,
    hasTimeAxis: allTimes.length > 1 && maxTime !== minTime,
  }
}
