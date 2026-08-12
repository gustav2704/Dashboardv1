import { describe, expect, it } from 'vitest'
import { buildPerformancePoints } from './App'
import type { Strategy } from './types'

function strategy(id: number, overrides: Partial<Strategy> = {}): Strategy {
  return {
    id,
    identity_strategy_id: id,
    symbol: 'XAUUSD',
    asset_key: 'XAUUSD',
    sqx_name: `Bot ${id}`,
    mql5_name: `Bot ${id}`,
    display_name: `Bot ${id}`,
    display_name_source: null,
    account_login: '100',
    timeframe: 'H1',
    lineage_accounts: { current: ['100'], predecessor: [] },
    origin: 'test',
    note: '',
    note_updated_at: null,
    selection: false,
    no_mm_accepted: false,
    no_mm_accepted_at: null,
    primary_magic_number: id,
    magic_numbers: [id],
    historical_magic_numbers: [],
    metrics: {
      trades: 10,
      trade_edge: 1,
      monthly_sqn: 2,
      performance_trades_per_month: 5,
      performance_months: 2,
      profit_factor: 1.5,
    },
    lifetime_metrics: null,
    historical_metrics: null,
    monthly_net_profit: [],
    health: { status: 'green', reasons: [] },
    state: 'active',
    link_state: 'linked',
    backtest: null,
    sqx: null,
    recent_equity_curve: [],
    account_pl_percent: null,
    initial_capital_pl_percent: null,
    ...overrides,
  } as Strategy
}

describe('performance ranking', () => {
  it('excludes stopped bots while retaining eligible active bots', () => {
    const active = strategy(1)
    const stopped = strategy(2, { selection: true })

    expect(buildPerformancePoints([active, stopped]).map(point => point.id)).toEqual([1])
  })

  it('keeps active bots with insufficient history excluded', () => {
    const eligible = strategy(1)
    const insufficient = strategy(2, { metrics: { ...strategy(2).metrics, trades: 9 } })

    expect(buildPerformancePoints([eligible, insufficient]).map(point => point.id)).toEqual([1])
  })
})
