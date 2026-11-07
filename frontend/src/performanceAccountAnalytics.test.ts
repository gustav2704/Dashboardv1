import { describe, expect, it } from 'vitest'
import type { JournalAnalysisData, JournalTrade } from './journalTypes'
import { buildAccountPerformance, tradeMovement } from './performanceAccountAnalytics'

describe('account Performance analytics', () => {
  it('uses canonical pip sizes and directional price movement', () => {
    expect(tradeMovement({ symbol: 'USDJPY.r', direction: 'Long', open_price: 159.5, close_price: 159.75 } as JournalTrade)).toEqual({ value: 25, label: 'Pips' })
    expect(tradeMovement({ symbol: 'EURUSD', direction: 'Short', open_price: 1.1, close_price: 1.099 } as JournalTrade)).toEqual({ value: 10.000000000001119, label: 'Pips' })
    expect(tradeMovement({ symbol: 'GER40', direction: 'Short', open_price: 25000, close_price: 24950 } as JournalTrade)).toEqual({ value: 50, label: 'Puntos' })
  })

  it('keeps mixed pip and point scopes unavailable', () => {
    const base = { strategy_id: 1, strategy_name: 'Bot', account: 'A', source_role: 'live', is_stopped: false,
      primary_magic_number: 1, magic_numbers: [1], historical_magic_numbers: [], magic_number: 1,
      open_time_msc: 1, close_time_msc: 2, volume: 1, profit: 1, commission: 0, swap: 0, net_profit: 1 }
    const data = { trades: [
      { ...base, symbol: 'EURUSD', direction: 'Long', open_price: 1.1, close_price: 1.101 },
      { ...base, symbol: 'GER40', direction: 'Long', open_price: 25000, close_price: 25010 },
    ], balances: {}, range_start_msc: 0, range_end_msc: 3 } as unknown as JournalAnalysisData
    expect(buildAccountPerformance(data).overview.movement).toBeNull()
    expect(buildAccountPerformance(data).overview.movementLabel).toBe('Unidades mixtas')
  })

  it('compares against the complete previous calendar period in UTC+02:00', () => {
    const trade = (closed: string, net: number) => ({ strategy_id: 1, strategy_name: 'Bot', symbol: 'USDJPY', account: 'A',
      source_role: 'live', is_stopped: false, primary_magic_number: 1, magic_numbers: [1], historical_magic_numbers: [], magic_number: 1,
      open_time_msc: Date.parse(closed) - 60_000, close_time_msc: Date.parse(closed), open_price: 150, close_price: 150.01,
      direction: 'Long', volume: 1, profit: net, commission: 0, swap: 0, net_profit: net }) as JournalTrade
    const data = { trades: [trade('2026-08-10T10:00:00Z', 40), trade('2026-09-10T10:00:00Z', 100)], balances: { A: 1_000 },
      range_start_msc: Date.parse('2026-08-01T00:00:00Z'), range_end_msc: Date.parse('2026-09-15T10:00:00Z') } as unknown as JournalAnalysisData
    const month = buildAccountPerformance(data).periods.find(period => period.key === 'month')!
    expect(month).toMatchObject({ profit: 100, profitDifference: 60, trades: 1, tradesDifference: 0 })
  })

  it('aggregates live account snapshots while keeping filtered floating separate', () => {
    const data = { trades: [], balances: {}, range_start_msc: 0, range_end_msc: 1,
      account_snapshots: {
        A: { captured_at: '2026-09-01T10:00:00Z', balance: 1000, equity: 1020, margin: 100, free_margin: 920, filtered_floating_profit: 12 },
        B: { captured_at: '2026-09-01T09:59:00Z', balance: 2000, equity: 1990, margin: 200, free_margin: 1790, filtered_floating_profit: -3 },
      } } as unknown as JournalAnalysisData
    expect(buildAccountPerformance(data).account).toEqual({ balance: 3000, equity: 3010, margin: 300, freeMargin: 2710,
      filteredFloating: 9, capturedAt: '2026-09-01T09:59:00Z', accounts: 2 })
  })

  it('uses every configured account capital instead of a late equity snapshot', () => {
    const data = { trades: [], balances: { '7396577': 95000, '60366027': 2100 },
      initial_capitals: { '7396577': 100000, '60366027': 2000 },
      range_start_msc: 0, range_end_msc: 1,
      real_equity_curve: [{ time_msc: 1, equity: 107000, net_profit: 7000, label: 'late snapshot' }] } as unknown as JournalAnalysisData

    const result = buildAccountPerformance(data)
    expect(result.capital).toBe(102000)
    expect(result.capitalConfigured).toBe(true)
  })

  it('reproduces the FP Markets USDJPY acceptance sample', () => {
    // [net, lots, commission, swap, opened, closed, open price, close price]
    const rows = [
      [-200.97,.51,-3.06,0,1785729615304,1785729718402,156.249,155.645],[156.17,.5,-3,0,1785730563953,1785749872409,156.249,156.748],[-202.14,.52,-3.12,0,1785772982133,1785777372203,156.844,156.246],[165.87,.53,-3.18,3.22,1785798905904,1785811561116,157.068,157.561],[318.21,.94,-5.64,0,1786034611159,1786042847512,157.937,158.483],[-204.46,1,-6,0,1786072501822,1786077750752,158.53,158.216],[186.54,.94,-5.64,0,1786336261440,1786357234293,158.153,158.477],[186.49,.98,-5.88,0,1786362608494,1786377297989,158.628,158.94],[318.06,1.05,-6.3,0,1786356439057,1786377298794,158.455,158.946],[187.31,.99,-5.94,0,1786380398027,1786398711471,159.011,159.322],[-198.69,1.12,-6.72,6.71,1786425143624,1786537421941,159.253,158.971],[-208.27,1.39,-8.34,0,1786598872568,1786607144653,159.442,159.213],[-182.45,1.37,-8.22,24.6,1786564033153,1786636924710,159.398,159.167],[-200.09,1.61,-9.66,9.63,1786653004679,1786688578496,159.544,159.346],[-200.15,1.41,-8.46,8.43,1786649029914,1786696801385,159.46,159.234],[178.53,1.97,-11.82,0,1786734001647,1786744740160,159.229,159.383],[185.82,1.4,-8.4,0,1786967067449,1786984442717,159.082,159.303],[-210.61,1.91,-11.46,0,1786985216918,1786987011425,159.376,159.21],[-211.26,1.92,-11.52,0,1787061923941,1787071125533,159.733,159.567],[-208.69,1.45,-8.7,0,1787037698429,1787072739331,159.727,159.507],[-199.74,1.93,-11.58,11.54,1787085053095,1787108528472,159.63,159.465],[-199.61,2,-12,11.95,1787024727784,1787116570384,159.511,159.352],[-200.12,1.57,-9.42,9.38,1787024702708,1787118278184,159.497,159.294],[-200.12,1.57,-9.42,9.38,1787024702709,1787118278184,159.497,159.294],[-211.89,2.1,-12.6,0,1787141772939,1787142871687,159.266,159.115],[-209.3,1.68,-10.08,0,1787238154903,1787249186288,158.723,158.535],[-200.14,1.27,-7.62,7.62,1787250975893,1787312252389,158.888,158.638],[-200.14,1.27,-7.62,7.62,1787250975893,1787312252390,158.888,158.638],[216.78,1.27,-7.62,0,1787321221156,1787349540071,158.751,159.032],[216.78,1.27,-7.62,0,1787321221155,1787349540071,158.751,159.032],[96.64,1.61,-9.66,0,1787333437275,1787349540071,158.927,159.032],[175.97,1.18,-7.08,0,1787554252339,1787569917937,158.974,159.221],[-199.67,1.23,-7.38,7.24,1787633834373,1787719821422,159.3,159.042],[-190.87,1.49,-8.94,17.71,1787594252049,1787719991850,159.187,158.974],[-192.07,1.29,-7.74,15.33,1787594252048,1787720599805,159.187,158.941],[-192.07,1.29,-7.74,15.33,1787594252049,1787720599805,159.187,158.941],[461.77,1.95,-11.7,45.87,1787760029865,1787918836548,159.26,159.61],[322.39,1.64,-9.84,9.64,1787807771354,1787918837756,159.317,159.631],[322.39,1.64,-9.84,9.64,1787807771354,1787918837756,159.317,159.631],[-211.08,1.94,-11.64,0,1787936399126,1787936417954,159.694,159.53],[-211.08,1.94,-11.64,0,1787936399126,1787936417955,159.694,159.53],[470.54,2.06,-12.36,12.11,1787863256332,1787936642263,159.346,159.711],[312.85,1.86,-11.16,0,1787937304835,1787946045813,159.884,160.163],[312.85,1.86,-11.16,0,1787937304835,1787946045814,159.884,160.163],[352.64,2.22,-13.32,0,1787937304835,1787954340414,159.884,160.148],[-209.94,1.68,-10.08,0,1788168842719,1788171174358,159.904,159.714],[180.99,1.64,-9.84,0,1788176649542,1788190700996,159.661,159.847],[180.83,1.65,-9.9,0,1788247657710,1788267564551,159.856,160.041],
    ]
    const trades = rows.map((row, index) => ({ strategy_id: index + 1, strategy_name: 'USDJPY', symbol: 'USDJPY.r', account: '7396577', source_role: 'live', is_stopped: false,
      primary_magic_number: 1, magic_numbers: [1], historical_magic_numbers: [], magic_number: 1,
      net_profit: row[0], profit: row[0] - row[2] - row[3], volume: row[1], commission: row[2], swap: row[3],
      open_time_msc: row[4], close_time_msc: row[5], open_price: row[6], close_price: row[7], direction: 'Long' })) as JournalTrade[]
    const first = trades[0]
    const data = { generated_at: '', window: 'all', account: '7396577', range_start_msc: first.open_time_msc,
      range_end_msc: trades.at(-1)!.close_time_msc, balances: { '7396577': 100250.8 }, trades,
      real_equity_curve: [{ time_msc: first.close_time_msc, equity: 100000 + first.net_profit, net_profit: first.net_profit, label: 'USDJPY' }],
      daily_balances: [], capital_coverage: { accounts: [], start: { time_msc: null, total_balance: null, accounts: {}, complete: false }, end: { time_msc: null, total_balance: null, accounts: {}, complete: false } },
      reconciliation: { raw_closed_trades: 48, attributed_trades: 48, unassigned_trades: 0, displayed_trades: 48, status: 'reconciled' } } as unknown as JournalAnalysisData
    const result = buildAccountPerformance(data)
    expect(result.overview).toMatchObject({ trades: 48, wins: 22, losses: 26 })
    expect(result.overview.profit).toBeCloseTo(250.8, 8)
    expect(result.overview.lots).toBeCloseTo(69.61, 8)
    expect(result.overview.movement).toBeCloseTo(51.7, 8)
    expect(result.overview.commissions).toBeCloseTo(-417.66, 8)
    expect(result.overview.swaps).toBeCloseTo(242.95, 8)
    expect(result.advanced.profitFactor).toBeCloseTo(1.047720345, 8)
    expect(result.advanced.expectancy).toBeCloseTo(5.225, 8)
    expect(result.advanced.deviation).toBeCloseTo(237.4593437, 7)
    expect(result.advanced.averageWin).toBeCloseTo(250.291818, 6)
    expect(result.advanced.averageLoss).toBeCloseTo(-202.139231, 6)
    expect(result.advanced.bestTrade).toBe(470.54)
    expect(result.advanced.worstTrade).toBe(-211.89)
    expect(result.advanced.averageDuration).toBeCloseTo(41476.393, 3)
  })

  it('adds MT5 deposits to the capital base for gain and drawdown percentages', () => {
    const trade = (closed: number, net: number) => ({ strategy_id: 1, strategy_name: 'Bot', symbol: 'NAS100', account: 'A',
      source_role: 'live', is_stopped: false, primary_magic_number: 1, magic_numbers: [1], historical_magic_numbers: [], magic_number: 1,
      open_time_msc: closed - 60_000, close_time_msc: closed, open_price: 1, close_price: 1, direction: 'Long',
      volume: 1, profit: net, commission: 0, swap: 0, net_profit: net }) as JournalTrade
    const data = { trades: [trade(1_000, -100), trade(3_000, -50)], balances: { A: 1350 },
      initial_capitals: { A: 1000 }, range_start_msc: 0, range_end_msc: 4_000,
      capital_movements: [{ account: 'A', ticket: 7, time_msc: 2_000, amount: 500, comment: 'Deposit' }] } as unknown as JournalAnalysisData
    const result = buildAccountPerformance(data)
    expect(result.capital).toBe(1000)
    expect(result.netDeposits).toBe(500)
    expect(result.overview.gainPct).toBeCloseTo(-150 / 1500 * 100, 8)
    const last = result.curves.profit.at(-1)!
    expect(last).toMatchObject({ profit: -150, drawdown: 150 })
    expect(last.drawdown_pct).toBeCloseTo(10, 8)
    expect(last.growth).toBeCloseTo(-10, 8)
  })
})
