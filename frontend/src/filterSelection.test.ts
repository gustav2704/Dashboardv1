import { describe, expect, it } from 'vitest'
import { normalizeFilterSelection, sameOptionalSet } from './filterSelection'

describe('batched filter selection', () => {
  it('normalizes every selected value to the canonical unrestricted scope', () => {
    expect(normalizeFilterSelection(['A', 'B'], new Set(['B', 'A']))).toBeNull()
  })

  it('preserves partial and empty selections without mutating the draft', () => {
    const partial = new Set(['A'])
    const normalized = normalizeFilterSelection(['A', 'B'], partial)
    expect(normalized).toEqual(new Set(['A']))
    expect(normalized).not.toBe(partial)
    expect(normalizeFilterSelection(['A', 'B'], new Set())).toEqual(new Set())
  })

  it('compares canonical filter scopes independent of Set insertion order', () => {
    expect(sameOptionalSet(new Set(['A', 'B']), new Set(['B', 'A']))).toBe(true)
    expect(sameOptionalSet(null, new Set(['A', 'B']))).toBe(false)
  })
})
