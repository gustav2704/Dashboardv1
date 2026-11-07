import { useState } from 'react'
import { Bar, BarChart, CartesianGrid, Cell, Legend, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { buildAccountPerformance } from './performanceAccountAnalytics'
import './AccountPerformance.css'

type ChartTab = 'growth' | 'balance' | 'profit' | 'drawdown' | 'margin'
const money = (value: number | null | undefined) => value == null || !Number.isFinite(value) ? 'N/D' : value.toLocaleString('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2 })
const number = (value: number | null | undefined, digits = 2) => value == null || !Number.isFinite(value) ? 'N/D' : value.toLocaleString('es-ES', { minimumFractionDigits: digits, maximumFractionDigits: digits })
const signed = (value: number | null | undefined, suffix = '') => value == null || !Number.isFinite(value) ? 'N/D' : `${value >= 0 ? '+' : ''}${number(value)}${suffix}`
const percent = (value: number | null | undefined) => value == null ? 'N/D' : `${number(value)}%`
function duration(seconds: number) {
  const minutes = Math.round(seconds / 60), days = Math.floor(minutes / 1440), hours = Math.floor((minutes % 1440) / 60), mins = minutes % 60
  return `${days ? `${days} d ` : ''}${hours} h ${mins} min`
}
function timeLabel(value: number) { return new Date(value).toLocaleDateString('es-ES', { day: '2-digit', month: 'short', year: '2-digit' }) }

function Scope({ global = false }: { global?: boolean }) { return <span className={`performance-scope ${global ? 'global' : ''}`}>{global ? 'Cuenta completa' : 'Filtros actuales'}</span> }
function Metric({ label, value, detail, tone }: { label: string; value: string; detail?: string; tone?: 'positive' | 'negative' }) {
  return <div className="account-performance-metric"><span>{label}</span><strong className={tone ? `value-${tone === 'positive' ? 'good' : 'bad'}` : ''}>{value}</strong>{detail && <small>{detail}</small>}</div>
}

export function Summary({ analytics, accountLabel }: { analytics: ReturnType<typeof buildAccountPerformance>; accountLabel: string }) {
  const a = analytics.account, o = analytics.overview
  return <>
    <div className="account-performance-scope-heading"><div><span className="eyebrow">ACCOUNT SNAPSHOT</span><h3>{accountLabel}</h3></div><Scope global/></div>
    <div className="account-performance-metrics account-global-metrics">
      <Metric label="Balance" value={money(a.balance)} detail={`${a.accounts} cuenta${a.accounts === 1 ? '' : 's'}`}/>
      <Metric label="Equity" value={money(a.equity)} detail={a.balance ? `${percent(a.equity / a.balance * 100)} del balance` : undefined}/>
      <Metric label="Margen usado" value={money(a.margin)} detail={a.margin ? `Nivel ${percent(a.equity / a.margin * 100)}` : 'Sin margen utilizado'}/>
      <Metric label="Margen libre" value={money(a.freeMargin)} detail={a.capturedAt ? `Actualizado ${new Date(a.capturedAt).toLocaleString('es-ES')}` : 'Sin snapshot'}/>
    </div>
    <div className="account-performance-scope-heading compact"><div><span className="eyebrow">FILTERED PERFORMANCE</span><h3>Resultado del alcance seleccionado</h3></div><Scope/></div>
    <div className="account-performance-metrics">
      <Metric label="Ganancia" value={percent(o.gainPct)} detail={`Capital inicial ${analytics.capitalConfigured ? 'configurado' : 'estimado'} ${money(analytics.capital)}`} tone={(o.gainPct || 0) >= 0 ? 'positive' : 'negative'}/>
      <Metric label="P/L neto" value={money(o.profit)} detail={`${o.trades} trades · ${o.wins}W / ${o.losses}L`} tone={o.profit >= 0 ? 'positive' : 'negative'}/>
      <Metric label={o.movementLabel} value={o.movement == null ? 'N/D' : signed(o.movement)} detail={o.movement == null ? 'No se mezclan pips y puntos' : 'Movimiento direccional acumulado'}/>
      <Metric label="Lotes" value={number(o.lots)} detail={`Win rate ${percent(o.trades ? o.wins / o.trades * 100 : null)}`}/>
      <Metric label="Drawdown máximo" value={`${money(o.maxDrawdown)} · ${percent(o.maxDrawdownPct)}`} detail="Curva filtrada" tone="negative"/>
      <Metric label="Floating filtrado" value={money(a.filteredFloating)} detail="Posiciones abiertas · filtros actuales" tone={a.filteredFloating >= 0 ? 'positive' : 'negative'}/>
      <Metric label="Costes" value={money(o.commissions + o.swaps)} detail={`Comisión ${money(o.commissions)} · swap ${money(o.swaps)}`}/>
    </div>
  </>
}

export function Periods({ analytics }: { analytics: ReturnType<typeof buildAccountPerformance> }) {
  return <div className="table-scroll"><table className="account-performance-table"><thead><tr><th>Período</th><th>Ganancia (Δ)</th><th>P/L (Δ)</th><th>{analytics.overview.movementLabel} (Δ)</th><th>Win % (Δ)</th><th>Trades (Δ)</th><th>Lotes (Δ)</th></tr></thead><tbody>{analytics.periods.map(row => <tr key={row.key}><th>{row.label}</th><td>{percent(row.gainPct)} <small>({signed(row.gainDifference, '%')})</small></td><td className={row.profit >= 0 ? 'value-good' : 'value-bad'}>{money(row.profit)} <small>({signed(row.profitDifference)})</small></td><td>{row.movement == null ? 'N/D' : signed(row.movement)} <small>({signed(row.movementDifference)})</small></td><td>{percent(row.winRate)} <small>({signed(row.winRateDifference, '%')})</small></td><td>{row.trades} <small>({signed(row.tradesDifference)})</small></td><td>{number(row.lots)} <small>({signed(row.lotsDifference)})</small></td></tr>)}</tbody></table></div>
}

export function Statistics({ analytics }: { analytics: ReturnType<typeof buildAccountPerformance> }) {
  const s = analytics.advanced, o = analytics.overview
  const stats = [
    ['Trades', String(s.trades), `${s.wins} ganadoras · ${s.losses} perdedoras`],
    ['Largos ganados', `${s.longTrades ? percent(s.longWins / s.longTrades * 100) : 'N/D'}`, `${s.longWins}/${s.longTrades}`],
    ['Cortos ganados', `${s.shortTrades ? percent(s.shortWins / s.shortTrades * 100) : 'N/D'}`, `${s.shortWins}/${s.shortTrades}`],
    ['Profit Factor', number(s.profitFactor), 'Beneficio bruto / pérdida bruta'],
    ['Desviación estándar', money(s.deviation), 'P/L neto por trade'],
    ['Sharpe por trade', number(s.tradeSharpe), 'Media / desviación · no anualizado'],
    ['Z-Score', number(s.zScore), s.zProbability == null ? 'Muestra insuficiente' : `Probabilidad ${percent(s.zProbability)}`],
    ['Expectancy', money(s.expectancy), 'Resultado medio por trade'],
    ['Ganancia media', money(s.averageWin), 'Trades positivos'],
    ['Pérdida media', money(s.averageLoss), 'Trades negativos'],
    ['Mejor trade', money(s.bestTrade), 'P/L neto'], ['Peor trade', money(s.worstTrade), 'P/L neto'],
    ['Duración media', duration(s.averageDuration), `Mediana ${duration(s.medianDuration)}`],
    ['Lotes', number(o.lots), `Comisiones ${money(o.commissions)}`],
  ]
  return <div className="account-stat-grid">{stats.map(([label, value, detail]) => <Metric key={label} label={label} value={value} detail={detail}/>)}</div>
}

export function Assets({ analytics }: { analytics: ReturnType<typeof buildAccountPerformance> }) {
  return <div className="table-scroll"><table className="account-performance-table asset-performance-table"><thead><tr><th rowSpan={2}>Activo</th><th colSpan={3}>Largos</th><th colSpan={3}>Cortos</th><th colSpan={5}>Total</th></tr><tr><th>Trades</th><th>Mov.</th><th>P/L</th><th>Trades</th><th>Mov.</th><th>P/L</th><th>Trades</th><th>{analytics.overview.movementLabel}</th><th>P/L</th><th>Win</th><th>Loss</th></tr></thead><tbody>{analytics.assets.map(row => <tr key={row.symbol}><th>{row.symbol}</th><td>{row.longs.trades}</td><td>{number(row.longs.movement, 1)}</td><td>{money(row.longs.profit)}</td><td>{row.shorts.trades}</td><td>{number(row.shorts.movement, 1)}</td><td>{money(row.shorts.profit)}</td><td>{row.trades}</td><td>{number(row.movement, 1)}</td><td className={row.profit >= 0 ? 'value-good' : 'value-bad'}>{money(row.profit)}</td><td>{row.wins} ({number(row.winRate, 0)}%)</td><td>{row.losses} ({number(100-row.winRate, 0)}%)</td></tr>)}</tbody></table></div>
}

export function WinnersLosers({ rows, title }: { rows: Array<{ key: string; label: string; wins: number; losses: number; total: number }>; title: string }) {
  return <div className="performance-temporal-chart"><div className="chart-title"><strong>{title}</strong><small>UTC+02:00 · <Scope/></small></div><ResponsiveContainer width="100%" height={360}><BarChart data={rows.filter(row => row.total)} margin={{ top: 18, right: 18, bottom: 8, left: 0 }}><CartesianGrid stroke="#1c303d" vertical={false}/><XAxis dataKey="label" tick={{ fill: '#8ba0ad', fontSize: 11 }}/><YAxis allowDecimals={false} tick={{ fill: '#718895', fontSize: 10 }}/><Tooltip/><Legend/><Bar dataKey="wins" name="Ganadoras" stackId="result" fill="#a3d43f"/><Bar dataKey="losses" name="Perdedoras" stackId="result" fill="#b68ae0" radius={[4,4,0,0]}/></BarChart></ResponsiveContainer></div>
}

export function DurationView({ analytics }: { analytics: ReturnType<typeof buildAccountPerformance> }) {
  return <div className="duration-view"><div className="account-performance-metrics"><Metric label="Duración media" value={duration(analytics.advanced.averageDuration)} detail="Tiempo entre apertura y cierre"/><Metric label="Mediana" value={duration(analytics.advanced.medianDuration)} detail={`${analytics.advanced.trades} trades`}/></div><ResponsiveContainer width="100%" height={340}><BarChart data={analytics.durations} layout="vertical" margin={{ left: 55, right: 25 }}><CartesianGrid stroke="#1c303d" horizontal={false}/><XAxis type="number" tick={{ fill: '#718895', fontSize: 10 }}/><YAxis type="category" dataKey="label" tick={{ fill: '#8ba0ad', fontSize: 11 }}/><Tooltip/><Bar dataKey="count" name="Trades" fill="#38bdf8" radius={[0,4,4,0]}/></BarChart></ResponsiveContainer></div>
}

export function Charts({ analytics }: { analytics: ReturnType<typeof buildAccountPerformance> }) {
  const [chart, setChart] = useState<ChartTab>('growth')
  const filtered = analytics.curves.profit, account = analytics.curves.account
  const titles: Record<ChartTab, string> = { growth: 'Growth', balance: 'Balance', profit: 'Profit', drawdown: 'Drawdown', margin: 'Margin' }
  const data = chart === 'balance' || chart === 'margin' ? account : filtered
  return <div className="performance-chart-panel"><div className="performance-subtabs">{(Object.keys(titles) as ChartTab[]).map(key => <button type="button" key={key} className={chart === key ? 'active' : ''} onClick={() => setChart(key)}>{titles[key]}</button>)}</div><div className="chart-title"><strong>{titles[chart]}</strong><Scope global={chart === 'balance' || chart === 'margin'}/></div>{!data.length ? <div className="empty-state">No hay puntos suficientes para este gráfico.</div> : <ResponsiveContainer width="100%" height={410}>{chart === 'drawdown' ? <BarChart data={filtered}><CartesianGrid stroke="#1c303d"/><XAxis dataKey="time_msc" tickFormatter={timeLabel} tick={{ fill:'#718895',fontSize:10 }}/><YAxis tickFormatter={value => `${Number(value).toFixed(1)}%`} tick={{fill:'#718895',fontSize:10}}/><Tooltip labelFormatter={value => new Date(Number(value)).toLocaleString('es-ES')}/><Bar dataKey="drawdown_pct" name="Drawdown %" fill="#fb4d5d"/></BarChart> : <LineChart data={data as any}><CartesianGrid stroke="#1c303d"/><XAxis dataKey="time_msc" tickFormatter={timeLabel} tick={{fill:'#718895',fontSize:10}}/><YAxis yAxisId="left" tick={{fill:'#718895',fontSize:10}} tickFormatter={value => chart === 'growth' || chart === 'margin' ? `${Number(value).toFixed(1)}%` : Number(value).toLocaleString('en-US',{maximumFractionDigits:0})}/>{chart === 'margin' && <YAxis yAxisId="right" orientation="right" tick={{fill:'#b68ae0',fontSize:10}} tickFormatter={value => money(Number(value))}/>}<Tooltip labelFormatter={value => new Date(Number(value)).toLocaleString('es-ES')}/>{chart === 'growth' && <><ReferenceLine yAxisId="left" y={0} stroke="#64748b"/><Line yAxisId="left" dataKey="growth" name="Growth %" stroke="#2dd4bf" dot={false} strokeWidth={2}/></>}{chart === 'profit' && <><ReferenceLine yAxisId="left" y={0} stroke="#64748b"/><Line yAxisId="left" dataKey="profit" name="P/L acumulado" stroke="#fb7185" dot={false} strokeWidth={2}/></>}{chart === 'balance' && <><Line yAxisId="left" dataKey="balance" name="Balance" stroke="#fb5b48" dot={false}/><Line yAxisId="left" dataKey="equity" name="Equity" stroke="#38bdf8" dot={false}/></>}{chart === 'margin' && <><Line yAxisId="left" dataKey="margin_level" name="Nivel de margen %" stroke="#3b82f6" dot={false}/><Line yAxisId="right" dataKey="filtered_floating_profit" name="Floating filtrado" stroke="#b68ae0" dot={false}/></>}</LineChart>}</ResponsiveContainer>}</div>
}

