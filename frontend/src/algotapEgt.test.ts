import { describe, expect, it } from 'vitest'
import { buildEgtIndex, egtForStrategy, type AlgoTapEgt } from './algotapEgt'

const row = (magic: number, score: number): AlgoTapEgt => ({ account_id: '111', magic, egt_overall_score: score, egt_grade: 'BUENO', trend_direction: 'STABLE', trend_slope: 0 })

describe('egtForStrategy', () => {
  const index = buildEgtIndex([row(1, 10), row(2, 20)])

  it('prefers the primary magic', () => {
    expect(egtForStrategy(index, { account_login: '111', primary_magic_number: 2, magic_numbers: [1, 2] })?.egt_overall_score).toBe(20)
  })

  it('falls back to other current magics', () => {
    expect(egtForStrategy(index, { account_login: '111', primary_magic_number: 9, magic_numbers: [9, 1] })?.egt_overall_score).toBe(10)
  })

  it('requires the same account', () => {
    expect(egtForStrategy(index, { account_login: '222', primary_magic_number: 1, magic_numbers: [1] })).toBeUndefined()
    expect(egtForStrategy(index, { account_login: null, primary_magic_number: 1 })).toBeUndefined()
  })
})
