export type Health = { status: 'green' | 'yellow' | 'red' | 'gray'; reasons: string[]; baseline_sample?: string }
export type Metrics = {
  net_profit: number; floating_profit: number; gross_profit: number; gross_loss: number; trades: number;
  winning_trades: number; losing_trades: number; breakeven_trades: number;
  open_positions: number; win_rate: number; profit_factor: number | null; expectancy: number;
  avg_duration_seconds: number; median_duration_seconds: number; avg_win: number; avg_loss: number;
  best_trade: number | null; worst_trade: number | null; today_profit: number; today_trades: number;
  max_consecutive_wins: number; max_consecutive_losses: number; trades_per_month: number;
  max_drawdown: number; return_dd: number | null; sqn: number | null; commissions: number; swaps: number;
}
export type Baseline = { sample_type: string; source: string; synced_at: string; metrics: Record<string, unknown> }
export type SQXInfo = {
  project: string; databank: string; strategy_name: string; symbol: string;
  timeframe: string; filter_result: string; last_synced_at: string;
}
export type Strategy = {
  id: number; symbol: string; sqx_name: string; mql5_name: string; account_login: string;
  origin: string; last_observed_at?: string;
  state: string; link_state: 'linked' | 'candidate' | 'sqx_only' | 'mt5_only' | 'catalog_only';
  sqx: SQXInfo | null; metrics: Metrics; health: Health; baseline: Baseline | null; baselines: Baseline[]; mapping_count: number;
  magic_numbers: number[];
}
export type Trade = {
  terminal_id: number; position_id: number; deal_ticket: number; symbol: string; direction: string;
  open_time_msc: number; close_time_msc: number; open_price: number; close_price: number; volume: number;
  magic: number; comment: string; exit_comment: string; profit: number; commission: number; swap: number;
  net_profit: number; status: string;
}
export type EquityPoint = { time_msc: number; equity: number; net_profit: number }
export type StrategyDetails = Strategy & { mappings: unknown[]; trades: Trade[]; equity_curve: EquityPoint[] }
export type Terminal = { id: number; name: string; data_dir: string; account_login?: string; server?: string; status: string; last_seen?: string; last_error?: string }
export type Dashboard = {
  generated_at: string; window: string;
  totals: { strategies: number; active: number; net_profit: number; floating_profit: number; trades: number; red: number };
  integration: { linked: number; candidate: number; sqx_only: number; mt5_only: number; catalog_only: number };
  terminals: Terminal[]; strategies: Strategy[];
}
