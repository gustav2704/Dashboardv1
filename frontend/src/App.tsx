import { useEffect, useMemo, useState } from 'react'
import { api } from './api'
import ChartPanel from './ChartPanel'
import type { Baseline, Dashboard, Metrics, Strategy } from './types'

type Tab = 'overview' | 'detail' | 'chart' | 'settings'
type SortKey = 'state' | 'strategy' | 'account' | 'magic' | 'net_profit' | 'trades' | 'win_rate' | 'profit_factor' | 'max_drawdown'
type SortDirection = 'ascending' | 'descending'
type SortState = { key: SortKey; direction: SortDirection } | null

const stateLabels: Record<string, string> = {
  active: 'Activa', no_recent_trades: 'Sin trades recientes', unlinked: 'No vinculada',
  retired: 'Retirada', terminal_disconnected: 'Terminal desconectada',
}

function money(value: number | null | undefined) {
  return value == null ? '—' : new Intl.NumberFormat('es-MX', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(value)
}
function number(value: number | null | undefined, digits = 2) { return value == null ? '—' : value.toFixed(digits) }
function duration(seconds: number) {
  if (!seconds) return '—'
  const hours = seconds / 3600
  return hours < 24 ? `${hours.toFixed(1)} h` : `${(hours / 24).toFixed(1)} d`
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
    case 'account': return strategy.account_login || null
    case 'magic': return strategy.magic_numbers?.[0] ?? null
    case 'net_profit': return strategy.metrics.net_profit
    case 'trades': return strategy.metrics.trades
    case 'win_rate': return strategy.metrics.win_rate
    case 'profit_factor': return strategy.metrics.profit_factor
    case 'max_drawdown': return strategy.metrics.max_drawdown
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
      <div className="strategy-title"><div><span className="symbol-pill">{strategy.symbol}</span><h2>{strategy.mql5_name || strategy.sqx_name}</h2><p>{strategy.sqx_name}</p></div><div className={`health-badge ${strategy.health.status}`}><HealthDot status={strategy.health.status}/>{strategy.health.status === 'gray' ? 'Sin evaluación' : strategy.health.status === 'green' ? 'Dentro de rango' : strategy.health.status === 'yellow' ? 'Atención' : 'Desviación'}</div></div>
      <div className="hero-stats"><div><span>P/L realizado</span><strong>{money(m.net_profit)}</strong></div><div><span>P/L flotante</span><strong>{money(m.floating_profit)}</strong></div><div><span>Trades</span><strong>{m.trades}</strong></div><div><span>Posiciones</span><strong>{m.open_positions}</strong></div></div>
      <div className="reason-list">{strategy.health.reasons.map(reason => <span key={reason}>{reason}</span>)}{!strategy.health.reasons.length && <span>Sin alertas activas</span>}</div>
    </section>
    <section className="panel comparison-panel">
      <div className="panel-heading"><div><span className="eyebrow">COMPORTAMIENTO</span><h2>Actual vs. {b?.sample_type?.toUpperCase() || 'SQX'}</h2></div><span className="source-tag">{b ? `${b.source} · ${new Date(b.synced_at).toLocaleDateString()}` : 'Sin baseline'}</span></div>
      <div className="comparison-head"><span>KPI</span><span>Actual</span><span>Backtest</span><span>Δ</span></div>
      <Comparison label="Profit factor" current={m.profit_factor} baseline={baselineValue(b, 'ProfitFactor')} />
      <Comparison label="Expectancy" current={m.expectancy} baseline={baselineValue(b, 'Expectancy')} format={money} />
      <Comparison label="Return / DD" current={m.return_dd} baseline={baselineValue(b, 'ReturnDDRatio', 'RetDD')} />
      <Comparison label="SQN" current={m.sqn} baseline={baselineValue(b, 'SQN')} />
      <Comparison label="Trades / mes" current={m.trades_per_month} baseline={baselineValue(b, 'AvgTradesPerMonth')} />
      <Comparison label="Max drawdown" current={m.max_drawdown} baseline={baselineValue(b, 'MaxDD', 'Drawdown')} format={money} />
    </section>
    <section className="panel compact-metrics"><div><span>Win rate</span><strong>{(m.win_rate * 100).toFixed(1)}%</strong></div><div><span>Duración media</span><strong>{duration(m.avg_duration_seconds)}</strong></div><div><span>Racha pérdidas</span><strong>{m.max_consecutive_losses}</strong></div><div><span>Ganancia media</span><strong>{money(m.avg_win)}</strong></div><div><span>Pérdida media</span><strong>{money(m.avg_loss)}</strong></div><div><span>Comisión + swap</span><strong>{money(m.commissions + m.swaps)}</strong></div></section>
  </div>
}

function Settings({ reload }: { reload: () => void }) {
  const [dataDir, setDataDir] = useState('')
  const [terminalName, setTerminalName] = useState('Nueva terminal')
  const [project, setProject] = useState('Retester')
  const [databank, setDatabank] = useState('Results')
  const [notice, setNotice] = useState('')
  const [mappingSuggestions, setMappingSuggestions] = useState<Array<{terminal_id:number;account_login:string;symbol:string;magic:number;comment:string;deal_count:number;candidates:Array<{strategy_id:number;name:string;score:number}>}>>([])
  const [mappingChoices, setMappingChoices] = useState<Record<number, number>>({})
  const [rules, setRules] = useState<Record<string, number>>({})
  useEffect(() => { api<Record<string,number>>('/api/alerts').then(setRules).catch(() => undefined) }, [])
  async function addTerminal() {
    try { await api('/api/terminals', { method: 'POST', body: JSON.stringify({ name: terminalName, data_dir: dataDir }) }); setNotice('Terminal registrada.'); reload() }
    catch (error) { setNotice(error instanceof Error ? error.message : 'Error') }
  }
  async function syncSqx() {
    setNotice('Consultando SQX…')
    try { const result = await api<{ imported: number; unmatched: number }>('/api/sqx/sync', { method: 'POST', body: JSON.stringify({ project, databank }) }); setNotice(`SQX: ${result.imported} importadas, ${result.unmatched} sin coincidencia.`); reload() }
    catch (error) { setNotice(error instanceof Error ? error.message : 'SQX no disponible') }
  }
  async function loadMappings() {
    const result = await api<typeof mappingSuggestions>('/api/mappings/suggestions')
    setMappingSuggestions(result)
    setMappingChoices(Object.fromEntries(result.map((item, index) => [index, item.candidates[0]?.strategy_id || 0])))
    setNotice(`${result.length} grupos de trades requieren revisión.`)
  }
  async function confirmSuggestion(index: number) {
    const item = mappingSuggestions[index]
    const strategyId = mappingChoices[index]
    if (!strategyId) { setNotice('Selecciona una estrategia candidata.'); return }
    const candidate = item.candidates.find(value => value.strategy_id === strategyId)
    await api('/api/mappings/confirm', { method: 'POST', body: JSON.stringify({ strategy_id: strategyId, terminal_id: item.terminal_id, account_login: item.account_login || '', symbol: item.symbol, magic: item.magic, comment_pattern: item.comment, confidence: candidate?.score || 1 }) })
    setNotice('Vínculo confirmado.')
    await loadMappings(); reload()
  }
  async function autoConfirmMappings() {
    const result = await api<{confirmed:number;review_required:number}>('/api/mappings/auto-confirm', { method: 'POST' })
    setNotice(`${result.confirmed} vínculos seguros confirmados; ${result.review_required} grupos requieren revisión.`)
    await loadMappings(); reload()
  }
  async function saveRules() {
    await api('/api/alerts', { method: 'PUT', body: JSON.stringify(rules) })
    setNotice('Umbrales globales guardados.'); reload()
  }
  return <div className="settings-grid">
    <section className="panel"><span className="eyebrow">FUENTES MT5</span><h2>Registrar terminal</h2><p className="help">Cada terminal necesita su DataDir y el servicio DashboardBridge activo.</p><label>Nombre<input value={terminalName} onChange={e => setTerminalName(e.target.value)} /></label><label>DataDir<input placeholder="C:\Users\…\MetaQuotes\Terminal\HASH" value={dataDir} onChange={e => setDataDir(e.target.value)} /></label><button className="button primary" onClick={addTerminal}>Guardar terminal</button></section>
    <section className="panel"><span className="eyebrow">HISTÓRICO</span><h2>Sincronizar SQX</h2><p className="help">SQX debe estar abierto. Los snapshots previos permanecen disponibles cuando se cierre.</p><label>Proyecto<input value={project} onChange={e => setProject(e.target.value)} /></label><label>Databank<input value={databank} onChange={e => setDatabank(e.target.value)} /></label><button className="button primary" onClick={syncSqx}>Sincronizar read-only</button></section>
    <section className="panel settings-wide"><span className="eyebrow">CATÁLOGO Y MAPPINGS</span><h2>Mantenimiento</h2><div className="maintenance-actions"><button className="button" onClick={async () => { await api('/api/catalog/import', { method: 'POST' }); setNotice('Catálogo recargado.'); reload() }}>Recargar Track_v1.xlsx</button><button className="button primary" onClick={autoConfirmMappings}>Vincular coincidencias seguras</button><button className="button" onClick={loadMappings}>Buscar vínculos sugeridos</button></div>
      {mappingSuggestions.length > 0 && <div className="mapping-review"><div className="mapping-review-head"><span>Trade observado</span><span>Estrategia propuesta</span><span>Confianza</span><span /></div>{mappingSuggestions.map((item,index) => <div className="mapping-review-row" key={`${item.terminal_id}-${item.magic}-${item.symbol}-${item.comment}`}><div><strong>{item.symbol} · magic {item.magic}</strong><small>{item.comment || 'Sin comentario'} · {item.deal_count} deals</small></div><select value={mappingChoices[index] || ''} onChange={event => setMappingChoices({...mappingChoices,[index]:Number(event.target.value)})}><option value="">Seleccionar…</option>{item.candidates.map(candidate => <option key={candidate.strategy_id} value={candidate.strategy_id}>{candidate.name}</option>)}</select><span>{item.candidates.find(candidate => candidate.strategy_id === mappingChoices[index])?.score ? `${Math.round((item.candidates.find(candidate => candidate.strategy_id === mappingChoices[index])?.score || 0) * 100)}%` : '—'}</span><button className="button" onClick={() => confirmSuggestion(index)}>Confirmar</button></div>)}</div>}
    </section>
    <section className="panel settings-wide"><span className="eyebrow">SEMÁFORO</span><h2>Umbrales globales</h2><div className="rules-grid">{Object.entries({min_trades:'Mínimo de trades',drawdown_yellow:'DD amarillo',drawdown_red:'DD rojo',performance_yellow:'Rendimiento amarillo',performance_red:'Rendimiento rojo',frequency_yellow_low:'Frecuencia amarilla mín.',frequency_yellow_high:'Frecuencia amarilla máx.',frequency_red_low:'Frecuencia roja mín.',frequency_red_high:'Frecuencia roja máx.'}).map(([key,label]) => <label key={key}>{label}<input type="number" step={key === 'min_trades' ? 1 : .05} value={rules[key] ?? ''} onChange={event => setRules({...rules,[key]:Number(event.target.value)})}/></label>)}</div><button className="button primary" onClick={saveRules}>Guardar umbrales</button></section>
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
  const [query, setQuery] = useState('')
  const [statusFilter, setStatusFilter] = useState('all')
  const [sort, setSort] = useState<SortState>(null)
  const [error, setError] = useState('')
  async function load() {
    const custom = windowName === 'custom' ? `&start=${encodeURIComponent(customStart)}&end=${encodeURIComponent(customEnd)}` : ''
    try { const payload = await api<Dashboard>(`/api/dashboard?window=${windowName}${custom}`); setData(payload); setSelectedId(current => current ?? payload.strategies[0]?.id ?? null); setError('') }
    catch (err) { setError(err instanceof Error ? err.message : 'No se pudo conectar con el backend') }
  }
  useEffect(() => { if (windowName !== 'custom' || (customStart && customEnd)) load(); const timer = setInterval(load, 300_000); return () => clearInterval(timer) }, [windowName, customStart, customEnd])
  const selected = data?.strategies.find(strategy => strategy.id === selectedId) || null
  const filtered = useMemo(() => (data?.strategies || []).filter(strategy => {
    const matchesText = `${strategy.symbol} ${strategy.sqx_name} ${strategy.mql5_name}`.toLowerCase().includes(query.toLowerCase())
    return matchesText && (statusFilter === 'all' || strategy.state === statusFilter)
  }), [data, query, statusFilter])
  const sortedStrategies = useMemo(() => {
    if (!sort) return filtered
    return [...filtered].sort((a, b) => compareStrategies(a, b, sort.key, sort.direction))
  }, [filtered, sort])
  function changeSort(key: SortKey) {
    setSort(current => current?.key === key
      ? { key, direction: current.direction === 'ascending' ? 'descending' : 'ascending' }
      : { key, direction: 'ascending' })
  }
  const t = data?.totals
  return <div className="shell">
    <aside className="sidebar"><Logo/><nav>{([['overview','Resumen'],['detail','Estrategia'],['chart','Gráfico'],['settings','Configuración']] as [Tab,string][]).map(([key,label]) => <button key={key} className={tab === key ? 'active' : ''} onClick={() => setTab(key)}><span>{key === 'overview' ? '⌁' : key === 'detail' ? '◎' : key === 'chart' ? '⌗' : '⚙'}</span>{label}</button>)}</nav><div className="sidebar-footer"><div className="pulse"/><div><strong>Sistema local</strong><small>Refresco cada 5 min</small></div></div></aside>
    <main><header><div><span className="eyebrow">PORTAFOLIO DE ESTRATEGIAS</span><h1>{tab === 'overview' ? 'Estado operativo' : tab === 'detail' ? 'Detalle de estrategia' : tab === 'chart' ? 'Trades sobre mercado' : 'Configuración'}</h1></div><div className="header-actions"><select value={windowName} onChange={e => setWindowName(e.target.value)}><option value="30d">30 días</option><option value="90d">90 días</option><option value="all">Todo</option><option value="custom">Personalizado</option></select>{windowName === 'custom' && <><input aria-label="Fecha inicial" type="date" value={customStart} onChange={e => setCustomStart(e.target.value)}/><input aria-label="Fecha final" type="date" value={customEnd} onChange={e => setCustomEnd(e.target.value)}/></>}<button className="button refresh" onClick={load}>↻ Actualizar</button></div></header>
      {error && <div className="error-banner">{error}</div>}
      {tab === 'overview' && <>
        <section className="metrics-grid"><MetricCard label="P/L realizado" value={money(t?.net_profit)} detail={`${t?.trades || 0} trades cerrados`} tone={(t?.net_profit || 0) >= 0 ? 'positive' : 'negative'}/><MetricCard label="P/L flotante" value={money(t?.floating_profit)} detail="Posiciones abiertas"/><MetricCard label="Estrategias activas" value={`${t?.active || 0} / ${t?.strategies || 0}`} detail="Catálogo completo"/><MetricCard label="Alertas críticas" value={`${t?.red || 0}`} detail="Desviaciones rojas" tone={t?.red ? 'negative' : 'positive'}/></section>
        <section className="terminal-strip">{data?.terminals.map(terminal => <div key={terminal.id}><HealthDot status={terminal.status === 'connected' ? 'green' : 'gray'}/><strong>{terminal.name}</strong><span>{terminal.status === 'connected' ? `Cuenta ${terminal.account_login}` : 'Desconectada'}</span>{terminal.last_seen && <small>{new Date(terminal.last_seen).toLocaleString()}</small>}</div>)}</section>
        <section className="panel table-panel"><div className="panel-heading"><div><span className="eyebrow">MONITOREO</span><h2>Todos los bots</h2></div><div className="filters"><input aria-label="Buscar estrategias" placeholder="Buscar estrategia o símbolo…" value={query} onChange={e => setQuery(e.target.value)}/><select value={statusFilter} onChange={e => setStatusFilter(e.target.value)}><option value="all">Todos los estados</option>{Object.entries(stateLabels).map(([value,label]) => <option key={value} value={value}>{label}</option>)}</select></div></div><div className="table-scroll"><table><thead><tr><SortableHeader label="Estado" sortKey="state" sort={sort} onSort={changeSort}/><SortableHeader label="Estrategia" sortKey="strategy" sort={sort} onSort={changeSort}/><SortableHeader label="Cuenta" sortKey="account" sort={sort} onSort={changeSort}/><SortableHeader label="Magic Number" sortKey="magic" sort={sort} onSort={changeSort}/><SortableHeader label="P/L neto" sortKey="net_profit" sort={sort} onSort={changeSort}/><SortableHeader label="Trades" sortKey="trades" sort={sort} onSort={changeSort}/><SortableHeader label="Win rate" sortKey="win_rate" sort={sort} onSort={changeSort}/><SortableHeader label="PF" sortKey="profit_factor" sort={sort} onSort={changeSort}/><SortableHeader label="Max DD" sortKey="max_drawdown" sort={sort} onSort={changeSort}/><th aria-label="Abrir estrategia"></th></tr></thead><tbody>{sortedStrategies.map(strategy => <tr key={strategy.id} onClick={() => { setSelectedId(strategy.id); setTab('detail') }}><td><span className={`state-tag ${strategy.state}`}><HealthDot status={strategy.health.status}/>{stateLabels[strategy.state] || strategy.state}</span></td><td><strong>{strategy.mql5_name || strategy.sqx_name}</strong><small>{strategy.symbol} · {strategy.sqx_name}</small></td><td>{strategy.account_login || '—'}</td><td className="magic-numbers">{strategy.magic_numbers?.length ? strategy.magic_numbers.join(', ') : '—'}</td><td className={strategy.metrics.net_profit >= 0 ? 'value-good' : 'value-bad'}>{money(strategy.metrics.net_profit)}</td><td>{strategy.metrics.trades}</td><td>{(strategy.metrics.win_rate * 100).toFixed(1)}%</td><td>{number(strategy.metrics.profit_factor)}</td><td>{money(strategy.metrics.max_drawdown)}</td><td>›</td></tr>)}</tbody></table></div></section>
      </>}
      {tab === 'detail' && <><div className="strategy-selector"><label>Estrategia</label><select value={selectedId || ''} onChange={e => setSelectedId(Number(e.target.value))}>{data?.strategies.map(strategy => <option key={strategy.id} value={strategy.id}>{strategy.symbol} — {strategy.mql5_name || strategy.sqx_name}</option>)}</select></div>{selected ? <StrategyDetail strategy={selected}/> : <div className="empty-state">No hay estrategias en el catálogo.</div>}</>}
      {tab === 'chart' && <><div className="strategy-selector"><label>Estrategia</label><select value={selectedId || ''} onChange={e => setSelectedId(Number(e.target.value))}>{data?.strategies.map(strategy => <option key={strategy.id} value={strategy.id}>{strategy.symbol} — {strategy.mql5_name || strategy.sqx_name}</option>)}</select></div>{selectedId ? <ChartPanel strategyId={selectedId}/> : <div className="empty-state">Selecciona una estrategia.</div>}</>}
      {tab === 'settings' && <Settings reload={load}/>}<footer>EA Observatory · Datos locales · {data ? `Actualizado ${new Date(data.generated_at).toLocaleTimeString()}` : 'Conectando…'}</footer>
    </main>
  </div>
}
