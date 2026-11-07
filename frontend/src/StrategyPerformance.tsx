import { useEffect, useMemo, useState, type ReactNode } from 'react'
import {
  Area, Bar, BarChart, CartesianGrid, Cell, ComposedChart, LabelList, Line,
  ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import { monthlyProfitColor } from './analyticsPanelMath'
import {
  curveFromTrades, dailyProfit, directionSplit, equityWithPeaks, hourlyProfit, isBuy, isSell,
  monthlyProfit, tradesToCsv, type DirectionFilter, type EquityPeakPoint,
} from './strategyPerformanceMath'
import type { Strategy, StrategyDetails } from './types'
import './StrategyPerformance.css'

const MONTHS = ['ENE', 'FEB', 'MAR', 'ABR', 'MAY', 'JUN', 'JUL', 'AGO', 'SEP', 'OCT', 'NOV', 'DIC']
const WEEKDAYS = ['Lu', 'Ma', 'Mi', 'Ju', 'Vi', 'Sa', 'Do']
const GREEN = '#34d399'
const RED = '#fb7185'
const PAGE_SIZE = 20
const LOW_SAMPLE = 30

function fmt(value: number | null | undefined, digits = 2) {
  return value == null || !Number.isFinite(value) ? '—' : new Intl.NumberFormat('es-ES', { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(value)
}
function signed(value: number | null | undefined) {
  return value == null || !Number.isFinite(value) ? '—' : `${value > 0 ? '+' : ''}${fmt(value)}`
}
function tone(value: number | null | undefined) {
  return value == null || value === 0 ? '' : value > 0 ? 'positive' : 'negative'
}
function shortDate(msc: number) {
  return new Date(msc).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: '2-digit' })
}
function dateTime(msc: number) {
  return msc ? new Date(msc).toLocaleString(undefined, { day: '2-digit', month: '2-digit', year: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—'
}
function price(value: number | null | undefined) {
  return value == null || !Number.isFinite(value) ? '—' : String(Number(value.toFixed(5)))
}
function durationLabel(seconds: number) {
  if (!seconds) return '—'
  const hours = seconds / 3600
  return hours >= 24 ? `${(hours / 24).toFixed(1)} d` : hours >= 1 ? `${hours.toFixed(1)} h` : `${Math.round(seconds / 60)} min`
}

function Section({ eyebrow, title, actions, className = '', children }: { eyebrow: string; title?: string; actions?: ReactNode; className?: string; children: ReactNode }) {
  return <section className={`panel sp-section ${className}`}>
    <div className="sp-heading"><div><span className="eyebrow">{eyebrow}</span>{title && <h3>{title}</h3>}</div>{actions && <div className="sp-actions">{actions}</div>}</div>
    {children}
  </section>
}

function Kpi({ label, value, detail, valueTone = '', accent, children }: { label: string; value: string; detail?: string; valueTone?: string; accent: string; children?: ReactNode }) {
  return <div className="sp-kpi" style={{ ['--accent' as string]: accent }}>
    <span>{label}</span>
    <strong className={valueTone}>{value}</strong>
    {children}
    {detail && <small>{detail}</small>}
  </div>
}

function KpiTiles({ strategy, maxDdPct }: { strategy: Strategy; maxDdPct: number | null }) {
  const m = strategy.metrics
  const winPct = m.win_rate * 100
  const avgTotal = Math.abs(m.avg_win) + Math.abs(m.avg_loss)
  const winShare = avgTotal ? Math.abs(m.avg_win) / avgTotal * 100 : 50
  const pf = m.profit_factor
  return <div className="sp-kpi-grid">
    <Kpi label="Profit neto" value={signed(m.net_profit)} valueTone={tone(m.net_profit)} accent={m.net_profit >= 0 ? GREEN : RED} detail={`Hoy ${signed(m.today_profit)} · ${m.today_trades} trades`} />
    <Kpi label="Win rate" value={`${winPct.toFixed(1)}%`} valueTone={winPct >= 50 ? 'positive' : 'negative'} accent="#38bdf8" detail={`${m.winning_trades} ganadas / ${m.losing_trades} perdidas`}>
      <div className="sp-meter"><i style={{ width: `${Math.min(100, winPct)}%` }} /></div>
    </Kpi>
    <Kpi label="Profit factor" value={fmt(pf)} valueTone={pf == null ? '' : pf >= 1 ? 'positive' : 'negative'} accent="#a78bfa" detail={`${fmt(m.gross_profit)} / ${fmt(m.gross_loss)}`}>
      <div className="sp-meter pf"><i style={{ width: `${Math.min(100, (pf ?? 0) / 3 * 100)}%` }} /><b style={{ left: `${100 / 3}%` }} /></div>
    </Kpi>
    <Kpi label="Max drawdown" value={`-${fmt(m.max_drawdown)}`} valueTone="negative" accent={RED} detail={maxDdPct == null ? 'Pico a valle' : `${maxDdPct.toFixed(1)}% sobre pico de ganancia`} />
    <Kpi label="Expectancy" value={signed(m.expectancy)} valueTone={tone(m.expectancy)} accent="#2dd4bf" detail={`SQN ${fmt(m.sqn)} · Ret/DD ${fmt(m.return_dd)}`} />
    <Kpi label="Avg win / Avg loss" value={`${fmt(m.avg_win)} / ${fmt(m.avg_loss)}`} accent="#fbbf24">
      <div className="sp-split"><i className="win" style={{ width: `${winShare}%` }} /><i className="loss" style={{ width: `${100 - winShare}%` }} /></div>
    </Kpi>
    <Kpi label="Rachas máx." value={`${strategy.lifetime_metrics.max_consecutive_wins} W · ${strategy.lifetime_metrics.max_consecutive_losses} L`} accent="#f472b6" detail={`Racha perdedora actual: ${m.current_consecutive_losses}`} />
    <Kpi label="Trades" value={String(m.trades)} accent="#94a3b8" detail={`${m.open_positions} abiertas · flotante ${signed(m.floating_profit)}`} />
  </div>
}

function EquityTooltip({ active, payload }: { active?: boolean; payload?: Array<{ payload: EquityPeakPoint }> }) {
  const point = active ? payload?.[0]?.payload : null
  if (!point) return null
  return <div className="analytics-tooltip"><strong>{new Date(point.time_msc).toLocaleString()}</strong><dl>
    <dt>Ganancia acumulada</dt><dd className={tone(point.equity)}>{signed(point.equity)}</dd>
    <dt>Trade</dt><dd className={tone(point.net_profit)}>{signed(point.net_profit)}</dd>
    <dt>Bajo el máximo</dt><dd className={point.underwater ? 'negative' : ''}>{point.underwater ? `-${fmt(point.underwater)}` : '—'}</dd>
  </dl></div>
}

function EquityChart({ detail }: { detail: StrategyDetails }) {
  const [direction, setDirection] = useState<DirectionFilter>('total')
  const curve = useMemo(() => direction === 'total' ? detail.equity_curve : curveFromTrades(detail.trades, direction), [direction, detail.equity_curve, detail.trades])
  const series = useMemo(() => equityWithPeaks(curve), [curve])
  const data = series.points.map(point => ({ ...point, band: [point.equity, point.peak] as [number, number] }))
  const truncated = direction !== 'total' && detail.trades.length < detail.equity_curve.length
  return <Section eyebrow="CURVA DE GANANCIAS" title="Equity de la estrategia" className="sp-wide"
    actions={<>
      {series.maxDrawdown > 0 && <span className="sp-dd-chip">Max DD -{fmt(series.maxDrawdown)}{series.maxDrawdownPct != null ? ` (${series.maxDrawdownPct.toFixed(1)}%)` : ''}</span>}
      <div className="temporal-metric-switch">{(['total', 'buy', 'sell'] as const).map(value => <button key={value} type="button" className={direction === value ? 'active' : ''} aria-pressed={direction === value} onClick={() => setDirection(value)}>{value.toUpperCase()}</button>)}</div>
    </>}>
    {data.length > 1 ? <div className="sp-equity-canvas"><ResponsiveContainer width="100%" height="100%">
      <ComposedChart data={data} margin={{ top: 12, right: 16, left: 4, bottom: 4 }}>
        <defs>
          <linearGradient id="spEquityFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor={GREEN} stopOpacity=".32" /><stop offset="100%" stopColor={GREEN} stopOpacity=".02" /></linearGradient>
        </defs>
        <CartesianGrid stroke="#1c303d" strokeDasharray="3 3" vertical={false} />
        <XAxis dataKey="time_msc" type="number" domain={['dataMin', 'dataMax']} tickFormatter={shortDate} tick={{ fill: '#78909f', fontSize: 11 }} axisLine={false} tickLine={false} minTickGap={40} />
        <YAxis tickFormatter={value => fmt(value, 0)} tick={{ fill: '#78909f', fontSize: 11 }} axisLine={false} tickLine={false} width={64} />
        <ReferenceLine y={0} stroke="#475569" />
        <Tooltip content={<EquityTooltip />} />
        <Area type="linear" dataKey="equity" stroke="none" fill="url(#spEquityFill)" isAnimationActive={false} />
        <Area type="linear" dataKey="band" stroke="none" fill={RED} fillOpacity={.22} isAnimationActive={false} />
        <Line type="linear" dataKey="peak" stroke="#94a3b8" strokeDasharray="5 4" strokeWidth={1.2} dot={false} isAnimationActive={false} />
        <Line type="linear" dataKey="equity" stroke={GREEN} strokeWidth={2.2} dot={data.length <= 60 ? { r: 2.5, fill: '#f59e0b', stroke: 'none' } : false} isAnimationActive={false} />
      </ComposedChart>
    </ResponsiveContainer></div> : <div className="analytics-empty">No hay operaciones cerradas {direction === 'total' ? '' : `${direction.toUpperCase()} `}en el período seleccionado.</div>}
    <div className="sp-legend"><span><i className="line green" />Ganancia acumulada</span><span><i className="line dashed" />Máximo histórico</span><span><i className="box red" />Zona en pérdida vs pico</span>{truncated && <span className="muted">BUY/SELL usan las últimas {detail.trades.length} operaciones.</span>}</div>
  </Section>
}

function MonthlyBars({ rows }: { rows: ReturnType<typeof monthlyProfit> }) {
  const [year, setYear] = useState<number | null>(rows[0]?.year ?? null)
  useEffect(() => { setYear(current => rows.some(row => row.year === current) ? current : rows[0]?.year ?? null) }, [rows])
  const row = rows.find(item => item.year === year)
  const data = row ? row.months.map((value, index) => ({ label: MONTHS[index], value, trades: row.tradeCounts[index] })) : []
  return <Section eyebrow="ANÁLISIS MENSUAL" title="Ganancia por mes"
    actions={rows.length > 0 && <div className="sp-year-tabs">{rows.map(item => <button key={item.year} type="button" className={item.year === year ? 'active' : ''} onClick={() => setYear(item.year)}>{item.year}</button>)}</div>}>
    {row ? <div className="sp-bars-canvas"><ResponsiveContainer width="100%" height="100%">
      <BarChart data={data} margin={{ top: 24, right: 8, left: 0, bottom: 0 }}>
        <CartesianGrid stroke="#1c303d" strokeDasharray="3 3" vertical={false} />
        <XAxis dataKey="label" tick={{ fill: '#78909f', fontSize: 11 }} axisLine={false} tickLine={false} />
        <YAxis tickFormatter={value => fmt(value, 0)} tick={{ fill: '#78909f', fontSize: 11 }} axisLine={false} tickLine={false} width={56} />
        <ReferenceLine y={0} stroke="#475569" />
        <Tooltip cursor={{ fill: '#ffffff08' }} content={({ active, payload }) => { const item = active ? payload?.[0]?.payload as typeof data[number] | undefined : undefined; return item ? <div className="analytics-tooltip"><strong>{item.label} {year}</strong><dl><dt>Profit</dt><dd className={tone(item.value)}>{signed(item.value)}</dd><dt>Operaciones</dt><dd>{item.trades}</dd></dl></div> : null }} />
        <Bar dataKey="value" radius={[4, 4, 4, 4]} maxBarSize={46} isAnimationActive={false}>
          {data.map(item => <Cell key={item.label} fill={item.trades === 0 ? '#1a2c38' : item.value >= 0 ? GREEN : RED} />)}
          <LabelList dataKey="value" position="top" formatter={(value: unknown) => Number(value) ? signed(Number(value)) : ''} style={{ fill: '#b6c8d2', fontSize: 11 }} />
        </Bar>
      </BarChart>
    </ResponsiveContainer></div> : <div className="analytics-empty">Sin datos mensuales.</div>}
  </Section>
}

function ProfitCalendar({ curve }: { curve: StrategyDetails['equity_curve'] }) {
  const latest = curve.length ? new Date(curve[curve.length - 1].time_msc).toISOString().slice(0, 7) : new Date().toISOString().slice(0, 7)
  const [month, setMonth] = useState(latest)
  useEffect(() => setMonth(latest), [latest])
  const days = useMemo(() => dailyProfit(curve, month), [curve, month])
  const [year, monthNumber] = month.split('-').map(Number)
  const offset = (new Date(Date.UTC(year, monthNumber - 1, 1)).getUTCDay() + 6) % 7
  const daysInMonth = new Date(Date.UTC(year, monthNumber, 0)).getUTCDate()
  const maxAbs = Math.max(0, ...Object.values(days).map(Math.abs))
  const total = Object.values(days).reduce((sum, value) => sum + value, 0)
  const tradingDays = Object.keys(days).length
  const label = new Date(Date.UTC(year, monthNumber - 1, 1)).toLocaleDateString(undefined, { month: 'long', year: 'numeric', timeZone: 'UTC' })
  function shift(amount: number) {
    const value = new Date(Date.UTC(year, monthNumber - 1 + amount, 1))
    setMonth(value.toISOString().slice(0, 7))
  }
  return <Section eyebrow="CALENDARIO" title="P/L diario"
    actions={<><span className={`sp-total ${tone(total)}`}>{signed(total)}</span><div className="sp-cal-nav"><button type="button" onClick={() => shift(-1)} aria-label="Mes anterior">‹</button><strong>{label}</strong><button type="button" onClick={() => shift(1)} aria-label="Mes siguiente">›</button></div></>}>
    <div className="sp-calendar">
      {WEEKDAYS.map(day => <span key={day} className="sp-cal-head">{day}</span>)}
      {Array.from({ length: offset }, (_, index) => <i key={`empty-${index}`} />)}
      {Array.from({ length: daysInMonth }, (_, index) => {
        const day = index + 1
        const key = `${month}-${String(day).padStart(2, '0')}`
        const pnl = days[key]
        return <div key={key} className={`sp-cal-day ${tone(pnl)}`} style={pnl ? { backgroundColor: monthlyProfitColor(pnl, maxAbs) } : undefined}><small>{day}</small>{pnl != null && <strong>{signed(pnl)}</strong>}</div>
      })}
    </div>
    <p className="analytics-footnote">{tradingDays} días con cierres · fechas en UTC.</p>
  </Section>
}

function MonthlyDistribution({ rows }: { rows: ReturnType<typeof monthlyProfit> }) {
  const maxAbs = Math.max(0, ...rows.flatMap(row => row.months.map(Math.abs)))
  return <Section eyebrow="DISTRIBUCIÓN MENSUAL" title="Resultado por año y mes" className="sp-wide">
    {rows.length ? <div className="sp-table-wrap"><table className="sp-distribution">
      <thead><tr><th>Año</th>{MONTHS.map(month => <th key={month}>{month}</th>)}<th>Total</th></tr></thead>
      <tbody>{rows.map(row => <tr key={row.year}><td><strong>{row.year}</strong></td>{row.months.map((value, index) => <td key={index} className={row.tradeCounts[index] ? tone(value) : 'empty'} style={row.tradeCounts[index] ? { backgroundColor: monthlyProfitColor(value, maxAbs) } : undefined} title={row.tradeCounts[index] ? `${row.tradeCounts[index]} operaciones` : undefined}>{row.tradeCounts[index] ? signed(value) : '-'}</td>)}<td className={`total ${tone(row.total)}`}>{signed(row.total)}</td></tr>)}</tbody>
    </table></div> : <div className="analytics-empty">Sin operaciones cerradas.</div>}
  </Section>
}

function AdvancedStats({ strategy, detail }: { strategy: Strategy; detail: StrategyDetails }) {
  const m = strategy.metrics
  const split = useMemo(() => directionSplit(detail.trades), [detail.trades])
  const hours = useMemo(() => hourlyProfit(detail.trades), [detail.trades])
  const hasHours = hours.some(bucket => bucket.trades)
  return <Section eyebrow="ESTADÍSTICAS AVANZADAS" title="Compras vs ventas y horario" className="sp-wide">
    <div className="sp-advanced">
      <div className="sp-direction-cards">
        {(['buy', 'sell'] as const).map(side => { const stats = split[side]; return <div key={side} className={`sp-direction ${side}`}>
          <b>{side.toUpperCase()}</b>
          <div><span>Ops</span><strong>{stats.trades}</strong></div>
          <div><span>Profit</span><strong className={tone(stats.profit)}>{signed(stats.profit)}</strong></div>
          <div><span>WR</span><strong>{(stats.winRate * 100).toFixed(1)}%</strong></div>
          <div className="sp-meter"><i style={{ width: `${stats.winRate * 100}%` }} /></div>
        </div> })}
        <div className="sp-mini-stats">
          <div><span>Mejor trade</span><strong className="positive">{signed(m.best_trade)}</strong></div>
          <div><span>Peor trade</span><strong className="negative">{signed(m.worst_trade)}</strong></div>
          <div><span>Duración media</span><strong>{durationLabel(m.avg_duration_seconds)}</strong></div>
          <div><span>Comisión + swap</span><strong className={tone(m.commissions + m.swaps)}>{signed(m.commissions + m.swaps)}</strong></div>
          <div><span>Trades / mes</span><strong>{fmt(m.trades_per_month, 1)}</strong></div>
          <div><span>SQN</span><strong>{fmt(m.sqn)}</strong></div>
        </div>
      </div>
      <div className="sp-hours">
        <h4>Distribución por hora de apertura (UTC)</h4>
        {hasHours ? <div className="sp-hours-canvas"><ResponsiveContainer width="100%" height="100%">
          <BarChart data={hours} margin={{ top: 8, right: 4, left: 0, bottom: 0 }}>
            <CartesianGrid stroke="#1c303d" strokeDasharray="3 3" vertical={false} />
            <XAxis dataKey="label" tick={{ fill: '#78909f', fontSize: 11 }} axisLine={false} tickLine={false} interval={2} />
            <YAxis tickFormatter={value => fmt(value, 0)} tick={{ fill: '#78909f', fontSize: 11 }} axisLine={false} tickLine={false} width={50} />
            <ReferenceLine y={0} stroke="#475569" />
            <Tooltip cursor={{ fill: '#ffffff08' }} content={({ active, payload }) => { const item = active ? payload?.[0]?.payload as typeof hours[number] | undefined : undefined; return item ? <div className="analytics-tooltip"><strong>{item.hour}:00 UTC</strong><dl><dt>Profit</dt><dd className={tone(item.profit)}>{signed(item.profit)}</dd><dt>Operaciones</dt><dd>{item.trades}</dd></dl></div> : null }} />
            <Bar dataKey="profit" radius={[3, 3, 3, 3]} isAnimationActive={false}>{hours.map(item => <Cell key={item.hour} fill={item.profit >= 0 ? GREEN : RED} />)}</Bar>
          </BarChart>
        </ResponsiveContainer></div> : <div className="analytics-empty">Sin operaciones.</div>}
        <small className="muted">Verde = profit positivo · Rojo = profit negativo{detail.trades.length < m.trades ? ` · últimas ${detail.trades.length} operaciones` : ''}</small>
      </div>
    </div>
  </Section>
}

function TradeHistory({ detail, strategy }: { detail: StrategyDetails; strategy: Strategy }) {
  const trades = useMemo(() => [...detail.trades].sort((a, b) => b.close_time_msc - a.close_time_msc), [detail.trades])
  const [page, setPage] = useState(0)
  useEffect(() => setPage(0), [detail.trades])
  const pages = Math.max(1, Math.ceil(trades.length / PAGE_SIZE))
  const visible = trades.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE)
  function downloadCsv() {
    const blob = new Blob([tradesToCsv(trades)], { type: 'text/csv;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `trades-${strategy.id}-${(strategy.display_name || strategy.sqx_name || 'strategy').replace(/[^\w.-]+/g, '_')}.csv`
    link.click()
    URL.revokeObjectURL(url)
  }
  return <Section eyebrow={`HISTORIAL DE OPERACIONES (${trades.length})`} className="sp-wide"
    actions={trades.length > 0 && <button type="button" className="button" onClick={downloadCsv}>↓ CSV</button>}>
    {trades.length ? <>
      <div className="sp-table-wrap"><table className="sp-trades">
        <thead><tr><th>Ticket</th><th>Símbolo</th><th>Tipo</th><th className="num">Vol</th><th>Apertura</th><th className="num">P. aper.</th><th>Cierre</th><th className="num">P. cie.</th><th className="num">Swap</th><th className="num">Comis.</th><th className="num">Profit</th><th>Magic</th><th>Comentario</th></tr></thead>
        <tbody>{visible.map(trade => <tr key={trade.trade_key || `${trade.terminal_id}-${trade.deal_ticket}`}>
          <td className="muted">{trade.position_id || trade.deal_ticket}</td>
          <td><strong>{trade.symbol}</strong>{trade.source_role === 'historical' && <small className="sp-hist" title={trade.source_account}>hist.</small>}</td>
          <td><span className={`sp-side ${isBuy(trade.direction) ? 'buy' : isSell(trade.direction) ? 'sell' : ''}`}>{isBuy(trade.direction) ? 'BUY' : isSell(trade.direction) ? 'SELL' : trade.direction}</span></td>
          <td className="num">{fmt(trade.volume)}</td>
          <td>{dateTime(trade.open_time_msc)}</td>
          <td className="num">{price(trade.open_price)}</td>
          <td>{dateTime(trade.close_time_msc)}</td>
          <td className="num">{price(trade.close_price)}</td>
          <td className={`num ${tone(trade.swap)}`}>{fmt(trade.swap)}</td>
          <td className={`num ${tone(trade.commission)}`}>{fmt(trade.commission)}</td>
          <td className={`num sp-profit ${tone(trade.net_profit)}`}>{signed(trade.net_profit)}</td>
          <td className="muted">{trade.magic}</td>
          <td className="muted sp-comment" title={trade.comment}>{trade.comment || '—'}</td>
        </tr>)}</tbody>
      </table></div>
      <div className="sp-pager"><span>Mostrando {page * PAGE_SIZE + 1}–{Math.min(trades.length, (page + 1) * PAGE_SIZE)} de {trades.length}{detail.trades.length < strategy.metrics.trades ? ` (últimas ${detail.trades.length} de ${strategy.metrics.trades})` : ''}</span><div><button type="button" disabled={page === 0} onClick={() => setPage(page - 1)} aria-label="Página anterior">‹</button><strong>{page + 1}/{pages}</strong><button type="button" disabled={page >= pages - 1} onClick={() => setPage(page + 1)} aria-label="Página siguiente">›</button></div></div>
    </> : <div className="analytics-empty">Sin operaciones en el período seleccionado.</div>}
  </Section>
}

export function StrategyPerformanceView({ strategy, detail }: { strategy: Strategy; detail: StrategyDetails }) {
  const monthly = useMemo(() => monthlyProfit(detail.equity_curve), [detail.equity_curve])
  const peaks = useMemo(() => equityWithPeaks(detail.equity_curve), [detail.equity_curve])
  return <div className="sp-view">
    {strategy.metrics.trades < LOW_SAMPLE && <div className="sp-warning">Datos insuficientes: {strategy.metrics.trades} trades cerrados (se recomiendan ≥ {LOW_SAMPLE} para conclusiones fiables).</div>}
    <KpiTiles strategy={strategy} maxDdPct={peaks.maxDrawdownPct} />
    <EquityChart detail={detail} />
    <div className="sp-pair">
      <MonthlyBars rows={monthly} />
      <ProfitCalendar curve={detail.equity_curve} />
    </div>
    <MonthlyDistribution rows={monthly} />
    <AdvancedStats strategy={strategy} detail={detail} />
    <TradeHistory detail={detail} strategy={strategy} />
  </div>
}
