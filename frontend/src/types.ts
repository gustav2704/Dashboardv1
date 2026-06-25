export type Health = { status: 'green' | 'yellow' | 'red' | 'gray'; reasons: string[]; baseline_sample?: string }
export type Metrics = {
  net_profit: number; floating_profit: number; gross_profit: number; gross_loss: number; trades: number;
  open_positions: number; win_rate: number; profit_factor: number | null; expectancy: number;
  avg_duration_seconds: number; median_duration_seconds: number; avg_win: number; avg_loss: number;
  max_consecutive_wins: number; max_consecutive_losses: number; trades_per_month: number;
  max_drawdown: number; return_dd: number | null; sqn: number | null; commissions: number; swaps: number;
}
export type Baseline = { sample_type: string; source: string; synced_at: string; metrics: Record<string, unknown> }
export type Strategy = {
  id: number; symbol: string; sqx_name: string; mql5_name: string; account_login: string;
  state: string; metrics: Metrics; health: Health; baseline: Baseline | null; baselines: Baseline[]; mapping_count: number;
  magic_numbers: number[];
}
export type Terminal = { id: number; name: string; data_dir: string; account_login?: string; server?: string; status: string; last_seen?: string; last_error?: string }
export type Dashboard = {
  generated_at: string; window: string;
  totals: { strategies: number; active: number; net_profit: number; floating_profit: number; trades: number; red: number };
  terminals: Terminal[]; strategies: Strategy[];
}
