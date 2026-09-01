import { describe, expect, it } from 'vitest'
import { classifyStrategyEvent, strategyEventCategoryLabels, summarizeStrategyEvents } from './strategyEventTokens'

describe('classifyStrategyEvent', () => {
  it('detects NoMM / missing money management entries', () => {
    expect(classifyStrategyEvent('XAUUSD,M15: Entrando con NoMM')).toBe('no_mm')
    expect(classifyStrategyEvent('XAUUSD,M15: Entrando con sin MM')).toBe('no_mm')
    expect(classifyStrategyEvent('no money management enabled', 'XAUUSD,M15 raw line')).toBe('no_mm')
  })

  it('detects too-tight / invalid stop-loss entries before generic denial', () => {
    expect(classifyStrategyEvent("Trades '7396577': failed buy stop 0.06 XAUUSD.cyr at 4495.25 sl: 4463.68 [Invalid price]")).toBe('invalid_stops')
    expect(classifyStrategyEvent('tried to place stop/limit order at incorrect price. Min Distance: 0.00000000')).toBe('invalid_stops')
    expect(classifyStrategyEvent('XAU_M1: Entrada negada por stop-loss muy ceñido')).toBe('invalid_stops')
    expect(classifyStrategyEvent('XAU_M1: stop loss demasiado ceñido')).toBe('invalid_stops')
  })

  it('detects denied entries and failed orders', () => {
    expect(classifyStrategyEvent('order send failed: rejected')).toBe('denied')
    expect(classifyStrategyEvent('not enough money for trade')).toBe('denied')
    expect(classifyStrategyEvent('XAU_M1: Entrada denegada por margen')).toBe('denied')
  })

  it('classifies anything else as other', () => {
    expect(classifyStrategyEvent('expert started successfully')).toBe('other')
    expect(classifyStrategyEvent('warning: terminal disconnected')).toBe('other')
  })
})

describe('summarizeStrategyEvents', () => {
  it('counts events per category', () => {
    const summary = summarizeStrategyEvents([
      { message: 'Entrando con NoMM', context: 'XAUUSD,M15' },
      { message: 'failed buy stop 0.06 XAUUSD.cyr [Invalid price]', context: 'Trades' },
      { message: 'order send failed: rejected', context: 'experts' },
      { message: 'order send failed: rejected', context: 'experts' },
      { message: 'expert started successfully', context: 'experts' },
    ])
    expect(summary).toEqual({ no_mm: 1, invalid_stops: 1, denied: 2, other: 1 })
  })

  it('provides a Spanish label for every category', () => {
    expect(strategyEventCategoryLabels.no_mm).toContain('NoMM')
    expect(strategyEventCategoryLabels.invalid_stops).toContain('SL')
  })
})
