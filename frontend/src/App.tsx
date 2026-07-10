import { useEffect, useMemo, useRef, useState } from 'react'
import { Activity, CandlestickChart, FlaskConical, LayoutDashboard, Pause, Play, RefreshCw, RotateCcw, Settings as SettingsIcon, Square, TrendingUp, Trash2 } from 'lucide-react'
import { Bar, BarChart, CartesianGrid, Cell, ReferenceLine, ResponsiveContainer, Scatter, ScatterChart, Tooltip, XAxis, YAxis, ZAxis } from 'recharts'
import { api } from './api'
import ChartPanel from './ChartPanel'
import type { BacktestBatch, BacktestCandidates, BacktestDefaults, BacktestRun, BacktestSummary, Baseline, Dashboard, Metrics, RiskCheck, RiskGuard, Strategy, StrategyDeletionImpact, StrategyDetails } from './types'

type Tab = 'overview' | 'performance' | 'detail' | 'chart' | 'backtests' | 'settings'
type SortKey = 'state' | 'source' | 'backtest' | 'edge' | 'egt' | 'strategy' | 'symbol' | 'account' | 'magic' | 'net_profit' | 'trades' | 'win_rate' | 'profit_factor' | 'max_drawdown' | 'avg_wl' | 'best_trade' | 'today_profit' | 'history'
type SortDirection = 'ascending' | 'descending'
type SortState = { key: SortKey; direction: SortDirection } | null
const SIDEBAR_STORAGE_KEY = 'dashboardv1:sidebar-collapsed'

const stateLabels: Record<string, string> = {
  active: 'Active', no_recent_trades: 'No recent trades', unlinked: 'Unlinked',
  retired: 'Retired', terminal_disconnected: 'Terminal disconnected',
}
const linkLabels: Record<string, string> = {
  linked: 'Linked', candidate: 'Candidate', sqx_catalog: 'SQX + Catalog', sqx_only: 'SQX only',
  mt5_only: 'MT5 only', catalog_only: 'Catalog only',
}

function money(value: number | null | undefined) {
  return value == null ? '—' : new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(value)
}
function number(value: number | null | undefined, digits = 2) { return value == null ? '—' : value.toFixed(digits) }
function signedMoney(value: number | null | undefined) {
  if (value == null) return '—'
  return `${value > 0 ? '+' : ''}${money(value)}`
}
function duration(seconds: number) {
  if (!seconds) return '—'
  const hours = seconds / 3600
  return hours < 24 ? `${hours.toFixed(1)} h` : `${(hours / 24).toFixed(1)} d`
}
function tradeDuration(openTime: number, closeTime: number) {
  return duration(Math.max(0, closeTime - openTime) / 1000)
}
function buildDashboardQuery(windowName: string, customStart: string, customEnd: string) {
  const params = new URLSearchParams({ window: windowName })
  if (windowName === 'custom' && customStart && customEnd) {
    params.set('start', customStart)
    params.set('end', customEnd)
  }
  return params.toString()
}
function recoveryFactor(metrics: Metrics) {
  if (metrics.return_dd != null) return number(metrics.return_dd)
  return metrics.max_drawdown === 0 && metrics.net_profit > 0 ? '∞' : '—'
}
function performanceMetrics(strategy: Strategy) {
  return strategy.lifetime_metrics || strategy.metrics
}
const UNASSIGNED_ACCOUNT = '__unassigned__'
const accountAliases: Record<string, string> = {
  '7396577': 'Demo FP master',
  '4000094894': 'Live Dwnx',
  '100121894': 'Demo FP experimental',
}
function accountAlias(account: string | null | undefined) {
  return accountAliases[String(account || '').trim()] || ''
}
function addBrokerAccount(accounts: Set<string>, account: string | null | undefined) {
  const normalized = String(account || '').trim()
  if (normalized) accounts.add(normalized)
}
function strategyBrokerAccounts(strategy: Strategy) {
  const accounts = new Set<string>()
  addBrokerAccount(accounts, strategy.account_login)
  for (const account of strategy.lineage_accounts?.current || []) addBrokerAccount(accounts, account)
  for (const account of strategy.lineage_accounts?.predecessor || []) addBrokerAccount(accounts, account)
  for (const account of Object.keys(strategy.account_metrics || {})) addBrokerAccount(accounts, account)
  return accounts
}
function strategyMatchesBrokerAccount(strategy: Strategy, selectedAccount: string) {
  if (!selectedAccount) return true
  const accounts = strategyBrokerAccounts(strategy)
  if (selectedAccount === UNASSIGNED_ACCOUNT) return accounts.size === 0
  return accounts.has(selectedAccount)
}
function brokerMetric(strategy: Strategy, selectedAccount: string) {
  if (selectedAccount && selectedAccount !== UNASSIGNED_ACCOUNT) return strategy.account_metrics?.[selectedAccount] || null
  return performanceMetrics(strategy)
}
function healthLabel(status: string) {
  return status === 'gray' ? 'Not evaluated' : status === 'green' ? 'In range' : status === 'yellow' ? 'Attention' : 'Deviation'
}
function directionLabel(direction: string) {
  return direction === 'Long' ? 'Buy' : direction === 'Short' ? 'Sell' : direction || '—'
}
function readSidebarCollapsed() {
  try { return localStorage.getItem(SIDEBAR_STORAGE_KEY) === 'true' }
  catch { return false }
}
function baselineValue(baseline: Baseline | null, ...keys: string[]) {
  if (!baseline) return null
  const folded = Object.fromEntries(Object.entries(baseline.metrics).map(([key, value]) => [key.toLowerCase().replaceAll('_', ''), value]))
  for (const key of keys) {
    const value = folded[key.toLowerCase().replaceAll('_', '')]
    const scalar = value && typeof value === 'object' && 'amount' in value ? (value as { amount: unknown }).amount : value
    const parsed = Number(scalar)
    if (value !== undefined && value !== '' && Number.isFinite(parsed)) return parsed
  }
  return null
}

function Logo() { return <div className="logo"><div className="logo-mark"><span /><span /><span /></div><div><strong>EA Observatory</strong><small>LIVE VS. BACKTEST</small></div></div> }
function HealthDot({ status }: { status: string }) { return <span className={`health-dot ${status}`} aria-label={status} /> }

const backtestLabels: Record<BacktestSummary['state'], string> = {
  validated: 'Validated',
  running: 'Running',
  failed: 'Failed',
  none: 'No backtest',
}

function BacktestBadge({ summary }: { summary: BacktestSummary }) {
  const details = summary.state === 'none'
    ? 'No MT5 backtest runs'
    : [
        `${summary.completed_count} completed backtest${summary.completed_count === 1 ? '' : 's'}`,
        summary.latest_completed_at ? `Last valid: ${new Date(summary.latest_completed_at).toLocaleString()}` : 'No valid result',
        `Latest run: ${summary.latest_status || 'unknown'}`,
      ].join(' · ')
  return <span className={`backtest-badge ${summary.state}`} title={details}>{backtestLabels[summary.state]}</span>
}

function SortableHeader({ label, sortKey, sort, onSort }: { label: string; sortKey: SortKey; sort: SortState; onSort: (key: SortKey) => void }) {
  const active = sort?.key === sortKey
  const direction = active ? sort.direction : 'none'
  return <th className="sortable-header" aria-sort={direction}>
    <button type="button" className={active ? 'sort-button active' : 'sort-button'} onClick={() => onSort(sortKey)}>
      <span>{label}</span><span className="sort-indicator" aria-hidden="true">{active ? (sort.direction === 'ascending' ? '↑' : '↓') : '↕'}</span>
    </button>
  </th>
}

const strategyCollator = new Intl.Collator('es-MX', { numeric: true, sensitivity: 'base' })

function sortValue(strategy: Strategy, key: SortKey): string | number | null {
  switch (key) {
    case 'state': return stateLabels[strategy.state] || strategy.state
    case 'source': return linkLabels[strategy.link_state] || strategy.link_state
    case 'backtest': return backtestLabels[strategy.backtest.state]
    case 'edge': return strategy.sqx_analytics?.edge.available ? strategy.sqx_analytics.edge.score ?? null : null
    case 'egt': return strategy.sqx_analytics?.egt.available ? strategy.sqx_analytics.egt.total ?? null : null
    case 'strategy': return strategy.mql5_name || strategy.sqx_name
    case 'symbol': return strategy.symbol || null
    case 'account': return strategy.account_login || null
    case 'magic': return strategy.magic_numbers?.[0] ?? null
    case 'net_profit': return performanceMetrics(strategy).net_profit
    case 'trades': return performanceMetrics(strategy).trades
    case 'win_rate': return performanceMetrics(strategy).win_rate
    case 'profit_factor': return performanceMetrics(strategy).profit_factor
    case 'max_drawdown': return performanceMetrics(strategy).max_drawdown
    case 'avg_wl': return performanceMetrics(strategy).avg_loss < 0 ? performanceMetrics(strategy).avg_win / Math.abs(performanceMetrics(strategy).avg_loss) : performanceMetrics(strategy).avg_win || null
    case 'best_trade': return performanceMetrics(strategy).best_trade
    case 'today_profit': return strategy.metrics.today_profit
    case 'history': return strategy.historical_metrics.trades
  }
}

export function compareStrategies(a: Strategy, b: Strategy, key: SortKey, direction: SortDirection) {
  const left = sortValue(a, key)
  const right = sortValue(b, key)
  if (left == null && right == null) return 0
  if (left == null) return 1
  if (right == null) return -1
  const comparison = typeof left === 'number' && typeof right === 'number'
    ? left - right
    : strategyCollator.compare(String(left), String(right))
  return direction === 'ascending' ? comparison : -comparison
}

function MetricCard({ label, value, detail, tone = '' }: { label: string; value: string; detail: string; tone?: string }) {
  return <div className={`metric-card ${tone}`}><span>{label}</span><strong>{value}</strong><small>{detail}</small></div>
}

function Comparison({ label, current, baseline, format = number }: { label: string; current: number | null; baseline: number | null; format?: (value: number | null | undefined) => string }) {
  const ratio = current != null && baseline ? current / baseline : null
  return <div className="comparison-row">
    <span>{label}</span><strong>{format(current)}</strong><span className="baseline-number">{format(baseline)}</span>
    <span className={ratio == null ? 'delta muted' : ratio >= .85 ? 'delta good' : ratio >= .7 ? 'delta warn' : 'delta bad'}>{ratio == null ? '—' : `${((ratio - 1) * 100).toFixed(0)}%`}</span>
  </div>
}

function StreakPair({ metrics }: { metrics: Metrics }) {
  return <div className="streak-pair" aria-label="Maximum consecutive winning and losing trades">
    <div><span>Lifetime winning streak</span><strong className="streak-win">{metrics.max_consecutive_wins ?? 0}</strong></div>
    <div><span>Lifetime losing streak</span><strong className="streak-loss">{metrics.max_consecutive_losses ?? 0}</strong></div>
  </div>
}

function riskCheckLabel(check: RiskCheck) {
  if (check.status === 'gray') return 'Not available'
  if (check.status === 'red') return 'Exceeded'
  if (check.status === 'yellow') return check.ratio === 1 ? 'At limit' : 'Approaching limit'
  return 'In range'
}

function RiskCheckCell({ check, format }: { check: RiskCheck; format: (value: number | null | undefined) => string }) {
  return <div className={`risk-check ${check.status}`}>
    <strong>{format(check.limit)}</strong>
    <span>{riskCheckLabel(check)}{check.source ? ` · ${check.source}` : ''}</span>
  </div>
}

function RiskGuardPanel({ risk }: { risk: RiskGuard }) {
  return <section className={`panel risk-guard-panel ${risk.status}`}>
    <div className="panel-heading">
      <div><span className="eyebrow">HARD RISK LIMITS</span><h2>Risk Guard</h2></div>
      <span className={`risk-verdict ${risk.stop_recommended ? 'stop' : risk.status}`}>
        {risk.stop_recommended ? 'Stop recommended' : risk.status === 'gray' ? 'Not evaluated' : risk.status === 'yellow' ? 'Attention' : 'Within limits'}
      </span>
    </div>
    <div className="risk-live-strip">
      <div><span>Current losing streak</span><strong>{risk.live.current_consecutive_losses}</strong></div>
      <div><span>Live max loss streak</span><strong>{risk.live.max_consecutive_losses}</strong></div>
      <div><span>Live max drawdown</span><strong>{money(risk.live.max_drawdown)}</strong></div>
      <div><span>History evaluated</span><strong>{risk.live.trades} trades</strong></div>
    </div>
    <div className="risk-grid risk-grid-head"><span>Metric</span><span>Live</span><span>IS limit</span><span>OOS limit</span></div>
    <div className="risk-grid">
      <span>Max drawdown</span><strong>{money(risk.live.max_drawdown)}</strong>
      <RiskCheckCell check={risk.checks.is.drawdown} format={money} />
      <RiskCheckCell check={risk.checks.oos.drawdown} format={money} />
    </div>
    <div className="risk-grid">
      <span>Max losing streak</span><strong>{risk.live.max_consecutive_losses}</strong>
      <RiskCheckCell check={risk.checks.is.loss_streak} format={value => value == null ? '—' : String(value)} />
      <RiskCheckCell check={risk.checks.oos.loss_streak} format={value => value == null ? '—' : String(value)} />
    </div>
  </section>
}

function MissingSqxNotice({ strategy, onDeleted }: { strategy: Strategy; onDeleted: (strategyId: number) => void }) {
  const [deleting, setDeleting] = useState(false)
  const [error, setError] = useState('')
  if (!strategy.sqx?.missing_from_sqx_at) return null
  async function remove() {
    setDeleting(true)
    setError('')
    try {
      const impact = await api<StrategyDeletionImpact>(`/api/strategies/${strategy.id}/deletion-impact`)
      if (!impact.allowed) {
        setError(impact.blockers.join('. '))
        return
      }
      const affected = Object.entries(impact.counts)
        .filter(([, count]) => count > 0)
        .map(([name, count]) => `${count} ${name.replaceAll('_', ' ')}`)
        .join(', ')
      const confirmed = window.confirm(
        `Permanently delete "${impact.name}" from the dashboard?\n\n` +
        `This cannot be undone. Records removed: ${affected || 'strategy record only'}.`
      )
      if (!confirmed) return
      await api(`/api/strategies/${strategy.id}`, { method: 'DELETE' })
      onDeleted(strategy.id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not delete strategy')
    } finally {
      setDeleting(false)
    }
  }
  return <div className="missing-sqx-notice">
    <div><strong>Missing from SQX</strong><span>Not found during the latest successful databank sync.</span></div>
    <button className="button danger" type="button" disabled={deleting} onClick={remove}><Trash2 size={14}/>{deleting ? 'Checking…' : 'Delete permanently'}</button>
    {error && <small>{error}</small>}
  </div>
}

function SQXAnalyticsBadge({ strategy, kind }: { strategy: Strategy; kind: 'edge' | 'egt' }) {
  if (kind === 'edge') {
    const edge = strategy.sqx_analytics?.edge
    if (!edge) return <span className="analytics-badge unavailable" title="No SQX analytics snapshot">—</span>
    if (!edge.available) return <span className="analytics-badge unavailable" title={edge.reason || 'Not available'}>N/A</span>
    return <span className="analytics-badge edge" title={`Edge Decay · ${edge.grade || 'No grade'}`}>{edge.score}<small>{edge.grade}</small></span>
  }
  const egt = strategy.sqx_analytics?.egt
  if (!egt) return <span className="analytics-badge unavailable" title="No SQX analytics snapshot">—</span>
  if (!egt.available) return <span className="analytics-badge unavailable" title={egt.reason || 'Not available'}>N/A</span>
  return <span className="analytics-badge egt" title={`EGT · ${egt.grade || 'No grade'} · ${egt.history_source || egt.source || 'Unknown source'}`}>{number(egt.total)}<small>{egt.grade}</small></span>
}

function SQXAnalyticsPanel({ strategy }: { strategy: Strategy }) {
  const analytics = strategy.sqx_analytics
  const edge = analytics?.edge
  const egt = analytics?.egt
  return <section className="panel sqx-analytics-panel">
    <div className="panel-heading">
      <div><span className="eyebrow">SQX ANALYTICS</span><h2>Edge Decay & EGT</h2></div>
      <span className="source-tag">{analytics ? `${analytics.project} · ${analytics.databank} · ${new Date(analytics.synced_at).toLocaleDateString()}` : 'No snapshot'}</span>
    </div>
    <div className="sqx-analytics-grid">
      <div className={edge?.available ? 'sqx-analysis-summary' : 'sqx-analysis-summary unavailable'}>
        <span>Edge Decay</span>
        <strong>{edge?.available ? `${edge.score} · ${edge.grade}` : 'Not available'}</strong>
        <small>{edge?.available ? `Default config · XS ${number(edge.xs_value, 3)}` : edge?.reason || 'Run an SQX sync to calculate it.'}</small>
      </div>
      <div className={egt?.available ? 'sqx-analysis-summary' : 'sqx-analysis-summary unavailable'}>
        <span>EGT</span>
        <strong>{egt?.available ? `${number(egt.total)} · ${egt.grade}` : 'Not available'}</strong>
        <small>{egt?.available ? `Buy ${number(egt.buy)} · Sell ${number(egt.sell)} · ${egt.months || 0} months · ${egt.history_source || egt.source || 'Unknown source'}${egt.bars ? ` · ${egt.bars} bars` : ''}` : egt?.reason || 'Run an SQX sync to calculate it.'}</small>
      </div>
    </div>
  </section>
}

type PerformancePoint = {
  id: number
  name: string
  accountLabel: string
  symbol: string
  tradeEdge: number
  monthlySqn: number
  tradesPerMonth: number
  trades: number
  months: number
  profitFactor: number | null
  confidence: 'low' | 'established'
}

function performanceColor(value: number) {
  if (value > 0) return '#2dd4bf'
  if (value < 0) return '#fb7185'
  return '#64748b'
}

function PerformanceTooltip({ active, payload }: { active?: boolean; payload?: Array<{ payload: PerformancePoint }> }) {
  const point = payload?.[0]?.payload
  if (!active || !point) return null
  return <div className="performance-tooltip">
    <strong>{point.name}</strong>
    <span>{point.symbol || 'No symbol'} · {point.accountLabel}</span>
    <dl>
      <dt>Trade Edge</dt><dd>{number(point.tradeEdge, 3)}</dd>
      <dt>Monthly SQN</dt><dd>{number(point.monthlySqn, 3)}</dd>
      <dt>Trades / month</dt><dd>{number(point.tradesPerMonth, 1)}</dd>
      <dt>Total trades</dt><dd>{point.trades}</dd>
      <dt>Observed months</dt><dd>{number(point.months, 1)}</dd>
      <dt>Profit factor</dt><dd>{number(point.profitFactor)}</dd>
    </dl>
    <small>{point.confidence === 'low' ? 'Low confidence · 10–29 trades' : 'Established sample · 30+ trades'} · Click to open strategy</small>
  </div>
}

type PerformanceProps = {
  strategies: Strategy[]
  onSelect: (strategyId: number) => void
  selectedAccount: string
  windowName: string
  onWindowChange: (windowName: string) => void
  customStart: string
  onCustomStartChange: (value: string) => void
  customEnd: string
  onCustomEndChange: (value: string) => void
}

function Performance({
  strategies,
  onSelect,
  selectedAccount,
  windowName,
  onWindowChange,
  customStart,
  onCustomStartChange,
  customEnd,
  onCustomEndChange,
}: PerformanceProps) {
  const candidates = selectedAccount
    ? strategies.filter(strategy => selectedAccount === UNASSIGNED_ACCOUNT || strategy.account_metrics?.[selectedAccount] != null)
    : strategies
  const available = candidates
    .map(strategy => {
      const metrics = brokerMetric(strategy, selectedAccount)
      if (!metrics) return null
      if (
        metrics.trades < 10
        || metrics.trade_edge == null
        || metrics.monthly_sqn == null
        || !Number.isFinite(metrics.trade_edge)
        || !Number.isFinite(metrics.monthly_sqn)
      ) return null
      return {
        id: strategy.id,
        name: strategy.mql5_name || strategy.sqx_name,
        accountLabel: selectedAccount && selectedAccount !== UNASSIGNED_ACCOUNT ? `Account ${selectedAccount}` : 'Lifetime lineage',
        symbol: strategy.symbol,
        tradeEdge: metrics.trade_edge,
        monthlySqn: metrics.monthly_sqn,
        tradesPerMonth: metrics.performance_trades_per_month,
        trades: metrics.trades,
        months: metrics.performance_months,
        profitFactor: metrics.profit_factor,
        confidence: metrics.trades < 30 ? 'low' as const : 'established' as const,
      }
    })
    .filter((point): point is PerformancePoint => point != null)
    .sort((left, right) => right.monthlySqn - left.monthlySqn)
  const excluded = candidates.length - available.length
  const lowConfidence = available.filter(point => point.confidence === 'low').length
  const rankingHeight = Math.max(360, available.length * 42 + 72)

  return <div className="performance-view">
    <div className="performance-intro">
      <div><span className="eyebrow">NORMALIZED COMPARISON</span><h2>Scale-free bot performance</h2></div>
      <div className="performance-sample-summary">
        <span><strong>{available.length}</strong> ranked</span>
        <span className="low"><strong>{lowConfidence}</strong> low confidence</span>
        <span><strong>{excluded}</strong> excluded</span>
      </div>
      <p>Monthly SQN standardizes each bot's net trade outcomes and projects its observed trading frequency onto the same one-month horizon. Rankings require at least 10 trades; amber outlines identify samples below 30.</p>
    </div>
    <div className="performance-filters" aria-label="Performance filters">
      <label><span>Date range</span><select value={windowName} onChange={event => onWindowChange(event.target.value)}><option value="30d">30 days</option><option value="90d">90 days</option><option value="all">All</option><option value="custom">Custom</option></select></label>
      {windowName === 'custom' && <>
        <label><span>From</span><input type="date" value={customStart} onChange={event => onCustomStartChange(event.target.value)} /></label>
        <label><span>To</span><input type="date" value={customEnd} onChange={event => onCustomEndChange(event.target.value)} /></label>
      </>}
    </div>
    {!available.length ? <div className="panel empty-state performance-empty">No strategies have enough variable trade history for normalized performance ranking.</div> : <>
      <section className="panel performance-panel">
        <div className="panel-heading">
          <div><span className="eyebrow">RISK-ADJUSTED RANKING</span><h2>Monthly SQN</h2></div>
          <span className="performance-formula">Trade Edge × √ trades/month</span>
        </div>
        <div className="performance-chart-scroll">
          <div className="performance-ranking-chart" style={{ height: rankingHeight }}>
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={available} layout="vertical" margin={{ top: 12, right: 28, bottom: 28, left: 14 }}>
                <CartesianGrid stroke="#172633" horizontal={false} />
                <XAxis type="number" stroke="#5f7889" tick={{ fill: '#74899a', fontSize: 10 }} label={{ value: 'Monthly SQN', position: 'insideBottom', offset: -18, fill: '#74899a', fontSize: 10 }} />
                <YAxis type="category" dataKey="name" width={210} stroke="#5f7889" tick={{ fill: '#a8bdca', fontSize: 10 }} />
                <ReferenceLine x={0} stroke="#496273" />
                <Tooltip content={<PerformanceTooltip />} cursor={{ fill: '#122431' }} />
                <Bar dataKey="monthlySqn" radius={[0, 3, 3, 0]} cursor="pointer" onClick={entry => onSelect((entry as unknown as PerformancePoint).id)}>
                  {available.map(point => <Cell key={point.id} fill={performanceColor(point.monthlySqn)} fillOpacity={point.confidence === 'low' ? .62 : .9} stroke={point.confidence === 'low' ? '#fbbf24' : 'transparent'} strokeWidth={point.confidence === 'low' ? 2 : 0} strokeDasharray={point.confidence === 'low' ? '4 3' : undefined} />)}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
      </section>
      <section className="panel performance-panel">
        <div className="panel-heading">
          <div><span className="eyebrow">EDGE DECOMPOSITION</span><h2>Trade Edge vs. trading frequency</h2></div>
          <span className="performance-formula">Bubble size = trade count</span>
        </div>
        <div className="performance-scatter-chart">
          <ResponsiveContainer width="100%" height="100%">
            <ScatterChart margin={{ top: 24, right: 34, bottom: 42, left: 18 }}>
              <CartesianGrid stroke="#172633" />
              <XAxis type="number" dataKey="tradesPerMonth" name="Trades / month" stroke="#5f7889" tick={{ fill: '#74899a', fontSize: 10 }} label={{ value: 'Trades per observed month', position: 'insideBottom', offset: -26, fill: '#74899a', fontSize: 10 }} />
              <YAxis type="number" dataKey="tradeEdge" name="Trade Edge" stroke="#5f7889" tick={{ fill: '#74899a', fontSize: 10 }} label={{ value: 'Trade Edge', angle: -90, position: 'insideLeft', fill: '#74899a', fontSize: 10 }} />
              <ZAxis type="number" dataKey="trades" range={[90, 520]} />
              <ReferenceLine y={0} stroke="#496273" />
              <Tooltip content={<PerformanceTooltip />} cursor={{ stroke: '#496273', strokeDasharray: '4 4' }} />
              <Scatter data={available} cursor="pointer" onClick={entry => onSelect((entry as unknown as PerformancePoint).id)}>
                {available.map(point => <Cell key={point.id} fill={performanceColor(point.monthlySqn)} fillOpacity={point.confidence === 'low' ? .62 : .86} stroke={point.confidence === 'low' ? '#fbbf24' : '#071018'} strokeWidth={point.confidence === 'low' ? 3 : 1.5} />)}
              </Scatter>
            </ScatterChart>
          </ResponsiveContainer>
        </div>
      </section>
    </>}
  </div>
}

type StrategyNoteResult = { strategy_id: number; note: string; note_updated_at: string }
type NoteSavedHandler = (strategyId: number, note: string, noteUpdatedAt: string) => void

function StrategyNoteEditor({ strategy, onSaved, placement }: { strategy: Strategy; onSaved: NoteSavedHandler; placement: 'drawer' | 'detail' }) {
  const [draft, setDraft] = useState(strategy.note || '')
  const [savedNote, setSavedNote] = useState(strategy.note || '')
  const [saving, setSaving] = useState(false)
  const [status, setStatus] = useState('')

  useEffect(() => {
    setDraft(strategy.note || '')
    setSavedNote(strategy.note || '')
    setStatus('')
  }, [strategy.id, strategy.note, strategy.note_updated_at])

  async function save() {
    setSaving(true)
    setStatus('')
    try {
      const result = await api<StrategyNoteResult>(`/api/strategies/${strategy.id}/note`, {
        method: 'PUT',
        body: JSON.stringify({ note: draft }),
      })
      setSavedNote(result.note)
      setStatus('Saved')
      onSaved(result.strategy_id, result.note, result.note_updated_at)
    } catch (error) {
      setStatus(error instanceof Error ? error.message : 'Could not save note')
    } finally {
      setSaving(false)
    }
  }

  const changed = draft !== savedNote
  const updatedLabel = strategy.note_updated_at
    ? `Last saved ${new Date(strategy.note_updated_at).toLocaleString('en-US')}`
    : 'Not saved yet'
  return <section className={`${placement === 'detail' ? 'panel ' : 'drawer-section '}strategy-note strategy-note-${placement}`}>
    <div className="strategy-note-heading"><div><span className="eyebrow">STRATEGY NOTES</span><small>{updatedLabel}</small></div><span className={status && status !== 'Saved' ? 'note-status error' : 'note-status'}>{status}</span></div>
    <textarea aria-label="Strategy notes" maxLength={10000} rows={placement === 'drawer' ? 5 : 6} placeholder="Add observations, follow-ups, or decisions for this strategy…" value={draft} onChange={event => { setDraft(event.target.value); setStatus('') }} />
    <div className="strategy-note-actions"><small>{draft.length.toLocaleString()} / 10,000</small><button className="button primary" type="button" disabled={!changed || saving} onClick={save}>{saving ? 'Saving…' : 'Save note'}</button></div>
  </section>
}

function StrategyDetail({ strategy, onDeleted, onNoteSaved }: { strategy: Strategy; onDeleted: (strategyId: number) => void; onNoteSaved: NoteSavedHandler }) {
  const m = strategy.metrics
  const lifetime = strategy.lifetime_metrics
  const [baselineId, setBaselineId] = useState('')
  useEffect(() => setBaselineId(''), [strategy.id])
  const b = strategy.baselines.find(item => `${item.source}:${item.sample_type}:${item.synced_at}` === baselineId) || strategy.baseline
  return <div className="detail-grid">
    <section className="panel strategy-hero">
      <div className="strategy-title"><div><span className="symbol-pill">{strategy.symbol}</span><h2>{strategy.mql5_name || strategy.sqx_name}</h2><p>{strategy.sqx_name}{strategy.sqx ? ` · ${strategy.sqx.project} / ${strategy.sqx.databank} · ${strategy.sqx.timeframe}` : ''}</p></div><div className={`health-badge ${strategy.health.status}`}><HealthDot status={strategy.health.status}/>{healthLabel(strategy.health.status)}</div></div>
      <MissingSqxNotice strategy={strategy} onDeleted={onDeleted} />
      <div className="hero-stats"><div><span>Realized P/L</span><strong>{money(m.net_profit)}</strong></div><div><span>Floating P/L</span><strong>{money(m.floating_profit)}</strong></div><div><span>Trades</span><strong>{m.trades}</strong></div><div><span>Positions</span><strong>{m.open_positions}</strong></div></div>
      <div className="reason-list">{strategy.health.reasons.map(reason => <span key={reason}>{reason}</span>)}{!strategy.health.reasons.length && <span>No active alerts</span>}</div>
    </section>
    <section className="panel comparison-panel">
      <div className="panel-heading"><div><span className="eyebrow">BEHAVIOR</span><h2>Current vs. {b?.sample_type?.toUpperCase() || 'baseline'}</h2></div><select className="baseline-select" value={b ? `${b.source}:${b.sample_type}:${b.synced_at}` : ''} onChange={event => setBaselineId(event.target.value)}>{strategy.baselines.map(item => <option key={`${item.source}:${item.sample_type}:${item.synced_at}`} value={`${item.source}:${item.sample_type}:${item.synced_at}`}>{item.source} · {item.sample_type.toUpperCase()} · {new Date(item.synced_at).toLocaleDateString()}</option>)}</select></div>
      <div className="comparison-head"><span>KPI</span><span>Current</span><span>Backtest</span><span>Δ</span></div>
      <Comparison label="Profit factor" current={m.profit_factor} baseline={baselineValue(b, 'ProfitFactor')} />
      <Comparison label="Expectancy" current={m.expectancy} baseline={baselineValue(b, 'Expectancy')} format={money} />
      <Comparison label="Return / DD" current={m.return_dd} baseline={baselineValue(b, 'ReturnDDRatio', 'RetDD')} />
      <Comparison label="SQN" current={m.sqn} baseline={baselineValue(b, 'SQN')} />
      <Comparison label="Trades / month" current={m.trades_per_month} baseline={baselineValue(b, 'AvgTradesPerMonth')} />
      <Comparison label="Max drawdown" current={m.max_drawdown} baseline={baselineValue(b, 'MaxDD', 'Drawdown')} format={money} />
    </section>
    <StrategyNoteEditor strategy={strategy} onSaved={onNoteSaved} placement="detail" />
    <section className="panel compact-metrics"><div><span>Win rate</span><strong>{(m.win_rate * 100).toFixed(1)}%</strong></div><div><span>Average duration</span><strong>{duration(m.avg_duration_seconds)}</strong></div><div><StreakPair metrics={lifetime} /></div><div><span>Average win</span><strong>{money(m.avg_win)}</strong></div><div><span>Average loss</span><strong>{money(m.avg_loss)}</strong></div><div><span>Commission + swap</span><strong>{money(m.commissions + m.swaps)}</strong></div></section>
    <SQXAnalyticsPanel strategy={strategy} />
    <RiskGuardPanel risk={strategy.risk_guard} />
  </div>
}

function DrawerMetric({ label, value, detail, tone = '', dotStatus }: { label: string; value: string; detail: string; tone?: string; dotStatus?: string }) {
  return <div className="drawer-card">
    <span>{label}</span>
    <strong className={tone}>{dotStatus && <HealthDot status={dotStatus}/>} {value}</strong>
    <small>{detail}</small>
  </div>
}

function EquitySparkline({ points }: { points: StrategyDetails['equity_curve'] }) {
  if (!points.length) return <div className="drawer-empty">No closed trades for this window.</div>
  const width = 420
  const height = 118
  const padding = 12
  const values = points.map(point => point.equity)
  const min = Math.min(0, ...values)
  const max = Math.max(0, ...values)
  const range = max - min || 1
  const xStep = points.length > 1 ? (width - padding * 2) / (points.length - 1) : 0
  const y = (value: number) => padding + (max - value) / range * (height - padding * 2)
  const coords = points.map((point, index) => `${padding + xStep * index},${y(point.equity)}`).join(' ')
  const zeroY = y(0)
  const last = points[points.length - 1]
  const lastX = padding + xStep * (points.length - 1)
  const lastY = y(last.equity)
  return <div className="equity-box">
    <svg className="equity-svg" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Equity curve">
      <line x1={padding} y1={zeroY} x2={width - padding} y2={zeroY} />
      <polyline points={coords} />
      <circle cx={lastX} cy={lastY} r="3.5" />
    </svg>
    <div className="equity-footer"><span>{points.length} closes</span><strong>{signedMoney(last.equity)}</strong></div>
  </div>
}

function StrategySidePanel({ strategy, query, onClose, onDeleted, onNoteSaved }: { strategy: Strategy; query: string; onClose: () => void; onDeleted: (strategyId: number) => void; onNoteSaved: NoteSavedHandler }) {
  const [detail, setDetail] = useState<StrategyDetails | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    let cancelled = false
    setDetail(null)
    setError('')
    api<StrategyDetails>(`/api/strategies/${strategy.id}?${query}`)
      .then(payload => { if (!cancelled) setDetail(payload) })
      .catch(err => { if (!cancelled) setError(err instanceof Error ? err.message : 'Could not load details') })
    return () => { cancelled = true }
  }, [strategy.id, query])

  const active = detail || strategy
  const m = active.metrics
  const historical = active.historical_metrics
  const lifetime = active.lifetime_metrics
  const history = detail ? [...detail.trades].reverse().slice(0, 80) : []
  return <aside className="strategy-drawer" aria-label="Quick strategy statistics">
    <div className="drawer-header">
      <div><strong>{active.mql5_name || active.sqx_name}</strong><span>{active.symbol || 'No symbol'} · {m.trades} trades in history</span></div>
      <button className="drawer-close" type="button" onClick={onClose}>Close</button>
    </div>
    <div className="drawer-body">
      {error && <div className="drawer-error">{error}</div>}
      {!detail && !error && <div className="drawer-loading">Loading details…</div>}
      <MissingSqxNotice strategy={active} onDeleted={onDeleted} />
      <StrategyNoteEditor strategy={active} placement="drawer" onSaved={(strategyId, note, noteUpdatedAt) => {
        setDetail(current => current ? { ...current, note, note_updated_at: noteUpdatedAt } : current)
        onNoteSaved(strategyId, note, noteUpdatedAt)
      }} />
      <div className="drawer-stat-grid">
        <DrawerMetric label="Lifetime P/L" value={signedMoney(lifetime.net_profit)} detail={`${lifetime.trades} total trades`} tone={lifetime.net_profit >= 0 ? 'positive' : 'negative'} />
        <DrawerMetric label="Current P/L" value={signedMoney(m.net_profit)} detail={`${m.trades} live-account trades | Exp ${money(m.expectancy)}`} tone={m.net_profit >= 0 ? 'positive' : 'negative'} />
        <DrawerMetric label="Historical P/L" value={signedMoney(historical.net_profit)} detail={`${historical.trades} imported/old-account trades`} tone={historical.net_profit >= 0 ? 'positive' : 'negative'} />
        <DrawerMetric label="Health" value={healthLabel(active.health.status)} detail={active.health.reasons.join(', ') || 'No active alerts'} tone={`status-${active.health.status}`} dotStatus={active.health.status} />
        <DrawerMetric label="Today" value={signedMoney(m.today_profit)} detail={`${m.today_trades} trades today`} tone={m.today_profit >= 0 ? 'positive' : 'negative'} />
        <DrawerMetric label="Win %" value={`${(m.win_rate * 100).toFixed(1)}%`} detail={`${m.winning_trades}/${m.trades} won`} tone={m.win_rate >= .5 ? 'positive' : 'negative'} />
        <DrawerMetric label="Factor P" value={number(m.profit_factor)} detail={`${money(m.gross_profit)} / ${money(m.gross_loss)}`} tone={m.profit_factor == null || m.profit_factor >= 1 ? 'positive' : 'negative'} />
        <DrawerMetric label="Max DD" value={money(m.max_drawdown)} detail="Peak-valley" tone={m.max_drawdown > 0 ? 'negative' : 'positive'} />
        <DrawerMetric label="Recovery factor" value={recoveryFactor(m)} detail="P/L / Max DD" tone={m.net_profit >= 0 ? 'positive' : 'negative'} />
        <DrawerMetric label="Avg W/L" value={`${money(m.avg_win)} / ${money(m.avg_loss)}`} detail="Average win / average loss" />
        <DrawerMetric label="Avg. dur." value={duration(m.avg_duration_seconds)} detail="Average time per trade" />
        <DrawerMetric label="Best trade" value={signedMoney(m.best_trade)} detail="Closed trade" tone={(m.best_trade || 0) >= 0 ? 'positive' : 'negative'} />
        <DrawerMetric label="Worst trade" value={signedMoney(m.worst_trade)} detail="Closed trade" tone={(m.worst_trade || 0) >= 0 ? 'positive' : 'negative'} />
        <div className="drawer-card streak-card"><StreakPair metrics={lifetime} /></div>
      </div>
      <section className={`drawer-section drawer-risk ${active.risk_guard.status}`}>
        <div className="drawer-section-title"><span className="eyebrow">RISK GUARD</span><span className={`risk-verdict ${active.risk_guard.stop_recommended ? 'stop' : active.risk_guard.status}`}>{active.risk_guard.stop_recommended ? 'Stop recommended' : active.risk_guard.status === 'gray' ? 'Not evaluated' : active.risk_guard.status === 'yellow' ? 'Attention' : 'Within limits'}</span></div>
        <div className="drawer-risk-live"><span>Current losing streak <strong>{active.risk_guard.live.current_consecutive_losses}</strong></span><span>Max DD <strong>{money(active.risk_guard.live.max_drawdown)}</strong></span></div>
        {(['is', 'oos'] as const).map(sample => <div className="drawer-risk-row" key={sample}>
          <strong>{sample.toUpperCase()}</strong>
          <span>DD: {riskCheckLabel(active.risk_guard.checks[sample].drawdown)}</span>
          <span>Streak: {riskCheckLabel(active.risk_guard.checks[sample].loss_streak)}</span>
        </div>)}
      </section>
      <section className="drawer-section">
        <div className="drawer-section-title"><span className="eyebrow">EQUITY CURVE</span></div>
        <EquitySparkline points={detail?.equity_curve || []} />
      </section>
      <section className="drawer-section drawer-history">
        <div className="drawer-section-title"><span className="eyebrow">HISTORY ({detail?.trades.length || 0})</span></div>
        {history.length ? <table className="drawer-table"><thead><tr><th>Close</th><th>Acct.</th><th>Sym.</th><th>Dir.</th><th>Lots</th><th>Net P/L</th><th>Dur.</th></tr></thead><tbody>{history.map(trade => <tr key={`${trade.terminal_id}-${trade.deal_ticket}`}><td>{new Date(trade.close_time_msc).toLocaleString('en-US', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })}</td><td><span className={`trade-source ${trade.source_role || 'live'}`}>{trade.source_account || 'Live'}</span></td><td>{trade.symbol}</td><td>{directionLabel(trade.direction)}</td><td>{trade.volume.toFixed(2)}</td><td className={trade.net_profit >= 0 ? 'value-good' : 'value-bad'}>{signedMoney(trade.net_profit)}</td><td>{tradeDuration(trade.open_time_msc, trade.close_time_msc)}</td></tr>)}</tbody></table> : <div className="drawer-empty">{detail ? 'No history for this window.' : 'Loading history…'}</div>}
      </section>
    </div>
  </aside>
}

function metricNumber(metrics: Record<string, unknown> | undefined, key: string) {
  const value = metrics?.[key]
  if (typeof value === 'number') return value
  if (value && typeof value === 'object' && 'amount' in value) {
    const amount = Number((value as { amount: unknown }).amount)
    return Number.isFinite(amount) ? amount : null
  }
  return null
}

function Backtests({ strategies, initialStrategyId, onCompleted }: { strategies: Strategy[]; initialStrategyId: number | null; onCompleted: () => void }) {
  const [strategyId, setStrategyId] = useState(initialStrategyId || strategies[0]?.id || 0)
  const [profile, setProfile] = useState<'reference' | 'sqx'>('reference')
  const [form, setForm] = useState<BacktestDefaults | null>(null)
  const [runs, setRuns] = useState<BacktestRun[]>([])
  const [notice, setNotice] = useState('')
  const [loading, setLoading] = useState(false)
  const [candidates, setCandidates] = useState<BacktestCandidates | null>(null)
  const [batches, setBatches] = useState<BacktestBatch[]>([])
  const [batchModel, setBatchModel] = useState(1)

  async function loadRuns() {
    if (!strategyId) {
      setRuns([])
      return
    }
    const result = await api<BacktestRun[]>(`/api/backtests${strategyId ? `?strategy_id=${strategyId}` : ''}`)
    setRuns(result)
    if (result.some(run => run.status === 'completed')) onCompleted()
  }
  async function loadAutomation() {
    const [candidateResult, batchResult] = await Promise.all([
      api<BacktestCandidates>('/api/backtests/candidates'),
      api<BacktestBatch[]>('/api/backtests/batches'),
    ])
    setCandidates(candidateResult)
    setBatches(batchResult)
  }
  useEffect(() => {
    if (!strategyId) {
      setForm(null)
      setRuns([])
      return
    }
    setLoading(true)
    setNotice('')
    api<BacktestDefaults>(`/api/strategies/${strategyId}/backtest-defaults?profile=${profile}`)
      .then(setForm)
      .catch(error => setNotice(error instanceof Error ? error.message : 'Could not load backtest settings'))
      .finally(() => setLoading(false))
    loadRuns().catch(() => undefined)
  }, [strategyId, profile])
  useEffect(() => {
    setStrategyId(current => current && strategies.some(strategy => strategy.id === current)
      ? current
      : strategies[0]?.id || 0)
  }, [strategies])
  useEffect(() => {
    loadAutomation().catch(error => setNotice(error instanceof Error ? error.message : 'Could not inspect missing backtests'))
  }, [])
  useEffect(() => {
    const active = runs.some(run => ['queued', 'preflight', 'running'].includes(run.status))
    if (!active) return
    const timer = setInterval(() => loadRuns().catch(() => undefined), 2000)
    return () => clearInterval(timer)
  }, [runs, strategyId])
  useEffect(() => {
    const active = batches.some(batch => ['resolving','waiting_terminal','queued','running'].includes(batch.status))
    if (!active) return
    const timer = setInterval(() => loadAutomation().catch(() => undefined), 5000)
    return () => clearInterval(timer)
  }, [batches])

  function update<K extends keyof BacktestDefaults>(key: K, value: BacktestDefaults[K]) {
    setForm(current => current ? { ...current, [key]: value } : current)
  }
  async function start() {
    if (!form || !strategyId) return
    setLoading(true)
    setNotice('Sending backtest to MT5...')
    try {
      await api('/api/backtests', {
        method: 'POST',
        body: JSON.stringify({
          strategy_id: strategyId, profile, symbol: form.symbol, timeframe: form.timeframe,
          from_date: form.from_date, to_date: form.to_date, deposit: form.deposit,
          currency: form.currency, leverage: form.leverage, model: form.model,
        }),
      })
      setNotice('Backtest queued.')
      await loadRuns()
    } catch (error) {
      setNotice(error instanceof Error ? error.message : 'Could not start backtest')
    } finally {
      setLoading(false)
    }
  }
  async function action(run: BacktestRun, command: 'cancel' | 'retry') {
    await api(`/api/backtests/${run.id}/${command}`, { method: 'POST' })
    await loadRuns()
  }
  async function startBatch() {
    setLoading(true)
    setNotice('Resolving EX5 and SQX configurations...')
    try {
      await api('/api/backtests/batches', {
        method: 'POST',
        body: JSON.stringify({ model: batchModel, policy: 'strict' }),
      })
      setNotice('Validation batch created.')
      await loadAutomation()
    } catch (error) {
      setNotice(error instanceof Error ? error.message : 'Could not create validation batch')
    } finally {
      setLoading(false)
    }
  }
  async function batchAction(batch: BacktestBatch, command: 'pause' | 'resume' | 'cancel') {
    await api(`/api/backtests/batches/${batch.id}/${command}`, { method: 'POST' })
    await loadAutomation()
  }

  const activeBatch = batches.find(batch => !['completed','cancelled'].includes(batch.status)) || batches[0]
  const batchFinished = activeBatch
    ? (activeBatch.counts.completed || 0) + (activeBatch.counts.validation_failed || 0) + (activeBatch.counts.blocked || 0) + (activeBatch.counts.cancelled || 0)
    : 0
  const batchTotal = activeBatch?.counts.total || 0
  return <div className="backtest-workspace">
    <section className="batch-automation">
      <div className="batch-heading">
        <div><span className="eyebrow">AUTOMATION</span><h2>Validate missing</h2><p>Strict EX5 matching with SQX configuration and resumable sequential runs.</p></div>
        <div className="batch-launch">
          <select aria-label="Batch test model" value={batchModel} onChange={event => setBatchModel(Number(event.target.value))}>
            <option value={1}>1 minute OHLC</option>
            <option value={4}>Real ticks</option>
          </select>
          <button className="button primary icon-command" disabled={loading || Boolean(activeBatch && !['completed','cancelled'].includes(activeBatch.status))} onClick={startBatch}><Play size={15}/>Validate missing</button>
        </div>
      </div>
      <div className="batch-counts">
        <div><span>Eligible</span><strong>{candidates?.counts.eligible ?? '?'}</strong></div>
        <div><span>Resolvable</span><strong>{candidates?.counts.resolvable ?? '?'}</strong></div>
        <div><span>Blocked</span><strong>{candidates?.counts.blocked ?? '?'}</strong></div>
        <div><span>Validated</span><strong>{candidates?.counts.validated ?? '?'}</strong></div>
      </div>
      {activeBatch && <div className="batch-progress">
        <div className="batch-progress-head">
          <div><span className={`run-status ${activeBatch.status}`}>{activeBatch.status.replace('_',' ')}</span><strong>Batch #{activeBatch.id}</strong><small>{activeBatch.error || `${batchFinished} of ${batchTotal} resolved`}</small></div>
          <div className="batch-actions">
            {activeBatch.status === 'paused'
              ? <button className="icon-button" title="Resume batch" onClick={() => batchAction(activeBatch,'resume')}><Play size={14}/></button>
              : !['completed','cancelled'].includes(activeBatch.status) && <button className="icon-button" title="Pause batch" onClick={() => batchAction(activeBatch,'pause')}><Pause size={14}/></button>}
            {!['completed','cancelled'].includes(activeBatch.status) && <button className="icon-button danger" title="Cancel batch" onClick={() => batchAction(activeBatch,'cancel')}><Square size={14}/></button>}
          </div>
        </div>
        <div className="batch-progress-track"><span style={{width: `${batchTotal ? Math.min(100,batchFinished / batchTotal * 100) : 0}%`}} /></div>
        {activeBatch.items.some(item => item.status === 'blocked') && <div className="batch-blocked">
          {activeBatch.items.filter(item => item.status === 'blocked').slice(0,5).map(item => <span key={item.id}><strong>{item.mql5_name || item.sqx_name}</strong>{item.error}</span>)}
        </div>}
      </div>}
    </section>
    <section className="backtest-controls">
      <div className="backtest-control-head">
        <div><span className="eyebrow">MT5 STRATEGY TESTER</span><h2>New backtest</h2></div>
        <div className="profile-switch" aria-label="Configuration source">
          <button className={profile === 'reference' ? 'active' : ''} onClick={() => setProfile('reference')}>Reference</button>
          <button className={profile === 'sqx' ? 'active' : ''} onClick={() => setProfile('sqx')}>SQX period</button>
        </div>
      </div>
      <div className="backtest-form">
        <label className="wide">Strategy<select value={strategyId} disabled={!strategies.length} onChange={event => setStrategyId(Number(event.target.value))}>{strategies.length ? strategies.map(strategy => <option key={strategy.id} value={strategy.id}>{strategy.symbol} - {strategy.mql5_name || strategy.sqx_name}</option>) : <option value={0}>No strategies for selected account</option>}</select></label>
        <label>Broker<input value={form?.broker || ''} readOnly /></label>
        <label>Symbol<input value={form?.symbol || ''} onChange={event => update('symbol', event.target.value)} /></label>
        <label>Timeframe<select value={form?.timeframe || 'H1'} onChange={event => update('timeframe', event.target.value)}>{['M15','M30','H1','H4','D1'].map(value => <option key={value}>{value}</option>)}</select></label>
        <label>From<input type="date" value={form?.from_date || ''} onChange={event => update('from_date', event.target.value)} /></label>
        <label>To<input type="date" value={form?.to_date || ''} onChange={event => update('to_date', event.target.value)} /></label>
        <label>Deposit<input type="number" min="1" value={form?.deposit || ''} onChange={event => update('deposit', Number(event.target.value))} /></label>
        <label>Model<select value={form?.model ?? 4} onChange={event => update('model', Number(event.target.value))}><option value={4}>Real ticks</option><option value={0}>Every tick</option><option value={1}>1 minute OHLC</option><option value={2}>Open prices</option></select></label>
      </div>
      <div className="backtest-actions">
        <span>{form ? `${form.sqx_symbol} -> ${form.symbol} | ${form.currency} | ${form.leverage}` : loading ? 'Loading configuration...' : 'Configuration unavailable'}</span>
        <button className="button primary icon-command" disabled={!strategyId || !form || loading} onClick={start}><Play size={15} />Run</button>
      </div>
      {notice && <div className="backtest-notice">{notice}</div>}
    </section>

    <section className="panel backtest-history">
      <div className="panel-heading"><div><span className="eyebrow">EXECUTIONS</span><h2>Run history</h2></div><button className="icon-button" title="Refresh runs" onClick={() => loadRuns()}><RefreshCw size={15}/></button></div>
      <div className="table-scroll"><table>
        <thead><tr><th>Status</th><th>Requested</th><th>Configuration</th><th>Net P/L</th><th>PF</th><th>Sharpe</th><th>Max DD</th><th>Trades</th><th></th></tr></thead>
        <tbody>{runs.map(run => {
          const active = ['queued','preflight','running'].includes(run.status)
          return <tr key={run.id}>
            <td><span className={`run-status ${run.status}`}>{run.status.replace('_', ' ')}</span>{run.error && <small className="run-error">{run.error}</small>}</td>
            <td>{new Date(run.requested_at).toLocaleString()}</td>
            <td><strong>{run.symbol} / {run.timeframe}</strong><small>{run.from_date} - {run.to_date} | {run.config_source}</small></td>
            <td className={(metricNumber(run.metrics, 'net_profit') || 0) >= 0 ? 'value-good' : 'value-bad'}>{money(metricNumber(run.metrics, 'net_profit'))}</td>
            <td>{number(metricNumber(run.metrics, 'profit_factor'))}</td>
            <td>{number(metricNumber(run.metrics, 'sharpe_ratio'))}</td>
            <td>{money(metricNumber(run.metrics, 'max_drawdown'))}</td>
            <td>{metricNumber(run.metrics, 'trades') ?? '?'}</td>
            <td>{active
              ? <button className="icon-button danger" title="Cancel run" onClick={() => action(run, 'cancel')}><Square size={14}/></button>
              : <button className="icon-button" title="Retry run" onClick={() => action(run, 'retry')}><RotateCcw size={14}/></button>}</td>
          </tr>
        })}{!runs.length && <tr><td colSpan={9} className="empty-state">No MT5 backtests for this strategy.</td></tr>}</tbody>
      </table></div>
    </section>
  </div>
}

function Settings({ reload }: { reload: () => void }) {
  const [dataDir, setDataDir] = useState('')
  const [terminalName, setTerminalName] = useState('New terminal')
  const [project, setProject] = useState('Retester')
  const [databank, setDatabank] = useState('Results')
  const [sqxStatus, setSqxStatus] = useState('Checking SQX connection...')
  const [sqxDatabanks, setSqxDatabanks] = useState<Record<string, Array<{name:string;records:number;view:string}>>>({})
  const [notice, setNotice] = useState('')
  const [mappingSuggestions, setMappingSuggestions] = useState<Array<{terminal_id:number;account_login:string;symbol:string;magic:number;comment:string;deal_count:number;candidates:Array<{strategy_id:number;name:string;score:number;evidence?:string[];deployment_required?:boolean}>}>>([])
  const [mappingChoices, setMappingChoices] = useState<Record<number, number>>({})
  const [deploymentSuggestions, setDeploymentSuggestions] = useState<Array<{deployment_id:number;name:string;account_login:string;symbol:string;safe:boolean;candidates:Array<{canonical_id:number;name:string;score:number;evidence:string[]}>}>>([])
  const [deploymentChoices, setDeploymentChoices] = useState<Record<number, number>>({})
  const [rules, setRules] = useState<Record<string, number>>({})
  useEffect(() => {
    api<Record<string,number>>('/api/alerts').then(setRules).catch(() => undefined)
    api<{available:boolean;details?:{app_version?:string}}>('/api/sqx/status')
      .then(result => setSqxStatus(result.available ? `Connected · SQX ${result.details?.app_version || ''}` : 'SQX unavailable'))
      .catch(() => setSqxStatus('SQX unavailable'))
    api<Record<string, Array<{name:string;records:number;view:string}>>>('/api/sqx/databanks')
      .then(setSqxDatabanks)
      .catch(() => undefined)
  }, [])
  async function addTerminal() {
    try { await api('/api/terminals', { method: 'POST', body: JSON.stringify({ name: terminalName, data_dir: dataDir }) }); setNotice('Terminal registered.'); reload() }
    catch (error) { setNotice(error instanceof Error ? error.message : 'Error') }
  }
  async function syncSqx() {
    setNotice('Querying SQX…')
    try {
      const result = await api<{ received:number; imported:number; matched:number; created:number; unmatched:number; passed:number; edge_available:number; egt_available:number; analytics_unavailable:number; renamed:number; promoted:number; rename_conflicts:Array<{strategy_name:string;reason:string}> }>('/api/sqx/sync', { method: 'POST', body: JSON.stringify({ project, databank }) })
      setNotice(`SQX: ${result.imported}/${result.received} imported · ${result.renamed + result.promoted} links reconciled · ${result.rename_conflicts.length} rename conflicts · Edge ${result.edge_available} · EGT ${result.egt_available} · ${result.analytics_unavailable} with unavailable analytics.`)
      reload()
      setSqxDatabanks(await api<Record<string, Array<{name:string;records:number;view:string}>>>('/api/sqx/databanks'))
    }
    catch (error) { setNotice(error instanceof Error ? error.message : 'SQX unavailable') }
  }
  async function loadMappings() {
    const result = await api<typeof mappingSuggestions>('/api/mappings/suggestions')
    setMappingSuggestions(result)
    setMappingChoices(Object.fromEntries(result.map((item, index) => [index, item.candidates[0]?.strategy_id || 0])))
    setNotice(`${result.length} trade groups need review.`)
  }
  async function loadDeploymentSuggestions() {
    const result = await api<typeof deploymentSuggestions>('/api/strategy-identities/deployment-suggestions')
    setDeploymentSuggestions(result)
    setDeploymentChoices(Object.fromEntries(result.map((item, index) => [index, item.candidates[0]?.canonical_id || 0])))
    setNotice(`${result.length} MT5 deployments need SQX identity review.`)
  }
  async function confirmSuggestion(index: number) {
    const item = mappingSuggestions[index]
    const strategyId = mappingChoices[index]
    if (!strategyId) { setNotice('Select a candidate strategy.'); return }
    const candidate = item.candidates.find(value => value.strategy_id === strategyId)
    await api('/api/mappings/confirm', { method: 'POST', body: JSON.stringify({ strategy_id: strategyId, terminal_id: item.terminal_id, account_login: item.account_login || '', symbol: item.symbol, magic: item.magic, comment_pattern: item.comment, confidence: candidate?.score || 1 }) })
    setNotice('Link confirmed.')
    await loadMappings(); reload()
  }
  async function autoConfirmMappings() {
    const result = await api<{confirmed:number;review_required:number}>('/api/mappings/auto-confirm', { method: 'POST' })
    setNotice(`${result.confirmed} safe links confirmed; ${result.review_required} groups need review.`)
    await loadMappings(); reload()
  }
  async function confirmDeployment(index: number) {
    const item = deploymentSuggestions[index]
    const canonicalId = deploymentChoices[index]
    if (!canonicalId) { setNotice('Select a canonical SQX strategy.'); return }
    await api('/api/strategy-identities/link-deployment', {
      method: 'POST',
      body: JSON.stringify({ deployment_id: item.deployment_id, canonical_id: canonicalId, dry_run: false }),
    })
    setNotice('MT5 deployment attached to its canonical SQX identity.')
    await loadDeploymentSuggestions(); reload()
  }
  async function autoLinkDeployments() {
    const result = await api<{linked:number;review_required:number;errors:Array<{deployment_id:number;reason:string}>}>('/api/strategy-identities/auto-link-deployments', { method: 'POST' })
    setNotice(`${result.linked} safe deployments linked; ${result.review_required} need review; ${result.errors.length} errors.`)
    await loadDeploymentSuggestions(); reload()
  }
  async function saveRules() {
    await api('/api/alerts', { method: 'PUT', body: JSON.stringify(rules) })
    setNotice('Global thresholds saved.'); reload()
  }
  return <div className="settings-grid">
    <section className="panel"><span className="eyebrow">MT5 SOURCES</span><h2>Register terminal</h2><p className="help">Each terminal needs its DataDir and the DashboardBridge service running.</p><label>Name<input value={terminalName} onChange={e => setTerminalName(e.target.value)} /></label><label>DataDir<input placeholder="C:\Users\…\MetaQuotes\Terminal\HASH" value={dataDir} onChange={e => setDataDir(e.target.value)} /></label><button className="button primary" onClick={addTerminal}>Save terminal</button></section>
    <section className="panel"><span className="eyebrow">HISTORY</span><h2>Sync SQX</h2><p className="help">{sqxStatus}. Previous snapshots remain available after SQX closes.</p><label>Project<select value={project} onChange={e => { setProject(e.target.value); setDatabank(sqxDatabanks[e.target.value]?.[0]?.name || '') }}>{Object.keys(sqxDatabanks).length ? Object.keys(sqxDatabanks).map(value => <option key={value} value={value}>{value}</option>) : <option value={project}>{project}</option>}</select></label><label>Databank<select value={databank} onChange={e => setDatabank(e.target.value)}>{sqxDatabanks[project]?.length ? sqxDatabanks[project].map(value => <option key={value.name} value={value.name}>{value.name} · {value.records} strategies</option>) : <option value={databank}>{databank}</option>}</select></label><button className="button primary" onClick={syncSqx}>Read-only sync</button></section>
    <section className="panel settings-wide"><span className="eyebrow">CATALOG AND MAPPINGS</span><h2>Maintenance</h2><div className="maintenance-actions"><button className="button" onClick={async () => { await api('/api/catalog/import', { method: 'POST' }); setNotice('Catalog reloaded.'); reload() }}>Reload Track_v1.xlsx</button><a className="button export-button" href="/api/catalog/export">Exportar Excel</a><button className="button primary" onClick={autoConfirmMappings}>Link safe matches</button><button className="button" onClick={loadMappings}>Find suggested links</button><button className="button primary" onClick={autoLinkDeployments}>Link safe SQX identities</button><button className="button" onClick={loadDeploymentSuggestions}>Review SQX identities</button></div>
      {mappingSuggestions.length > 0 && <div className="mapping-review"><div className="mapping-review-head"><span>Observed trade</span><span>Proposed strategy</span><span>Confidence</span><span /></div>{mappingSuggestions.map((item,index) => <div className="mapping-review-row" key={`${item.terminal_id}-${item.magic}-${item.symbol}-${item.comment}`}><div><strong>{item.symbol} · magic {item.magic}</strong><small>{item.comment || 'No comment'} · {item.deal_count} deals</small></div><select value={mappingChoices[index] || ''} onChange={event => setMappingChoices({...mappingChoices,[index]:Number(event.target.value)})}><option value="">Select…</option>{item.candidates.map(candidate => <option key={candidate.strategy_id} value={candidate.strategy_id}>{candidate.name}</option>)}</select><span>{item.candidates.find(candidate => candidate.strategy_id === mappingChoices[index])?.score ? `${Math.round((item.candidates.find(candidate => candidate.strategy_id === mappingChoices[index])?.score || 0) * 100)}%` : '—'}</span><button className="button" onClick={() => confirmSuggestion(index)}>Confirm</button></div>)}</div>}
      {deploymentSuggestions.length > 0 && <div className="mapping-review"><div className="mapping-review-head"><span>MT5 deployment</span><span>Canonical SQX identity</span><span>Evidence</span><span /></div>{deploymentSuggestions.map((item,index) => {
        const selectedCandidate = item.candidates.find(candidate => candidate.canonical_id === deploymentChoices[index])
        return <div className="mapping-review-row" key={`deployment-${item.deployment_id}`}><div><strong>{item.symbol} · {item.account_login || 'No account'}</strong><small>{item.name}</small></div><select value={deploymentChoices[index] || ''} onChange={event => setDeploymentChoices({...deploymentChoices,[index]:Number(event.target.value)})}><option value="">Select…</option>{item.candidates.map(candidate => <option key={candidate.canonical_id} value={candidate.canonical_id}>{candidate.name}</option>)}</select><span>{selectedCandidate ? `${Math.round(selectedCandidate.score * 100)}% · ${selectedCandidate.evidence.join(', ')}` : '—'}</span><button className="button" onClick={() => confirmDeployment(index)}>Attach</button></div>
      })}</div>}
    </section>
    <section className="panel settings-wide"><span className="eyebrow">HEALTH RULES</span><h2>Global thresholds</h2><div className="rules-grid">{Object.entries({min_trades:'Minimum trades',drawdown_yellow:'Yellow DD',drawdown_red:'Red DD',performance_yellow:'Yellow performance',performance_red:'Red performance',frequency_yellow_low:'Yellow frequency min.',frequency_yellow_high:'Yellow frequency max.',frequency_red_low:'Red frequency min.',frequency_red_high:'Red frequency max.'}).map(([key,label]) => <label key={key}>{label}<input type="number" step={key === 'min_trades' ? 1 : .05} value={rules[key] ?? ''} onChange={event => setRules({...rules,[key]:Number(event.target.value)})}/></label>)}</div><button className="button primary" onClick={saveRules}>Save thresholds</button></section>
    {notice && <div className="toast">{notice}</div>}
  </div>
}

export default function App() {
  const [data, setData] = useState<Dashboard | null>(null)
  const [tab, setTab] = useState<Tab>('overview')
  const [windowName, setWindowName] = useState('all')
  const [customStart, setCustomStart] = useState('')
  const [customEnd, setCustomEnd] = useState('')
  const [brokerAccountFilter, setBrokerAccountFilter] = useState('')
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [panelStrategyId, setPanelStrategyId] = useState<number | null>(null)
  const [query, setQuery] = useState('')
  const [magicQuery, setMagicQuery] = useState('')
  const [statusFilter, setStatusFilter] = useState('all')
  const [linkFilter, setLinkFilter] = useState('all')
  const [sort, setSort] = useState<SortState>(null)
  const [error, setError] = useState('')
  const [selectionSaving, setSelectionSaving] = useState<Set<number>>(() => new Set())
  const [sidebarCollapsed, setSidebarCollapsed] = useState(readSidebarCollapsed)
  const overviewTopScrollRef = useRef<HTMLDivElement>(null)
  const overviewTableScrollRef = useRef<HTMLDivElement>(null)
  const overviewTableRef = useRef<HTMLTableElement>(null)
  const [overviewScrollWidth, setOverviewScrollWidth] = useState(0)
  const [overviewHasOverflow, setOverviewHasOverflow] = useState(false)
  const dashboardQuery = useMemo(() => buildDashboardQuery(windowName, customStart, customEnd), [windowName, customStart, customEnd])
  async function load() {
    try { const payload = await api<Dashboard>(`/api/dashboard?${dashboardQuery}`); setData(payload); setSelectedId(current => current ?? payload.strategies[0]?.id ?? null); setError('') }
    catch (err) { setError(err instanceof Error ? err.message : 'Could not connect to the backend') }
  }
  async function handleDeleted() {
    setPanelStrategyId(null)
    await load()
    setSelectedId(null)
  }
  function handleNoteSaved(strategyId: number, note: string, noteUpdatedAt: string) {
    setData(current => current ? {
      ...current,
      strategies: current.strategies.map(strategy => strategy.id === strategyId
        ? { ...strategy, note, note_updated_at: noteUpdatedAt }
        : strategy),
    } : current)
  }
  async function updateStrategySelection(strategyId: number, selection: boolean) {
    setData(current => current ? {
      ...current,
      strategies: current.strategies.map(strategy => strategy.id === strategyId
        ? { ...strategy, selection }
        : strategy),
    } : current)
    setSelectionSaving(current => new Set(current).add(strategyId))
    try {
      await api(`/api/strategies/${strategyId}/selection`, {
        method: 'PUT',
        body: JSON.stringify({ selection }),
      })
      setError('')
    } catch (err) {
      setData(current => current ? {
        ...current,
        strategies: current.strategies.map(strategy => strategy.id === strategyId
          ? { ...strategy, selection: !selection }
          : strategy),
      } : current)
      setError(err instanceof Error ? err.message : 'Could not save strategy selection')
    } finally {
      setSelectionSaving(current => {
        const next = new Set(current)
        next.delete(strategyId)
        return next
      })
    }
  }
  useEffect(() => { if (windowName !== 'custom' || (customStart && customEnd)) load(); const timer = setInterval(load, 300_000); return () => clearInterval(timer) }, [windowName, customStart, customEnd])
  const allStrategies = data?.strategies || []
  const brokerAccountOptions = useMemo(() => {
    const accounts = new Set<string>()
    let hasUnassigned = false
    for (const strategy of allStrategies) {
      const strategyAccounts = strategyBrokerAccounts(strategy)
      if (strategyAccounts.size) {
        for (const account of strategyAccounts) accounts.add(account)
      } else {
        hasUnassigned = true
      }
    }
    const options = [...accounts].sort(strategyCollator.compare)
    return hasUnassigned ? [...options, UNASSIGNED_ACCOUNT] : options
  }, [allStrategies])
  const brokerFilteredStrategies = useMemo(() => allStrategies.filter(strategy => strategyMatchesBrokerAccount(strategy, brokerAccountFilter)), [allStrategies, brokerAccountFilter])
  useEffect(() => {
    if (!data) return
    setSelectedId(current => current && brokerFilteredStrategies.some(strategy => strategy.id === current)
      ? current
      : brokerFilteredStrategies[0]?.id ?? null)
    setPanelStrategyId(current => current && brokerFilteredStrategies.some(strategy => strategy.id === current)
      ? current
      : null)
  }, [data, brokerFilteredStrategies])
  const selected = brokerFilteredStrategies.find(strategy => strategy.id === selectedId) || null
  const panelStrategy = brokerFilteredStrategies.find(strategy => strategy.id === panelStrategyId) || null
  const visibleTotals = useMemo(() => {
    if (!data) return null
    if (!brokerAccountFilter) return data.totals
    return {
      strategies: brokerFilteredStrategies.length,
      active: brokerFilteredStrategies.filter(strategy => strategy.state === 'active').length,
      net_profit: brokerFilteredStrategies.reduce((total, strategy) => total + (brokerMetric(strategy, brokerAccountFilter)?.net_profit || 0), 0),
      floating_profit: brokerFilteredStrategies.reduce((total, strategy) => total + (brokerMetric(strategy, brokerAccountFilter)?.floating_profit || 0), 0),
      trades: brokerFilteredStrategies.reduce((total, strategy) => total + (brokerMetric(strategy, brokerAccountFilter)?.trades || 0), 0),
      red: brokerFilteredStrategies.filter(strategy => strategy.health.status === 'red').length,
    }
  }, [data, brokerAccountFilter, brokerFilteredStrategies])
  const filtered = useMemo(() => brokerFilteredStrategies.filter(strategy => {
    const matchesText = `${strategy.symbol} ${strategy.sqx_name} ${strategy.mql5_name}`.toLowerCase().includes(query.toLowerCase())
    const normalizedMagic = magicQuery.trim()
    const matchesMagic = !normalizedMagic || strategy.magic_numbers?.some(magic => String(magic).includes(normalizedMagic))
    return matchesText && matchesMagic
      && (statusFilter === 'all' || strategy.state === statusFilter)
      && (linkFilter === 'all' || strategy.link_state === linkFilter)
  }), [brokerFilteredStrategies, query, magicQuery, statusFilter, linkFilter])
  const sortedStrategies = useMemo(() => {
    if (!sort) return filtered
    return [...filtered].sort((a, b) => compareStrategies(a, b, sort.key, sort.direction))
  }, [filtered, sort])
  useEffect(() => {
    const topScroll = overviewTopScrollRef.current
    const tableScroll = overviewTableScrollRef.current
    const table = overviewTableRef.current
    if (!topScroll || !tableScroll || !table) return
    const measure = () => {
      const scrollWidth = Math.max(table.scrollWidth, tableScroll.scrollWidth)
      setOverviewScrollWidth(scrollWidth)
      setOverviewHasOverflow(scrollWidth > tableScroll.clientWidth + 1)
      topScroll.scrollLeft = tableScroll.scrollLeft
    }
    const observer = new ResizeObserver(measure)
    observer.observe(tableScroll)
    observer.observe(table)
    measure()
    return () => observer.disconnect()
  }, [sidebarCollapsed, sortedStrategies.length])
  function syncOverviewScroll(source: 'top' | 'table') {
    const topScroll = overviewTopScrollRef.current
    const tableScroll = overviewTableScrollRef.current
    if (!topScroll || !tableScroll) return
    const from = source === 'top' ? topScroll : tableScroll
    const to = source === 'top' ? tableScroll : topScroll
    if (Math.abs(to.scrollLeft - from.scrollLeft) > 1) to.scrollLeft = from.scrollLeft
  }
  function changeSort(key: SortKey) {
    setSort(current => current?.key === key
      ? { key, direction: current.direction === 'ascending' ? 'descending' : 'ascending' }
      : { key, direction: 'ascending' })
  }
  function toggleSidebar() {
    setSidebarCollapsed(current => {
      const next = !current
      try { localStorage.setItem(SIDEBAR_STORAGE_KEY, String(next)) } catch { undefined }
      return next
    })
  }
  const t = visibleTotals
  return <div className={sidebarCollapsed ? 'shell collapsed-sidebar' : 'shell'}>
    <button className="sidebar-toggle" type="button" aria-label={sidebarCollapsed ? 'Show sidebar' : 'Hide sidebar'} onClick={toggleSidebar}>{sidebarCollapsed ? '›' : '‹'}</button>
    <aside className="sidebar"><Logo/><nav>{([
      ['overview','Overview',LayoutDashboard],['performance','Performance',TrendingUp],['detail','Strategy',Activity],['chart','Chart',CandlestickChart],
      ['backtests','Backtests',FlaskConical],['settings','Settings',SettingsIcon],
    ] as const).map(([key,label,Icon]) => <button key={key} className={tab === key ? 'active' : ''} onClick={() => setTab(key)}><Icon size={15}/>{label}</button>)}</nav><div className="sidebar-footer"><div className="pulse"/><div><strong>Local system</strong><small>Refresh every 5 min</small></div></div></aside>
    <main className={tab === 'overview' ? 'overview-main' : undefined}><header><div><span className="eyebrow">STRATEGY PORTFOLIO</span><h1>{tab === 'overview' ? 'Operational status' : tab === 'performance' ? 'Normalized performance' : tab === 'detail' ? 'Strategy detail' : tab === 'chart' ? 'Market trades' : tab === 'backtests' ? 'MT5 backtests' : 'Settings'}</h1></div><div className="header-actions"><label className="global-account-filter"><span>Broker account</span><select value={brokerAccountFilter} onChange={e => setBrokerAccountFilter(e.target.value)}><option value="">All broker accounts</option>{brokerAccountOptions.map(account => <option key={account} value={account}>{account === UNASSIGNED_ACCOUNT ? 'Unassigned' : account}</option>)}</select></label><select value={windowName} onChange={e => setWindowName(e.target.value)}><option value="30d">30 days</option><option value="90d">90 days</option><option value="all">All</option><option value="custom">Custom</option></select>{windowName === 'custom' && <><input aria-label="Start date" type="date" value={customStart} onChange={e => setCustomStart(e.target.value)}/><input aria-label="End date" type="date" value={customEnd} onChange={e => setCustomEnd(e.target.value)}/></>}<button className="button refresh icon-command" onClick={load}><RefreshCw size={14}/>Refresh</button></div></header>
      {error && <div className="error-banner">{error}</div>}
      {tab === 'overview' && <>
        <section className="metrics-grid"><MetricCard label="Realized P/L" value={money(t?.net_profit)} detail={`${t?.trades || 0} closed trades`} tone={(t?.net_profit || 0) >= 0 ? 'positive' : 'negative'}/><MetricCard label="Floating P/L" value={money(t?.floating_profit)} detail="Open positions"/><MetricCard label="Active strategies" value={`${t?.active || 0} / ${t?.strategies || 0}`} detail="Full catalog"/><MetricCard label="Critical alerts" value={`${t?.red || 0}`} detail="Red deviations" tone={t?.red ? 'negative' : 'positive'}/></section>
        <section className="terminal-strip">{data?.terminals.map(terminal => <div key={terminal.id}><HealthDot status={terminal.status === 'connected' ? 'green' : 'gray'}/><strong>{terminal.name}</strong><span>{terminal.status === 'connected' ? `Account ${terminal.account_login}` : 'Disconnected'}</span>{terminal.last_seen && <small>{new Date(terminal.last_seen).toLocaleString('en-US')}</small>}</div>)}</section>
        <section className="panel table-panel">
          <div className="panel-heading">
            <div><span className="eyebrow">MONITORING</span><h2>All bots</h2></div>
            <div className="filters"><input aria-label="Search strategies" placeholder="Search strategy or symbol…" value={query} onChange={e => setQuery(e.target.value)}/><input className="magic-filter" aria-label="Search MN" placeholder="Search MN…" value={magicQuery} onChange={e => setMagicQuery(e.target.value.replace(/\D/g, ''))}/><select aria-label="Broker account" value={brokerAccountFilter} onChange={e => setBrokerAccountFilter(e.target.value)}><option value="">All broker accounts</option>{brokerAccountOptions.map(account => <option key={account} value={account}>{account === UNASSIGNED_ACCOUNT ? 'Unassigned' : account}</option>)}</select><select value={statusFilter} onChange={e => setStatusFilter(e.target.value)}><option value="all">All states</option>{Object.entries(stateLabels).map(([value,label]) => <option key={value} value={value}>{label}</option>)}</select><select value={linkFilter} onChange={e => setLinkFilter(e.target.value)}><option value="all">All sources</option>{Object.entries(linkLabels).map(([value,label]) => <option key={value} value={value}>{label}</option>)}</select></div>
          </div>
          <div ref={overviewTopScrollRef} className={`table-scroll-top${overviewHasOverflow ? '' : ' hidden'}`} aria-label="Horizontal table scroll" tabIndex={overviewHasOverflow ? 0 : -1} onScroll={() => syncOverviewScroll('top')}>
            <div className="table-scroll-top-spacer" style={{ width: overviewScrollWidth }}/>
          </div>
          <div ref={overviewTableScrollRef} className="table-scroll" aria-label="Scrollable strategy table" tabIndex={0} onScroll={() => syncOverviewScroll('table')}>
            <table ref={overviewTableRef}>
              <thead><tr><th className="selection-column">Selection</th><SortableHeader label="State" sortKey="state" sort={sort} onSort={changeSort}/><SortableHeader label="Source" sortKey="source" sort={sort} onSort={changeSort}/><SortableHeader label="Backtest" sortKey="backtest" sort={sort} onSort={changeSort}/><SortableHeader label="Edge" sortKey="edge" sort={sort} onSort={changeSort}/><SortableHeader label="EGT" sortKey="egt" sort={sort} onSort={changeSort}/><SortableHeader label="Strategy" sortKey="strategy" sort={sort} onSort={changeSort}/><SortableHeader label="Symbol" sortKey="symbol" sort={sort} onSort={changeSort}/><SortableHeader label="Account" sortKey="account" sort={sort} onSort={changeSort}/><SortableHeader label="Magic" sortKey="magic" sort={sort} onSort={changeSort}/><SortableHeader label="Net P/L" sortKey="net_profit" sort={sort} onSort={changeSort}/><SortableHeader label="Trades" sortKey="trades" sort={sort} onSort={changeSort}/><SortableHeader label="Win %" sortKey="win_rate" sort={sort} onSort={changeSort}/><SortableHeader label="Avg W/L" sortKey="avg_wl" sort={sort} onSort={changeSort}/><SortableHeader label="Best trade" sortKey="best_trade" sort={sort} onSort={changeSort}/><SortableHeader label="P/L today" sortKey="today_profit" sort={sort} onSort={changeSort}/><SortableHeader label="Factor P" sortKey="profit_factor" sort={sort} onSort={changeSort}/><SortableHeader label="Max DD" sortKey="max_drawdown" sort={sort} onSort={changeSort}/><SortableHeader label="History" sortKey="history" sort={sort} onSort={changeSort}/><th aria-label="Open panel"></th></tr></thead>
              <tbody>{sortedStrategies.map(strategy => {
                const p = performanceMetrics(strategy)
                const h = strategy.historical_metrics
                const alias = accountAlias(strategy.account_login)
                return <tr key={strategy.id} className={panelStrategyId === strategy.id ? 'selected-row' : ''} onClick={() => { setSelectedId(strategy.id); setPanelStrategyId(strategy.id) }}><td className="selection-column"><input type="checkbox" aria-label={`Select ${strategy.mql5_name || strategy.sqx_name} for monitoring`} checked={strategy.selection} disabled={selectionSaving.has(strategy.id)} onClick={event => event.stopPropagation()} onChange={event => updateStrategySelection(strategy.id, event.target.checked)}/></td><td><span className={`state-tag ${strategy.state}`}><HealthDot status={strategy.health.status}/>{stateLabels[strategy.state] || strategy.state}</span></td><td><span className={`link-tag ${strategy.link_state}`}>{linkLabels[strategy.link_state] || strategy.link_state}</span>{strategy.sqx?.missing_from_sqx_at && <small className="sqx-missing">MISSING FROM SQX</small>}</td><td className="backtest-cell"><BacktestBadge summary={strategy.backtest}/></td><td><SQXAnalyticsBadge strategy={strategy} kind="edge"/></td><td><SQXAnalyticsBadge strategy={strategy} kind="egt"/></td><td><strong>{strategy.mql5_name || strategy.sqx_name}</strong><small>{strategy.sqx_name}</small></td><td>{strategy.symbol || '—'}</td><td>{strategy.account_login ? <><strong>{strategy.account_login}</strong>{alias && <small>{alias}</small>}</> : '—'}</td><td className="magic-numbers">{strategy.magic_numbers?.length ? strategy.magic_numbers.join(', ') : '—'}</td><td className={p.net_profit >= 0 ? 'value-good' : 'value-bad'}>{money(p.net_profit)}</td><td>{p.trades}</td><td>{(p.win_rate * 100).toFixed(1)}%</td><td>{money(p.avg_win)} / {money(p.avg_loss)}</td><td className={(p.best_trade || 0) >= 0 ? 'value-good' : 'value-bad'}>{money(p.best_trade)}</td><td className={strategy.metrics.today_profit >= 0 ? 'value-good' : 'value-bad'}>{money(strategy.metrics.today_profit)}</td><td>{number(p.profit_factor)}</td><td>{money(p.max_drawdown)}</td><td>{h.trades ? <span className={h.net_profit >= 0 ? 'history-badge positive' : 'history-badge negative'}>{h.trades} / {signedMoney(h.net_profit)}</span> : '—'}</td><td className="row-action">›</td></tr>
              })}</tbody>
            </table>
          </div>
        </section>
      </>}
      {tab === 'overview' && panelStrategy && <StrategySidePanel strategy={panelStrategy} query={dashboardQuery} onClose={() => setPanelStrategyId(null)} onDeleted={handleDeleted} onNoteSaved={handleNoteSaved} />}
      {tab === 'performance' && <Performance
        strategies={brokerFilteredStrategies}
        onSelect={strategyId => { setSelectedId(strategyId); setTab('detail') }}
        selectedAccount={brokerAccountFilter}
        windowName={windowName}
        onWindowChange={setWindowName}
        customStart={customStart}
        onCustomStartChange={setCustomStart}
        customEnd={customEnd}
        onCustomEndChange={setCustomEnd}
      />}
      {tab === 'detail' && <><div className="strategy-selector"><label>Strategy</label><select value={selectedId || ''} disabled={!brokerFilteredStrategies.length} onChange={e => setSelectedId(Number(e.target.value))}>{brokerFilteredStrategies.map(strategy => <option key={strategy.id} value={strategy.id}>{strategy.symbol} — {strategy.mql5_name || strategy.sqx_name}</option>)}</select></div>{selected ? <StrategyDetail strategy={selected} onDeleted={handleDeleted} onNoteSaved={handleNoteSaved}/> : <div className="empty-state">No strategy selected for this broker account.</div>}</>}
      {tab === 'chart' && <><div className="strategy-selector"><label>Strategy</label><select value={selectedId || ''} disabled={!brokerFilteredStrategies.length} onChange={e => setSelectedId(Number(e.target.value))}>{brokerFilteredStrategies.map(strategy => <option key={strategy.id} value={strategy.id}>{strategy.symbol} — {strategy.mql5_name || strategy.sqx_name}</option>)}</select></div>{selected ? <ChartPanel strategyId={selected.id}/> : <div className="empty-state">Select a strategy for this broker account.</div>}</>}
      {tab === 'backtests' && <Backtests strategies={brokerFilteredStrategies} initialStrategyId={selectedId} onCompleted={load}/>}
      {tab === 'settings' && <Settings reload={load}/>}<footer>EA Observatory · Local data · {data ? `Updated ${new Date(data.generated_at).toLocaleTimeString('en-US')}` : 'Connecting…'}</footer>
    </main>
  </div>
}
