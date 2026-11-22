import { useEffect, useMemo, useState } from 'react'
import {
  Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, Pie, PieChart,
  ReferenceArea, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import { api } from './api'
import { bestAndWorst, monthlyProfitColor, selectedCalendarTotal, temporalMetricValue, type StrategyTemporalMetric } from './analyticsPanelMath'
import type { HistoricalExposureGroup, LiveExposureGroup, OverviewAnalytics, StrategyAnalytics, StrategyDrawdownEpisode, StrategyTemporalBucket } from './types'
import './AnalyticsPanels.css'

const COLORS = ['var(--tt-good)', 'var(--tt-info)', 'var(--tt-warn)', 'var(--tt-violet)', 'var(--tt-bad)', 'var(--tt-info)', 'var(--tt-warn)', 'var(--tt-text-2)']
const MONTHS = ['ENE', 'FEB', 'MAR', 'ABR', 'MAY', 'JUN', 'JUL', 'AGO', 'SEP', 'OCT', 'NOV', 'DIC']

function money(value: number | null | undefined) {
  return value == null ? '—' : new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(value)
}

function lots(value: number) {
  return new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 }).format(value)
}

function exposurePercent(value: number, total: number) {
  return total > 0 ? `${(value / total * 100).toFixed(1)}%` : '0.0%'
}

type ExposureView = 'historical' | 'live'
type ExposureGroup = HistoricalExposureGroup | LiveExposureGroup

function ExposureTooltip({ active, payload, view }: { active?: boolean; payload?: Array<{ payload: ExposureGroup }>; view: ExposureView }) {
  const row = active ? payload?.[0]?.payload : null
  if (!row) return null
  const isLiveExposure = view === 'live'
  return <div className="analytics-tooltip">
    <strong>{row.label}</strong><small>{row.symbols.join(', ')}</small>
    <dl>
      {!isLiveExposure && 'trades' in row && <><dt>Operaciones cerradas</dt><dd>{row.trades}</dd><dt>Volumen acumulado</dt><dd>{lots(row.lots)} lotes</dd></>}
      {isLiveExposure && 'positions' in row && <><dt>Posiciones</dt><dd>{row.positions}</dd><dt>Exposición bruta</dt><dd>{lots(row.lots)} lotes</dd><dt>BUY / SELL</dt><dd>{lots(row.buy_lots)} / {lots(row.sell_lots)} lotes</dd><dt>Exposición neta</dt><dd>{lots(row.net_lots)} lotes</dd></>}
      {isLiveExposure && 'floating_profit' in row && <><dt>P/L flotante</dt><dd className={row.floating_profit >= 0 ? 'positive' : 'negative'}>{money(row.floating_profit)}</dd></>}
    </dl>
  </div>
}

export function OverviewAnalyticsPanel({ analytics }: { analytics: OverviewAnalytics | undefined }) {
  const years = analytics?.calendar_profit || []
  const [year, setYear] = useState<number | null>(years[0]?.year ?? null)
  const [exposureView, setExposureView] = useState<ExposureView>('historical')
  useEffect(() => {
    setYear(current => current != null && years.some(item => item.year === current) ? current : years[0]?.year ?? null)
  }, [years])
  const selected = years.find(item => item.year === year) || null
  const maxMonth = Math.max(0, ...(selected?.months.map(Math.abs) || []))
  const total = selected ? selectedCalendarTotal(selected.year, selected.months) : 0
  const currentYear = new Date().getFullYear()
  const historicalExposure = analytics?.exposure?.historical
  const liveExposure = analytics?.exposure?.live
  useEffect(() => {
    const historicalHasData = Boolean(historicalExposure?.groups.length)
    const liveHasData = Boolean(liveExposure?.groups.length)
    if (!historicalHasData && liveHasData) setExposureView('live')
  }, [historicalExposure, liveExposure])
  const isLiveExposure = exposureView === 'live'
  const exposure = isLiveExposure ? liveExposure : historicalExposure
  const exposureTitle = isLiveExposure ? 'Exposición en vivo por activo' : 'Operaciones cerradas por símbolo'
  const exposureTotal = isLiveExposure ? liveExposure?.total_lots : historicalExposure?.total_trades
  const exposureUnit = isLiveExposure ? 'lotes' : 'operaciones'
  const exposureDataKey = isLiveExposure ? 'lots' : 'trades'
  const realizedPeriods = analytics?.realized_period_profit

  return <div className="overview-analytics-grid">
    <section className="summary-analytics-card exposure-card">
      <div className="summary-analytics-heading"><div><span className="eyebrow">{isLiveExposure ? 'EXPOSICIÓN EN VIVO' : 'OPERACIONES CERRADAS'}</span><h3>{exposureTitle}</h3></div><div className="exposure-controls"><div className="exposure-view-switch" aria-label="Tipo de exposición">{(['historical', 'live'] as const).map(view => <button key={view} type="button" className={exposureView === view ? 'active' : ''} aria-pressed={exposureView === view} onClick={() => setExposureView(view)}>{view === 'historical' ? 'Histórica' : 'En vivo'}</button>)}</div>{exposure && <span className="summary-count">{exposureTotal} {exposureUnit}</span>}</div></div>
      {exposure?.groups.length ? <div className="exposure-layout">
        <div className="exposure-chart">
          <ResponsiveContainer width="100%" height="100%">
            <PieChart><Pie data={exposure.groups} dataKey={exposureDataKey} nameKey="label" innerRadius="58%" outerRadius="84%" paddingAngle={2} stroke="var(--tt-surface)" strokeWidth={2}>{exposure.groups.map((row, index) => <Cell key={row.key} fill={COLORS[index % COLORS.length]} />)}</Pie><Tooltip content={<ExposureTooltip view={exposureView} />} /></PieChart>
          </ResponsiveContainer>
          <div className="exposure-total"><strong>{exposureTotal}</strong><span>{exposureUnit}</span></div>
        </div>
        <div className="exposure-legend">{exposure.groups.map((row, index) => <div key={row.key}><i style={{ background: COLORS[index % COLORS.length] }} /><span><strong>{row.label}</strong><small>{isLiveExposure ? `${lots(row.lots)} lotes · ${exposurePercent(row.lots, liveExposure?.total_lots ?? 0)} exposición${'positions' in row ? ` · ${row.positions} pos.` : ''}` : `${'trades' in row ? row.trades : 0} operaciones · ${exposurePercent('trades' in row ? row.trades : 0, historicalExposure?.total_trades ?? 0)} del total`}</small></span>{isLiveExposure && 'floating_profit' in row && <b className={row.floating_profit >= 0 ? 'positive' : 'negative'}>{money(row.floating_profit)}</b>}</div>)}</div>
      </div> : <div className="analytics-empty">{isLiveExposure ? 'No hay posiciones abiertas para las cuentas y activos seleccionados.' : 'No hay operaciones cerradas para las cuentas, símbolos y período seleccionados.'}</div>}
    </section>
    <section className="summary-analytics-card calendar-profit-card">
      <div className="summary-analytics-heading"><div><span className="eyebrow">GANANCIA REALIZADA</span><h3>Resultado mensual</h3></div>{years.length > 0 && <select aria-label="Año del resultado mensual" value={year ?? ''} onChange={event => setYear(Number(event.target.value))}>{years.map(item => <option key={item.year} value={item.year}>{item.year}</option>)}</select>}</div>
      {selected ? <div className="calendar-profit-strip">
        {selected.months.map((value, index) => <div key={MONTHS[index]}><span>{MONTHS[index]}</span><strong className={value > 0 ? 'positive' : value < 0 ? 'negative' : ''} style={{ backgroundColor: monthlyProfitColor(value, maxMonth) }}>{money(value)}</strong></div>)}
        <div className="calendar-total"><span>{selected.year === currentYear ? 'YTD' : 'TOTAL'}</span><strong className={total > 0 ? 'positive' : total < 0 ? 'negative' : ''} style={{ backgroundColor: monthlyProfitColor(total, Math.max(maxMonth, Math.abs(total))) }}>{money(total)}</strong></div>
      </div> : <div className="analytics-empty">No hay operaciones cerradas para construir el resumen anual.</div>}
      {realizedPeriods && <div className="realized-period-profit"><div><span>Esta semana</span><strong className={realizedPeriods.week >= 0 ? 'positive' : 'negative'}>{money(realizedPeriods.week)}</strong></div><div><span>Este mes</span><strong className={realizedPeriods.month >= 0 ? 'positive' : 'negative'}>{money(realizedPeriods.month)}</strong></div><div><span>Este año</span><strong className={realizedPeriods.year >= 0 ? 'positive' : 'negative'}>{money(realizedPeriods.year)}</strong></div></div>}
      <p className="analytics-footnote">P/L neto cerrado · hora broker UTC+2 · independiente del rango 30d/90d.</p>
    </section>
  </div>
}

function axisNumber(value: number) {
  return new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: Math.abs(value) < 10 ? 1 : 0 }).format(value)
}

function metricLabel(metric: StrategyTemporalMetric) {
  return metric === 'win_rate' ? 'Win Rate' : metric === 'expectancy' ? 'Expectancy' : 'Profit Factor'
}

function metricFormat(value: number | null, metric: StrategyTemporalMetric) {
  if (value == null) return '—'
  if (value === Infinity) return '∞ sin pérdidas'
  return metric === 'win_rate' ? `${value.toFixed(1)}%` : money(value)
}

function metricColor(value: number | null, metric: StrategyTemporalMetric) {
  if (value == null) return 'var(--tt-line)'
  const positive = metric === 'win_rate' ? value >= 50 : metric === 'profit_factor' ? value >= 1 : value >= 0
  return positive ? 'var(--tt-good)' : 'var(--tt-bad)'
}

function TemporalTooltip({ active, payload, metric }: { active?: boolean; payload?: Array<{ payload: StrategyTemporalBucket & { value: number } }>; metric: StrategyTemporalMetric }) {
  const row = active ? payload?.[0]?.payload : null
  if (!row) return null
  return <div className="analytics-tooltip"><strong>{row.label}</strong><dl><dt>{metricLabel(metric)}</dt><dd>{metricFormat(temporalMetricValue(row, metric), metric)}</dd><dt>Operaciones</dt><dd>{row.count}</dd></dl>{row.low_sample && <small>Muestra baja: menos de 10 operaciones.</small>}</div>
}

function TemporalChart({ title, rows, metric, id }: { title: string; rows: StrategyTemporalBucket[]; metric: StrategyTemporalMetric; id: string }) {
  // A 0% win rate (or PF 0) with trades would draw no bar; give it a visible red stub.
  const raw = rows.map(row => ({ ...row, value: temporalMetricValue(row, metric) ?? 0 }))
  const values = raw.filter(row => row.count && Number.isFinite(row.value)).map(row => row.value)
  const extent = Math.ceil(Math.max(1, ...values.map(Math.abs)))
  const pfTop = Math.ceil(Math.max(2, ...values))
  const domain: [number, number] = metric === 'win_rate' ? [0, 100] : metric === 'expectancy' ? [-extent, extent] : [0, pfTop]
  // A 0% win rate (or PF 0) with trades would draw no bar; give it a visible red stub.
  // An unbounded PF (no losses) is drawn full height.
  const stub = metric === 'win_rate' ? 2 : metric === 'profit_factor' ? pfTop * .02 : 0
  const data = raw.map(row => ({ ...row, bar: row.value === Infinity ? pfTop : row.count > 0 && stub && row.value < stub ? stub : row.value }))
  return <div className={`strategy-temporal-chart ${id === 'hours' ? 'wide' : ''}`}><h4>{title}</h4><div className="strategy-temporal-canvas"><ResponsiveContainer width="100%" height="100%"><BarChart data={data} margin={{ top: 8, right: 8, left: -18, bottom: 0 }}><defs><pattern id={`low-pos-${id}`} width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="6" height="6" fill="#34d39959"/><line x1="0" y1="0" x2="0" y2="6" stroke="var(--tt-good)" strokeWidth="2.5"/></pattern><pattern id={`low-neg-${id}`} width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="6" height="6" fill="#fb718559"/><line x1="0" y1="0" x2="0" y2="6" stroke="var(--tt-bad)" strokeWidth="2.5"/></pattern></defs><CartesianGrid stroke="var(--tt-raised)" vertical={false} strokeDasharray="3 3"/><XAxis dataKey="label" tick={{ fill: 'var(--tt-muted)', fontSize: 11 }} axisLine={false} tickLine={false}/><YAxis domain={domain} ticks={metric === 'expectancy' ? [-extent, -extent / 2, 0, extent / 2, extent] : undefined} tickFormatter={axisNumber} width={52} tick={{ fill: 'var(--tt-muted)', fontSize: 11 }} axisLine={false} tickLine={false}/>{metric === 'expectancy' && <ReferenceLine y={0} stroke="var(--tt-muted)"/>}<Tooltip cursor={{ fill: '#ffffff0d' }} content={<TemporalTooltip metric={metric}/>} /><Bar dataKey="bar" radius={[5, 5, 2, 2]}>{data.map(row => <Cell key={row.key} fill={row.count === 0 ? 'var(--tt-raised)' : row.low_sample ? `url(#low-${metricColor(row.value, metric) === 'var(--tt-good)' ? 'pos' : 'neg'}-${id})` : metricColor(row.value, metric)} />)}</Bar></BarChart></ResponsiveContainer></div></div>
}

function Insight({ label, row, metric, tone }: { label: string; row: StrategyTemporalBucket | null; metric: StrategyTemporalMetric; tone: 'best' | 'worst' }) {
  return <span className={`strategy-insight ${tone}`}><small>{label}</small><strong>{row ? row.label : '—'}</strong><b>{row ? metricFormat(temporalMetricValue(row, metric), metric) : 'Sin datos'}</b></span>
}

function DrawdownTooltip({ active, payload, percentage }: { active?: boolean; payload?: Array<{ payload: StrategyAnalytics['drawdown']['curve'][number] }>; percentage: boolean }) {
  const point = active ? payload?.[0]?.payload : null
  if (!point) return null
  return <div className="analytics-tooltip"><strong>{new Date(point.time_msc).toLocaleString()}</strong><dl><dt>Drawdown</dt><dd>{money(point.drawdown)}</dd>{percentage && <><dt>% cuenta</dt><dd>{point.drawdown_pct?.toFixed(2)}%</dd></>}</dl></div>
}

function episodeEnd(episode: StrategyDrawdownEpisode) {
  return episode.recovery_msc ?? episode.end_msc
}

export function StrategyAnalyticsPanel({ strategyId, query }: { strategyId: number; query: string }) {
  const [data, setData] = useState<StrategyAnalytics | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [metric, setMetric] = useState<StrategyTemporalMetric>('win_rate')
  const [selectedEpisode, setSelectedEpisode] = useState<StrategyDrawdownEpisode | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    setLoading(true); setError(''); setSelectedEpisode(null)
    api<StrategyAnalytics>(`/api/strategies/${strategyId}/analytics?${query}`, { signal: controller.signal })
      .then(result => setData(result))
      .catch(err => { if (!controller.signal.aborted) setError(err instanceof Error ? err.message : 'No se pudo cargar la analítica') })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [strategyId, query])
  const insights = useMemo(() => data ? {
    hours: bestAndWorst(data.temporal.hours, metric),
    weekdays: bestAndWorst(data.temporal.weekdays, metric),
    months: bestAndWorst(data.temporal.months, metric),
  } : null, [data, metric])
  if (loading) return <section className="panel strategy-analytics-section analytics-loading">Cargando analítica de la estrategia…</section>
  if (error || !data) return <section className="panel strategy-analytics-section analytics-error">{error || 'Analítica no disponible.'}</section>
  return <>
    <section className="panel strategy-analytics-section temporal-strategy-panel">
      <div className="strategy-analytics-heading"><div><span className="eyebrow">COMPORTAMIENTO TEMPORAL</span><h2>Rendimiento por hora, día y mes</h2><p>{data.trade_count} operaciones · hora broker {data.timezone}</p></div><div className="temporal-metric-switch">{(['win_rate','expectancy','profit_factor'] as StrategyTemporalMetric[]).map(value => <button type="button" key={value} className={metric === value ? 'active' : ''} onClick={() => setMetric(value)}>{metricLabel(value)}</button>)}</div></div>
      {insights && <div className="strategy-insights"><Insight label="Mejor hora" row={insights.hours.best} metric={metric} tone="best"/><Insight label="Peor hora" row={insights.hours.worst} metric={metric} tone="worst"/><Insight label="Mejor día" row={insights.weekdays.best} metric={metric} tone="best"/><Insight label="Peor día" row={insights.weekdays.worst} metric={metric} tone="worst"/><Insight label="Mejor mes" row={insights.months.best} metric={metric} tone="best"/><Insight label="Peor mes" row={insights.months.worst} metric={metric} tone="worst"/></div>}
      <div className="strategy-temporal-grid"><TemporalChart title="Horas" rows={data.temporal.hours} metric={metric} id="hours"/><TemporalChart title="Días" rows={data.temporal.weekdays} metric={metric} id="days"/><TemporalChart title="Meses" rows={data.temporal.months} metric={metric} id="months"/></div>
      <div className="temporal-legend"><span><i className="pos" />{metric === 'win_rate' ? 'Win rate ≥ 50%' : metric === 'profit_factor' ? 'PF ≥ 1' : 'Ganancia'}</span><span><i className="neg" />{metric === 'win_rate' ? 'Win rate < 50%' : metric === 'profit_factor' ? 'PF < 1' : 'Pérdida'}</span><span><i className="pos hatch" /><i className="neg hatch" />Rayado = muestra baja (&lt; 10 operaciones)</span></div>
    </section>
    <section className="panel strategy-analytics-section drawdown-strategy-panel">
      <div className="strategy-analytics-heading"><div><span className="eyebrow">RIESGO REALIZADO</span><h2>Análisis de drawdown</h2><p>Retroceso de la curva acumulada respecto a su máximo anterior.</p></div><span className={data.drawdown.percentage_available ? 'percentage-basis available' : 'percentage-basis'} title={data.drawdown.percentage_reason}>{data.drawdown.percentage_available ? `% sobre balance ${money(data.drawdown.balance_basis)}` : '% no disponible'}</span></div>
      {data.drawdown.curve.length ? <div className="drawdown-analysis-layout">
        <div className="drawdown-chart"><ResponsiveContainer width="100%" height="100%"><AreaChart data={data.drawdown.curve} margin={{ top: 12, right: 18, left: 4, bottom: 4 }}><defs><linearGradient id="strategyDrawdown" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="var(--tt-bad)" stopOpacity=".1"/><stop offset="100%" stopColor="var(--tt-bad)" stopOpacity=".38"/></linearGradient></defs><CartesianGrid stroke="var(--tt-raised)" strokeDasharray="3 3"/><XAxis dataKey="time_msc" type="number" domain={['dataMin','dataMax']} tickFormatter={value => new Date(value).toLocaleDateString(undefined, { month: 'short', day: '2-digit' })} tick={{ fill: 'var(--tt-muted)', fontSize: 11 }} axisLine={false}/><YAxis tickFormatter={value => money(value)} tick={{ fill: 'var(--tt-muted)', fontSize: 11 }} axisLine={false}/><ReferenceLine y={0} stroke="var(--tt-muted)"/>{selectedEpisode && <ReferenceArea x1={selectedEpisode.start_msc} x2={episodeEnd(selectedEpisode)} fill="var(--tt-bad)" fillOpacity={.12} stroke="var(--tt-bad)" strokeOpacity={.5}/>}<Tooltip content={<DrawdownTooltip percentage={data.drawdown.percentage_available}/>} /><Area type="stepAfter" dataKey="drawdown" stroke="var(--tt-bad)" strokeWidth={2} fill="url(#strategyDrawdown)" isAnimationActive={false}/></AreaChart></ResponsiveContainer></div>
        <div className="drawdown-episode-table"><table><thead><tr><th>Profundidad</th><th>Días</th><th>Inicio</th><th>Final</th></tr></thead><tbody>{data.drawdown.episodes.map((episode, index) => <tr key={`${episode.start_msc}-${index}`} className={selectedEpisode === episode ? 'selected' : ''} onClick={() => setSelectedEpisode(current => current === episode ? null : episode)}><td><strong>-{money(episode.depth)}</strong>{episode.depth_pct != null && <small>-{episode.depth_pct.toFixed(2)}%</small>}</td><td>{episode.days.toFixed(1)}{episode.ongoing && <small className="ongoing">En curso</small>}</td><td>{new Date(episode.start_msc).toLocaleDateString()}</td><td>{episode.recovery_msc ? new Date(episode.recovery_msc).toLocaleDateString() : '—'}</td></tr>)}</tbody></table>{!data.drawdown.episodes.length && <div className="analytics-empty">No se detectaron episodios de drawdown.</div>}</div>
      </div> : <div className="analytics-empty">No hay operaciones en el período seleccionado.</div>}
      <p className="analytics-footnote">{data.drawdown.percentage_reason}</p>
    </section>
  </>
}
