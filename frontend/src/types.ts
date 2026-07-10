export type Health = { status: 'green' | 'yellow' | 'red' | 'gray'; reasons: string[]; baseline_sample?: string }
export type Metrics = {
  net_profit: number; floating_profit: number; gross_profit: number; gross_loss: number; trades: number;
  winning_trades: number; losing_trades: number; breakeven_trades: number;
  open_positions: number; win_rate: number; profit_factor: number | null; expectancy: number;
  avg_duration_seconds: number; median_duration_seconds: number; avg_win: number; avg_loss: number;
  best_trade: number | null; worst_trade: number | null; today_profit: number; today_trades: number;
  max_consecutive_wins: number; max_consecutive_losses: number; current_consecutive_losses: number; trades_per_month: number;
  trade_edge: number | null; performance_months: number; performance_trades_per_month: number; monthly_sqn: number | null;
  max_drawdown: number; return_dd: number | null; sqn: number | null; commissions: number; swaps: number;
}
export type RiskStatus = 'green' | 'yellow' | 'red' | 'gray'
export type RiskCheck = {
  status: RiskStatus; actual: number; limit: number | null; ratio: number | null; source: string | null;
}
export type RiskGuard = {
  status: RiskStatus; stop_recommended: boolean; reasons: string[];
  live: { trades: number; max_drawdown: number; max_consecutive_losses: number; current_consecutive_losses: number };
  checks: Record<'is' | 'oos', { drawdown: RiskCheck; loss_streak: RiskCheck }>;
}
export type Baseline = { sample_type: string; source: string; synced_at: string; metrics: Record<string, unknown> }
export type SQXInfo = {
  project: string; databank: string; strategy_name: string; symbol: string;
  timeframe: string; filter_result: string; last_synced_at: string; missing_from_sqx_at: string | null;
}
export type SQXAnalysisResult = {
  available: boolean; reason?: string; detail?: string;
}
export type SQXEdge = SQXAnalysisResult & {
  score?: number; grade?: string; pillars?: Record<string, number>;
  xs_value?: number | null; config_source?: string; strategy_type?: string;
}
export type SQXEgt = SQXAnalysisResult & {
  total?: number; buy?: number; sell?: number; n_buy?: number; n_sell?: number;
  months?: number; grade?: string; sample_type?: string; pl_unit?: string;
  from_month?: string; source?: string; history_source?: string; source_file?: string | null;
  bars?: number | null; symbol?: string; timeframe?: string;
}
export type SQXAnalytics = {
  project: string; databank: string; synced_at: string; edge: SQXEdge; egt: SQXEgt;
}
export type StrategyDeletionImpact = {
  strategy_id: number; name: string; missing_from_sqx_at: string | null;
  allowed: boolean; blockers: string[];
  counts: Record<'mappings' | 'sqx_links' | 'baseline_snapshots' | 'sqx_analytics_snapshots' | 'backtest_runs' | 'backtest_metrics' | 'backtest_batch_items' | 'expert_links' | 'alert_settings', number>;
}
export type BacktestSummary = {
  state: 'validated' | 'running' | 'failed' | 'none';
  has_completed: boolean; completed_count: number; latest_run_id: number | null;
  latest_status: string | null; latest_completed_at: string | null;
}
export type Strategy = {
  id: number; identity_strategy_id: number; symbol: string; sqx_name: string; mql5_name: string; account_login: string;
  lineage_accounts: { current: string[]; predecessor: string[] };
  origin: string; last_observed_at?: string; note: string; note_updated_at: string | null; selection: boolean;
  state: string; link_state: 'linked' | 'candidate' | 'sqx_catalog' | 'sqx_only' | 'mt5_only' | 'catalog_only';
  sqx: SQXInfo | null; sqx_analytics: SQXAnalytics | null; metrics: Metrics; historical_metrics: Metrics; lifetime_metrics: Metrics; account_metrics: Record<string, Metrics>; health: Health; risk_guard: RiskGuard; baseline: Baseline | null; baselines: Baseline[];
  backtest: BacktestSummary; mapping_count: number; historical_mapping_count: number;
  magic_numbers: number[];
}
export type Trade = {
  terminal_id: number; position_id: number; deal_ticket: number; symbol: string; direction: string;
  open_time_msc: number; close_time_msc: number; open_price: number; close_price: number; volume: number;
  magic: number; comment: string; exit_comment: string; profit: number; commission: number; swap: number;
  net_profit: number; status: string; source_account?: string; source_role?: 'live' | 'historical';
}
export type EquityPoint = { time_msc: number; equity: number; net_profit: number }
export type StrategyDetails = Strategy & { mappings: unknown[]; trades: Trade[]; current_trades: Trade[]; historical_trades: Trade[]; equity_curve: EquityPoint[] }
export type Terminal = { id: number; name: string; data_dir: string; account_login?: string; server?: string; status: string; last_seen?: string; last_error?: string }
export type Dashboard = {
  generated_at: string; window: string;
  totals: { strategies: number; active: number; net_profit: number; floating_profit: number; trades: number; red: number };
  integration: { linked: number; candidate: number; sqx_catalog: number; sqx_only: number; mt5_only: number; catalog_only: number };
  terminals: Terminal[]; strategies: Strategy[];
}

export type BacktestDefaults = {
  strategy_id: number; broker: string; expert_path: string; sqx_symbol: string; symbol: string;
  timeframe: string; from_date: string; to_date: string; deposit: number; currency: string;
  leverage: string; model: number; spread: number; config_source: string;
}
export type BacktestRun = {
  id: number; strategy_id: number; broker: string; expert_path: string; expert_hash: string;
  sqx_symbol?: string; symbol: string; timeframe: string; from_date: string; to_date: string;
  deposit: number; currency: string; leverage: string; model: number; spread?: number;
  config_source: string; status: string; requested_at: string; started_at?: string;
  finished_at?: string; report_path?: string; error?: string;
  metrics?: Record<string, unknown>; raw_metrics?: Record<string, string>;
}
export type BacktestCandidates = {
  counts: { eligible: number; resolvable: number; blocked: number; validated: number };
  expert_files: number;
  candidates: Array<{
    id: number; sqx_name: string; state: 'eligible' | 'resolvable' | 'blocked' | 'validated';
    reason: string; resolution_method?: string; confidence?: number;
  }>;
}
export type BacktestBatchItem = {
  id: number; strategy_id: number; status: string; sqx_name: string; mql5_name?: string;
  symbol?: string; resolution_method?: string; confidence: number; error?: string;
}
export type BacktestBatch = {
  id: number; status: string; model: number; policy: string; created_at: string;
  started_at?: string; finished_at?: string; current_strategy_id?: number; error?: string;
  counts: Record<string, number>; items: BacktestBatchItem[];
}
