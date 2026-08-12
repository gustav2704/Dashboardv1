import { describe, expect, it } from 'vitest'
import type { JournalAnalysisData, JournalTrade } from './journalTypes'
import { filterJournalByStrategyIds, filterStrategiesByIds, parseStrategyIds } from './strategyIdFilter'

function trade(strategyId: number, sourceRole: JournalTrade['source_role'] = 'live'): JournalTrade {
  return {
    strategy_id: strategyId,
    strategy_name: `Bot ${strategyId}`,
    primary_magic_number: strategyId,
    magic_numbers: [strategyId],
    historical_magic_numbers: [],
    symbol: 'XAUUSD',
    account: '100',
    source_role: sourceRole,
    is_stopped: false,
    magic_number: strategyId,
    open_time_msc: 1,
    close_time_msc: 2,
    direction: 'Long',
    volume: 0.1,
    profit: 10,
    commission: 0,
    swap: 0,
    net_profit: 10,
  }
}

function journal(trades: JournalTrade[]): JournalAnalysisData {
  return {
    generated_at: '2026-08-03T00:00:00Z',
    window: 'all',
    account: null,
    range_start_msc: 0,
    range_end_msc: 10,
    balances: {},
    trades,
    daily_balances: [],
    capital_coverage: {
      accounts: [],
      start: { time_msc: null, total_balance: null, accounts: {}, complete: false },
      end: { time_msc: null, total_balance: null, accounts: {}, complete: false },
    },
    reconciliation: {
      raw_closed_trades: trades.length,
      attributed_trades: trades.filter(item => item.source_role !== 'unassigned').length,
      unassigned_trades: trades.filter(item => item.source_role === 'unassigned').length,
      displayed_trades: trades.length,
      status: 'reconciled',
    },
  }
}

describe('inherited internal bot ID filter', () => {
  it('accepts comma- and whitespace-separated IDs and removes duplicates', () => {
    expect([...parseStrategyIds('12, 34  12\n56')]).toEqual([12, 34, 56])
  })

  it('filters Performance candidates to one or multiple exact internal IDs', () => {
    const strategies = [{ id: 12 }, { id: 34 }, { id: 123 }]

    expect(filterStrategiesByIds(strategies, parseStrategyIds('12')).map(item => item.id)).toEqual([12])
    expect(filterStrategiesByIds(strategies, parseStrategyIds('12, 34')).map(item => item.id)).toEqual([12, 34])
    expect(filterStrategiesByIds(strategies, parseStrategyIds('999'))).toEqual([])
  })

  it('restores every Performance candidate when the filter is cleared', () => {
    const strategies = [{ id: 12 }, { id: 34 }]
    expect(filterStrategiesByIds(strategies, parseStrategyIds(''))).toBe(strategies)
  })

  it('filters all Journal trade consumers and reconciles the filtered subset', () => {
    const data = journal([trade(12), trade(34), trade(12, 'historical')])
    const filtered = filterJournalByStrategyIds(data, parseStrategyIds('12'))

    expect(filtered?.trades.map(item => item.strategy_id)).toEqual([12, 12])
    expect(filtered?.reconciliation).toEqual({
      raw_closed_trades: 2,
      attributed_trades: 2,
      unassigned_trades: 0,
      displayed_trades: 2,
      status: 'reconciled',
    })
  })

  it('returns an empty Journal subset for unknown IDs and restores data when cleared', () => {
    const data = journal([trade(12), trade(34)])

    expect(filterJournalByStrategyIds(data, parseStrategyIds('999'))?.trades).toEqual([])
    expect(filterJournalByStrategyIds(data, parseStrategyIds(''))).toBe(data)
  })
})
