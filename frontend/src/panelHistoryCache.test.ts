import { describe, expect, it } from 'vitest'
import { mergePanelHistory, panelHistoryKey } from './panelHistoryCache'
import type { PanelHistory, Trade } from './types'

function trade(ticket: number, close: number, profit: number): Trade {
  return {
    trade_key: `live:A:${ticket}`,
    terminal_id: 1,
    position_id: ticket,
    deal_ticket: ticket,
    symbol: 'US100',
    direction: 'Long',
    open_time_msc: close - 100,
    close_time_msc: close,
    open_price: 1,
    close_price: 2,
    volume: 1,
    magic: 77,
    comment: 'bot',
    exit_comment: 'bot',
    profit,
    commission: 0,
    swap: 0,
    net_profit: profit,
    status: 'CLOSED',
    source_account: 'A',
    source_role: 'live',
  }
}

function history(trades: Trade[], reset: boolean, scope = 'scope'): PanelHistory {
  return {
    trades,
    total_trades: trades.length,
    equity_curve: trades.map((item, index) => ({
      time_msc: item.close_time_msc,
      equity: trades.slice(0, index + 1).reduce((sum, value) => sum + value.net_profit, 0),
      net_profit: item.net_profit,
    })),
    mappings: [],
    cursor: { max_deal_id: Math.max(0, ...trades.map(item => item.deal_ticket)), max_import_id: 0 },
    scope_version: scope,
    reset,
  }
}

describe('panel history session cache helpers', () => {
  it('merges a delta once and retains the previous equity when no replacement is returned', () => {
    const previous = history([trade(1, 1000, 10)], true)
    const incoming = {
      ...history([trade(1, 1000, 10), trade(2, 2000, 20)], false),
      trades: [trade(2, 2000, 20), trade(2, 2000, 20)],
      total_trades: 2,
      equity_curve: [],
    }
    const merged = mergePanelHistory(previous, incoming)
    expect(merged.trades.map(item => item.deal_ticket)).toEqual([1, 2])
    expect(merged.equity_curve).toEqual(previous.equity_curve)
  })

  it('replaces stale trades when the server resets the scope', () => {
    const previous = history([trade(1, 1000, 10)], true, 'old')
    const incoming = history([trade(2, 2000, 20)], true, 'new')
    expect(mergePanelHistory(previous, incoming)).toBe(incoming)
  })

  it('keeps filter combinations in separate cache keys', () => {
    expect(panelHistoryKey(7, 'window=30d')).not.toBe(panelHistoryKey(7, 'window=all'))
    expect(panelHistoryKey(7, 'window=all')).not.toBe(panelHistoryKey(8, 'window=all'))
  })
})
