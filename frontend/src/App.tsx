import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Activity, Archive, BookOpen, CandlestickChart, Eye, EyeOff, HelpCircle, LayoutDashboard, Pause, Play, RefreshCw, RotateCcw, Settings as SettingsIcon, Square, TrendingUp, Trash2, TriangleAlert, X } from 'lucide-react'
import { Bar, BarChart, CartesianGrid, Cell, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api } from './api'
import ChartPanel from './ChartPanel'
import JournalAnalysis, { TopTenCharts, type PerformanceGrouping } from './JournalAnalysis'
import { getCachedPanelHistory, prefetchPanelHistory, requestPanelHistory } from './panelHistoryCache'
import type { AccountAliasSettings, AlertEpisode, BacktestBatch, BacktestCandidates, BacktestDefaults, BacktestRetentionPreview, BacktestRun, BacktestSummary, Baseline, DailyPnl, Dashboard, Metrics, PanelHistory, RiskCheck, RiskGuard, Strategy, StrategyArchiveImpact, StrategyDeletionImpact, StrategyDetails, TerminalEventsData } from './types'
import type { JournalAnalysisData, JournalTrade } from './journalTypes'
import { detailGuides, type DetailGuideId } from './detailGuides'
import { filterJournalByStrategyIds, filterStrategiesByIds, parseStrategyIds } from './strategyIdFilter'
import { matchesStrategyStatus, type StrategyStatus, type StrategyStatusFilter, withStrategyStatus } from './strategyStatusFilter'
import { PositiveStrategyStatusIcon, StrategyStatusIcon, strategyStatusLabels } from './StrategyStatusIcon'
import './performanceMode.css'

type Tab = 'overview' | 'performance' | 'journal' | 'events' | 'detail' | 'chart' | 'settings'
type EventDateRange = 'today' | '24h' | '3d' | '5d'
type SortKey = 'state' | 'source' | 'backtest' | 'edge' | 'egt' | 'doctor' | 'strategy' | 'symbol' | 'account' | 'magic' | 'net_profit' | 'expectancy' | 'trades' | 'win_rate' | 'profit_factor' | 'sqn' | 'return_dd' | 'max_drawdown' | 'avg_wl' | 'monthly_net_profit_0' | 'monthly_net_profit_1' | 'monthly_net_profit_2' | 'monthly_net_profit_3' | 'best_trade' | 'today_profit' | 'floating_profit' | 'account_pl_percent' | 'initial_capital_pl_percent' | 'health' | 'id'
type SortDirection = 'ascending' | 'descending'
type SortState = { key: SortKey; direction: SortDirection } | null
type BacktestFilter = 'all' | BacktestSummary['state'] | 'risk_exceeded' | 'risk_validated' | 'no_mm' | 'not_auditable' | 'no_risk_audit'
type OverviewViewSnapshot = {
  query: string
  strategyIdQuery: string
  magicQuery: string
  strategyStatusFilter: StrategyStatusFilter
  showStopped: boolean
  statusFilter: string
  linkFilter: string
  backtestStatusFilter: BacktestFilter
  sort: SortState
  pageScrollY: number
  tableScrollLeft: number
}
const SIDEBAR_STORAGE_KEY = 'dashboardv1:sidebar-collapsed'
const eventDateRangeLabels: Record<EventDateRange, string> = {
  today: 'Hoy', '24h': 'Últimas 24 h', '3d': 'Últimos 3 días', '5d': 'Últimos 5 días',
}

function eventDateBounds(range: EventDateRange, now = new Date()) {
  const start = new Date(now)
  if (range === 'today') start.setHours(0, 0, 0, 0)
  else if (range === '24h') start.setHours(start.getHours() - 24)
  else if (range === '3d') start.setDate(start.getDate() - 3)
  else start.setDate(start.getDate() - 5)
  return { start: start.toISOString(), end: now.toISOString() }
}

const stateLabels: Record<string, string> = {
  active: 'Active', no_recent_trades: 'No recent trades', unlinked: 'Unlinked',
  retired: 'Retired', terminal_disconnected: 'Terminal disconnected',
}
const linkLabels: Record<string, string> = {
  linked: 'Linked', candidate: 'Candidate', sqx_catalog: 'SQX + Catalog', sqx_only: 'SQX only',
  mt5_only: 'MT5 only', catalog_only: 'Catalog only',
}
const deploymentLabels: Record<string, string> = {
  attached_active: 'Adjunto activo', attached_inactive: 'Adjunto inactivo', observed: 'Despliegue observado',
}
const activityLabels: Record<string, string> = {
  with_recent_trades: 'Con trades recientes', without_recent_trades: 'Sin trades recientes',
  undetermined: 'Actividad no determinable', not_deployed: 'Sin inventario VPS',
}
const timeframeSourceLabels: Record<Strategy['timeframe_source'], string> = {
  terminal_profile: 'Perfil MT5', sqx: 'SQX', missing: 'Sin evidencia',
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
function buildDashboardQuery(windowName: string, customStart: string, customEnd: string, selectedAccounts: Set<string> | null, selectedAssets: Set<string> | null) {
  const params = new URLSearchParams({ window: windowName })
  if (windowName === 'custom' && customStart && customEnd) {
    params.set('start', customStart)
    params.set('end', customEnd)
  }
  if (selectedAccounts !== null) {
    params.set('filter_accounts', 'true')
    params.set('include_unassigned', String(selectedAccounts.has(UNASSIGNED_ACCOUNT)))
    for (const account of selectedAccounts) if (account !== UNASSIGNED_ACCOUNT) params.append('accounts', account)
  }
  if (selectedAssets !== null) {
    params.set('filter_assets', 'true')
    for (const asset of selectedAssets) params.append('assets', asset)
  }
  return params.toString()
}
function signedPercent(value: number | null | undefined) {
  if (value == null) return '—'
  return `${value > 0 ? '+' : ''}${value.toFixed(2)}%`
}
function performanceMetrics(strategy: Strategy) {
  return strategy.lifetime_metrics || strategy.metrics
}
const UNASSIGNED_ACCOUNT = '__unassigned__'
function accountAlias(aliases: Record<string, string>, account: string | null | undefined) {
  return aliases[String(account || '').trim()] || ''
}
type AccountFilterProps = {
  accounts: string[]
  aliases: Record<string, string>
  hasUnassigned: boolean
  selected: Set<string> | null
  onChange: (selected: Set<string>) => void
  compact?: boolean
}

function AccountFilter({ accounts, aliases, hasUnassigned, selected, onChange, compact = false }: AccountFilterProps) {
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement>(null)
  const values = [...accounts, ...(hasUnassigned ? [UNASSIGNED_ACCOUNT] : [])]
  const active = selected ?? new Set(values)
  const allSelected = values.length > 0 && values.every(account => active.has(account))
  const selectedCount = values.filter(account => active.has(account)).length
  const label = allSelected ? 'Todas las cuentas' : selectedCount ? `${selectedCount} cuenta${selectedCount === 1 ? '' : 's'}` : 'Sin cuentas'

  useEffect(() => {
    const close = (event: MouseEvent) => { if (root.current && !root.current.contains(event.target as Node)) setOpen(false) }
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', close)
    document.addEventListener('keydown', onKeyDown)
    return () => { document.removeEventListener('mousedown', close); document.removeEventListener('keydown', onKeyDown) }
  }, [])

  function setValue(value: string, checked: boolean) {
    const next = new Set(active)
    if (checked) next.add(value); else next.delete(value)
    onChange(next)
  }
  function toggleAll(checked: boolean) { onChange(checked ? new Set(values) : new Set()) }

  return <div ref={root} className={`account-filter${compact ? ' compact' : ''}`}>
    {!compact && <span className="account-filter-label">Cuentas activas</span>}
    <button type="button" className="account-filter-trigger" aria-haspopup="listbox" aria-expanded={open} onClick={() => setOpen(current => !current)}>{label}<span aria-hidden="true">⌄</span></button>
    {open && <div className="account-filter-menu" role="listbox" aria-label="Cuentas de broker" aria-multiselectable="true">
      <label className="account-filter-option all"><input type="checkbox" checked={allSelected} onChange={event => toggleAll(event.target.checked)} /><span className="account-filter-all-label">Todas las cuentas</span></label>
      <div className="account-filter-divider" />
      {accounts.map(account => {
        const alias = accountAlias(aliases, account)
        return <label key={account} className="account-filter-option"><input type="checkbox" checked={active.has(account)} onChange={event => setValue(account, event.target.checked)} /><strong className="account-filter-account">{account}</strong>{alias && <small className="account-filter-alias" title={alias}>{alias}</small>}</label>
      })}
      {hasUnassigned && <label className="account-filter-option"><input type="checkbox" checked={active.has(UNASSIGNED_ACCOUNT)} onChange={event => setValue(UNASSIGNED_ACCOUNT, event.target.checked)} /><span className="account-filter-unassigned">Sin asignar</span></label>}
    </div>}
  </div>
}

type AssetOption = { key: string; label: string; symbols: string[] }
type AssetFilterProps = { assets: AssetOption[]; selected: Set<string> | null; onChange: (selected: Set<string>) => void; compact?: boolean }

function AssetFilter({ assets, selected, onChange, compact = false }: AssetFilterProps) {
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement>(null)
  const values = assets.map(asset => asset.key)
  const active = selected ?? new Set(values)
  const allSelected = values.length > 0 && values.every(asset => active.has(asset))
  const selectedCount = values.filter(asset => active.has(asset)).length
  const label = allSelected ? 'Todos los activos' : selectedCount ? `${selectedCount} activo${selectedCount === 1 ? '' : 's'}` : 'Sin activos'

  useEffect(() => {
    const close = (event: MouseEvent) => { if (root.current && !root.current.contains(event.target as Node)) setOpen(false) }
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', close)
    document.addEventListener('keydown', onKeyDown)
    return () => { document.removeEventListener('mousedown', close); document.removeEventListener('keydown', onKeyDown) }
  }, [])

  function setValue(value: string, checked: boolean) {
    const next = new Set(active)
    if (checked) next.add(value); else next.delete(value)
    onChange(next)
  }

  return <div ref={root} className={`asset-filter account-filter${compact ? ' compact' : ''}`}>
    {!compact && <span className="account-filter-label">Activos</span>}
    <button type="button" className="account-filter-trigger" aria-haspopup="listbox" aria-expanded={open} onClick={() => setOpen(current => !current)}>{label}<span aria-hidden="true">⌄</span></button>
    {open && <div className="account-filter-menu" role="listbox" aria-label="Activos" aria-multiselectable="true">
      <label className="account-filter-option all"><input type="checkbox" checked={allSelected} onChange={event => onChange(event.target.checked ? new Set(values) : new Set())} /><span className="account-filter-all-label">Todos los activos</span></label>
      <div className="account-filter-divider" />
      {assets.map(asset => <label key={asset.key} className="account-filter-option asset-filter-option"><input type="checkbox" checked={active.has(asset.key)} onChange={event => setValue(asset.key, event.target.checked)} /><strong className="account-filter-account">{asset.label}</strong><small className="account-filter-alias" title={asset.symbols.join(', ')}>{asset.symbols.join(', ')}</small></label>)}
    </div>}
  </div>
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
const backtestAuditFilterLabels: Record<Exclude<BacktestFilter, 'all' | BacktestSummary['state']>, string> = {
  risk_exceeded: 'Risk alert',
  risk_validated: 'Risk validated',
  no_mm: 'No money management',
  not_auditable: 'Risk not auditable',
  no_risk_audit: 'No risk audit data',
}

function matchesBacktestFilter(summary: BacktestSummary, filter: BacktestFilter) {
  if (filter === 'all') return true
  if (filter in backtestLabels) return summary.state === filter
  if (filter === 'risk_exceeded') return Boolean(summary.risk_alert)
  if (filter === 'risk_validated') return summary.risk_audit?.status === 'validated' || Boolean(summary.risk_alert_accepted)
  if (filter === 'no_risk_audit') return summary.has_completed && !summary.risk_audit
  return summary.risk_audit?.status === filter
}

function BacktestBadge({ summary }: { summary: BacktestSummary }) {
  const details = summary.state === 'none'
    ? 'No MT5 backtest runs'
    : [
        `${summary.completed_count} completed backtest${summary.completed_count === 1 ? '' : 's'}`,
        summary.latest_completed_at ? `Last valid: ${new Date(summary.latest_completed_at).toLocaleString()}` : 'No valid result',
        `Latest run: ${summary.latest_status || 'unknown'}`,
        summary.history_source === 'Custom' ? `Custom: ${summary.custom_symbol || 'unknown symbol'}` : '',
      ].join(' · ')
  const risk = summary.risk_audit
  const riskDetails = risk?.status === 'risk_exceeded'
    ? `Riesgo excedido: peor pérdida ${money(risk.worst_loss)} / declarado ${money(risk.declared_risk)}`
    : ''
  const noMmDetails = summary.no_mm_reasons?.join(' · ') || ''
  const noMmWarning = summary.state === 'validated' && summary.no_mm_warning
  const noMmAccepted = noMmWarning && summary.no_mm_accepted
  const acceptedDetails = noMmAccepted ? 'NoMM aceptado para esta estrategia; la auditoría sigue disponible en el detalle.' : ''
  return <span className={`backtest-badge ${summary.state} ${summary.risk_alert ? 'risk-alert' : ''}`} title={[details, riskDetails, noMmDetails, acceptedDetails].filter(Boolean).join(' · ')}>{summary.risk_alert ? 'Risk alert' : noMmWarning && !noMmAccepted ? 'Validated but NoMM' : backtestLabels[summary.state]}</span>
}

function SortableHeader({ label, sortKey, sort, onSort, className = '' }: { label: string; sortKey: SortKey; sort: SortState; onSort: (key: SortKey) => void; className?: string }) {
  const active = sort?.key === sortKey
  const direction = active ? sort.direction : 'none'
  return <th className={`sortable-header ${className}`} aria-sort={direction}>
    <button type="button" className={active ? 'sort-button active' : 'sort-button'} onClick={() => onSort(sortKey)}>
      <span>{label}</span><span className="sort-indicator" aria-hidden="true">{active ? (sort.direction === 'ascending' ? '↑' : '↓') : '↕'}</span>
    </button>
  </th>
}

function OverviewTableColGroup() {
  return <colgroup>
    {[60, 62, 100, 115, 116, 340, 75, 112, 70, 115, 115, 100, 92, 92, 92, 92, 92, 94, 69, 90, 120, 120, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 30].map((width, index) => <col key={index} style={{ width }} />)}
  </colgroup>
}

function OverviewTableHead({ sort, onSort, months }: { sort: SortState; onSort: (key: SortKey) => void; months: Dashboard['monthly_net_profit_months'] }) {
  return <tr><SortableHeader label="ID" sortKey="id" sort={sort} onSort={onSort}/><th className="selection-column">Stopped</th><SortableHeader label="Backtest" sortKey="backtest" sort={sort} onSort={onSort}/><SortableHeader label="Health" sortKey="health" sort={sort} onSort={onSort}/><th className="equity-curve-header">Curve</th><SortableHeader label="Strategy" sortKey="strategy" sort={sort} onSort={onSort}/><SortableHeader className="overview-symbol-column" label="Symbol" sortKey="symbol" sort={sort} onSort={onSort}/><SortableHeader label="Account" sortKey="account" sort={sort} onSort={onSort}/><SortableHeader label="Magic" sortKey="magic" sort={sort} onSort={onSort}/><SortableHeader label="Account P/L %" sortKey="account_pl_percent" sort={sort} onSort={onSort}/><SortableHeader label="Init cap PL %" sortKey="initial_capital_pl_percent" sort={sort} onSort={onSort}/><SortableHeader label="P/L today" sortKey="today_profit" sort={sort} onSort={onSort}/><SortableHeader label="Win %" sortKey="win_rate" sort={sort} onSort={onSort}/><SortableHeader label="Trades" sortKey="trades" sort={sort} onSort={onSort}/><SortableHeader label="PF" sortKey="profit_factor" sort={sort} onSort={onSort}/><SortableHeader label="SQN" sortKey="sqn" sort={sort} onSort={onSort}/><SortableHeader label="Ret/DD" sortKey="return_dd" sort={sort} onSort={onSort}/><SortableHeader label="Edge" sortKey="edge" sort={sort} onSort={onSort}/><SortableHeader label="EGT" sortKey="egt" sort={sort} onSort={onSort}/><SortableHeader label="Doctor" sortKey="doctor" sort={sort} onSort={onSort}/><SortableHeader label="Net P/L" sortKey="net_profit" sort={sort} onSort={onSort}/><SortableHeader label="Avg W/L" sortKey="avg_wl" sort={sort} onSort={onSort}/>{months.map((month, index) => <SortableHeader key={month.key} label={month.label} sortKey={`monthly_net_profit_${index}` as SortKey} sort={sort} onSort={onSort}/>)}<SortableHeader label="Best trade" sortKey="best_trade" sort={sort} onSort={onSort}/><SortableHeader label="Max DD" sortKey="max_drawdown" sort={sort} onSort={onSort}/><SortableHeader label="Esperanza" sortKey="expectancy" sort={sort} onSort={onSort}/><SortableHeader label="Floating P/L" sortKey="floating_profit" sort={sort} onSort={onSort}/><SortableHeader label="Source" sortKey="source" sort={sort} onSort={onSort}/><SortableHeader className="overview-state-column" label="State" sortKey="state" sort={sort} onSort={onSort}/><th aria-label="Open panel"></th></tr>
}

const strategyCollator = new Intl.Collator('es-MX', { numeric: true, sensitivity: 'base' })

function displayName(strategy: Pick<Strategy, 'display_name' | 'mql5_name' | 'sqx_name'>) {
  return strategy.display_name || strategy.mql5_name || strategy.sqx_name
}

function primaryMagicLabel(strategy: Pick<Strategy, 'primary_magic_number' | 'magic_numbers' | 'historical_magic_numbers'>) {
  if (strategy.primary_magic_number == null) return '—'
  const count = new Set([...strategy.magic_numbers, ...strategy.historical_magic_numbers]).size
  return `${strategy.primary_magic_number}${count > 1 ? ` +${count - 1}` : ''}`
}

function backtestSortRank(summary: BacktestSummary) {
  if (summary.risk_alert) return 0
  if (summary.state === 'validated' && summary.no_mm_warning && !summary.no_mm_accepted) return 1
  if (summary.risk_alert_accepted) return 2
  if (summary.state === 'validated') return 3
  if (summary.state === 'running') return 4
  if (summary.state === 'failed') return 5
  return 6
}

function sortValue(strategy: Strategy, key: SortKey): string | number | null {
  switch (key) {
    case 'state': return stateLabels[strategy.state] || strategy.state
    case 'source': return linkLabels[strategy.link_state] || strategy.link_state
    case 'backtest': return backtestSortRank(strategy.backtest)
    case 'edge': return strategy.sqx_analytics?.edge.available ? strategy.sqx_analytics.edge.score ?? null : null
    case 'egt': return strategy.sqx_analytics?.egt.available ? strategy.sqx_analytics.egt.total ?? null : null
    case 'doctor': return strategy.sqx_analytics?.doctor.available ? strategy.sqx_analytics.doctor.score ?? null : null
    case 'strategy': return displayName(strategy)
    case 'symbol': return strategy.symbol || null
    case 'account': return strategy.account_login || null
    case 'magic': return strategy.primary_magic_number
    case 'net_profit': return performanceMetrics(strategy).net_profit
    case 'expectancy': return performanceMetrics(strategy).expectancy
    case 'trades': return performanceMetrics(strategy).trades
    case 'win_rate': return performanceMetrics(strategy).win_rate
    case 'profit_factor': return performanceMetrics(strategy).profit_factor
    case 'return_dd': return performanceMetrics(strategy).return_dd
    case 'max_drawdown': return performanceMetrics(strategy).max_drawdown
    case 'avg_wl': return performanceMetrics(strategy).avg_loss < 0 ? performanceMetrics(strategy).avg_win / Math.abs(performanceMetrics(strategy).avg_loss) : performanceMetrics(strategy).avg_win || null
    case 'monthly_net_profit_0': return strategy.monthly_net_profit[0]?.net_profit ?? 0
    case 'monthly_net_profit_1': return strategy.monthly_net_profit[1]?.net_profit ?? 0
    case 'monthly_net_profit_2': return strategy.monthly_net_profit[2]?.net_profit ?? 0
    case 'monthly_net_profit_3': return strategy.monthly_net_profit[3]?.net_profit ?? 0
    case 'best_trade': return performanceMetrics(strategy).best_trade
    case 'today_profit': return strategy.metrics.today_profit
    case 'floating_profit': return strategy.metrics.floating_profit
    case 'account_pl_percent': return strategy.account_pl_percent
    case 'initial_capital_pl_percent': return strategy.initial_capital_pl_percent
    case 'health': return { red: 0, yellow: 1, green: 2, gray: 3 }[strategy.health.status]
    case 'sqn': return performanceMetrics(strategy).sqn
    case 'id': return strategy.id
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

type ComparisonDirection = 'higher' | 'lower' | 'close'
function Comparison({ label, current, baseline, format = number, direction = 'higher' }: { label: string; current: number | null; baseline: number | null; format?: (value: number | null | undefined) => string; direction?: ComparisonDirection }) {
  const ratio = current != null && baseline ? current / baseline : null
  const tone = ratio == null ? 'muted'
    : direction === 'lower'
      ? ratio <= 1 ? 'good' : ratio <= 1.15 ? 'warn' : 'bad'
      : direction === 'close'
        ? ratio >= .85 && ratio <= 1.15 ? 'good' : ratio >= .7 && ratio <= 1.3 ? 'warn' : 'bad'
        : ratio >= .85 ? 'good' : ratio >= .7 ? 'warn' : 'bad'
  return <div className="comparison-row">
    <span>{label}</span><strong>{format(current)}</strong><span className="baseline-number">{format(baseline)}</span>
    <span className={`delta ${tone}`}>{ratio == null ? '—' : `${((ratio - 1) * 100).toFixed(0)}%`}</span>
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

function riskCheck(risk: RiskGuard, sample: 'is' | 'oos' | 'mt5', metric: 'drawdown' | 'loss_streak'): RiskCheck {
  return risk.checks?.[sample]?.[metric] ?? {
    status: 'gray', actual: 0, limit: null, ratio: null, source: null,
  }
}

function RiskCheckCell({ check, format }: { check: RiskCheck; format: (value: number | null | undefined) => string }) {
  return <div className={`risk-check ${check.status}`}>
    <strong>{format(check.limit)}</strong>
    <span>{riskCheckLabel(check)}{check.source ? ` · ${check.source}` : ''}</span>
  </div>
}

function GuideButton({ guide, onOpen }: { guide: DetailGuideId; onOpen: (guide: DetailGuideId) => void }) {
  return <button className="guide-button" type="button" onClick={() => onOpen(guide)} aria-label={`Cómo interpretar: ${detailGuides[guide].title}`}>
    <HelpCircle size={14} aria-hidden="true" />
  </button>
}

function DetailGuidePanel({ guide, onClose }: { guide: DetailGuideId; onClose: () => void }) {
  const content = detailGuides[guide]
  const closeRef = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    closeRef.current?.focus()
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [guide, onClose])
  return <aside className="detail-guide-panel" aria-label={`Guía: ${content.title}`}>
    <div className="detail-guide-heading"><div><span className="eyebrow">{content.eyebrow}</span><h2>{content.title}</h2></div><button ref={closeRef} className="guide-close" type="button" onClick={onClose} aria-label="Cerrar guía"><X size={18} /></button></div>
    <section><h3>Qué analiza</h3><p>{content.analyzes}</p></section>
    <section><h3>Fuente y ventana</h3><p>{content.source}</p></section>
    <section><h3>Atributos</h3><dl>{content.attributes.map(([name, description]) => <div key={name}><dt>{name}</dt><dd>{description}</dd></div>)}</dl></section>
    <section><h3>Cómo interpretarlo</h3><p>{content.interpretation}</p></section>
    <section className="guide-example"><h3>Ejemplo</h3><p>{content.example}</p></section>
    <section className="guide-action"><h3>Qué hacer</h3><p>{content.action}</p></section>
    {guide === 'risk-guard' && <section className="guide-action"><h3>Fallback MT5</h3><p>Los límites OOS de StrategyQuant tienen prioridad. Si falta uno, Risk Guard puede usar el backtest MT5 completo como fallback y lo muestra por separado: no equivale a una validación OOS.</p></section>}
  </aside>
}

function RiskGuardPanel({ risk, onGuide }: { risk: RiskGuard; onGuide: (guide: DetailGuideId) => void }) {
  return <section className={`panel risk-guard-panel ${risk.status}`}>
    <div className="panel-heading">
      <div><span className="eyebrow">HARD RISK LIMITS</span><h2>Risk Guard</h2></div>
      <div className="panel-heading-actions"><GuideButton guide="risk-guard" onOpen={onGuide} /><span className={`risk-verdict ${risk.stop_recommended ? 'stop' : risk.status}`}>
        {risk.stop_recommended ? 'Stop recommended' : risk.status === 'gray' ? 'Not evaluated' : risk.status === 'yellow' ? 'Attention' : 'Within limits'}
      </span></div>
    </div>
    <div className="risk-live-strip">
      <div><span>Current losing streak</span><strong>{risk.live.current_consecutive_losses}</strong></div>
      <div><span>Combined max loss streak</span><strong>{risk.live.max_consecutive_losses}</strong></div>
      <div><span>Combined max drawdown</span><strong>{money(risk.live.max_drawdown)}</strong></div>
      <div><span>History evaluated</span><strong>{risk.live.trades} trades</strong></div>
    </div>
    <div className="risk-grid risk-grid-head"><span>Metric</span><span>Combined</span><span>IS limit</span><span>OOS limit</span></div>
    <div className="risk-grid">
      <span>Max drawdown</span><strong>{money(risk.live.max_drawdown)}</strong>
      <RiskCheckCell check={riskCheck(risk, 'is', 'drawdown')} format={money} />
      <RiskCheckCell check={riskCheck(risk, 'oos', 'drawdown')} format={money} />
    </div>
    <div className="risk-grid">
      <span>Max losing streak</span><strong>{risk.live.max_consecutive_losses}</strong>
      <RiskCheckCell check={riskCheck(risk, 'is', 'loss_streak')} format={value => value == null ? '—' : String(value)} />
      <RiskCheckCell check={riskCheck(risk, 'oos', 'loss_streak')} format={value => value == null ? '—' : String(value)} />
    </div>
    <div className="risk-fallback">
      <div className="risk-fallback-heading"><span>MT5 BACKTEST FALLBACK</span><small>Used only when the matching SQX OOS limit is unavailable.</small></div>
      <div className="risk-fallback-grid"><span>Max drawdown</span><RiskCheckCell check={riskCheck(risk, 'mt5', 'drawdown')} format={money} /></div>
      <div className="risk-fallback-grid"><span>Max losing streak</span><RiskCheckCell check={riskCheck(risk, 'mt5', 'loss_streak')} format={value => value == null ? '-' : String(value)} /></div>
    </div>
    {!risk.baseline_available && <div className="risk-baseline-missing"><span>{risk.missing_message || 'Risk Guard needs a valid historical baseline.'}</span></div>}
  </section>
}

function PermanentDeleteNotice({ strategy, onDeleted }: { strategy: Strategy; onDeleted: (strategyId: number) => void }) {
  const [deleting, setDeleting] = useState(false)
  const [error, setError] = useState('')
  const isStopped = strategy.selection
  const isMissingFromSqx = Boolean(strategy.sqx?.missing_from_sqx_at)
  if (!isStopped && !isMissingFromSqx) return null
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
        `Eliminar permanentemente "${impact.name}" del dashboard?\n\n` +
        `Esta accion no se puede deshacer. Se eliminaran: ${affected || 'solo el registro de la estrategia'}.`
      )
      if (!confirmed) return
      await api(`/api/strategies/${strategy.id}`, { method: 'DELETE' })
      onDeleted(strategy.id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'No se pudo eliminar la estrategia')
    } finally {
      setDeleting(false)
    }
  }
  const title = isStopped ? 'Bot detenido' : 'Ausente de SQX'
  const description = isStopped
    ? 'El bot esta marcado como stopped. Elimina su historial y todos sus datos del dashboard.'
    : 'No se encontro durante la ultima sincronizacion correcta del databank.'
  return <div className="missing-sqx-notice">
    <div><strong>{title}</strong><span>{description}</span></div>
    <button className="button danger" type="button" disabled={deleting} onClick={remove}><Trash2 size={14}/>{deleting ? 'Checking…' : 'Delete permanently'}</button>
    {error && <small>{error}</small>}
  </div>
}

function SQXAnalyticsBadge({ strategy, kind }: { strategy: Strategy; kind: 'edge' | 'egt' | 'doctor' }) {
  if (kind === 'edge') {
    const edge = strategy.sqx_analytics?.edge
    if (!edge) return <span className="analytics-badge unavailable" title="No SQX analytics snapshot">—</span>
    if (!edge.available) return <span className="analytics-badge unavailable" title={edge.reason || 'Not available'}>N/A</span>
    return <span className="analytics-badge edge" title={`Edge Decay · ${edge.grade || 'No grade'}`}>{edge.score}<small>{edge.grade}</small></span>
  }
  if (kind === 'doctor') {
    const doctor = strategy.sqx_analytics?.doctor
    if (!doctor) return <span className="analytics-badge unavailable" title="No SQX Doctor snapshot">—</span>
    if (!doctor.available) return <span className="analytics-badge unavailable" title={doctor.reason || 'Not available'}>N/A</span>
    const tone = doctor.verdict === 'ACEPTABLE' ? 'doctor-good' : doctor.verdict === 'NO ACEPTABLE' ? 'doctor-bad' : 'doctor-warn'
    return <span className={`analytics-badge doctor ${tone}`} title={`Strategy Doctor · ${doctor.verdict} · calidad ${doctor.data_quality || 'N/D'}`}><span className="analytics-badge-value">{doctor.score}</span><small>{doctor.verdict}</small></span>
  }
  const egt = strategy.sqx_analytics?.egt
  if (!egt) return <span className="analytics-badge unavailable" title="No SQX analytics snapshot">—</span>
  if (!egt.available) return <span className="analytics-badge unavailable" title={egt.reason || 'Not available'}>N/A</span>
  return <span className="analytics-badge egt" title={`EGT · ${egt.grade || 'No grade'} · ${egt.history_source || egt.source || 'Unknown source'}`}><span className="analytics-badge-value">{number(egt.total)}</span><small>{egt.grade}</small></span>
}

function SQXAnalyticsPanel({ strategy, onGuide }: { strategy: Strategy; onGuide: (guide: DetailGuideId) => void }) {
  const analytics = strategy.sqx_analytics
  const edge = analytics?.edge
  const egt = analytics?.egt
  const doctor = analytics?.doctor
  return <section className="panel sqx-analytics-panel">
    <div className="panel-heading">
      <div><span className="eyebrow">SQX ANALYTICS</span><h2>Edge Decay, EGT & Doctor</h2></div>
      <GuideButton guide="sqx-analytics" onOpen={onGuide} />
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
      <div className={doctor?.available ? 'sqx-analysis-summary doctor-summary' : 'sqx-analysis-summary unavailable'}>
        <span>Strategy Doctor</span>
        <strong>{doctor?.available ? `${doctor.score}/100 · ${doctor.verdict}` : 'Not available'}</strong>
        <small>{doctor?.available ? `Calidad ${doctor.data_quality || 'N/D'} · ${doctor.metrics?.trades || 0} trades · ${(doctor.issues || []).map(issue => issue.title).join(' · ') || 'Sin hallazgos graves'}` : doctor?.reason || 'Run an SQX sync to calculate it.'}</small>
        {doctor?.available && doctor.recipes?.length ? <small className="doctor-recipes">Recetas: {doctor.recipes.map(recipe => recipe.title).join(' · ')}</small> : null}
      </div>
    </div>
  </section>
}

export type PerformancePoint = {
  key: string
  id: number
  name: string
  strategyName: string
  strategyStatus: StrategyStatus
  accountLabel: string
  magicNumber: number | null
  symbol: string
  tradeEdge: number
  monthlySqn: number
  tradesPerMonth: number
  trades: number
  months: number
  profitFactor: number | null
  confidence: 'low' | 'established'
}

function ArchiveStrategyNotice({ strategy, onArchived }: { strategy: Strategy; onArchived: (strategyId: number) => void }) {
  const [impact, setImpact] = useState<StrategyArchiveImpact | null>(null)
  const [archiving, setArchiving] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    let cancelled = false
    const controller = new AbortController()
    setImpact(null)
    setError('')
    api<StrategyArchiveImpact>(`/api/strategies/${strategy.id}/archive-impact`, { signal: controller.signal })
      .then(value => { if (!cancelled) setImpact(value) })
      .catch(err => { if (!cancelled) setError(err instanceof Error ? err.message : 'Could not check archive eligibility') })
    return () => { cancelled = true; controller.abort() }
  }, [strategy.id])
  if (!impact?.allowed) return error ? <small className="archive-error">{error}</small> : null
  const archiveImpact = impact
  async function archive() {
    setArchiving(true)
    setError('')
    try {
      const labels: Record<string, string> = { sin_mn: 'no live magic number', sin_trades: 'no lifetime trades' }
      const reason = archiveImpact.reasons.map(value => labels[value] || value).join(' and ')
      if (!window.confirm(`Archive "${archiveImpact.name}" from the dashboard?\n\nIt is eligible because it has ${reason}. All its data will be preserved and it will return automatically if MT5 detects a position or trade.`)) return
      await api(`/api/strategies/${strategy.id}/archive`, { method: 'POST' })
      onArchived(strategy.id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not archive strategy')
    } finally {
      setArchiving(false)
    }
  }
  const reasonLabel = archiveImpact.reasons.map(value => value === 'sin_mn' ? 'No live MN' : 'No lifetime trades').join(' · ')
  return <div className="archive-strategy-notice">
    <div><strong>Archive candidate</strong><span>{reasonLabel}. Data will be retained.</span></div>
    <button className="button" type="button" disabled={archiving} onClick={archive}><Archive size={14}/>{archiving ? 'Archiving…' : 'Archive'}</button>
    {error && <small>{error}</small>}
  </div>
}

function performanceColor(value: number) {
  if (value > 0) return '#2dd4bf'
  if (value < 0) return '#fb7185'
  return '#64748b'
}

function PerformanceNameTick({ x = 0, y = 0, payload, points }: { x?: number; y?: number; payload?: { value?: string; index?: number }; points: PerformancePoint[] }) {
  const point = payload?.index == null ? null : points[payload.index]
  const label = String(payload?.value || '')
  const statusLabel = point ? strategyStatusLabels[point.strategyStatus] : ''
  const iconOffset = Math.min(195, label.length * 5.5) + 17
  return <g transform={`translate(${x},${y})`}>
    <title>{statusLabel ? `${label} · ${statusLabel}` : label}</title>
    {point && <g transform={`translate(${-iconOffset},-7)`}><PositiveStrategyStatusIcon status={point.strategyStatus} size={13}/></g>}
    <text dy={4} textAnchor="end" fill="#a8bdca" fontSize={10}>{label}</text>
  </g>
}

function PerformanceTooltip({ active, payload }: { active?: boolean; payload?: Array<{ payload: PerformancePoint }> }) {
  const point = payload?.[0]?.payload
  if (!active || !point) return null
  return <div className="performance-tooltip">
    <strong>{point.name}</strong>
    <span>{point.symbol || 'No symbol'} · {point.accountLabel}</span>
    <dl>
      <dt>ID</dt><dd>{point.id}</dd>
      <dt>Bot</dt><dd>{point.strategyName}</dd>
      <dt>Categoría</dt><dd>{strategyStatusLabels[point.strategyStatus]}</dd>
      <dt>MN</dt><dd>{point.magicNumber ?? '—'}</dd>
      <dt>Account</dt><dd>{point.accountLabel || '—'}</dd>
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
  journalData: JournalAnalysisData | null
  accountLabel: string
}

export function buildPerformancePoints(strategies: Strategy[]): PerformancePoint[] {
  return strategies
    .filter(strategy => !strategy.selection)
    .map(strategy => {
      const metrics = performanceMetrics(strategy)
      if (!metrics) return null
      if (
        metrics.trades < 10
        || metrics.trade_edge == null
        || metrics.monthly_sqn == null
        || !Number.isFinite(metrics.trade_edge)
        || !Number.isFinite(metrics.monthly_sqn)
      ) return null
      return {
        key: `bot:${strategy.id}`,
        id: strategy.id,
        name: displayName(strategy),
        strategyName: displayName(strategy),
        strategyStatus: strategy.strategy_status,
        accountLabel: strategy.account_login,
        magicNumber: strategy.primary_magic_number,
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
}

function metricsForPerformanceTrades(trades: JournalTrade[]) {
  const ordered = [...trades].sort((left, right) => left.close_time_msc - right.close_time_msc)
  const profits = ordered.map(trade => trade.net_profit)
  const count = profits.length
  const mean = count ? profits.reduce((total, value) => total + value, 0) / count : 0
  const variance = count > 1
    ? profits.reduce((total, value) => total + (value - mean) ** 2, 0) / (count - 1)
    : 0
  const tradeEdge = variance > 0 ? mean / Math.sqrt(variance) : null
  const first = Math.min(...ordered.map(trade => trade.open_time_msc))
  const last = Math.max(...ordered.map(trade => trade.close_time_msc))
  const observedMonths = count ? Math.max((last - first) / (1000 * 86400 * 30.4375), 1 / 30.4375) : 0
  const months = count ? Math.max(observedMonths, 1) : 0
  const tradesPerMonth = months ? count / months : 0
  const monthlySqn = tradeEdge == null ? null : tradeEdge * Math.sqrt(tradesPerMonth)
  const grossProfit = profits.filter(value => value > 0).reduce((total, value) => total + value, 0)
  const grossLoss = Math.abs(profits.filter(value => value < 0).reduce((total, value) => total + value, 0))
  return { count, tradeEdge, months, tradesPerMonth, monthlySqn, profitFactor: grossLoss ? grossProfit / grossLoss : grossProfit ? 999 : null }
}

function magicNumberPerformanceGroups(trades: JournalTrade[]) {
  const groups = new Map<string, JournalTrade[]>()
  for (const trade of trades) {
    if (trade.strategy_id <= 0 || trade.is_stopped) continue
    const key = `${trade.strategy_id}:${trade.magic_number}`
    groups.set(key, [...(groups.get(key) || []), trade])
  }
  return groups
}

function botPerformanceGroups(trades: JournalTrade[]) {
  const groups = new Map<string, JournalTrade[]>()
  for (const trade of trades) {
    if (trade.strategy_id <= 0 || trade.is_stopped) continue
    const key = String(trade.strategy_id)
    groups.set(key, [...(groups.get(key) || []), trade])
  }
  return groups
}

function buildTradePerformancePoints(
  groups: Map<string, JournalTrade[]>,
  grouping: PerformanceGrouping,
  strategyStatuses: Record<number, StrategyStatus>,
): PerformancePoint[] {
  return [...groups.entries()]
    .map<PerformancePoint | null>(([key, items]) => {
      const metrics = metricsForPerformanceTrades(items)
      if (metrics.count < 10 || metrics.tradeEdge == null || metrics.monthlySqn == null || !Number.isFinite(metrics.monthlySqn)) return null
      const first = items[0]
      const accounts = [...new Set(items.map(item => item.account).filter(Boolean))]
      return {
        key: `${grouping}:${key}`,
        id: first.strategy_id,
        name: grouping === 'mn' ? `MN ${first.magic_number}` : first.strategy_name,
        strategyName: first.strategy_name,
        strategyStatus: strategyStatuses[first.strategy_id] || '',
        accountLabel: accounts.join(', '),
        magicNumber: grouping === 'mn'
          ? first.magic_number
          : items.find(item => item.primary_magic_number != null)?.primary_magic_number ?? null,
        symbol: first.symbol,
        tradeEdge: metrics.tradeEdge,
        monthlySqn: metrics.monthlySqn,
        tradesPerMonth: metrics.tradesPerMonth,
        trades: metrics.count,
        months: metrics.months,
        profitFactor: metrics.profitFactor,
        confidence: metrics.count < 30 ? 'low' as const : 'established' as const,
      }
    })
    .filter((point): point is PerformancePoint => point != null)
    .sort((left, right) => right.monthlySqn - left.monthlySqn)
}

export function buildBotPerformancePoints(trades: JournalTrade[], strategyStatuses: Record<number, StrategyStatus> = {}): PerformancePoint[] {
  return buildTradePerformancePoints(botPerformanceGroups(trades), 'bot', strategyStatuses)
}

export function buildMagicNumberPerformancePoints(trades: JournalTrade[], strategyStatuses: Record<number, StrategyStatus> = {}): PerformancePoint[] {
  return buildTradePerformancePoints(magicNumberPerformanceGroups(trades), 'mn', strategyStatuses)
}

function Performance({
  strategies,
  onSelect,
  journalData,
  accountLabel,
}: PerformanceProps) {
  const [grouping, setGrouping] = useState<PerformanceGrouping>('bot')
  const strategyStatuses = useMemo(() => Object.fromEntries(strategies.map(strategy => [strategy.id, strategy.strategy_status])) as Record<number, StrategyStatus>, [strategies])
  const performanceTrades = journalData?.trades || []
  const botGroups = botPerformanceGroups(performanceTrades)
  const magicGroups = magicNumberPerformanceGroups(performanceTrades)
  const available = grouping === 'bot'
    ? buildBotPerformancePoints(performanceTrades, strategyStatuses)
    : buildMagicNumberPerformancePoints(performanceTrades, strategyStatuses)
  const excluded = (grouping === 'bot' ? botGroups.size : magicGroups.size) - available.length
  const lowConfidence = available.filter(point => point.confidence === 'low').length
  const rankingHeight = Math.max(360, available.length * 42 + 72)

  return <div className="performance-view">
    <div className="performance-intro">
      <div><span className="eyebrow">NORMALIZED COMPARISON</span><h2>Scale-free performance</h2></div>
      <div className="performance-sample-summary">
        <span><strong>{available.length}</strong> ranked</span>
        <span className="low"><strong>{lowConfidence}</strong> low confidence</span>
        <span><strong>{excluded}</strong> excluded</span>
      </div>
      <p>Monthly SQN standardizes each entity's net trade outcomes and projects its observed trading frequency onto the same one-month horizon. Rankings require at least 10 trades; amber outlines identify samples below 30.</p>
    </div>
    <div className="performance-grouping" role="group" aria-label="Agrupar visualización de performance">
      <span>Agrupar por</span>
      <button type="button" className={grouping === 'bot' ? 'active' : ''} aria-pressed={grouping === 'bot'} onClick={() => setGrouping('bot')}>Bot</button>
      <button type="button" className={grouping === 'mn' ? 'active' : ''} aria-pressed={grouping === 'mn'} onClick={() => setGrouping('mn')}>MN</button>
    </div>
    {!available.length ? <div className="panel empty-state performance-empty">No {grouping === 'mn' ? 'Magic Numbers' : 'strategies'} have enough variable trade history for normalized performance ranking.</div> : <>
      <section className="panel performance-panel">
        <div className="panel-heading">
          <div><span className="eyebrow">RISK-ADJUSTED RANKING</span><h2>Monthly SQN · {grouping === 'mn' ? 'MN' : 'Bots'}</h2></div>
          <span className="performance-formula" title="Trade Edge = media del P/L neto ÷ desviación estándar; la frecuencia se anualiza a un horizonte mensual.">Trade Edge × √ trades/month · mín. 10 trades</span>
        </div>
        <div className="performance-chart-scroll">
          <div className="performance-ranking-chart" style={{ height: rankingHeight }}>
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={available} layout="vertical" margin={{ top: 12, right: 28, bottom: 28, left: 14 }}>
                <CartesianGrid stroke="#172633" horizontal={false} />
                <XAxis type="number" stroke="#5f7889" tick={{ fill: '#74899a', fontSize: 10 }} label={{ value: 'Monthly SQN', position: 'insideBottom', offset: -18, fill: '#74899a', fontSize: 10 }} />
                <YAxis type="category" dataKey="name" width={210} stroke="#5f7889" tick={<PerformanceNameTick points={available}/>} />
                <ReferenceLine x={0} stroke="#496273" />
                <Tooltip content={<PerformanceTooltip />} cursor={{ fill: '#122431' }} />
                <Bar dataKey="monthlySqn" radius={[0, 3, 3, 0]} cursor="pointer" onClick={entry => onSelect((entry as unknown as PerformancePoint).id)}>
                  {available.map(point => <Cell key={point.key} fill={performanceColor(point.monthlySqn)} fillOpacity={point.confidence === 'low' ? .62 : .9} stroke={point.confidence === 'low' ? '#fbbf24' : 'transparent'} strokeWidth={point.confidence === 'low' ? 2 : 0} strokeDasharray={point.confidence === 'low' ? '4 3' : undefined} />)}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
      </section>
    </>}
    <TopTenCharts data={journalData} accountLabel={accountLabel} grouping={grouping} strategyStatuses={strategyStatuses}/>
  </div>
}

type StrategyNoteResult = { strategy_id: number; note: string; note_updated_at: string }
type NoteSavedHandler = (strategyId: number, note: string, noteUpdatedAt: string) => void
type StrategyDisplayNameResult = { strategy_id: number; display_name: string; display_name_source: string; display_name_updated_at: string }
type DisplayNameSavedHandler = (strategyId: number, displayName: string, source: string) => void

function StrategyDisplayNameEditor({ strategy, onSaved }: { strategy: Strategy; onSaved: DisplayNameSavedHandler }) {
  const isAssigned = strategy.display_name_source != null
  const [draft, setDraft] = useState(isAssigned ? strategy.display_name : '')
  const [savedName, setSavedName] = useState(isAssigned ? strategy.display_name : '')
  const [saving, setSaving] = useState(false)
  const [status, setStatus] = useState('')

  useEffect(() => {
    const current = strategy.display_name_source != null ? strategy.display_name : ''
    setDraft(current)
    setSavedName(current)
    setStatus('')
  }, [strategy.id, strategy.display_name, strategy.display_name_source])

  async function save() {
    setSaving(true)
    setStatus('')
    try {
      const result = await api<StrategyDisplayNameResult>(`/api/strategies/${strategy.id}/display-name`, {
        method: 'PUT',
        body: JSON.stringify({ display_name: draft }),
      })
      setSavedName(result.display_name)
      setStatus('Saved')
      onSaved(result.strategy_id, result.display_name, result.display_name_source)
    } catch (error) {
      setStatus(error instanceof Error ? error.message : 'Could not save name')
    } finally {
      setSaving(false)
    }
  }

  const technicalName = [strategy.mql5_name, strategy.sqx_name].filter((value, index, values) => value && values.indexOf(value) === index).join(' · ')
  return <details className="drawer-section drawer-collapsible strategy-display-name">
    <summary className="drawer-collapsible-summary"><div className="strategy-note-heading"><div><span className="eyebrow">NOMBRE PERSONALIZADO</span><small>Se usa como nombre principal en el panel.</small></div><span className={status && status !== 'Saved' ? 'note-status error' : 'note-status'}>{status}</span></div><span className="drawer-collapsible-chevron" aria-hidden="true">⌄</span></summary>
    <div className="drawer-collapsible-content">
      <input aria-label="Nombre personalizado de estrategia" maxLength={160} placeholder="Ej. XAU reversión conservadora" value={draft} onChange={event => { setDraft(event.target.value); setStatus('') }} />
      {technicalName && <small className="strategy-display-name-reference">Referencia técnica: {technicalName}</small>}
      <div className="strategy-note-actions"><small>{draft.length} / 160</small><button className="button primary" type="button" disabled={!draft.trim() || draft === savedName || saving} onClick={save}>{saving ? 'Saving…' : 'Save name'}</button></div>
    </div>
  </details>
}

function BacktestRiskAudit({ strategy, onGuide, onNoMmAcceptance, savingNoMmAcceptance }: { strategy: Strategy; onGuide: (guide: DetailGuideId) => void; onNoMmAcceptance: (strategyId: number, accepted: boolean) => void; savingNoMmAcceptance: boolean }) {
  const audit = strategy.backtest_risk || strategy.backtest.risk_audit
  const backtest = strategy.backtest
  const alert = audit?.status === 'risk_exceeded'
  const manualRiskAcceptance = alert && backtest.risk_alert_accepted
  const noMmReasons = audit?.no_mm_reasons || backtest.no_mm_reasons || []
  const noMmWarning = noMmReasons.length > 0
  const noMmAccepted = noMmWarning && strategy.no_mm_accepted
  const label = !audit ? 'No audit available' : manualRiskAcceptance ? 'Validated manually' : alert ? 'Risk alert' : noMmWarning && !noMmAccepted ? 'Validated but NoMM' : audit.status === 'validated' || noMmAccepted ? 'Validated' : 'Not auditable'
  const excess = audit?.risk_ratio != null ? Math.max(0, (audit.risk_ratio - 1) * 100) : null
  return <section className={`panel backtest-risk-audit ${alert ? 'alert' : audit?.status === 'validated' ? 'validated' : 'neutral'}`}>
    <div className="panel-heading"><div><span className="eyebrow">MT5 BACKTEST CONTROL</span><h2>Backtest Risk Audit</h2></div><div className="panel-heading-actions"><GuideButton guide="backtest-risk" onOpen={onGuide} /><span className={`risk-verdict ${manualRiskAcceptance ? 'manual' : alert ? 'stop' : audit?.status === 'validated' ? 'green' : 'gray'}`}>{label}</span></div></div>
    {audit ? <>
      <div className="backtest-audit-grid"><div><span>Declared risk</span><strong>{money(audit.declared_risk)}</strong></div><div><span>Worst loss</span><strong className={alert ? 'value-bad' : ''}>{money(audit.worst_loss)}</strong></div><div><span>Risk ratio</span><strong>{audit.risk_ratio == null ? '—' : `${audit.risk_ratio.toFixed(2)}×`}</strong></div><div><span>Excess</span><strong className={alert ? 'value-bad' : ''}>{excess == null ? '—' : `${excess.toFixed(1)}%`}</strong></div></div>
      <p className={alert ? 'backtest-audit-message alert' : 'backtest-audit-message'}>{audit.warning || (audit.status === 'validated' ? 'Worst trade stayed within the declared-risk tolerance.' : 'The report inputs do not allow this risk check.')}</p>
      {manualRiskAcceptance && <p className="backtest-manual-acceptance">Risk alert validado manualmente{backtest.risk_alert_accepted_at ? ` el ${new Date(backtest.risk_alert_accepted_at).toLocaleString()}` : ''}. La evidencia original permanece visible.</p>}
      {noMmWarning && <div className="backtest-no-mm-explanation"><p>Este backtest terminó correctamente, pero no se puede validar un riesgo monetario fijo: usa lote fijo o no declara los inputs de gestión monetaria necesarios.</p><p>Evidencia: {audit.fixed_lot != null ? `lote fijo ${audit.fixed_lot.toFixed(2)}; ` : ''}{audit.executed_entry_count != null ? `${audit.executed_entry_count} entradas analizadas; ` : ''}peor pérdida {money(audit.worst_loss)} y riesgo declarado no disponible. {noMmReasons.join(' · ')}.</p><label className="no-mm-acceptance"><input type="checkbox" checked={noMmAccepted} disabled={savingNoMmAcceptance} onChange={event => onNoMmAcceptance(strategy.id, event.target.checked)} />Acepto este resultado sin gestión monetaria verificable</label>{noMmAccepted && <small>Marcado{strategy.no_mm_accepted_at ? ` el ${new Date(strategy.no_mm_accepted_at).toLocaleString()}` : ''}. La evidencia NoMM sigue visible y el filtro de auditoría la conserva.</small>}</div>}
    </> : <p className="backtest-audit-message">No risk audit is available for the latest valid backtest.</p>}
    <div className="backtest-audit-context"><span>Run {backtest.latest_run_id ?? '—'}</span><span>{backtest.history_source || 'MT5 history'}{backtest.custom_symbol ? ` · ${backtest.custom_symbol}` : ''}</span><span>{backtest.latest_completed_at ? new Date(backtest.latest_completed_at).toLocaleString() : 'No valid result'}</span></div>
  </section>
}

function StrategyIdentifiers({ strategy, detail }: { strategy: Strategy; detail: Pick<PanelHistory, 'mappings' | 'trades'> | null }) {
  const identifiers = new Map<string, Set<string>>()
  function add(label: string, value: string | number | null | undefined) {
    const normalized = String(value ?? '').trim()
    if (!normalized) return
    const values = identifiers.get(label) || new Set<string>()
    values.add(normalized)
    identifiers.set(label, values)
  }
  if (strategy.display_name_source) add(strategy.display_name_source === 'manual_user' ? 'Nombre personalizado' : 'Nombre asignado', strategy.display_name)
  if (strategy.operational_identity) add('Identidad operativa', `${strategy.operational_identity.server} / ${strategy.operational_identity.account_login} / ${strategy.operational_identity.asset_key} / ${strategy.operational_identity.expert_name}`)
  add('Nombre SQX', strategy.sqx_name)
  add('Nombre MT5 / comentario', strategy.mql5_name)
  add('Cuenta', strategy.account_login)
  strategy.lineage_accounts.current.forEach(value => add('Cuenta actual', value))
  strategy.lineage_accounts.predecessor.forEach(value => add('Cuenta histórica', value))
  add('Símbolo', strategy.symbol)
  strategy.magic_numbers.forEach(value => add('Magic Number', value))
  strategy.historical_magic_numbers.forEach(value => add('Magic Number histórico', value))
  strategy.deployments.forEach(deployment => {
    add('Archivo .ex5', deployment.ex5_name)
    add('Cuenta', deployment.account_login)
    add('Símbolo', deployment.symbol)
    add('Magic Number', deployment.magic)
  })
  detail?.mappings.forEach(mapping => {
    add('Comentario mapeado', mapping.comment_pattern)
    add('Cuenta', mapping.account_login)
    add('Símbolo', mapping.symbol)
    add(mapping.role === 'historical' ? 'Magic Number histórico' : 'Magic Number', mapping.magic)
  })
  detail?.trades.forEach(trade => {
    add('Comentario observado', trade.comment)
    add('Comentario de salida', trade.exit_comment)
    add('Cuenta', trade.source_account)
    add('Símbolo', trade.symbol)
    add(trade.source_role === 'historical' ? 'Magic Number histórico' : 'Magic Number', trade.magic)
  })
  return <details className="drawer-section drawer-collapsible strategy-identifiers">
    <summary className="drawer-collapsible-summary"><span className="eyebrow">IDENTIFICADORES ENCONTRADOS</span><span className="drawer-collapsible-chevron" aria-hidden="true">⌄</span></summary>
    {strategy.identity_conflicts.length > 0 && <p className="note-status error">Conflicto de identidad: hay {strategy.identity_conflicts.length} identidades operativas registradas. El historial normal usa sólo la identidad principal.</p>}
    <div className="strategy-identifier-list">{[...identifiers].map(([label, values]) => <div key={label}><strong>{label}</strong><span>{[...values].join(' · ')}</span></div>)}</div>
  </details>
}

function StrategyNoteEditor({ strategy, onSaved, placement, onGuide }: { strategy: Strategy; onSaved: NoteSavedHandler; placement: 'drawer' | 'detail'; onGuide?: (guide: DetailGuideId) => void }) {
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
    <div className="strategy-note-heading"><div><span className="eyebrow">STRATEGY NOTES</span><small>{updatedLabel}</small></div><div className="panel-heading-actions">{onGuide && <GuideButton guide="notes" onOpen={onGuide} />}<span className={status && status !== 'Saved' ? 'note-status error' : 'note-status'}>{status}</span></div></div>
    <textarea aria-label="Strategy notes" maxLength={10000} rows={placement === 'drawer' ? 5 : 6} placeholder="Add observations, follow-ups, or decisions for this strategy…" value={draft} onChange={event => { setDraft(event.target.value); setStatus('') }} />
    <div className="strategy-note-actions"><small>{draft.length.toLocaleString()} / 10,000</small><button className="button primary" type="button" disabled={!changed || saving} onClick={save}>{saving ? 'Saving…' : 'Save note'}</button></div>
  </section>
}

type MagicEntry = { magic: number; roles: string[]; mappings: StrategyDetails['mappings']; detail?: Strategy['magic_number_details'][number] }

function magicEvidence(detail?: Strategy['magic_number_details'][number]) {
  if (!detail) return null
  const dates = detail.first_trade_at
    ? `${new Date(detail.first_trade_at).toLocaleDateString()}${detail.last_trade_at && detail.last_trade_at !== detail.first_trade_at ? ` – ${new Date(detail.last_trade_at).toLocaleDateString()}` : ''}`
    : 'Sin operaciones registradas'
  return `${detail.trades} trades · ${dates}`
}

function MagicNumberEntries({ entries }: { entries: MagicEntry[] }) {
  return <div className="magic-number-list">{entries.map(entry => <article className="magic-number-entry" key={entry.magic}>
    <div className="magic-number-entry-header">
      <strong>MN {entry.magic}</strong>
      <span>{entry.roles.join(' + ')}</span>
    </div>
    {(entry.detail || entry.mappings.length > 0) && <div className="magic-number-entry-details">
      {entry.detail && <small className="magic-number-entry-evidence">{magicEvidence(entry.detail)}</small>}
      {entry.mappings.length ? <small className="magic-number-entry-mappings">{entry.mappings.map(mapping => [mapping.symbol, mapping.account_login, mapping.comment_pattern || 'No comment'].filter(Boolean).join(' · ')).join(' / ')}</small> : null}
    </div>}
  </article>)}</div>
}

function DrawerMagicNumbers({ entries }: { entries: MagicEntry[] }) {
  return <details className="drawer-section drawer-collapsible magic-numbers-panel">
    <summary className="drawer-collapsible-summary"><div className="magic-numbers-heading"><div><span className="eyebrow">MAGIC NUMBERS</span><h2>{entries.length ? `${entries.length} registered` : 'No registered MN'}</h2></div></div><span className="drawer-collapsible-chevron" aria-hidden="true">⌄</span></summary>
    <div className="drawer-collapsible-content">{entries.length ? <MagicNumberEntries entries={entries} /> : <div className="drawer-empty">No live or historical mappings have a Magic Number.</div>}</div>
  </details>
}

function MagicNumbersPanel({ strategy, mappings, placement = 'detail', onGuide }: {
  strategy: Strategy; mappings?: StrategyDetails['mappings']; placement?: 'detail' | 'drawer'; onGuide?: (guide: DetailGuideId) => void
}) {
  const fallback = [
    ...strategy.magic_numbers.map(magic => ({ magic, roles: ['Live'], mappings: [] as StrategyDetails['mappings'] })),
    ...strategy.historical_magic_numbers.map(magic => ({ magic, roles: ['Historical'], mappings: [] as StrategyDetails['mappings'] })),
  ]
  const grouped = new Map<number, MagicEntry>()
  for (const mapping of mappings || fallback.flatMap(item => item.mappings.length ? item.mappings : [{ magic: item.magic, role: item.roles[0].toLowerCase() } as StrategyDetails['mappings'][number]])) {
    if (mapping.magic == null) continue
    const current = grouped.get(mapping.magic) || { magic: mapping.magic, roles: [], mappings: [] }
    const role = mapping.role === 'historical' ? 'Historical' : 'Live'
    if (!current.roles.includes(role)) current.roles.push(role)
    if (mappings) current.mappings.push(mapping)
    grouped.set(mapping.magic, current)
  }
  for (const detail of strategy.magic_number_details || []) {
    const current = grouped.get(detail.magic) || { magic: detail.magic, roles: [], mappings: [] }
    current.detail = detail
    for (const role of detail.roles.map(value => value === 'historical' ? 'Historical' : 'Live')) if (!current.roles.includes(role)) current.roles.push(role)
    grouped.set(detail.magic, current)
  }
  const entries = [...grouped.values()].sort((left, right) => left.magic - right.magic)
  if (placement === 'drawer') return <DrawerMagicNumbers entries={entries} />
  return <section className="panel magic-numbers-panel">
    <div className="magic-numbers-heading"><div><span className="eyebrow">MAGIC NUMBERS</span><h2>{entries.length ? `${entries.length} registered` : 'No registered MN'}</h2></div>{onGuide && <GuideButton guide="magic-numbers" onOpen={onGuide} />}</div>
    {entries.length ? <MagicNumberEntries entries={entries} /> : <div className="drawer-empty">No live or historical mappings have a Magic Number.</div>}
  </section>
}

function StrategyDetail({ strategy, onDeleted, onNoteSaved, onNoMmAcceptance, savingNoMmAcceptance }: { strategy: Strategy; onDeleted: (strategyId: number) => void; onNoteSaved: NoteSavedHandler; onNoMmAcceptance: (strategyId: number, accepted: boolean) => void; savingNoMmAcceptance: boolean }) {
  const m = strategy.metrics
  const lifetime = strategy.lifetime_metrics
  const [baselineId, setBaselineId] = useState('')
  const [activeGuide, setActiveGuide] = useState<DetailGuideId | null>(null)
  useEffect(() => setBaselineId(''), [strategy.id])
  const b = strategy.baselines.find(item => `${item.source}:${item.sample_type}:${item.synced_at}` === baselineId) || strategy.baseline
  return <div className={`detail-workbench ${activeGuide ? 'guide-open' : ''}`}>
    <div className="detail-grid">
    <section className="panel strategy-hero">
      <div className="strategy-title"><div><span className="symbol-pill">{strategy.symbol}</span><h2>{displayName(strategy)}</h2><p>{strategy.sqx_name}{strategy.sqx ? ` · ${strategy.sqx.project} / ${strategy.sqx.databank} · ${strategy.sqx.timeframe}` : ''}</p></div><div className="panel-heading-actions"><GuideButton guide="summary" onOpen={setActiveGuide} /><div className={`health-badge ${strategy.health.status}`}><HealthDot status={strategy.health.status}/>{healthLabel(strategy.health.status)}</div></div></div>
      <PermanentDeleteNotice strategy={strategy} onDeleted={onDeleted} />
      <ArchiveStrategyNotice strategy={strategy} onArchived={onDeleted} />
      <div className="hero-stats"><div><span>Realized P/L</span><strong>{money(m.net_profit)}</strong></div><div><span>Floating P/L</span><strong>{money(m.floating_profit)}</strong></div><div><span>Trades</span><strong>{m.trades}</strong></div><div><span>Positions</span><strong>{m.open_positions}</strong></div></div>
      <div className="reason-list">{strategy.health.reasons.map(reason => <span key={reason}>{reason}</span>)}{!strategy.health.reasons.length && <span>No active alerts</span>}</div>
    </section>
    <section className="panel comparison-panel">
      <div className="panel-heading"><div><span className="eyebrow">BEHAVIOR</span><h2>Observed history vs. {b?.sample_type?.toUpperCase() || 'baseline'}</h2></div><div className="panel-heading-actions"><GuideButton guide="comparison" onOpen={setActiveGuide} /><select className="baseline-select" value={b ? `${b.source}:${b.sample_type}:${b.synced_at}` : ''} onChange={event => setBaselineId(event.target.value)}>{strategy.baselines.map(item => <option key={`${item.source}:${item.sample_type}:${item.synced_at}`} value={`${item.source}:${item.sample_type}:${item.synced_at}`}>{item.source} · {item.sample_type.toUpperCase()} · {new Date(item.synced_at).toLocaleDateString()}</option>)}</select></div></div>
      <div className="comparison-head"><span>KPI</span><span>Current</span><span>Backtest</span><span>Δ</span></div>
      <Comparison label="Profit factor" current={m.profit_factor} baseline={baselineValue(b, 'ProfitFactor')} />
      <Comparison label="Recovery factor" current={m.return_dd} baseline={baselineValue(b, 'RecoveryFactor', 'ReturnDDRatio', 'RetDD')} />
      <Comparison label="Expectancy" current={m.expectancy} baseline={baselineValue(b, 'Expectancy')} format={money} />
      <Comparison label="Return / DD" current={m.return_dd} baseline={baselineValue(b, 'ReturnDDRatio', 'RetDD')} />
      <Comparison label="SQN" current={m.sqn} baseline={baselineValue(b, 'SQN')} />
      <Comparison label="Trades / month" current={m.trades_per_month} baseline={baselineValue(b, 'AvgTradesPerMonth')} direction="close" />
      <Comparison label="Max drawdown" current={m.max_drawdown} baseline={baselineValue(b, 'MaxDD', 'Drawdown', 'max_drawdown')} format={money} direction="lower" />
    </section>
    <section className="panel compact-metrics"><div className="compact-metrics-heading"><span className="eyebrow">OPERATING METRICS</span><GuideButton guide="metrics" onOpen={setActiveGuide} /></div><div><span>Win rate</span><strong>{(m.win_rate * 100).toFixed(1)}%</strong></div><div><span>Average duration</span><strong>{duration(m.avg_duration_seconds)}</strong></div><div><StreakPair metrics={lifetime} /></div><div><span>Average win</span><strong>{money(m.avg_win)}</strong></div><div><span>Average loss</span><strong>{money(m.avg_loss)}</strong></div><div><span>Commission + swap</span><strong>{money(m.commissions + m.swaps)}</strong></div></section>
    <RiskGuardPanel risk={strategy.risk_guard} onGuide={setActiveGuide} />
    <BacktestRiskAudit strategy={strategy} onGuide={setActiveGuide} onNoMmAcceptance={onNoMmAcceptance} savingNoMmAcceptance={savingNoMmAcceptance} />
    <SQXAnalyticsPanel strategy={strategy} onGuide={setActiveGuide} />
    <StrategyNoteEditor strategy={strategy} onSaved={onNoteSaved} placement="detail" onGuide={setActiveGuide} />
    <DeploymentStatusPanel strategy={strategy} onGuide={setActiveGuide} />
    <MagicNumbersPanel strategy={strategy} onGuide={setActiveGuide} />
    </div>
    {activeGuide && <DetailGuidePanel guide={activeGuide} onClose={() => setActiveGuide(null)} />}
  </div>
}

function DeploymentStatusPanel({ strategy, onGuide }: { strategy: Strategy; onGuide: (guide: DetailGuideId) => void }) {
  const deployment = strategy.deployments[0]
  if (!deployment) return <section className="panel deployment-detail"><span className="eyebrow">VPS DEPLOYMENT</span><h2>Sin inventario VPS</h2><p className="help">No hay evidencia de que este EA esté adjunto a una terminal VPS real.</p></section>
  return <section className="panel deployment-detail">
    <div className="panel-heading"><div><span className="eyebrow">VPS DEPLOYMENT</span><h2>{deploymentLabels[strategy.deployment_state || 'observed']}</h2></div><GuideButton guide="deployment" onOpen={onGuide} /></div>
    <div className="deployment-detail-badges"><span className={`deployment-badge ${strategy.deployment_state || 'observed'}`}>{deploymentLabels[strategy.deployment_state || 'observed']}</span><span className={`activity-badge ${strategy.activity_state}`}>{activityLabels[strategy.activity_state]}</span></div>
    <dl><div><dt>Cuenta</dt><dd>{deployment.account_login}</dd></div><div><dt>Terminal</dt><dd>{deployment.server}</dd></div><div><dt>Chart</dt><dd>{deployment.symbol} · {deployment.timeframe || '—'}</dd></div><div><dt>Magic</dt><dd>{deployment.magic ?? 'Desconocido'}</dd></div><div><dt>Última operación</dt><dd>{deployment.last_trade_at ? new Date(deployment.last_trade_at).toLocaleString() : 'Sin operaciones registradas'}</dd></div><div><dt>Último inventario</dt><dd>{new Date(deployment.observed_at).toLocaleString()}</dd></div></dl>
  </section>
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

function RecentEquitySparkline({ points }: { points: Strategy['recent_equity_curve'] }) {
  if (!points.length) return <span className="equity-sparkline-empty">—</span>
  const values = [0, ...points.map(point => point.equity)]
  const min = Math.min(...values)
  const max = Math.max(...values)
  const range = max - min || 1
  const width = 92
  const height = 30
  const path = values.map((value, index) => {
    const x = index / (values.length - 1 || 1) * width
    const y = 3 + (1 - (value - min) / range) * (height - 6)
    return `${index ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`
  }).join(' ')
  const recent = points.slice(-8).map(point => point.net_profit)
  const wins = recent.filter(value => value > 0).length
  const losses = recent.filter(value => value < 0).length
  const net = recent.reduce((total, value) => total + value, 0)
  const tone = wins >= 5 && net > 0 ? 'green' : losses >= 5 && net < 0 ? 'red' : 'yellow'
  const tendency = tone === 'green' ? 'positiva' : tone === 'red' ? 'negativa' : 'mixta'
  const label = `${points.length} últimos trades; tendencia ${tendency}. Últimos 8: ${wins} ganadores y ${losses} perdedores.`
  return <div className={`equity-sparkline ${tone}`} title={label} aria-label={label}>
    <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-hidden="true"><path d={path} /></svg>
  </div>
}

function MonthlyPnlCalendar({ strategyId }: { strategyId: number }) {
  const initialMonth = () => new Date().toISOString().slice(0, 7)
  const [month, setMonth] = useState(initialMonth)
  const [data, setData] = useState<DailyPnl | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    let cancelled = false
    const controller = new AbortController()
    setError('')
    api<DailyPnl>(`/api/strategies/${strategyId}/daily-pnl?month=${month}`, { signal: controller.signal })
      .then(value => { if (!cancelled) setData(value) })
      .catch(err => { if (!cancelled) setError(err instanceof Error ? err.message : 'Could not load daily P/L') })
    return () => { cancelled = true; controller.abort() }
  }, [strategyId, month])
  const [year, monthNumber] = month.split('-').map(Number)
  const firstWeekday = new Date(year, monthNumber - 1, 1).getDay()
  const daysInMonth = new Date(year, monthNumber, 0).getDate()
  const label = new Date(year, monthNumber - 1, 1).toLocaleDateString(undefined, { month: 'long', year: 'numeric' })
  function shift(amount: number) {
    const value = new Date(year, monthNumber - 1 + amount, 1)
    setMonth(`${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}`)
  }
  return <section className="drawer-section monthly-pnl-calendar">
    <div className="drawer-section-title"><span className="eyebrow">P/L DIARIO NETO</span><strong>{signedMoney(data?.net_profit)}</strong></div>
    <div className="calendar-navigation"><button type="button" onClick={() => shift(-1)} aria-label="Previous month">‹</button><strong>{label}</strong><button type="button" onClick={() => shift(1)} aria-label="Next month">›</button></div>
    {error ? <div className="drawer-error">{error}</div> : <div className="calendar-grid"><span>Su</span><span>Mo</span><span>Tu</span><span>We</span><span>Th</span><span>Fr</span><span>Sa</span>{Array.from({ length: firstWeekday }, (_, index) => <i key={`empty-${index}`} />)}{Array.from({ length: daysInMonth }, (_, index) => {
      const day = index + 1
      const key = `${month}-${String(day).padStart(2, '0')}`
      const pnl = data?.days[key]
      return <div key={key} className={`calendar-day ${pnl == null || pnl === 0 ? '' : pnl > 0 ? 'positive' : 'negative'}`}><small>{day}</small>{pnl != null && pnl !== 0 && <strong>{signedMoney(pnl)}</strong>}</div>
    })}</div>}
  </section>
}

function StrategySidePanel({ strategy, query, refreshVersion, onClose, onDeleted, onNoteSaved, onDisplayNameSaved, onStatusChange, statusSaving, onOpenDetail, onBacktestRiskAcceptance, savingBacktestRiskAcceptance }: { strategy: Strategy; query: string; refreshVersion: number; onClose: () => void; onDeleted: (strategyId: number) => void; onNoteSaved: NoteSavedHandler; onDisplayNameSaved: DisplayNameSavedHandler; onStatusChange: (strategyId: number, status: StrategyStatus) => void; statusSaving: boolean; onOpenDetail: (strategyId: number) => void; onBacktestRiskAcceptance: (strategyId: number, runId: number, accepted: boolean) => void; savingBacktestRiskAcceptance: boolean }) {
  const [panelHistory, setPanelHistory] = useState<PanelHistory | null>(() => getCachedPanelHistory(strategy.id, query))
  const [alertEpisodes, setAlertEpisodes] = useState<AlertEpisode[]>([])
  const [historyError, setHistoryError] = useState('')
  const [historyRefreshing, setHistoryRefreshing] = useState(false)
  const [backtestExpanded, setBacktestExpanded] = useState(Boolean(strategy.backtest.risk_alert))
  useEffect(() => {
    setBacktestExpanded(Boolean(strategy.backtest.risk_alert))
  }, [strategy.id])
  useEffect(() => {
    let cancelled = false
    setPanelHistory(getCachedPanelHistory(strategy.id, query))
    setHistoryError('')
    setHistoryRefreshing(true)
    requestPanelHistory(strategy.id, query, 'selected')
      .then(payload => { if (!cancelled) setPanelHistory(payload) })
      .catch(err => { if (!cancelled) setHistoryError(err instanceof Error ? err.message : 'Could not update history') })
      .finally(() => { if (!cancelled) setHistoryRefreshing(false) })
    return () => { cancelled = true }
  }, [strategy.id, query, refreshVersion])
  useEffect(() => {
    let cancelled = false
    const controller = new AbortController()
    setAlertEpisodes([])
    api<AlertEpisode[]>(`/api/strategies/${strategy.id}/alert-episodes?include_resolved=true&limit=50`, { signal: controller.signal })
      .then(payload => { if (!cancelled) setAlertEpisodes(payload) })
      .catch(() => { if (!cancelled) setAlertEpisodes([]) })
    return () => { cancelled = true; controller.abort() }
  }, [strategy.id])

  const active = strategy
  const m = active.metrics
  const history = panelHistory ? [...panelHistory.trades].reverse() : []
  const riskAlertAudit = active.backtest.risk_audit?.status === 'risk_exceeded'
  const reportRunId = active.backtest.latest_completed_run_id
  const backtestWorstTrade = active.backtest.risk_audit?.worst_loss
  return <aside className="strategy-drawer" aria-label="Quick strategy statistics">
    <div className="drawer-header">
      <div className="drawer-strategy-heading"><div className="strategy-status-controls" role="group" aria-label="Estado manual de estrategia">{(['live', 'monitor', 'candidate_live'] as const).map(status => <button key={status} className={`strategy-status-button ${status} ${active.strategy_status === status ? 'active' : ''}`} type="button" aria-label={`Marcar ${displayName(active)} como ${strategyStatusLabels[status]}`} aria-pressed={active.strategy_status === status} title={strategyStatusLabels[status]} disabled={statusSaving} onClick={() => onStatusChange(active.id, active.strategy_status === status ? '' : status)}><StrategyStatusIcon status={status} size={18} /></button>)}<button className={`strategy-status-button clear ${active.strategy_status === '' ? 'active' : ''}`} type="button" aria-label={`Quitar estado de ${displayName(active)}`} aria-pressed={active.strategy_status === ''} title="Quitar estado" disabled={statusSaving} onClick={() => onStatusChange(active.id, '')}><X size={16} /></button></div><div><strong>{displayName(active)}</strong><span>{active.symbol || 'No symbol'} · {m.trades} trades in history</span></div></div>
      <button className="drawer-close" type="button" onClick={onClose}>Close</button>
    </div>
    <div className="drawer-body">
      <PermanentDeleteNotice strategy={active} onDeleted={onDeleted} />
      <ArchiveStrategyNotice strategy={active} onArchived={onDeleted} />
      {active.backtest.has_completed && <details className="drawer-section drawer-collapsible drawer-backtest-status" open={backtestExpanded} onToggle={event => setBacktestExpanded(event.currentTarget.open)}>
        <summary className="drawer-collapsible-summary">
          <div><span className="eyebrow">MT5 BACKTEST</span><strong><BacktestBadge summary={active.backtest}/></strong></div>
          <div className="drawer-backtest-summary-metric"><span>Worst trade</span><strong className={active.backtest.risk_alert ? 'value-bad' : ''}>{backtestWorstTrade == null ? '—' : money(backtestWorstTrade)}</strong></div>
          <span className="drawer-collapsible-chevron" aria-hidden="true">⌄</span>
        </summary>
        <div className="drawer-collapsible-content">
          {riskAlertAudit && <label className="backtest-risk-acceptance"><input type="checkbox" checked={Boolean(active.backtest.risk_alert_accepted)} disabled={!reportRunId || savingBacktestRiskAcceptance} onChange={event => { if (reportRunId) onBacktestRiskAcceptance(active.id, reportRunId, event.target.checked) }} />Validado manualmente tras revisar el reporte</label>}
          {active.backtest.risk_alert_accepted && <small>La auditoría detectó un exceso de riesgo; fue validado manualmente{active.backtest.risk_alert_accepted_at ? ` el ${new Date(active.backtest.risk_alert_accepted_at).toLocaleString()}` : ''}.</small>}
          <div className="drawer-backtest-actions">{reportRunId && <a className="button" href={`/api/backtests/${reportRunId}/report`} target="_blank" rel="noreferrer">Abrir reporte</a>}<button className="button" type="button" onClick={() => onOpenDetail(active.id)}>Ver auditoría completa</button></div>
        </div>
      </details>}
      <StrategyNoteEditor strategy={active} placement="drawer" onSaved={(strategyId, note, noteUpdatedAt) => {
        onNoteSaved(strategyId, note, noteUpdatedAt)
      }} />
      <div className="drawer-stat-grid">
        <DrawerMetric label="Realized P/L" value={signedMoney(m.net_profit)} detail={`${m.trades} combined trades | Exp ${money(m.expectancy)}`} tone={m.net_profit >= 0 ? 'positive' : 'negative'} />
        <DrawerMetric label="Accumulated P/L %" value={signedPercent(active.account_pl_percent)} detail={active.account_balance == null ? 'No active-account balance snapshot' : `Combined P/L + floating / current ${money(active.account_balance)} balance`} tone={(active.account_pl_percent ?? 0) >= 0 ? 'positive' : 'negative'} />
        <DrawerMetric label="Health" value={healthLabel(active.health.status)} detail={active.health.reasons.join(', ') || 'No active alerts'} tone={`status-${active.health.status}`} dotStatus={active.health.status} />
        <DrawerMetric label="Today" value={signedMoney(m.today_profit)} detail={`${m.today_trades} trades today`} tone={m.today_profit >= 0 ? 'positive' : 'negative'} />
        <DrawerMetric label="Win %" value={`${(m.win_rate * 100).toFixed(1)}%`} detail={`${m.winning_trades}/${m.trades} won`} tone={m.win_rate >= .5 ? 'positive' : 'negative'} />
        <DrawerMetric label="Factor P" value={number(m.profit_factor)} detail={`${money(m.gross_profit)} / ${money(m.gross_loss)}`} tone={m.profit_factor == null || m.profit_factor >= 1 ? 'positive' : 'negative'} />
        <DrawerMetric label="Max DD" value={money(m.max_drawdown)} detail="Peak-valley" tone={m.max_drawdown > 0 ? 'negative' : 'positive'} />
        <DrawerMetric label="Avg W/L" value={`${money(m.avg_win)} / ${money(m.avg_loss)}`} detail="Average win / average loss" />
        <DrawerMetric label="Avg. dur." value={duration(m.avg_duration_seconds)} detail="Average time per trade" />
        <DrawerMetric label="Best trade" value={signedMoney(m.best_trade)} detail="Closed trade" tone={(m.best_trade || 0) >= 0 ? 'positive' : 'negative'} />
        <DrawerMetric label="Worst trade" value={signedMoney(m.worst_trade)} detail="Closed trade" tone={(m.worst_trade || 0) >= 0 ? 'positive' : 'negative'} />
        <div className="drawer-card streak-card"><StreakPair metrics={m} /></div>
      </div>
      <MonthlyPnlCalendar strategyId={active.id} />
      <section className="drawer-section">
        <div className="drawer-section-title"><span className="eyebrow">EQUITY CURVE</span></div>
        <EquitySparkline points={panelHistory?.equity_curve || []} />
      </section>
      <section className={`drawer-section drawer-risk ${active.risk_guard.status}`}>
        <div className="drawer-section-title"><span className="eyebrow">RISK GUARD</span><span className={`risk-verdict ${active.risk_guard.stop_recommended ? 'stop' : active.risk_guard.status}`}>{active.risk_guard.stop_recommended ? 'Stop recommended' : active.risk_guard.status === 'gray' ? 'Not evaluated' : active.risk_guard.status === 'yellow' ? 'Attention' : 'Within limits'}</span></div>
        <div className="drawer-risk-live"><span>Combined losing streak <strong>{active.risk_guard.live.current_consecutive_losses}</strong></span><span>Max DD histórico <strong>{money(active.risk_guard.live.max_drawdown)}</strong></span></div>
        {(['is', 'oos', 'mt5'] as const).map(sample => <div className="drawer-risk-row" key={sample}>
          <strong>{sample === 'mt5' ? 'MT5 fallback' : sample.toUpperCase()}</strong>
          <span>DD: {riskCheckLabel(riskCheck(active.risk_guard, sample, 'drawdown'))}</span>
          <span>Streak: {riskCheckLabel(riskCheck(active.risk_guard, sample, 'loss_streak'))}</span>
        </div>)}
      </section>
      <section className={`drawer-section drawer-alerts ${active.active_alerts.some(item => item.severity === 'red') ? 'red' : active.active_alerts.length ? 'yellow' : ''}`}>
        <div className="drawer-section-title"><span className="eyebrow">ALERTAS PERSISTENTES</span><strong>{active.active_alerts.length ? `${active.active_alerts.length} activas` : 'Sin alertas'}</strong></div>
        <div className="drawer-alert-thresholds"><span>Racha actual <strong>{active.risk_guard.live.current_consecutive_losses}</strong></span><span>Amarillo <strong>{active.alert_thresholds.loss_streak_yellow}</strong></span><span>Rojo <strong>{active.alert_thresholds.loss_streak_red}</strong></span></div>
        {active.active_alerts.map(item => <div className={`drawer-alert-entry ${item.severity}`} key={item.id}><div><strong>{item.reason}</strong><small>{item.sample_type ? `${item.sample_type.toUpperCase()} · ` : ''}{item.status === 'recovering' ? `Recuperación ${item.recovery_wins}/2` : item.severity === 'red' && item.alert_type === 'loss_streak_absolute' ? 'Revisión urgente' : 'Activa'}</small></div><span>{item.actual == null ? '—' : item.actual.toFixed(2)}{item.threshold == null ? '' : ` / ${item.threshold.toFixed(2)}`}</span></div>)}
        {!!alertEpisodes.length && <details className="drawer-alert-history"><summary>Historial de episodios</summary><div>{alertEpisodes.map(item => <div className={`drawer-alert-history-row ${item.max_severity}`} key={item.id}><span>{item.reason}</span><small>{new Date(item.first_detected_at).toLocaleString()} · {item.resolved_at ? `Resuelta ${new Date(item.resolved_at).toLocaleString()}` : item.status === 'recovering' ? `Recuperación ${item.recovery_wins}/2` : 'Activa'}</small></div>)}</div></details>}
      </section>
      <StrategyDisplayNameEditor strategy={active} onSaved={(strategyId, displayName, source) => {
        onDisplayNameSaved(strategyId, displayName, source)
      }} />
      {panelHistory && <MagicNumbersPanel key={`magic-numbers-${strategy.id}`} strategy={active} mappings={panelHistory.mappings} placement="drawer" />}
      <StrategyIdentifiers key={`identifiers-${strategy.id}`} strategy={active} detail={panelHistory} />
      <section className="drawer-section drawer-history">
        <div className="drawer-section-title"><span className="eyebrow">HISTORY ({panelHistory?.total_trades ?? 0})</span>{historyRefreshing && panelHistory && <small>Actualizando…</small>}</div>
        {historyError && <div className="drawer-history-warning">{panelHistory ? 'No se pudo actualizar; se muestran los datos guardados.' : historyError}</div>}
        {history.length ? <table className="drawer-table"><thead><tr><th>Close</th><th>Acct.</th><th>Sym.</th><th>Dir.</th><th>Lots</th><th>Net P/L</th><th>Dur.</th></tr></thead><tbody>{history.map(trade => <tr key={trade.trade_key || `${trade.terminal_id}-${trade.deal_ticket}`}><td>{new Date(trade.close_time_msc).toLocaleString('en-US', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })}</td><td><span className={`trade-source ${trade.source_role || 'live'}`}>{trade.source_account || 'Live'}</span></td><td>{trade.symbol}</td><td>{directionLabel(trade.direction)}</td><td>{trade.volume.toFixed(2)}</td><td className={trade.net_profit >= 0 ? 'value-good' : 'value-bad'}>{signedMoney(trade.net_profit)}</td><td>{tradeDuration(trade.open_time_msc, trade.close_time_msc)}</td></tr>)}</tbody></table> : <div className="drawer-empty">{panelHistory ? 'No history for this window.' : 'Loading history…'}</div>}
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
  const [profile, setProfile] = useState<'reference' | 'sqx' | 'darwinex'>('darwinex')
  const [form, setForm] = useState<BacktestDefaults | null>(null)
  const [runs, setRuns] = useState<BacktestRun[]>([])
  const [notice, setNotice] = useState('')
  const [loading, setLoading] = useState(false)
  const [candidates, setCandidates] = useState<BacktestCandidates | null>(null)
  const [batches, setBatches] = useState<BacktestBatch[]>([])
  const [batchModel, setBatchModel] = useState(1)
  const [retention, setRetention] = useState<BacktestRetentionPreview | null>(null)
  const [cleaningRetention, setCleaningRetention] = useState(false)

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
        body: JSON.stringify({ model: batchModel, policy: 'strict', broker: 'Darwinex' }),
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
  async function loadRetention() {
    try { setRetention(await api<BacktestRetentionPreview>('/api/backtests/retention-preview')) }
    catch (error) { setNotice(error instanceof Error ? error.message : 'Could not inspect retention candidates') }
  }
  async function cleanupRetention() {
    if (!retention?.candidate_count || !window.confirm(`Delete ${retention.candidate_count} old MT5 backtest runs? A database backup will be created first.`)) return
    setCleaningRetention(true)
    try {
      const result = await api<{ deleted_count: number; backup_path: string | null }>('/api/backtests/retention-cleanup', { method: 'POST', body: JSON.stringify({ confirm: true }) })
      setNotice(`Deleted ${result.deleted_count} old runs. Backup: ${result.backup_path || 'not needed'}`)
      await Promise.all([loadRuns(), loadRetention()])
      onCompleted()
    } catch (error) { setNotice(error instanceof Error ? error.message : 'Could not clean old backtests') }
    finally { setCleaningRetention(false) }
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
          <button className={profile === 'darwinex' ? 'active' : ''} onClick={() => setProfile('darwinex')}>Darwinex</button>
          <button className={profile === 'reference' ? 'active' : ''} onClick={() => setProfile('reference')}>FPM reference</button>
          <button className={profile === 'sqx' ? 'active' : ''} onClick={() => setProfile('sqx')}>SQX period</button>
        </div>
      </div>
      <div className="backtest-form">
        <label className="wide">Strategy<select value={strategyId} disabled={!strategies.length} onChange={event => setStrategyId(Number(event.target.value))}>{strategies.length ? strategies.map(strategy => <option key={strategy.id} value={strategy.id}>{strategy.symbol} - {displayName(strategy)}</option>) : <option value={0}>No strategies for selected account</option>}</select></label>
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
      <div className="panel-heading"><div><span className="eyebrow">EXECUTIONS</span><h2>Run history</h2></div><div className="actions"><button className="button" onClick={loadRetention}>Preview cleanup</button>{retention && <button className="button danger" disabled={!retention.candidate_count || cleaningRetention} onClick={cleanupRetention}>Clean {retention.candidate_count} old</button>}<button className="icon-button" title="Refresh runs" onClick={() => loadRuns()}><RefreshCw size={15}/></button></div></div>
      {retention && <div className="backtest-notice">Preview: {retention.completed_superseded} completed runs superseded and {retention.failed_or_terminal} terminal failed runs. Active runs are excluded.</div>}
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

function Settings({ reload, aliases: savedAliases, onAliasesSaved }: { reload: () => void; aliases: Record<string, string>; onAliasesSaved: (aliases: Record<string, string>) => void }) {
  const [dataDir, setDataDir] = useState('')
  const [terminalName, setTerminalName] = useState('New terminal')
  const [terminalTransport, setTerminalTransport] = useState<'local'|'ssh'>('local')
  const [sshHost, setSshHost] = useState('')
  const [sshPort, setSshPort] = useState(22)
  const [sshUsername, setSshUsername] = useState('Administrator')
  const [sshKeyPath, setSshKeyPath] = useState('')
  const [sshFingerprint, setSshFingerprint] = useState('')
  const [project, setProject] = useState('Retester')
  const [databank, setDatabank] = useState('Results')
  const [sqxStatus, setSqxStatus] = useState('Checking SQX connection...')
  const [sqxDatabanks, setSqxDatabanks] = useState<Record<string, Array<{name:string;records:number;view:string}>>>({})
  const [notice, setNotice] = useState('')
  const [mappingSuggestions, setMappingSuggestions] = useState<Array<{terminal_id:number;account_login:string;symbol:string;magic:number;comment:string;deal_count:number;candidates:Array<{strategy_id:number;name:string;score:number;evidence?:string[];deployment_required?:boolean}>}>>([])
  const [mappingChoices, setMappingChoices] = useState<Record<number, number>>({})
  const [deploymentSuggestions, setDeploymentSuggestions] = useState<Array<{deployment_id:number;name:string;account_login:string;symbol:string;safe:boolean;candidates:Array<{canonical_id:number;name:string;score:number;evidence:string[]}>}>>([])
  const [deploymentChoices, setDeploymentChoices] = useState<Record<number, number>>({})
  const [inventoryCandidates, setInventoryCandidates] = useState<Array<{id:number;account_login:string;server:string;symbol:string;timeframe:string;ex5_name:string;magic:number|null;observed_at:string;candidates:Array<{strategy_id:number;name:string;confidence:number;evidence:string[]}>}>>([])
  const [inventoryChoices, setInventoryChoices] = useState<Record<number, number>>({})
  const [rules, setRules] = useState<Record<string, number>>({})
  const [accountAliases, setAccountAliases] = useState<Record<string, string>>(savedAliases)
  const [aliasAccounts, setAliasAccounts] = useState<string[]>([])
  useEffect(() => {
    api<Record<string,number>>('/api/alerts').then(setRules).catch(() => undefined)
    api<{available:boolean;details?:{app_version?:string}}>('/api/sqx/status')
      .then(result => setSqxStatus(result.available ? `Connected · SQX ${result.details?.app_version || ''}` : 'SQX unavailable'))
      .catch(() => setSqxStatus('SQX unavailable'))
    api<Record<string, Array<{name:string;records:number;view:string}>>>('/api/sqx/databanks')
      .then(setSqxDatabanks)
      .catch(() => undefined)
    api<AccountAliasSettings>('/api/settings/account-aliases')
      .then(result => { setAliasAccounts(result.accounts); setAccountAliases(result.aliases) })
      .catch(() => undefined)
  }, [])
  useEffect(() => { setAccountAliases(savedAliases) }, [savedAliases])
  async function addTerminal() {
    try { await api('/api/terminals', { method: 'POST', body: JSON.stringify({ name: terminalName, data_dir: dataDir, transport: terminalTransport, ssh_host: sshHost || null, ssh_port: sshPort, ssh_username: sshUsername || null, ssh_key_path: sshKeyPath || null, ssh_host_fingerprint: sshFingerprint || null }) }); setNotice('Terminal registered.'); reload() }
    catch (error) { setNotice(error instanceof Error ? error.message : 'Error') }
  }
  async function syncSqx() {
    setNotice('Querying SQX…')
    try {
      const result = await api<{ received:number; imported:number; matched:number; created:number; unmatched:number; passed:number; edge_available:number; egt_available:number; doctor_available:number; analytics_unavailable:number; renamed:number; promoted:number; rename_conflicts:Array<{strategy_name:string;reason:string}> }>('/api/sqx/sync', { method: 'POST', body: JSON.stringify({ project, databank }) })
      setNotice(`SQX: ${result.imported}/${result.received} imported · ${result.renamed + result.promoted} links reconciled · ${result.rename_conflicts.length} rename conflicts · Edge ${result.edge_available} · EGT ${result.egt_available} · Doctor ${result.doctor_available} · ${result.analytics_unavailable} with unavailable analytics.`)
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
  async function scanInventory() {
    const result = await api<{imported:number;skipped:number;failed:number}>('/api/bot-inventory/scan', { method: 'POST' })
    setNotice(`Inventarios VPS: ${result.imported} nuevos · ${result.skipped} ya importados · ${result.failed} con error.`)
    await loadInventoryCandidates(); reload()
  }
  async function loadInventoryCandidates() {
    const result = await api<typeof inventoryCandidates>('/api/bot-inventory/candidates')
    setInventoryCandidates(result)
    setInventoryChoices(Object.fromEntries(result.map((item, index) => [index, item.candidates[0]?.strategy_id || 0])))
    setNotice(`${result.length} despliegues VPS necesitan revisión.`)
  }
  async function confirmInventoryCandidate(index: number) {
    const item = inventoryCandidates[index]
    const strategyId = inventoryChoices[index]
    if (!strategyId) { setNotice('Selecciona una estrategia SQX.'); return }
    await api(`/api/bot-inventory/deployments/${item.id}/confirm`, { method: 'POST', body: JSON.stringify({ strategy_id: strategyId }) })
    setNotice('Despliegue VPS vinculado.')
    await loadInventoryCandidates(); reload()
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
  async function saveAccountAliases() {
    try {
      const result = await api<AccountAliasSettings>('/api/settings/account-aliases', {
        method: 'PUT', body: JSON.stringify({ aliases: accountAliases }),
      })
      setAliasAccounts(result.accounts)
      setAccountAliases(result.aliases)
      onAliasesSaved(result.aliases)
      setNotice('Aliases de cuentas guardados.')
    } catch (error) {
      setNotice(error instanceof Error ? error.message : 'No se pudieron guardar los aliases.')
    }
  }
  return <div className="settings-grid">
    <section className="panel"><span className="eyebrow">MT5 SOURCES</span><h2>Register terminal</h2><p className="help">SSH transports DashboardBridge files from a VPS. The MT5 service remains the read-only source of live account data.</p><label>Name<input value={terminalName} onChange={e => setTerminalName(e.target.value)} /></label><label>Transport<select value={terminalTransport} onChange={e => setTerminalTransport(e.target.value as 'local'|'ssh')}><option value="local">Local folder</option><option value="ssh">SSH / SFTP VPS</option></select></label><label>{terminalTransport === 'ssh' ? 'Remote MT5 DataDir' : 'DataDir'}<input placeholder="C:\Users\…\MetaQuotes\Terminal\HASH" value={dataDir} onChange={e => setDataDir(e.target.value)} /></label>{terminalTransport === 'ssh' && <><label>SSH host<input value={sshHost} onChange={e => setSshHost(e.target.value)} /></label><label>SSH port<input type="number" value={sshPort} onChange={e => setSshPort(Number(e.target.value))} /></label><label>SSH user<input value={sshUsername} onChange={e => setSshUsername(e.target.value)} /></label><label>Private key path on this PC<input value={sshKeyPath} onChange={e => setSshKeyPath(e.target.value)} /></label><label>Server fingerprint<input placeholder="SHA256:…" value={sshFingerprint} onChange={e => setSshFingerprint(e.target.value)} /></label></>}<button className="button primary" onClick={addTerminal}>Save terminal</button></section>
    <section className="panel"><span className="eyebrow">HISTORY</span><h2>Sync SQX</h2><p className="help">{sqxStatus}. Previous snapshots remain available after SQX closes.</p><label>Project<select value={project} onChange={e => { setProject(e.target.value); setDatabank(sqxDatabanks[e.target.value]?.[0]?.name || '') }}>{Object.keys(sqxDatabanks).length ? Object.keys(sqxDatabanks).map(value => <option key={value} value={value}>{value}</option>) : <option value={project}>{project}</option>}</select></label><label>Databank<select value={databank} onChange={e => setDatabank(e.target.value)}>{sqxDatabanks[project]?.length ? sqxDatabanks[project].map(value => <option key={value.name} value={value.name}>{value.name} · {value.records} strategies</option>) : <option value={databank}>{databank}</option>}</select></label><button className="button primary" onClick={syncSqx}>Read-only sync</button></section>
    <section className="panel settings-wide"><span className="eyebrow">CATALOG AND MAPPINGS</span><h2>Maintenance</h2><div className="maintenance-actions"><button className="button" onClick={async () => { await api('/api/catalog/import', { method: 'POST' }); setNotice('Catalog reloaded.'); reload() }}>Reload Track_v1.xlsx</button><a className="button export-button" href="/api/catalog/export">Exportar Excel</a><button className="button primary" onClick={scanInventory}>Importar inventarios VPS</button><button className="button" onClick={loadInventoryCandidates}>Revisar despliegues VPS</button><button className="button primary" onClick={autoConfirmMappings}>Link safe matches</button><button className="button" onClick={loadMappings}>Find suggested links</button><button className="button primary" onClick={autoLinkDeployments}>Link safe SQX identities</button><button className="button" onClick={loadDeploymentSuggestions}>Review SQX identities</button></div>
      {inventoryCandidates.length > 0 && <div className="mapping-review"><div className="mapping-review-head"><span>Despliegue VPS</span><span>Estrategia SQX</span><span>Evidencia</span><span /></div>{inventoryCandidates.map((item,index) => {
        const selected = item.candidates.find(candidate => candidate.strategy_id === inventoryChoices[index])
        return <div className="mapping-review-row" key={`inventory-${item.id}`}><div><strong>{item.account_login} · {item.symbol} · {item.timeframe || '—'}</strong><small>{item.ex5_name} · MN {item.magic ?? 'desconocido'} · {item.server}</small></div><select value={inventoryChoices[index] || ''} onChange={event => setInventoryChoices({...inventoryChoices,[index]:Number(event.target.value)})}><option value="">Seleccionar…</option>{item.candidates.map(candidate => <option key={candidate.strategy_id} value={candidate.strategy_id}>{candidate.name}</option>)}</select><span>{selected ? `${Math.round(selected.confidence * 100)}% · ${selected.evidence.join(', ')}` : '—'}</span><button className="button" onClick={() => confirmInventoryCandidate(index)}>Confirmar vínculo</button></div>
      })}</div>}
      {mappingSuggestions.length > 0 && <div className="mapping-review"><div className="mapping-review-head"><span>Observed trade</span><span>Proposed strategy</span><span>Confidence</span><span /></div>{mappingSuggestions.map((item,index) => <div className="mapping-review-row" key={`${item.terminal_id}-${item.magic}-${item.symbol}-${item.comment}`}><div><strong>{item.symbol} · magic {item.magic}</strong><small>{item.comment || 'No comment'} · {item.deal_count} deals</small></div><select value={mappingChoices[index] || ''} onChange={event => setMappingChoices({...mappingChoices,[index]:Number(event.target.value)})}><option value="">Select…</option>{item.candidates.map(candidate => <option key={candidate.strategy_id} value={candidate.strategy_id}>{candidate.name}</option>)}</select><span>{item.candidates.find(candidate => candidate.strategy_id === mappingChoices[index])?.score ? `${Math.round((item.candidates.find(candidate => candidate.strategy_id === mappingChoices[index])?.score || 0) * 100)}%` : '—'}</span><button className="button" onClick={() => confirmSuggestion(index)}>Confirm</button></div>)}</div>}
      {deploymentSuggestions.length > 0 && <div className="mapping-review"><div className="mapping-review-head"><span>MT5 deployment</span><span>Canonical SQX identity</span><span>Evidence</span><span /></div>{deploymentSuggestions.map((item,index) => {
        const selectedCandidate = item.candidates.find(candidate => candidate.canonical_id === deploymentChoices[index])
        return <div className="mapping-review-row" key={`deployment-${item.deployment_id}`}><div><strong>{item.symbol} · {item.account_login || 'No account'}</strong><small>{item.name}</small></div><select value={deploymentChoices[index] || ''} onChange={event => setDeploymentChoices({...deploymentChoices,[index]:Number(event.target.value)})}><option value="">Select…</option>{item.candidates.map(candidate => <option key={candidate.canonical_id} value={candidate.canonical_id}>{candidate.name}</option>)}</select><span>{selectedCandidate ? `${Math.round(selectedCandidate.score * 100)}% · ${selectedCandidate.evidence.join(', ')}` : '—'}</span><button className="button" onClick={() => confirmDeployment(index)}>Attach</button></div>
      })}</div>}
    </section>
    <section className="panel settings-wide"><span className="eyebrow">ACCOUNTS</span><h2>Account aliases</h2><p className="help">Asigna un nombre corto a cada cuenta. El login MT5 se conserva como referencia.</p><div className="account-aliases">{aliasAccounts.map(account => <label key={account} className="account-alias-row"><strong>{account}</strong><input aria-label={`Alias para cuenta ${account}`} placeholder="Sin alias" value={accountAliases[account] || ''} onChange={event => setAccountAliases({ ...accountAliases, [account]: event.target.value })} /></label>)}{!aliasAccounts.length && <div className="empty-state">No hay cuentas detectadas todavía.</div>}</div><button className="button primary" onClick={saveAccountAliases}>Save aliases</button></section>
    <section className="panel settings-wide"><span className="eyebrow">HEALTH RULES</span><h2>Global thresholds</h2><div className="rules-grid">{Object.entries({min_trades:'Minimum trades',loss_streak_yellow:'Yellow loss streak',loss_streak_red:'Red loss streak',loss_streak_relative_yellow:'Yellow relative streak',loss_streak_relative_red:'Red relative streak',drawdown_yellow:'Yellow DD',drawdown_red:'Red DD',performance_yellow:'Yellow performance',performance_red:'Red performance',frequency_yellow_low:'Yellow frequency min.',frequency_yellow_high:'Yellow frequency max.',frequency_red_low:'Red frequency min.',frequency_red_high:'Red frequency max.'}).map(([key,label]) => <label key={key}>{label}<input type="number" step={key === 'min_trades' || key === 'loss_streak_yellow' || key === 'loss_streak_red' ? 1 : .05} value={rules[key] ?? ''} onChange={event => setRules({...rules,[key]:Number(event.target.value)})}/></label>)}</div><button className="button primary" onClick={saveRules}>Save thresholds</button></section>
    {notice && <div className="toast">{notice}</div>}
  </div>
}

function TerminalEventsPanel({
  data, loading, severity, sourceId, logKind, search, dateRange, onSeverity, onSourceId, onLogKind,
  onSearch, onDateRange, onRefresh, refreshing, onLoadMore,
}: {
  data: TerminalEventsData | null; loading: boolean; severity: string; sourceId: string; logKind: string;
  search: string; onSeverity: (value: string) => void; onSourceId: (value: string) => void;
  onLogKind: (value: string) => void; onSearch: (value: string) => void;
  dateRange: EventDateRange; onDateRange: (value: EventDateRange) => void; onRefresh: () => void;
  refreshing: boolean; onLoadMore: () => void;
}) {
  const events = data?.events || []
  return <section className="terminal-events" aria-busy={loading}>
    <div className="event-summary-grid">
      <div className="event-summary error"><span>Errores</span><strong>{data?.summary.errors ?? 0}</strong><small>{eventDateRangeLabels[dateRange]}</small></div>
      <div className="event-summary warning"><span>Avisos</span><strong>{data?.summary.warnings ?? 0}</strong><small>{eventDateRangeLabels[dateRange]}</small></div>
      <div className="event-summary"><span>Eventos mostrados</span><strong>{data?.total ?? 0}</strong><small>Journal y Expert logs</small></div>
    </div>
    <section className="panel event-panel">
      <div className="panel-heading">
        <div><span className="eyebrow">MT5 LOG MONITORING</span><h2>Eventos de terminal y Expert Advisors</h2><p className="help">Solo fuentes VPS leídas por SSH/SFTP. No ejecuta comandos remotos ni modifica MT5.</p></div>
        <button className="button refresh icon-command" onClick={onRefresh} disabled={refreshing}><RefreshCw size={14}/> {refreshing ? 'Leyendo…' : 'Actualizar ahora'}</button>
      </div>
      <div className="filters event-filters">
        <div className="event-date-ranges" role="group" aria-label="Rango de fechas">{(Object.keys(eventDateRangeLabels) as EventDateRange[]).map(range => <button type="button" key={range} className={`button ${dateRange === range ? 'active' : ''}`} aria-pressed={dateRange === range} onClick={() => onDateRange(range)}>{eventDateRangeLabels[range]}</button>)}</div>
        <select aria-label="Severidad" value={severity} onChange={event => onSeverity(event.target.value)}><option value="all">Errores y avisos</option><option value="error">Solo errores</option><option value="warning">Solo avisos</option></select>
        <select aria-label="Origen de log" value={logKind} onChange={event => onLogKind(event.target.value)}><option value="all">Journal y Experts</option><option value="journal">Journal</option><option value="experts">Experts</option></select>
        <select aria-label="Terminal de origen" value={sourceId} onChange={event => onSourceId(event.target.value)}><option value="all">Todas las VPS</option>{data?.sources.map(source => <option key={source.id} value={String(source.id)}>{source.label}{source.account_login ? ` · ${source.account_login}` : ''}</option>)}</select>
        <input aria-label="Buscar eventos" placeholder="Buscar mensaje…" value={search} onChange={event => onSearch(event.target.value)} />
      </div>
      {!events.length && !loading ? <div className="empty-state">No hay errores ni avisos capturados con estos filtros.</div> : <div className="event-list">{events.map(event => <details className={`event-row ${event.severity}`} key={event.id}>
        <summary><span className={`event-severity ${event.severity}`}>{event.severity === 'error' ? 'ERROR' : 'AVISO'}</span><time>{new Date(event.occurred_at || event.captured_at).toLocaleString()}</time><strong>{event.source_label}</strong><span className="event-kind">{event.log_kind === 'journal' ? 'Journal' : 'Experts'}</span><span className="event-message">{event.message}</span></summary>
        <div className="event-context"><small>{event.terminal_name} · {event.log_name}</small><pre>{event.context}</pre></div>
      </details>)}</div>}
      {data && data.offset + data.events.length < data.total && <button className="button" onClick={onLoadMore}>Mostrar más eventos</button>}
    </section>
  </section>
}

export default function App() {
  const [data, setData] = useState<Dashboard | null>(null)
  const [journalData, setJournalData] = useState<JournalAnalysisData | null>(null)
  const [terminalEvents, setTerminalEvents] = useState<TerminalEventsData | null>(null)
  const [accountAliases, setAccountAliases] = useState<Record<string, string>>({})
  const [tab, setTab] = useState<Tab>('overview')
  const [windowName, setWindowName] = useState('all')
  const [customStart, setCustomStart] = useState('')
  const [customEnd, setCustomEnd] = useState('')
  const [brokerAccounts, setBrokerAccounts] = useState<Set<string> | null>(null)
  const [assets, setAssets] = useState<Set<string> | null>(null)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [panelStrategyId, setPanelStrategyId] = useState<number | null>(null)
  const [query, setQuery] = useState('')
  const [strategyIdQuery, setStrategyIdQuery] = useState('')
  const [magicQuery, setMagicQuery] = useState('')
  const [showStopped, setShowStopped] = useState(false)
  const [statusFilter, setStatusFilter] = useState('all')
  const [strategyStatusFilter, setStrategyStatusFilter] = useState<StrategyStatusFilter>('all')
  const [linkFilter, setLinkFilter] = useState('all')
  const [backtestStatusFilter, setBacktestStatusFilter] = useState<BacktestFilter>('all')
  const [sort, setSort] = useState<SortState>(null)
  const [overviewView, setOverviewView] = useState<OverviewViewSnapshot | null>(null)
  const [error, setError] = useState('')
  const [dashboardLoading, setDashboardLoading] = useState(false)
  const [historyRefreshVersion, setHistoryRefreshVersion] = useState(0)
  const [journalLoading, setJournalLoading] = useState(false)
  const [terminalEventsLoading, setTerminalEventsLoading] = useState(false)
  const [terminalEventsRefreshing, setTerminalEventsRefreshing] = useState(false)
  const [eventSeverity, setEventSeverity] = useState('all')
  const [eventSourceId, setEventSourceId] = useState('all')
  const [eventLogKind, setEventLogKind] = useState('all')
  const [eventSearch, setEventSearch] = useState('')
  const [eventDateRange, setEventDateRange] = useState<EventDateRange>('5d')
  const [selectionSaving, setSelectionSaving] = useState<Set<number>>(() => new Set())
  const [strategyStatusSaving, setStrategyStatusSaving] = useState<Set<number>>(() => new Set())
  const [noMmAcceptanceSaving, setNoMmAcceptanceSaving] = useState<Set<number>>(() => new Set())
  const [backtestRiskAcceptanceSaving, setBacktestRiskAcceptanceSaving] = useState<Set<number>>(() => new Set())
  const [sidebarCollapsed, setSidebarCollapsed] = useState(readSidebarCollapsed)
  const overviewTopScrollRef = useRef<HTMLDivElement>(null)
  const overviewTableScrollRef = useRef<HTMLDivElement>(null)
  const overviewTableRef = useRef<HTMLTableElement>(null)
  const overviewTableWidthRef = useRef(0)
  const restoreOverviewViewRef = useRef(false)
  const dashboardRequestRef = useRef<{ id: number; controller: AbortController | null }>({ id: 0, controller: null })
  const journalRequestRef = useRef<{ id: number; controller: AbortController | null }>({ id: 0, controller: null })
  const dashboardQuery = useMemo(() => buildDashboardQuery(windowName, customStart, customEnd, brokerAccounts, assets), [windowName, customStart, customEnd, brokerAccounts, assets])
  const restoreOverviewScroll = useCallback((scrollLeft: number, scrollTop: number) => {
    requestAnimationFrame(() => {
      if (overviewTopScrollRef.current) overviewTopScrollRef.current.scrollLeft = scrollLeft
      if (overviewTableScrollRef.current) overviewTableScrollRef.current.scrollLeft = scrollLeft
      window.scrollTo({ top: scrollTop })
    })
  }, [])
  const loadDashboard = useCallback(async () => {
    dashboardRequestRef.current.controller?.abort()
    const controller = new AbortController()
    const id = dashboardRequestRef.current.id + 1
    dashboardRequestRef.current = { id, controller }
    const scrollLeft = overviewTableScrollRef.current?.scrollLeft ?? overviewTopScrollRef.current?.scrollLeft ?? 0
    const scrollTop = window.scrollY
    setDashboardLoading(true)
    try {
      const payload = await api<Dashboard>(`/api/dashboard?${dashboardQuery}`, { signal: controller.signal })
      if (dashboardRequestRef.current.id !== id) return
      setData(payload)
      setHistoryRefreshVersion(current => current + 1)
      // null represents "all" and avoids a second request after initial load.
      setAssets(current => current === null ? null : new Set([...current].filter(asset => payload.asset_options.some(option => option.key === asset))))
      setSelectedId(current => current ?? payload.strategies[0]?.id ?? null)
      setError('')
      restoreOverviewScroll(scrollLeft, scrollTop)
    }
    catch (err) {
      if (controller.signal.aborted || dashboardRequestRef.current.id !== id) return
      setError(err instanceof Error ? err.message : 'Could not connect to the backend')
    } finally {
      if (dashboardRequestRef.current.id === id) setDashboardLoading(false)
    }
  }, [dashboardQuery, restoreOverviewScroll])
  const loadJournal = useCallback(async () => {
    journalRequestRef.current.controller?.abort()
    const controller = new AbortController()
    const id = journalRequestRef.current.id + 1
    journalRequestRef.current = { id, controller }
    setJournalLoading(true)
    try {
      const payload = await api<JournalAnalysisData>(`/api/journal-analysis?${dashboardQuery}`, { signal: controller.signal })
      if (journalRequestRef.current.id !== id) return
      setJournalData(payload)
      setError('')
    } catch (err) {
      if (controller.signal.aborted || journalRequestRef.current.id !== id) return
      setError(err instanceof Error ? err.message : 'Could not connect to the backend')
    } finally {
      if (journalRequestRef.current.id === id) setJournalLoading(false)
    }
  }, [dashboardQuery])
  const loadTerminalEvents = useCallback(async (offset = 0, append = false) => {
    const params = new URLSearchParams({ severity: eventSeverity, log_kind: eventLogKind, limit: '200', offset: String(offset) })
    const bounds = eventDateBounds(eventDateRange)
    params.set('start', bounds.start)
    params.set('end', bounds.end)
    if (eventSourceId !== 'all') params.set('source_id', eventSourceId)
    if (eventSearch.trim()) params.set('search', eventSearch.trim())
    if (brokerAccounts) [...brokerAccounts].forEach(account => params.append('accounts', account))
    setTerminalEventsLoading(true)
    try {
      const payload = await api<TerminalEventsData>(`/api/terminal-events?${params}`)
      setTerminalEvents(current => append && current ? { ...payload, events: [...current.events, ...payload.events], offset: 0, limit: current.events.length + payload.events.length } : payload)
      setError('')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load MT5 events')
    } finally {
      setTerminalEventsLoading(false)
    }
  }, [brokerAccounts, eventDateRange, eventLogKind, eventSearch, eventSeverity, eventSourceId])
  const refreshTerminalEvents = useCallback(async () => {
    setTerminalEventsRefreshing(true)
    try {
      await api('/api/terminal-events/refresh', { method: 'POST' })
      await loadTerminalEvents()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not refresh MT5 events')
    } finally {
      setTerminalEventsRefreshing(false)
    }
  }, [loadTerminalEvents])
  function load() {
    if (tab === 'events') return loadTerminalEvents()
    if (tab === 'journal') return loadJournal()
    if (tab === 'performance') return Promise.all([loadDashboard(), loadJournal()])
    return loadDashboard()
  }
  async function handleDeleted() {
    setPanelStrategyId(null)
    await load()
    setSelectedId(null)
  }
  useEffect(() => {
    api<AccountAliasSettings>('/api/settings/account-aliases')
      .then(result => setAccountAliases(result.aliases))
      .catch(() => undefined)
  }, [])
  function handleNoteSaved(strategyId: number, note: string, noteUpdatedAt: string) {
    setData(current => current ? {
      ...current,
      strategies: current.strategies.map(strategy => strategy.id === strategyId
        ? { ...strategy, note, note_updated_at: noteUpdatedAt }
        : strategy),
    } : current)
  }
  function handleDisplayNameSaved(strategyId: number, displayName: string, source: string) {
    setData(current => current ? {
      ...current,
      strategies: current.strategies.map(strategy => strategy.id === strategyId
        ? { ...strategy, display_name: displayName, display_name_source: source }
        : strategy),
    } : current)
    void load()
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
  async function updateStrategyStatus(strategyId: number, status: StrategyStatus) {
    const previousStatus = data?.strategies.find(strategy => strategy.id === strategyId)?.strategy_status ?? ''
    setData(current => current ? {
      ...current,
      strategies: withStrategyStatus(current.strategies, strategyId, status),
    } : current)
    setStrategyStatusSaving(current => new Set(current).add(strategyId))
    try {
      await api(`/api/strategies/${strategyId}/status`, {
        method: 'PUT',
        body: JSON.stringify({ status }),
      })
      setError('')
    } catch (err) {
      setData(current => current ? {
        ...current,
        strategies: withStrategyStatus(current.strategies, strategyId, previousStatus),
      } : current)
      setError(err instanceof Error ? err.message : 'Could not save strategy status')
    } finally {
      setStrategyStatusSaving(current => {
        const next = new Set(current)
        next.delete(strategyId)
        return next
      })
    }
  }
  async function updateStrategyNoMmAcceptance(strategyId: number, accepted: boolean) {
    setData(current => current ? {
      ...current,
      strategies: current.strategies.map(strategy => strategy.id === strategyId
        ? { ...strategy, no_mm_accepted: accepted, no_mm_accepted_at: accepted ? new Date().toISOString() : null, backtest: { ...strategy.backtest, no_mm_accepted: accepted, no_mm_accepted_at: accepted ? new Date().toISOString() : null } }
        : strategy),
    } : current)
    setNoMmAcceptanceSaving(current => new Set(current).add(strategyId))
    try {
      const result = await api<{ strategy_id: number; no_mm_accepted: boolean; no_mm_accepted_at: string | null }>(`/api/strategies/${strategyId}/no-mm-acceptance`, {
        method: 'PUT',
        body: JSON.stringify({ accepted }),
      })
      setData(current => current ? {
        ...current,
        strategies: current.strategies.map(strategy => strategy.id === strategyId
          ? { ...strategy, no_mm_accepted: result.no_mm_accepted, no_mm_accepted_at: result.no_mm_accepted_at, backtest: { ...strategy.backtest, no_mm_accepted: result.no_mm_accepted, no_mm_accepted_at: result.no_mm_accepted_at } }
          : strategy),
      } : current)
      setError('')
    } catch (err) {
      setData(current => current ? {
        ...current,
        strategies: current.strategies.map(strategy => strategy.id === strategyId
          ? { ...strategy, no_mm_accepted: !accepted, no_mm_accepted_at: null, backtest: { ...strategy.backtest, no_mm_accepted: !accepted, no_mm_accepted_at: null } }
          : strategy),
      } : current)
      setError(err instanceof Error ? err.message : 'Could not save NoMM acceptance')
    } finally {
      setNoMmAcceptanceSaving(current => {
        const next = new Set(current)
        next.delete(strategyId)
        return next
      })
    }
  }
  async function updateStrategyBacktestRiskAcceptance(strategyId: number, runId: number, accepted: boolean) {
    setBacktestRiskAcceptanceSaving(current => new Set(current).add(strategyId))
    try {
      const result = await api<{ strategy_id: number; backtest_risk_accepted: boolean; backtest_risk_accepted_run_id: number | null; backtest_risk_accepted_at: string | null }>(`/api/strategies/${strategyId}/backtest-risk-acceptance`, {
        method: 'PUT',
        body: JSON.stringify({ accepted, run_id: runId }),
      })
      setData(current => current ? {
        ...current,
        strategies: current.strategies.map(strategy => strategy.id === strategyId
          ? {
              ...strategy,
              backtest_risk_alert: !result.backtest_risk_accepted && strategy.backtest.risk_audit?.status === 'risk_exceeded',
              backtest: {
                ...strategy.backtest,
                risk_alert: !result.backtest_risk_accepted && strategy.backtest.risk_audit?.status === 'risk_exceeded',
                risk_alert_accepted: result.backtest_risk_accepted,
                risk_alert_accepted_at: result.backtest_risk_accepted_at,
              },
            }
          : strategy),
      } : current)
      setError('')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not save manual backtest validation')
    } finally {
      setBacktestRiskAcceptanceSaving(current => {
        const next = new Set(current)
        next.delete(strategyId)
        return next
      })
    }
  }
  useEffect(() => {
    if (tab !== 'overview' && tab !== 'performance') return
    if (windowName === 'custom' && !(customStart && customEnd)) return
    const timer = window.setTimeout(() => { void loadDashboard() }, 200)
    return () => window.clearTimeout(timer)
  }, [tab, dashboardQuery, windowName, customStart, customEnd, loadDashboard])
  useEffect(() => {
    if (tab !== 'journal' && tab !== 'performance') return
    void loadJournal()
    const timer = window.setInterval(() => { void loadJournal() }, 60_000)
    return () => window.clearInterval(timer)
  }, [tab, loadJournal])
  useEffect(() => {
    if (tab !== 'events') return
    void loadTerminalEvents()
    const timer = window.setInterval(() => { void loadTerminalEvents() }, 60_000)
    return () => window.clearInterval(timer)
  }, [tab, loadTerminalEvents])
  useEffect(() => () => {
    dashboardRequestRef.current.controller?.abort()
    journalRequestRef.current.controller?.abort()
  }, [])
  const allStrategies = data?.strategies || []
  const brokerAccountOptions = data?.broker_accounts || []
  const assetOptions = data?.asset_options || []
  const strategyIds = useMemo(() => parseStrategyIds(strategyIdQuery), [strategyIdQuery])
  const activeAccountLabel = brokerAccounts === null
    ? 'Todas las cuentas activas'
    : brokerAccounts.size === 1
      ? ([...brokerAccounts][0] === UNASSIGNED_ACCOUNT ? 'Unassigned' : `Account ${[...brokerAccounts][0]}`)
      : `${brokerAccounts.size} broker accounts`
  const brokerFilteredStrategies = useMemo(
    () => filterStrategiesByIds(allStrategies, strategyIds),
    [allStrategies, strategyIds]
  )
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
  const filteredJournalData = useMemo(
    () => filterJournalByStrategyIds(journalData, strategyIds),
    [journalData, strategyIds]
  )
  const filtered = useMemo(() => brokerFilteredStrategies.filter(strategy => {
    const matchesText = `${strategy.symbol} ${strategy.display_name} ${strategy.sqx_name} ${strategy.mql5_name}`.toLowerCase().includes(query.toLowerCase())
    const normalizedMagic = magicQuery.trim()
    const matchesMagic = !normalizedMagic || [...strategy.magic_numbers, ...strategy.historical_magic_numbers].some(magic => String(magic).includes(normalizedMagic))
    return matchesText && matchesMagic
      && (showStopped || !strategy.selection)
      && matchesStrategyStatus(strategy, strategyStatusFilter)
      && (statusFilter === 'all' || strategy.state === statusFilter)
      && (linkFilter === 'all' || strategy.link_state === linkFilter)
      && matchesBacktestFilter(strategy.backtest, backtestStatusFilter)
  }), [brokerFilteredStrategies, query, magicQuery, showStopped, strategyStatusFilter, statusFilter, linkFilter, backtestStatusFilter])
  const visibleTotals = useMemo(() => ({
    strategies: filtered.length,
    active: filtered.filter(strategy => strategy.state === 'active').length,
    net_profit: filtered.reduce((total, strategy) => total + strategy.lifetime_metrics.net_profit, 0),
    floating_profit: filtered.reduce((total, strategy) => total + strategy.metrics.floating_profit, 0),
    trades: filtered.reduce((total, strategy) => total + strategy.lifetime_metrics.trades, 0),
    red: filtered.filter(strategy => strategy.health.status === 'red').length,
  }), [filtered])
  const sortedStrategies = useMemo(() => {
    if (!sort) return filtered
    return [...filtered].sort((a, b) => compareStrategies(a, b, sort.key, sort.direction))
  }, [filtered, sort])
  useEffect(() => {
    if (tab !== 'overview') return
    const idleCallbacks = new Set<number>()
    const observer = new IntersectionObserver(entries => {
      entries.forEach(entry => {
        if (!entry.isIntersecting) return
        const strategyId = Number((entry.target as HTMLElement).dataset.strategyId)
        if (!strategyId) return
        observer.unobserve(entry.target)
        const schedule = window.requestIdleCallback || ((callback: IdleRequestCallback) => window.setTimeout(callback, 1))
        const callbackId = schedule(() => {
          idleCallbacks.delete(callbackId)
          prefetchPanelHistory(strategyId, dashboardQuery, 'idle')
        })
        idleCallbacks.add(callbackId)
      })
    }, { root: overviewTableScrollRef.current, rootMargin: '120px 0px' })
    document.querySelectorAll<HTMLTableRowElement>('.overview-table tbody tr[data-strategy-id]')
      .forEach(row => observer.observe(row))
    return () => {
      observer.disconnect()
      idleCallbacks.forEach(callbackId => {
        if (window.cancelIdleCallback) window.cancelIdleCallback(callbackId)
        else window.clearTimeout(callbackId)
      })
    }
  }, [tab, sortedStrategies, dashboardQuery])
  useEffect(() => {
    const topScroll = overviewTopScrollRef.current
    const tableScroll = overviewTableScrollRef.current
    const table = overviewTableRef.current
    if (!topScroll || !tableScroll || !table) return
    const measure = () => {
      const width = table.scrollWidth
      if (width === overviewTableWidthRef.current) return
      overviewTableWidthRef.current = width
      topScroll.scrollLeft = tableScroll.scrollLeft
    }
    const observer = new ResizeObserver(measure)
    observer.observe(tableScroll)
    observer.observe(table)
    measure()
    return () => observer.disconnect()
  }, [sidebarCollapsed])
  useEffect(() => {
    if (tab !== 'overview' || !restoreOverviewViewRef.current || !overviewView) return
    const restore = requestAnimationFrame(() => {
      setQuery(overviewView.query)
      setStrategyIdQuery(overviewView.strategyIdQuery)
      setMagicQuery(overviewView.magicQuery)
      setStrategyStatusFilter(overviewView.strategyStatusFilter)
      setShowStopped(overviewView.showStopped)
      setStatusFilter(overviewView.statusFilter)
      setLinkFilter(overviewView.linkFilter)
      setBacktestStatusFilter(overviewView.backtestStatusFilter)
      setSort(overviewView.sort)
      if (overviewTopScrollRef.current) overviewTopScrollRef.current.scrollLeft = overviewView.tableScrollLeft
      if (overviewTableScrollRef.current) overviewTableScrollRef.current.scrollLeft = overviewView.tableScrollLeft
      window.scrollTo({ top: overviewView.pageScrollY })
      restoreOverviewViewRef.current = false
    })
    return () => cancelAnimationFrame(restore)
  }, [tab, overviewView])
  useEffect(() => {
    if (tab !== 'overview' || selectedId == null) return
    if (!sortedStrategies.some(strategy => strategy.id === selectedId)) setSelectedId(null)
  }, [tab, selectedId, sortedStrategies])
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
  function navigateTo(nextTab: Tab) {
    if (tab === 'overview' && nextTab !== 'overview') {
      setOverviewView({
        query,
        strategyIdQuery,
        magicQuery,
        strategyStatusFilter,
        showStopped,
        statusFilter,
        linkFilter,
        backtestStatusFilter,
        sort,
        pageScrollY: window.scrollY,
        tableScrollLeft: overviewTableScrollRef.current?.scrollLeft ?? overviewTopScrollRef.current?.scrollLeft ?? 0,
      })
      restoreOverviewViewRef.current = true
    }
    setTab(nextTab)
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
      ['overview','Overview',LayoutDashboard],['detail','Strategy',Activity],['journal','Journal analysis',BookOpen],['events','MT5 Events',TriangleAlert],['performance','Performance',TrendingUp],['chart','Chart',CandlestickChart],
      ['settings','Settings',SettingsIcon],
    ] as const).map(([key,label,Icon]) => <button key={key} className={tab === key ? 'active' : ''} onClick={() => navigateTo(key)}><Icon size={15}/>{label}</button>)}</nav><div className="sidebar-footer"><div className="pulse"/><div><strong>Local system</strong><small>Refresh every 5 min</small></div></div></aside>
    <main className={tab === 'overview' ? 'overview-main' : undefined}><header><div><span className="eyebrow">STRATEGY PORTFOLIO</span><h1>{tab === 'overview' ? 'Operational status' : tab === 'performance' ? 'Normalized performance' : tab === 'journal' ? 'Journal analysis' : tab === 'events' ? 'MT5 Events' : tab === 'detail' ? 'Strategy detail' : tab === 'chart' ? 'Market trades' : 'Settings'}</h1></div>{tab !== 'overview' && <div className="header-actions"><AccountFilter accounts={brokerAccountOptions} aliases={accountAliases} hasUnassigned={data?.has_unassigned_broker_account || false} selected={brokerAccounts} onChange={setBrokerAccounts}/>{tab !== 'events' && <><AssetFilter assets={assetOptions} selected={assets} onChange={setAssets}/><select value={windowName} onChange={e => setWindowName(e.target.value)}><option value="30d">30 days</option><option value="90d">90 days</option><option value="all">All</option><option value="custom">Custom</option></select>{windowName === 'custom' && <><input aria-label="Start date" type="date" value={customStart} onChange={e => setCustomStart(e.target.value)}/><input aria-label="End date" type="date" value={customEnd} onChange={e => setCustomEnd(e.target.value)}/></>}</>}<button className="button refresh icon-command" onClick={() => { void load() }}><RefreshCw size={14}/>Refresh</button></div>}</header>
      {error && <div className="error-banner">{error}</div>}
      {tab === 'overview' && <>
        <section className="terminal-strip">{data?.terminals.map(terminal => <div key={terminal.id}><HealthDot status={terminal.status === 'connected' ? 'green' : 'gray'}/><strong>{terminal.name}</strong><span>{terminal.status === 'connected' ? `Account ${terminal.account_login}` : 'Disconnected'}</span>{terminal.last_seen && <small>{new Date(terminal.last_seen).toLocaleString('en-US')}</small>}</div>)}</section>
        <details className="overview-summary"><summary><span>Resumen general</span><small>Métricas</small></summary><div className="overview-summary-content"><section className="metrics-grid"><MetricCard label="Realized P/L" value={money(t.net_profit)} detail={`${t.trades} closed trades`} tone={t.net_profit >= 0 ? 'positive' : 'negative'}/><MetricCard label="Floating P/L" value={money(t.floating_profit)} detail="Open positions"/><MetricCard label="Active strategies" value={`${t.active} / ${t.strategies}`} detail="Filtered strategies"/><MetricCard label="Critical alerts" value={`${t.red}`} detail="Red deviations" tone={t.red ? 'negative' : 'positive'}/></section>
        </div></details><section className="panel table-panel" aria-busy={dashboardLoading}>
          <div className="overview-sticky-header">
            <div className="panel-heading">
            <div><span className="eyebrow">MONITORING</span><h2>All bots</h2><p className="timeframe-coverage">Temporalidad: <strong>{data?.timeframe_coverage.terminal_profile ?? 0} perfil MT5</strong> · {data?.timeframe_coverage.sqx ?? 0} SQX · <strong className="missing">{data?.timeframe_coverage.missing ?? 0} sin evidencia</strong></p></div>
            <div className="filters"><button type="button" className={`button stopped-visibility-toggle ${showStopped ? 'active' : ''}`} aria-pressed={showStopped} aria-label={showStopped ? 'Ocultar bots stopped' : 'Mostrar bots stopped'} onClick={() => setShowStopped(current => !current)}>{showStopped ? <EyeOff size={14}/> : <Eye size={14}/>} {showStopped ? 'Ocultar stopped' : 'Mostrar stopped'}</button><select aria-label="Categoria manual" value={strategyStatusFilter} onChange={e => setStrategyStatusFilter(e.target.value as StrategyStatusFilter)}><option value="all">Todas las categorias</option><option value="live">★ Live</option><option value="monitor">☹ Revisar</option><option value="candidate_live">☺ Candidata live</option><option value="">Sin categoria</option></select><input aria-label="Search strategies" placeholder="Search strategy or symbol…" value={query} onChange={e => setQuery(e.target.value)}/><input className="magic-filter" aria-label="Search MN" placeholder="Search MN…" value={magicQuery} onChange={e => setMagicQuery(e.target.value.replace(/\D/g, ''))}/><input className="strategy-id-filter" aria-label="Filtrar por ID interno" placeholder="IDs internos: 12, 34…" inputMode="numeric" value={strategyIdQuery} onChange={e => setStrategyIdQuery(e.target.value.replace(/[^\d,\s]/g, ''))}/><AccountFilter compact accounts={brokerAccountOptions} aliases={accountAliases} hasUnassigned={data?.has_unassigned_broker_account || false} selected={brokerAccounts} onChange={setBrokerAccounts}/><AssetFilter compact assets={assetOptions} selected={assets} onChange={setAssets}/><select value={statusFilter} onChange={e => setStatusFilter(e.target.value)}><option value="all">All states</option>{Object.entries(stateLabels).map(([value,label]) => <option key={value} value={value}>{label}</option>)}</select><select aria-label="Backtest status or audit" value={backtestStatusFilter} onChange={e => setBacktestStatusFilter(e.target.value as BacktestFilter)}><option value="all">All backtests</option><optgroup label="Backtest status">{Object.entries(backtestLabels).map(([value,label]) => <option key={value} value={value}>{label}</option>)}</optgroup><optgroup label="Risk audit">{Object.entries(backtestAuditFilterLabels).map(([value,label]) => <option key={value} value={value}>{label}</option>)}</optgroup></select><select value={linkFilter} onChange={e => setLinkFilter(e.target.value)}><option value="all">All sources</option>{Object.entries(linkLabels).map(([value,label]) => <option key={value} value={value}>{label}</option>)}</select></div>
            <div className="overview-global-filters"><select aria-label="Time range" value={windowName} onChange={e => setWindowName(e.target.value)}><option value="30d">30 days</option><option value="90d">90 days</option><option value="all">All</option><option value="custom">Custom</option></select>{windowName === 'custom' && <><input aria-label="Start date" type="date" value={customStart} onChange={e => setCustomStart(e.target.value)}/><input aria-label="End date" type="date" value={customEnd} onChange={e => setCustomEnd(e.target.value)}/></>}<button className="button refresh icon-command" onClick={() => { void loadDashboard() }} disabled={dashboardLoading}><RefreshCw size={14}/>Refresh</button>{dashboardLoading && <span className="overview-loading" role="status" aria-live="polite">Actualizando…</span>}</div>
          </div>
          <div ref={overviewTopScrollRef} className="table-scroll-top" aria-label="Scrollable table columns" tabIndex={0} onScroll={() => syncOverviewScroll('top')}>
            <table className="overview-table">
              <OverviewTableColGroup />
              <thead><OverviewTableHead sort={sort} onSort={changeSort} months={data?.monthly_net_profit_months ?? []}/></thead>
            </table>
          </div>
          </div>
          <div ref={overviewTableScrollRef} className="table-scroll" aria-label="Scrollable strategy table" tabIndex={0} onScroll={() => syncOverviewScroll('table')}>
            <table ref={overviewTableRef} className="overview-table">
              <OverviewTableColGroup />
              <tbody>{sortedStrategies.map(strategy => {
                const p = performanceMetrics(strategy)
                const alias = accountAlias(accountAliases, strategy.account_login)
                const stateLabel = stateLabels[strategy.state] || strategy.state
                const healthTitle = strategy.health.reasons.join(', ') || 'No active alerts'
                return <tr key={strategy.id} data-strategy-id={strategy.id} tabIndex={0} className={`${selectedId === strategy.id ? 'selected-row ' : ''}${strategy.selection ? 'stopped-row' : ''}`} onPointerEnter={() => prefetchPanelHistory(strategy.id, dashboardQuery, 'hover')} onFocus={() => prefetchPanelHistory(strategy.id, dashboardQuery, 'hover')} onClick={() => { setSelectedId(strategy.id); setPanelStrategyId(strategy.id) }}>
                  <td>{strategy.id}</td>
                  <td className="selection-column"><input type="checkbox" aria-label={`Mark ${displayName(strategy)} as stopped`} checked={strategy.selection} disabled={selectionSaving.has(strategy.id)} onClick={event => event.stopPropagation()} onChange={event => updateStrategySelection(strategy.id, event.target.checked)}/></td>
                  <td className="backtest-cell"><BacktestBadge summary={strategy.backtest}/></td>
                  <td title={healthTitle}><span className={`health-badge ${strategy.health.status}`}><HealthDot status={strategy.health.status}/>{healthLabel(strategy.health.status)}</span></td>
                  <td className="equity-curve-cell"><RecentEquitySparkline points={strategy.recent_equity_curve}/></td>
                  <td><strong className="overview-strategy-name">{strategy.strategy_status && <span className={`strategy-status-indicator ${strategy.strategy_status}`} title={strategyStatusLabels[strategy.strategy_status]}><StrategyStatusIcon status={strategy.strategy_status} size={15} /></span>}{displayName(strategy)}<span className={`strategy-timeframe ${strategy.timeframe_source}`} title={timeframeSourceLabels[strategy.timeframe_source]}> · {strategy.timeframe || 'TF pendiente'}</span></strong><span className={`selected-row-indicator ${selectedId === strategy.id ? 'visible' : ''}`}>Selected</span><small>{strategy.sqx_name}</small></td>
                  <td className="overview-symbol-column" title={strategy.symbol || '—'}>{strategy.symbol || '—'}</td>
                  <td>{strategy.account_login ? <><strong>{strategy.account_login}</strong>{alias && <small>{alias}</small>}</> : '—'}</td>
                  <td className="magic-numbers">{primaryMagicLabel(strategy)}</td>
                  <td className={(strategy.account_pl_percent ?? 0) >= 0 ? 'value-good' : 'value-bad'}>{signedPercent(strategy.account_pl_percent)}</td><td className={(strategy.initial_capital_pl_percent ?? 0) >= 0 ? 'value-good' : 'value-bad'}>{signedPercent(strategy.initial_capital_pl_percent)}</td>
                  <td className={strategy.metrics.today_profit >= 0 ? 'value-good' : 'value-bad'}>{money(strategy.metrics.today_profit)}</td><td>{(p.win_rate * 100).toFixed(1)}%</td><td>{p.trades}</td><td>{p.profit_factor == null ? '—' : p.profit_factor.toFixed(2)}</td><td>{number(p.sqn, 2)}</td><td>{number(p.return_dd, 2)}</td>
                  <td><SQXAnalyticsBadge strategy={strategy} kind="edge"/></td><td><SQXAnalyticsBadge strategy={strategy} kind="egt"/></td><td><SQXAnalyticsBadge strategy={strategy} kind="doctor"/></td>
                  <td className={p.net_profit >= 0 ? 'value-good' : 'value-bad'}>{money(p.net_profit)}</td><td>{money(p.avg_win)} / {money(p.avg_loss)}</td>
                  {strategy.monthly_net_profit.map(month => <td key={month.key} className={month.net_profit >= 0 ? 'value-good' : 'value-bad'}>{money(month.net_profit)}</td>)}
                  <td className={(p.best_trade || 0) >= 0 ? 'value-good' : 'value-bad'}>{money(p.best_trade)}</td>
                  <td>{money(p.max_drawdown)}</td>
                  <td className={p.expectancy >= 0 ? 'value-good' : 'value-bad'}>{money(p.expectancy)}</td>
                  <td className={strategy.metrics.floating_profit >= 0 ? 'value-good' : 'value-bad'}>{signedMoney(strategy.metrics.floating_profit)}</td>
                  <td><span className={`link-tag ${strategy.link_state}`}>{linkLabels[strategy.link_state] || strategy.link_state}</span>{strategy.sqx?.missing_from_sqx_at && <small className="sqx-missing">MISSING FROM SQX</small>}</td>
                  <td className="overview-state-column"><span className={`state-tag ${strategy.state}`}><span className="state-tag-label" title={stateLabel}>{stateLabel}</span></span>{strategy.deployment_state && <span className={`deployment-badge ${strategy.deployment_state}`} title={strategy.last_inventory_at ? `Último inventario: ${new Date(strategy.last_inventory_at).toLocaleString()}` : ''}>{deploymentLabels[strategy.deployment_state]}</span>} {strategy.deployment_state && <span className={`activity-badge ${strategy.activity_state}`}>{activityLabels[strategy.activity_state]}</span>}</td>
                  <td className="row-action">›</td>
                </tr>
              })}</tbody>
            </table>
          </div>
        </section>
      </>}
      {tab === 'overview' && panelStrategy && <StrategySidePanel strategy={panelStrategy} query={dashboardQuery} refreshVersion={historyRefreshVersion} onClose={() => setPanelStrategyId(null)} onDeleted={handleDeleted} onNoteSaved={handleNoteSaved} onDisplayNameSaved={handleDisplayNameSaved} onStatusChange={updateStrategyStatus} statusSaving={strategyStatusSaving.has(panelStrategy.id)} onOpenDetail={strategyId => { setSelectedId(strategyId); setPanelStrategyId(null); navigateTo('detail') }} onBacktestRiskAcceptance={updateStrategyBacktestRiskAcceptance} savingBacktestRiskAcceptance={backtestRiskAcceptanceSaving.has(panelStrategy.id)} />}
      {tab === 'performance' && <Performance
        strategies={brokerFilteredStrategies}
        onSelect={strategyId => { setSelectedId(strategyId); navigateTo('detail') }}
        journalData={filteredJournalData}
        accountLabel={activeAccountLabel}
      />}
      {tab === 'journal' && (journalLoading && !filteredJournalData ? <div className="empty-state" role="status">Cargando análisis…</div> : <JournalAnalysis data={filteredJournalData} capitalEligible={strategyIds.size === 0 && assets === null}/>)}
      {tab === 'events' && <TerminalEventsPanel data={terminalEvents} loading={terminalEventsLoading} severity={eventSeverity} sourceId={eventSourceId} logKind={eventLogKind} search={eventSearch} dateRange={eventDateRange} onSeverity={setEventSeverity} onSourceId={setEventSourceId} onLogKind={setEventLogKind} onSearch={setEventSearch} onDateRange={setEventDateRange} onRefresh={() => { void refreshTerminalEvents() }} refreshing={terminalEventsRefreshing} onLoadMore={() => { if (terminalEvents) void loadTerminalEvents(terminalEvents.offset + terminalEvents.events.length, true) }} />}
      {tab === 'detail' && <><div className="strategy-selector"><label>Strategy</label><select value={selectedId || ''} disabled={!brokerFilteredStrategies.length} onChange={e => setSelectedId(Number(e.target.value))}>{brokerFilteredStrategies.map(strategy => <option key={strategy.id} value={strategy.id}>{strategy.symbol} — {displayName(strategy)}</option>)}</select></div>{selected ? <StrategyDetail strategy={selected} onDeleted={handleDeleted} onNoteSaved={handleNoteSaved} onNoMmAcceptance={updateStrategyNoMmAcceptance} savingNoMmAcceptance={noMmAcceptanceSaving.has(selected.id)} /> : <div className="empty-state">No strategy selected for this broker account.</div>}</>}
      {tab === 'chart' && <><div className="strategy-selector"><label>Strategy</label><select value={selectedId || ''} disabled={!brokerFilteredStrategies.length} onChange={e => setSelectedId(Number(e.target.value))}>{brokerFilteredStrategies.map(strategy => <option key={strategy.id} value={strategy.id}>{strategy.symbol} — {displayName(strategy)}</option>)}</select></div>{selected ? <ChartPanel strategyId={selected.id} accountQuery={dashboardQuery}/> : <div className="empty-state">Select a strategy for the selected broker accounts.</div>}</>}
      {tab === 'settings' && <Settings reload={load} aliases={accountAliases} onAliasesSaved={setAccountAliases}/>}<footer>EA Observatory · Local data · {data ? `Updated ${new Date(data.generated_at).toLocaleTimeString('en-US')}` : 'Connecting…'}</footer>
    </main>
  </div>
}
