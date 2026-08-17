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
  baseline_available: boolean; missing_message: string | null;
  live: { trades: number; max_drawdown: number; max_consecutive_losses: number; current_consecutive_losses: number };
  checks: Record<'is' | 'oos' | 'mt5', { drawdown: RiskCheck; loss_streak: RiskCheck }>;
}
export type AlertEpisode = {
  id: number; strategy_id: number; alert_type: string; sample_type: string | null;
  severity: 'yellow' | 'red'; max_severity: 'yellow' | 'red';
  reason: string; actual: number | null; threshold: number | null;
  status: 'active' | 'recovering' | 'resolved'; recovery_wins: number;
  first_detected_at: string; last_detected_at: string; resolved_at: string | null;
  details: Record<string, unknown>;
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
export type SQXDoctor = SQXAnalysisResult & {
  score?: number; verdict?: 'ACEPTABLE' | 'REPARABLE' | 'NO ACEPTABLE' | 'DATOS INSUFICIENTES'; data_quality?: 'ALTA' | 'MEDIA' | 'BAJA';
  metrics?: { trades: number; pf: number; dd_pct: number; sharpe: number; stability: number };
  issues?: Array<{ severity: 'critical' | 'medium' | 'low'; title: string; evidence: string }>;
  recipes?: Array<{ priority: string; title: string; confidence: number; evidence: string; action: string }>;
}
export type SQXAnalytics = {
  project: string; databank: string; synced_at: string; edge: SQXEdge; egt: SQXEgt; doctor: SQXDoctor;
}
export type StrategyDeletionImpact = {
  strategy_id: number; name: string; missing_from_sqx_at: string | null;
  eligibility_reasons: Array<'stopped' | 'missing_from_sqx'>;
  allowed: boolean; blockers: string[];
  counts: Record<'mappings' | 'sqx_links' | 'baseline_snapshots' | 'sqx_analytics_snapshots' | 'backtest_runs' | 'backtest_metrics' | 'backtest_batch_items' | 'expert_links' | 'alert_settings' | 'alert_episodes', number>;
}
export type StrategyArchiveImpact = {
  strategy_id: number; name: string; allowed: boolean; reasons: string[]; blockers: string[];
  magic_numbers: number[]; lifetime_trades: number; open_positions: number;
}
export type BacktestSummary = {
  state: 'validated' | 'running' | 'failed' | 'none';
  has_completed: boolean; completed_count: number; latest_run_id: number | null;
  latest_status: string | null; latest_completed_at: string | null; latest_completed_run_id?: number | null;
  equity_curve_available?: boolean; equity_curve_reason?: string | null;
  report_available?: boolean; report_reason?: string | null;
  history_source?: string | null; custom_symbol?: string | null;
  risk_alert?: boolean;
  risk_alert_accepted?: boolean;
  risk_alert_accepted_at?: string | null;
  no_mm_warning?: boolean;
  no_mm_reasons?: string[];
  no_mm_accepted?: boolean;
  no_mm_accepted_at?: string | null;
  risk_audit?: {
    status: string; tolerance?: number; use_money_management?: boolean; mm_risked_money?: number | null; fixed_lot?: number | null;
    mm_multiplier?: number | null; declared_risk: number | null; worst_loss: number; risk_ratio: number | null;
    warning?: string | null; evidence?: string | null;
    executed_entry_count?: number; all_entries_min_lot?: boolean;
    no_mm_warning?: boolean; no_mm_reasons?: string[];
  } | null;
}
export type Strategy = {
  id: number; identity_strategy_id: number; symbol: string; asset_key: string; sqx_name: string; mql5_name: string; display_name: string; display_name_source: string | null; account_login: string; timeframe: string | null;
  timeframe_source: 'terminal_profile' | 'sqx' | 'missing';
  lineage_accounts: { current: string[]; predecessor: string[] };
  origin: string; last_observed_at?: string; note: string; note_updated_at: string | null; selection: boolean; strategy_status: '' | 'live' | 'monitor' | 'candidate_live'; no_mm_accepted: boolean; no_mm_accepted_at: string | null;
  state: string; link_state: 'linked' | 'candidate' | 'sqx_catalog' | 'sqx_only' | 'mt5_only' | 'catalog_only';
  deployment_state: 'attached_active' | 'attached_inactive' | 'observed' | null;
  activity_state: 'with_recent_trades' | 'without_recent_trades' | 'undetermined' | 'not_deployed';
  last_inventory_at: string | null;
  deployments: BotDeployment[];
  sqx: SQXInfo | null; sqx_analytics: SQXAnalytics | null; metrics: Metrics; live_metrics: Metrics; historical_metrics: Metrics; lifetime_metrics: Metrics; monthly_net_profit: Array<{ key: string; label: string; net_profit: number }>; account_metrics: Record<string, Metrics>; account_balance: number | null; account_pl_percent: number | null; account_initial_capital: number | null; initial_capital_pl_percent: number | null; health: Health; risk_guard: RiskGuard; baseline: Baseline | null; baselines: Baseline[];
  active_alerts: AlertEpisode[];
  alert_thresholds: { loss_streak_yellow: number; loss_streak_red: number; loss_streak_relative_yellow: number; loss_streak_relative_red: number };
  backtest: BacktestSummary; backtest_risk?: BacktestSummary['risk_audit']; backtest_risk_alert?: boolean; mapping_count: number; historical_mapping_count: number;
  magic_numbers: number[]; historical_magic_numbers: number[]; primary_magic_number: number | null;
  magic_number_details: Array<{ magic: number; roles: string[]; trades: number; first_trade_at: string | null; last_trade_at: string | null; confirmed: boolean }>;
  recent_equity_curve: Array<{ net_profit: number; equity: number }>;
  operational_identity: { server: string; account_login: string; asset_key: string; expert_name: string; expert_key: string; symbol: string } | null;
  identity_conflicts: Array<{ identity: { server: string; account_login: string; asset_key: string; expert_key: string }; primary: boolean; mapping_ids: number[]; roles: string[] }>;
}
export type BotDeployment = {
  id: number; account_login: string; server: string; chart_id: string; symbol: string; timeframe: string;
  ex5_name: string; magic: number | null; deployment_status: string; trade_status: string;
  activity_status: string; observed_at: string; confirmed: number; strategy_id: number | null;
  first_trade_at?: string | null; last_trade_at?: string | null;
}
export type InventorySummary = {
  account_login: string; server: string; inventoried: number; linked: number; pending: number;
  active: number; inactive: number; with_recent_trades: number; without_recent_trades: number;
  activity_undetermined: number; last_inventory_at: string;
}
export type Trade = {
  trade_key: string;
  terminal_id: number; position_id: number; deal_ticket: number; symbol: string; direction: string;
  open_time_msc: number; close_time_msc: number; open_price: number; close_price: number; volume: number;
  magic: number; comment: string; exit_comment: string; profit: number; commission: number; swap: number;
  net_profit: number; status: string; source_account?: string; source_role?: 'live' | 'historical';
  source_deal_id?: number; source_import_id?: number;
}
export type EquityPoint = { time_msc: number; equity: number; net_profit: number }
export type StrategyMapping = {
  id: number; terminal_id: number; account_login: string; symbol: string; magic: number | null;
  comment_pattern: string; role: 'live' | 'historical'; confirmed: number;
}
export type StrategyDetails = Strategy & { mappings: StrategyMapping[]; operational_mappings?: StrategyMapping[]; lineage_included?: boolean; trades: Trade[]; current_trades: Trade[]; historical_trades: Trade[]; equity_curve: EquityPoint[] }
export type PanelHistory = {
  trades: Trade[];
  total_trades: number;
  equity_curve: EquityPoint[];
  mappings: StrategyMapping[];
  operational_mappings?: StrategyMapping[];
  cursor: { max_deal_id: number; max_import_id: number };
  scope_version: string;
  reset: boolean;
}
export type Terminal = { id: number; name: string; data_dir: string; transport?: 'local'|'ssh'; remote_data_dir?: string; account_login?: string; server?: string; status: string; last_seen?: string; last_error?: string }
export type TerminalEvent = {
  id: number; source_id: number; terminal_id: number; account_login: string | null;
  severity: 'warning' | 'error'; log_kind: 'journal' | 'experts'; log_name: string;
  occurred_at: string | null; captured_at: string; message: string; context: string;
  source_label: string; terminal_name: string;
}
export type TerminalEventsData = {
  generated_at: string; events: TerminalEvent[]; total: number; limit: number; offset: number;
  summary: { errors: number; warnings: number };
  sources: Array<{ id: number; label: string; account_login: string | null; terminal_name: string }>;
}
export type Dashboard = {
  generated_at: string; window: string;
  monthly_net_profit_months: Array<{ key: string; label: string }>;
  totals: { strategies: number; active: number; net_profit: number; floating_profit: number; trades: number; red: number };
  timeframe_coverage: { terminal_profile: number; sqx: number; missing: number };
  integration: { linked: number; candidate: number; sqx_catalog: number; sqx_only: number; mt5_only: number; catalog_only: number };
  terminals: Terminal[]; strategies: Strategy[];
  inventory_summary: InventorySummary[];
  broker_accounts: string[]; has_unassigned_broker_account: boolean;
  asset_options: Array<{ key: string; label: string; symbols: string[] }>;
}

export type AccountAliasSettings = {
  accounts: string[];
  aliases: Record<string, string>;
}

export type JournalTrade = {
  strategy_id: number; strategy_name: string; symbol: string; account: string; source_role: 'live' | 'historical' | 'unassigned';
  is_stopped: boolean;
  primary_magic_number: number | null; magic_numbers: number[]; historical_magic_numbers: number[];
  open_time_msc: number; close_time_msc: number; direction: string; volume: number; profit: number; commission: number; swap: number; net_profit: number;
}
export type JournalBalancePoint = { time_msc: number; balance: number }
export type JournalBalanceBoundary = {
  time_msc: number | null; total_balance: number | null; accounts: Record<string, JournalBalancePoint>;
  complete: boolean; max_age_msc?: number | null;
}
export type JournalAnalysisData = {
  generated_at: string; window: string; account: string | null; range_start_msc: number; range_end_msc: number;
  balances: Record<string, number>; trades: JournalTrade[];
  unassigned_groups?: Array<{ account: string; symbol: string; magic_number: number; reason: string; count: number; first_close_time_msc: number; last_close_time_msc: number }>;
  daily_balances: Array<{ day: string; time_msc: number; total_balance: number; accounts: Record<string, JournalBalancePoint>; complete: boolean }>;
  capital_coverage: { accounts: string[]; start: JournalBalanceBoundary; end: JournalBalanceBoundary };
  reconciliation: { raw_closed_trades: number; attributed_trades: number; unassigned_trades: number; displayed_trades: number; status: 'reconciled' | 'mismatch' };
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
export type BacktestRetentionPreview = {
  candidate_count: number; completed_superseded: number; failed_or_terminal: number;
  active_excluded_states: string[]; runs: Array<{ id: number; strategy_id: number; status: string; broker: string; requested_at: string; finished_at?: string; run_dir?: string }>;
}
export type DailyPnl = { strategy_id: number; month: string; days: Record<string, number>; net_profit: number }
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
