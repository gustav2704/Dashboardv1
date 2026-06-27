import { useEffect, useMemo, useState } from 'react'
import { api } from './api'
import ChartPanel from './ChartPanel'
import type { Baseline, Dashboard, Metrics, Strategy, StrategyDetails } from './types'

type Tab = 'overview' | 'detail' | 'chart' | 'settings'
type SortKey = 'state' | 'strategy' | 'symbol' | 'account' | 'magic' | 'net_profit' | 'trades' | 'win_rate' | 'profit_factor' | 'max_drawdown' | 'avg_wl' | 'best_trade' | 'today_profit'
type SortDirection = 'ascending' | 'descending'
type SortState = { key: SortKey; direction: SortDirection } | null
const SIDEBAR_STORAGE_KEY = 'dashboardv1:sidebar-collapsed'

const stateLabels: Record<string, string> = {
  active: 'Active', no_recent_trades: 'No recent trades', unlinked: 'Unlinked',
  retired: 'Retired', terminal_disconnected: 'Terminal disconnected',
}
const linkLabels: Record<string, string> = {
  linked: 'Linked', candidate: 'Candidate', sqx_only: 'SQX only',
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
    const parsed = Number(value)
    if (value !== undefined && value !== '' && Number.isFinite(parsed)) return parsed
  }
  return null
}

function Logo() { return <div className="logo"><div className="logo-mark"><span /><span /><span /></div><div><strong>EA Observatory</strong><small>LIVE VS. BACKTEST</small></div></div> }
function HealthDot({ status }: { status: string }) { return <span className={`health-dot ${status}`} aria-label={status} /> }

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
    case 'strategy': return strategy.mql5_name || strategy.sqx_name
    case 'symbol': return strategy.symbol || null
    case 'account': return strategy.account_login || null
    case 'magic': return strategy.magic_numbers?.[0] ?? null
    case 'net_profit': return strategy.metrics.net_profit
    case 'trades': return strategy.metrics.trades
    case 'win_rate': return strategy.metrics.win_rate
    case 'profit_factor': return strategy.metrics.profit_factor
    case 'max_drawdown': return strategy.metrics.max_drawdown
    case 'avg_wl': return strategy.metrics.avg_loss < 0 ? strategy.metrics.avg_win / Math.abs(strategy.metrics.avg_loss) : strategy.metrics.avg_win || null
    case 'best_trade': return strategy.metrics.best_trade
    case 'today_profit': return strategy.metrics.today_profit
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

function StrategyDetail({ strategy }: { strategy: Strategy }) {
  const m = strategy.metrics
  const b = strategy.baseline
  return <div className="detail-grid">
    <section className="panel strategy-hero">
      <div className="strategy-title"><div><span className="symbol-pill">{strategy.symbol}</span><h2>{strategy.mql5_name || strategy.sqx_name}</h2><p>{strategy.sqx_name}{strategy.sqx ? ` · ${strategy.sqx.project} / ${strategy.sqx.databank} · ${strategy.sqx.timeframe}` : ''}</p></div><div className={`health-badge ${strategy.health.status}`}><HealthDot status={strategy.health.status}/>{healthLabel(strategy.health.status)}</div></div>
      <div className="hero-stats"><div><span>Realized P/L</span><strong>{money(m.net_profit)}</strong></div><div><span>Floating P/L</span><strong>{money(m.floating_profit)}</strong></div><div><span>Trades</span><strong>{m.trades}</strong></div><div><span>Positions</span><strong>{m.open_positions}</strong></div></div>
      <div className="reason-list">{strategy.health.reasons.map(reason => <span key={reason}>{reason}</span>)}{!strategy.health.reasons.length && <span>No active alerts</span>}</div>
    </section>
    <section className="panel comparison-panel">
      <div className="panel-heading"><div><span className="eyebrow">BEHAVIOR</span><h2>Current vs. {b?.sample_type?.toUpperCase() || 'SQX'}</h2></div><span className="source-tag">{b ? `${b.source} · ${new Date(b.synced_at).toLocaleDateString('en-US')}` : 'No baseline'}</span></div>
      <div className="comparison-head"><span>KPI</span><span>Current</span><span>Backtest</span><span>Δ</span></div>
      <Comparison label="Profit factor" current={m.profit_factor} baseline={baselineValue(b, 'ProfitFactor')} />
      <Comparison label="Expectancy" current={m.expectancy} baseline={baselineValue(b, 'Expectancy')} format={money} />
      <Comparison label="Return / DD" current={m.return_dd} baseline={baselineValue(b, 'ReturnDDRatio', 'RetDD')} />
      <Comparison label="SQN" current={m.sqn} baseline={baselineValue(b, 'SQN')} />
      <Comparison label="Trades / month" current={m.trades_per_month} baseline={baselineValue(b, 'AvgTradesPerMonth')} />
      <Comparison label="Max drawdown" current={m.max_drawdown} baseline={baselineValue(b, 'MaxDD', 'Drawdown')} format={money} />
    </section>
    <section className="panel compact-metrics"><div><span>Win rate</span><strong>{(m.win_rate * 100).toFixed(1)}%</strong></div><div><span>Average duration</span><strong>{duration(m.avg_duration_seconds)}</strong></div><div><span>Loss streak</span><strong>{m.max_consecutive_losses}</strong></div><div><span>Average win</span><strong>{money(m.avg_win)}</strong></div><div><span>Average loss</span><strong>{money(m.avg_loss)}</strong></div><div><span>Commission + swap</span><strong>{money(m.commissions + m.swaps)}</strong></div></section>
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

function StrategySidePanel({ strategy, query, onClose }: { strategy: Strategy; query: string; onClose: () => void }) {
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
  const history = detail ? [...detail.trades].reverse().slice(0, 80) : []
  return <aside className="strategy-drawer" aria-label="Quick strategy statistics">
    <div className="drawer-header">
      <div><strong>{active.mql5_name || active.sqx_name}</strong><span>{active.symbol || 'No symbol'} · {m.trades} trades in history</span></div>
      <button className="drawer-close" type="button" onClick={onClose}>Close</button>
    </div>
    <div className="drawer-body">
      {error && <div className="drawer-error">{error}</div>}
      {!detail && !error && <div className="drawer-loading">Loading details…</div>}
      <div className="drawer-stat-grid">
        <DrawerMetric label="Total net P/L" value={signedMoney(m.net_profit)} detail={`${m.trades} trades | Exp ${money(m.expectancy)}`} tone={m.net_profit >= 0 ? 'positive' : 'negative'} />
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
      </div>
      <section className="drawer-section">
        <div className="drawer-section-title"><span className="eyebrow">EQUITY CURVE</span></div>
        <EquitySparkline points={detail?.equity_curve || []} />
      </section>
      <section className="drawer-section drawer-history">
        <div className="drawer-section-title"><span className="eyebrow">HISTORY ({detail?.trades.length || 0})</span></div>
        {history.length ? <table className="drawer-table"><thead><tr><th>Close</th><th>Sym.</th><th>Dir.</th><th>Lots</th><th>Net P/L</th><th>Dur.</th></tr></thead><tbody>{history.map(trade => <tr key={`${trade.terminal_id}-${trade.deal_ticket}`}><td>{new Date(trade.close_time_msc).toLocaleString('en-US', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })}</td><td>{trade.symbol}</td><td>{directionLabel(trade.direction)}</td><td>{trade.volume.toFixed(2)}</td><td className={trade.net_profit >= 0 ? 'value-good' : 'value-bad'}>{signedMoney(trade.net_profit)}</td><td>{tradeDuration(trade.open_time_msc, trade.close_time_msc)}</td></tr>)}</tbody></table> : <div className="drawer-empty">{detail ? 'No history for this window.' : 'Loading history…'}</div>}
      </section>
    </div>
  </aside>
}

function Settings({ reload }: { reload: () => void }) {
  const [dataDir, setDataDir] = useState('')
  const [terminalName, setTerminalName] = useState('New terminal')
  const [project, setProject] = useState('Retester')
  const [databank, setDatabank] = useState('Results')
  const [sqxStatus, setSqxStatus] = useState('Checking SQX connection...')
  const [sqxDatabanks, setSqxDatabanks] = useState<Record<string, Array<{name:string;records:number;view:string}>>>({})
  const [notice, setNotice] = useState('')
  const [mappingSuggestions, setMappingSuggestions] = useState<Array<{terminal_id:number;account_login:string;symbol:string;magic:number;comment:string;deal_count:number;candidates:Array<{strategy_id:number;name:string;score:number}>}>>([])
  const [mappingChoices, setMappingChoices] = useState<Record<number, number>>({})
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
      const result = await api<{ received:number; imported:number; matched:number; created:number; unmatched:number; passed:number }>('/api/sqx/sync', { method: 'POST', body: JSON.stringify({ project, databank }) })
      setNotice(`SQX: ${result.imported}/${result.received} imported · ${result.matched} linked · ${result.created} created · ${result.passed} passed · ${result.unmatched} unmatched.`)
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
  async function saveRules() {
    await api('/api/alerts', { method: 'PUT', body: JSON.stringify(rules) })
    setNotice('Global thresholds saved.'); reload()
  }
  return <div className="settings-grid">
    <section className="panel"><span className="eyebrow">MT5 SOURCES</span><h2>Register terminal</h2><p className="help">Each terminal needs its DataDir and the DashboardBridge service running.</p><label>Name<input value={terminalName} onChange={e => setTerminalName(e.target.value)} /></label><label>DataDir<input placeholder="C:\Users\…\MetaQuotes\Terminal\HASH" value={dataDir} onChange={e => setDataDir(e.target.value)} /></label><button className="button primary" onClick={addTerminal}>Save terminal</button></section>
    <section className="panel"><span className="eyebrow">HISTORY</span><h2>Sync SQX</h2><p className="help">{sqxStatus}. Previous snapshots remain available after SQX closes.</p><label>Project<select value={project} onChange={e => { setProject(e.target.value); setDatabank(sqxDatabanks[e.target.value]?.[0]?.name || '') }}>{Object.keys(sqxDatabanks).length ? Object.keys(sqxDatabanks).map(value => <option key={value} value={value}>{value}</option>) : <option value={project}>{project}</option>}</select></label><label>Databank<select value={databank} onChange={e => setDatabank(e.target.value)}>{sqxDatabanks[project]?.length ? sqxDatabanks[project].map(value => <option key={value.name} value={value.name}>{value.name} · {value.records} strategies</option>) : <option value={databank}>{databank}</option>}</select></label><button className="button primary" onClick={syncSqx}>Read-only sync</button></section>
    <section className="panel settings-wide"><span className="eyebrow">CATALOG AND MAPPINGS</span><h2>Maintenance</h2><div className="maintenance-actions"><button className="button" onClick={async () => { await api('/api/catalog/import', { method: 'POST' }); setNotice('Catalog reloaded.'); reload() }}>Reload Track_v1.xlsx</button><a className="button export-button" href="/api/catalog/export">Exportar Excel</a><button className="button primary" onClick={autoConfirmMappings}>Link safe matches</button><button className="button" onClick={loadMappings}>Find suggested links</button></div>
      {mappingSuggestions.length > 0 && <div className="mapping-review"><div className="mapping-review-head"><span>Observed trade</span><span>Proposed strategy</span><span>Confidence</span><span /></div>{mappingSuggestions.map((item,index) => <div className="mapping-review-row" key={`${item.terminal_id}-${item.magic}-${item.symbol}-${item.comment}`}><div><strong>{item.symbol} · magic {item.magic}</strong><small>{item.comment || 'No comment'} · {item.deal_count} deals</small></div><select value={mappingChoices[index] || ''} onChange={event => setMappingChoices({...mappingChoices,[index]:Number(event.target.value)})}><option value="">Select…</option>{item.candidates.map(candidate => <option key={candidate.strategy_id} value={candidate.strategy_id}>{candidate.name}</option>)}</select><span>{item.candidates.find(candidate => candidate.strategy_id === mappingChoices[index])?.score ? `${Math.round((item.candidates.find(candidate => candidate.strategy_id === mappingChoices[index])?.score || 0) * 100)}%` : '—'}</span><button className="button" onClick={() => confirmSuggestion(index)}>Confirm</button></div>)}</div>}
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
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [panelStrategyId, setPanelStrategyId] = useState<number | null>(null)
  const [query, setQuery] = useState('')
  const [magicQuery, setMagicQuery] = useState('')
  const [statusFilter, setStatusFilter] = useState('all')
  const [linkFilter, setLinkFilter] = useState('all')
  const [sort, setSort] = useState<SortState>(null)
  const [error, setError] = useState('')
  const [sidebarCollapsed, setSidebarCollapsed] = useState(readSidebarCollapsed)
  const dashboardQuery = useMemo(() => buildDashboardQuery(windowName, customStart, customEnd), [windowName, customStart, customEnd])
  async function load() {
    try { const payload = await api<Dashboard>(`/api/dashboard?${dashboardQuery}`); setData(payload); setSelectedId(current => current ?? payload.strategies[0]?.id ?? null); setError('') }
    catch (err) { setError(err instanceof Error ? err.message : 'Could not connect to the backend') }
  }
  useEffect(() => { if (windowName !== 'custom' || (customStart && customEnd)) load(); const timer = setInterval(load, 300_000); return () => clearInterval(timer) }, [windowName, customStart, customEnd])
  const selected = data?.strategies.find(strategy => strategy.id === selectedId) || null
  const panelStrategy = data?.strategies.find(strategy => strategy.id === panelStrategyId) || null
  const filtered = useMemo(() => (data?.strategies || []).filter(strategy => {
    const matchesText = `${strategy.symbol} ${strategy.sqx_name} ${strategy.mql5_name}`.toLowerCase().includes(query.toLowerCase())
    const normalizedMagic = magicQuery.trim()
    const matchesMagic = !normalizedMagic || strategy.magic_numbers?.some(magic => String(magic).includes(normalizedMagic))
    return matchesText && matchesMagic
      && (statusFilter === 'all' || strategy.state === statusFilter)
      && (linkFilter === 'all' || strategy.link_state === linkFilter)
  }), [data, query, magicQuery, statusFilter, linkFilter])
  const sortedStrategies = useMemo(() => {
    if (!sort) return filtered
    return [...filtered].sort((a, b) => compareStrategies(a, b, sort.key, sort.direction))
  }, [filtered, sort])
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
  const t = data?.totals
  return <div className={sidebarCollapsed ? 'shell collapsed-sidebar' : 'shell'}>
    <button className="sidebar-toggle" type="button" aria-label={sidebarCollapsed ? 'Show sidebar' : 'Hide sidebar'} onClick={toggleSidebar}>{sidebarCollapsed ? '›' : '‹'}</button>
    <aside className="sidebar"><Logo/><nav>{([['overview','Overview'],['detail','Strategy'],['chart','Chart'],['settings','Settings']] as [Tab,string][]).map(([key,label]) => <button key={key} className={tab === key ? 'active' : ''} onClick={() => setTab(key)}><span>{key === 'overview' ? '⌁' : key === 'detail' ? '◎' : key === 'chart' ? '⌗' : '⚙'}</span>{label}</button>)}</nav><div className="sidebar-footer"><div className="pulse"/><div><strong>Local system</strong><small>Refresh every 5 min</small></div></div></aside>
    <main><header><div><span className="eyebrow">STRATEGY PORTFOLIO</span><h1>{tab === 'overview' ? 'Operational status' : tab === 'detail' ? 'Strategy detail' : tab === 'chart' ? 'Market trades' : 'Settings'}</h1></div><div className="header-actions"><select value={windowName} onChange={e => setWindowName(e.target.value)}><option value="30d">30 days</option><option value="90d">90 days</option><option value="all">All</option><option value="custom">Custom</option></select>{windowName === 'custom' && <><input aria-label="Start date" type="date" value={customStart} onChange={e => setCustomStart(e.target.value)}/><input aria-label="End date" type="date" value={customEnd} onChange={e => setCustomEnd(e.target.value)}/></>}<button className="button refresh" onClick={load}>↻ Refresh</button></div></header>
      {error && <div className="error-banner">{error}</div>}
      {tab === 'overview' && <>
        <section className="metrics-grid"><MetricCard label="Realized P/L" value={money(t?.net_profit)} detail={`${t?.trades || 0} closed trades`} tone={(t?.net_profit || 0) >= 0 ? 'positive' : 'negative'}/><MetricCard label="Floating P/L" value={money(t?.floating_profit)} detail="Open positions"/><MetricCard label="Active strategies" value={`${t?.active || 0} / ${t?.strategies || 0}`} detail="Full catalog"/><MetricCard label="Critical alerts" value={`${t?.red || 0}`} detail="Red deviations" tone={t?.red ? 'negative' : 'positive'}/></section>
        <section className="terminal-strip">{data?.terminals.map(terminal => <div key={terminal.id}><HealthDot status={terminal.status === 'connected' ? 'green' : 'gray'}/><strong>{terminal.name}</strong><span>{terminal.status === 'connected' ? `Account ${terminal.account_login}` : 'Disconnected'}</span>{terminal.last_seen && <small>{new Date(terminal.last_seen).toLocaleString('en-US')}</small>}</div>)}</section>
        <section className="panel table-panel">
          <div className="panel-heading">
            <div><span className="eyebrow">MONITORING</span><h2>All bots</h2></div>
            <div className="filters"><input aria-label="Search strategies" placeholder="Search strategy or symbol…" value={query} onChange={e => setQuery(e.target.value)}/><input className="magic-filter" aria-label="Search MN" placeholder="Search MN…" value={magicQuery} onChange={e => setMagicQuery(e.target.value.replace(/\D/g, ''))}/><select value={statusFilter} onChange={e => setStatusFilter(e.target.value)}><option value="all">All states</option>{Object.entries(stateLabels).map(([value,label]) => <option key={value} value={value}>{label}</option>)}</select><select value={linkFilter} onChange={e => setLinkFilter(e.target.value)}><option value="all">All sources</option>{Object.entries(linkLabels).map(([value,label]) => <option key={value} value={value}>{label}</option>)}</select></div>
          </div>
          <div className="table-scroll">
            <table>
              <thead><tr><SortableHeader label="State" sortKey="state" sort={sort} onSort={changeSort}/><th>Source</th><SortableHeader label="Strategy" sortKey="strategy" sort={sort} onSort={changeSort}/><SortableHeader label="Symbol" sortKey="symbol" sort={sort} onSort={changeSort}/><SortableHeader label="Account" sortKey="account" sort={sort} onSort={changeSort}/><SortableHeader label="Magic" sortKey="magic" sort={sort} onSort={changeSort}/><SortableHeader label="Net P/L" sortKey="net_profit" sort={sort} onSort={changeSort}/><SortableHeader label="Trades" sortKey="trades" sort={sort} onSort={changeSort}/><SortableHeader label="Win %" sortKey="win_rate" sort={sort} onSort={changeSort}/><SortableHeader label="Avg W/L" sortKey="avg_wl" sort={sort} onSort={changeSort}/><SortableHeader label="Best trade" sortKey="best_trade" sort={sort} onSort={changeSort}/><SortableHeader label="P/L today" sortKey="today_profit" sort={sort} onSort={changeSort}/><SortableHeader label="Factor P" sortKey="profit_factor" sort={sort} onSort={changeSort}/><SortableHeader label="Max DD" sortKey="max_drawdown" sort={sort} onSort={changeSort}/><th aria-label="Open panel"></th></tr></thead>
              <tbody>{sortedStrategies.map(strategy => <tr key={strategy.id} className={panelStrategyId === strategy.id ? 'selected-row' : ''} onClick={() => { setSelectedId(strategy.id); setPanelStrategyId(strategy.id) }}><td><span className={`state-tag ${strategy.state}`}><HealthDot status={strategy.health.status}/>{stateLabels[strategy.state] || strategy.state}</span></td><td><span className={`link-tag ${strategy.link_state}`}>{linkLabels[strategy.link_state] || strategy.link_state}</span>{strategy.sqx?.filter_result === 'PASSED' && <small className="sqx-passed">PASSED</small>}</td><td><strong>{strategy.mql5_name || strategy.sqx_name}</strong><small>{strategy.sqx_name}</small></td><td>{strategy.symbol || '—'}</td><td>{strategy.account_login || '—'}</td><td className="magic-numbers">{strategy.magic_numbers?.length ? strategy.magic_numbers.join(', ') : '—'}</td><td className={strategy.metrics.net_profit >= 0 ? 'value-good' : 'value-bad'}>{money(strategy.metrics.net_profit)}</td><td>{strategy.metrics.trades}</td><td>{(strategy.metrics.win_rate * 100).toFixed(1)}%</td><td>{money(strategy.metrics.avg_win)} / {money(strategy.metrics.avg_loss)}</td><td className={(strategy.metrics.best_trade || 0) >= 0 ? 'value-good' : 'value-bad'}>{money(strategy.metrics.best_trade)}</td><td className={strategy.metrics.today_profit >= 0 ? 'value-good' : 'value-bad'}>{money(strategy.metrics.today_profit)}</td><td>{number(strategy.metrics.profit_factor)}</td><td>{money(strategy.metrics.max_drawdown)}</td><td className="row-action">›</td></tr>)}</tbody>
            </table>
          </div>
        </section>
      </>}
      {tab === 'overview' && panelStrategy && <StrategySidePanel strategy={panelStrategy} query={dashboardQuery} onClose={() => setPanelStrategyId(null)} />}
      {tab === 'detail' && <><div className="strategy-selector"><label>Strategy</label><select value={selectedId || ''} onChange={e => setSelectedId(Number(e.target.value))}>{data?.strategies.map(strategy => <option key={strategy.id} value={strategy.id}>{strategy.symbol} — {strategy.mql5_name || strategy.sqx_name}</option>)}</select></div>{selected ? <StrategyDetail strategy={selected}/> : <div className="empty-state">No strategies in the catalog.</div>}</>}
      {tab === 'chart' && <><div className="strategy-selector"><label>Strategy</label><select value={selectedId || ''} onChange={e => setSelectedId(Number(e.target.value))}>{data?.strategies.map(strategy => <option key={strategy.id} value={strategy.id}>{strategy.symbol} — {strategy.mql5_name || strategy.sqx_name}</option>)}</select></div>{selectedId ? <ChartPanel strategyId={selectedId}/> : <div className="empty-state">Select a strategy.</div>}</>}
      {tab === 'settings' && <Settings reload={load}/>}<footer>EA Observatory · Local data · {data ? `Updated ${new Date(data.generated_at).toLocaleTimeString('en-US')}` : 'Connecting…'}</footer>
    </main>
  </div>
}
