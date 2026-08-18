import { describe, expect, it } from 'vitest'
import { buildEquitySeries } from './equitySparklineMath'

function point(time: number, equity: number) {
  return { time_msc: time, equity }
}

describe('buildEquitySeries', () => {
  it('returns no last point and empty coords for an empty main curve', () => {
    const result = buildEquitySeries([])
    expect(result.last).toBeNull()
    expect(result.coords).toBe('')
    // An all-null time range must not produce NaN coords.
    expect(result.coords.includes('NaN')).toBe(false)
  })

  it('does not crash when the main curve is empty but overlays have data', () => {
    const result = buildEquitySeries([], [
      { label: 'brother (#5)', points: [point(1000, 100), point(2000, 150)] },
    ])
    expect(result.last).toBeNull()
    expect(result.overlayCoords['brother (#5)']).toContain(',')
    expect(result.coords.includes('NaN')).toBe(false)
    expect(result.hasTimeAxis).toBe(true)
  })

  it('lays each curve on the shared time axis', () => {
    const main = [point(1000, 0), point(2000, 100)]
    const overlay = [point(2000, 50), point(3000, 90)]
    const result = buildEquitySeries(main, [{ label: 'late', points: overlay }])
    expect(result.minTime).toBe(1000)
    expect(result.maxTime).toBe(3000)
    // The overlay starts at the later time, so its first x > padding.
    const xOf = (coord: string) => Number(coord.split(',')[0])
    expect(xOf(result.overlayCoords.late)).toBeGreaterThan(12)
  })

  it('tolerates very large series without overflow', () => {
    const big: Array<{ time_msc: number; equity: number }> = []
    for (let i = 0; i < 300000; i += 1) big.push(point(i * 1000, i))
    const result = buildEquitySeries(big)
    expect(result.coords.includes('NaN')).toBe(false)
    expect(result.minTime).toBe(0)
    expect(result.maxTime).toBe(299999 * 1000)
  })
})
