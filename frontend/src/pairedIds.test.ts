import { describe, expect, it } from 'vitest'
import { formatPairedIds, parsePairedIds } from './pairedIds'

describe('parsePairedIds', () => {
  it('parses space-separated internal ids', () => {
    expect(parsePairedIds('12 34 56')).toEqual([12, 34, 56])
  })

  it('accepts commas and mixed separators', () => {
    expect(parsePairedIds('12, 34   56')).toEqual([12, 34, 56])
  })

  it('deduplicates and sorts', () => {
    expect(parsePairedIds('56 12 34 12')).toEqual([12, 34, 56])
  })

  it('ignores empty, non-numeric and non-positive tokens', () => {
    expect(parsePairedIds('  abc 12 -3 0 4.5 ')).toEqual([12])
  })

  it('returns an empty list for blank input', () => {
    expect(parsePairedIds('')).toEqual([])
    expect(parsePairedIds('   ')).toEqual([])
  })
})

describe('formatPairedIds', () => {
  it('formats ids as sorted space-separated text', () => {
    expect(formatPairedIds([34, 12, 56])).toBe('12 34 56')
  })

  it('round-trips through parsePairedIds', () => {
    const ids = parsePairedIds(formatPairedIds([8, 3, 8, 21]))
    expect(ids).toEqual([3, 8, 21])
  })
})
