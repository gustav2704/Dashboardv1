import { describe, expect, it } from 'vitest'
import { meetsMinTrades, parseMinTrades } from './minTradesFilter'

describe('parseMinTrades', () => {
  it('treats empty or zero input as no filter', () => {
    expect(parseMinTrades('')).toBeNull()
    expect(parseMinTrades('   ')).toBeNull()
    expect(parseMinTrades('0')).toBeNull()
  })

  it('parses a positive integer', () => {
    expect(parseMinTrades('30')).toBe(30)
    expect(parseMinTrades(' 7 ')).toBe(7)
  })
})

describe('meetsMinTrades', () => {
  it('keeps every strategy when there is no minimum', () => {
    expect(meetsMinTrades(0, null)).toBe(true)
    expect(meetsMinTrades(undefined, null)).toBe(true)
  })

  it('excludes strategies below the minimum and keeps the boundary', () => {
    expect(meetsMinTrades(29, 30)).toBe(false)
    expect(meetsMinTrades(30, 30)).toBe(true)
    expect(meetsMinTrades(120, 30)).toBe(true)
  })

  it('treats a missing trade count as zero', () => {
    expect(meetsMinTrades(undefined, 1)).toBe(false)
    expect(meetsMinTrades(null, 1)).toBe(false)
  })
})
