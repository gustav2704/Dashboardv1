import { useMemo, useState } from 'react'
import { Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import type { JournalAnalysisData, JournalTrade } from './types'

const GREEN = '#2dd4bf'
const RED = '#fb7185'
const YELLOW = '#fbbf24'
const GRID = '#172633'
const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

function dayKey(ms: number) { const d = new Date(ms); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}` }
function monthKey(ms: number) { const d = new Date(ms); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}` }
function maxDrawdown(trades: JournalTrade[]) { let equity = 0; let peak = 0; let dd = 0; for (const t of [...trades].sort((a,b) => a.close_time_msc - b.close_time_msc)) { equity += t.net_profit; peak = Math.max(peak, equity); dd = Math.max(dd, peak - equity) } return dd }
function normalizePf(value: number) { return !Number.isFinite(value) ? 1 : value <= 0 ? 0 : Math.min(value, 3) / 3 }
function scoreColor(score: number) { return score >= 60 ? GREEN : score >= 40 ? YELLOW : RED }

export type BotRanking = { id: number; name: string; symbol: string; count: number; wins: number; net: number; volume: number; winRate: number; pf: number; dd: number; netPerLot: number | null; returnToDd: number; score: number; efficiencyScore: number }
export function buildBotRankings(trades: JournalTrade[]): BotRanking[] {
  const grouped = new Map<number, JournalTrade[]>()
  for (const trade of trades) grouped.set(trade.strategy_id, [...(grouped.get(trade.strategy_id) || []), trade])
  const rows = [...grouped.entries()].map(([id, items]) => {
    const net = items.reduce((sum, t) => sum + t.net_profit, 0)
    const grossProfit = items.filter(t => t.profit > 0).reduce((sum,t) => sum + t.profit, 0)
    const grossLoss = Math.abs(items.filter(t => t.profit <= 0).reduce((sum,t) => sum + t.profit, 0))
    const volume = items.reduce((sum,t) => sum + t.volume, 0)
    const dd = maxDrawdown(items)
    return { id, name: items[0].strategy_name, symbol: items[0].symbol, count: items.length, wins: items.filter(t => t.net_profit > 0).length, net, volume, winRate: items.filter(t => t.net_profit > 0).length / items.length * 100, pf: grossLoss ? grossProfit / grossLoss : grossProfit ? Infinity : 0, dd, netPerLot: volume > 0 ? net / volume : null, returnToDd: dd > 0 ? net / dd : net > 0 ? Infinity : 0, score: 0, efficiencyScore: 0 }
  })
  const maxNet = Math.max(0, ...rows.map(r => Math.max(r.net, 0))), maxDd = Math.max(0, ...rows.map(r => r.dd)), maxTrades = Math.max(0, ...rows.map(r => r.count))
  for (const r of rows) r.score = (maxNet ? Math.max(r.net, 0) / maxNet * 45 : 0) + r.winRate / 100 * 25 + normalizePf(r.pf) * 15 + (maxTrades ? r.count / maxTrades * 10 : 0) - (maxDd ? r.dd / maxDd * 20 : 0)
  const eligible = rows.filter(r => r.count >= 10 && r.netPerLot != null)
  const positive = (values: Array<number | null>, value: number | null) => value === Infinity ? 1 : value == null || value <= 0 ? 0 : value / Math.max(1, ...values.filter((v): v is number => v != null && Number.isFinite(v) && v > 0))
  for (const r of eligible) r.efficiencyScore = positive(eligible.map(x => x.netPerLot), r.netPerLot) * 35 + positive(eligible.map(x => x.returnToDd), r.returnToDd) * 30 + r.winRate / 100 * 15 + normalizePf(r.pf) * 15 + (maxTrades ? r.count / maxTrades * 5 : 0)
  return rows
}

function ChartTip({ active, payload, suffix = '' }: { active?: boolean; payload?: Array<{name:string;value:number;color:string}>; suffix?: string }) {
  if (!active || !payload?.length) return null
  return <div className="journal-tooltip">{payload.map(item => <div key={item.name}><span>{item.name}</span><strong>{item.value.toLocaleString('en-US', {maximumFractionDigits:2})}{suffix}</strong></div>)}</div>
}
function JournalChart({ title, children, wide = false }: { title: string; children: React.ReactNode; wide?: boolean }) { return <section className={`panel journal-chart${wide ? ' wide' : ''}`}><div className="journal-chart-title">{title}</div><div className="journal-chart-body">{children}</div></section> }

function Calendar({ data }: { data: JournalAnalysisData }) {
  const [view, setView] = useState<'year'|'month'>('month'), [metric, setMetric] = useState<'amount'|'pct'>('amount')
  const [cursor, setCursor] = useState(() => { const now = new Date(); return new Date(now.getFullYear(), now.getMonth(), 1) })
  const balance = Object.values(data.balances).reduce((sum,value) => sum + value, 0)
  const daily = useMemo(() => { const map = new Map<string,number>(); for (const t of data.trades) map.set(dayKey(t.close_time_msc), (map.get(dayKey(t.close_time_msc)) || 0) + t.net_profit); return map }, [data.trades])
  const valueLabel = (value: number) => metric === 'pct'
    ? balance > 0 ? `${value >= 0 ? '+' : ''}${(value / balance * 100).toFixed(2)}%` : 'N/A'
    : `${value >= 0 ? '+ $' : '- $'}${Math.abs(value) >= 1000 ? `${(Math.abs(value) / 1000).toFixed(1)}k` : Math.abs(value).toFixed(0)}`
  const shift = (delta:number) => setCursor(new Date(cursor.getFullYear() + (view === 'year' ? delta : 0), cursor.getMonth() + (view === 'month' ? delta : 0), 1))
  const yearMonths = Array.from({length:12}, (_, month) => {
    const trades = data.trades.filter(trade => { const date = new Date(trade.close_time_msc); return date.getFullYear() === cursor.getFullYear() && date.getMonth() === month })
    return { month, trades: trades.length, value: trades.reduce((sum, trade) => sum + trade.net_profit, 0) }
  })
  const first = new Date(cursor.getFullYear(), cursor.getMonth(), 1), dayCount = new Date(cursor.getFullYear(), cursor.getMonth() + 1, 0).getDate()
  return <section className="panel journal-calendar"><div className="journal-calendar-head"><div><span className="eyebrow">DAILY RESULTS</span><h2>Profitability calendar</h2></div><div className="calendar-controls"><button className={view === 'year' ? 'active' : ''} onClick={() => setView('year')}>Year</button><button className={view === 'month' ? 'active' : ''} onClick={() => setView('month')}>Month</button><button onClick={() => shift(-1)}>‹</button><strong>{view === 'year' ? cursor.getFullYear() : `${MONTHS[cursor.getMonth()]} ${cursor.getFullYear()}`}</strong><button onClick={() => shift(1)}>›</button><button className={metric === 'amount' ? 'active' : ''} onClick={() => setMetric('amount')}>Amount</button><button className={metric === 'pct' ? 'active' : ''} onClick={() => setMetric('pct')}>%</button></div></div>{view === 'year' ? <div className="calendar-year-grid">{yearMonths.map(item => <button type="button" key={item.month} className={`calendar-month-card ${item.trades ? item.value >= 0 ? 'positive' : 'negative' : ''}`} onClick={() => { setCursor(new Date(cursor.getFullYear(), item.month, 1)); setView('month') }}><span>{MONTHS[item.month]}</span><strong>{item.trades ? valueLabel(item.value) : '—'}</strong>{item.trades > 0 && <small>{item.trades} ops</small>}</button>)}</div> : <div className="calendar-months month"><div className="calendar-month"><strong>{MONTHS[cursor.getMonth()]}</strong><div className="calendar-weekdays">{['M','T','W','T','F','S','S'].map((day,index)=><span key={index}>{day}</span>)}</div><div className="calendar-days">{Array.from({length:(first.getDay()+6)%7},(_,index)=><span key={`empty-${index}`}/>)}{Array.from({length:dayCount},(_,index) => { const date = new Date(cursor.getFullYear(),cursor.getMonth(),index+1), value = daily.get(dayKey(date.getTime())) || 0; return <span key={index} className={value > 0 ? 'positive' : value < 0 ? 'negative' : ''} title={`${dayKey(date.getTime())}: ${value.toFixed(2)}`}><small>{index+1}</small>{value !== 0 && <b>{valueLabel(value)}</b>}</span> })}</div></div></div>}</section>
}

export function TopTenCharts({ data, accountLabel }: { data: JournalAnalysisData | null; accountLabel: string }) {
  const rankings = useMemo(() => buildBotRankings(data?.trades || []), [data])
  const eligible = rankings.filter(r => r.count >= 10)
  const charts = [
    {title:`Top 10 mejores bots por eficiencia — ${accountLabel} — orientativo`, key:'efficiencyScore' as const, rows:eligible.filter(r => r.netPerLot != null).sort((a,b)=>b.efficiencyScore-a.efficiencyScore).slice(0,10)},
    {title:`Ranking compuesto de bots — ${accountLabel} — orientativo`, key:'score' as const, rows:[...eligible].sort((a,b)=>b.score-a.score).slice(0,10)},
    {title:`Top 10 porcentaje ganador por bot — ${accountLabel} — mín. 10 trades`, key:'winRate' as const, rows:[...eligible].sort((a,b)=>b.winRate-a.winRate || b.net-a.net).slice(0,10)},
  ]
  return <section className="top-ten-section"><div className="top-ten-heading"><span className="eyebrow">JOURNAL RANKINGS</span><h2>Todos los Top 10</h2></div><div className="top-ten-grid">{charts.map(chart => <JournalChart title={chart.title} key={chart.key}><ResponsiveContainer><BarChart data={chart.rows} layout="vertical" margin={{left:25,right:22}}><CartesianGrid stroke={GRID} horizontal={false}/><XAxis type="number" tick={{fill:'#74899a',fontSize:9}}/><YAxis type="category" dataKey="name" width={170} tick={{fill:'#a8bdca',fontSize:9}}/><Tooltip content={<ChartTip suffix={chart.key === 'winRate' ? '%' : ''}/>} /><Bar dataKey={chart.key} radius={[0,3,3,0]}>{chart.rows.map(row=><Cell key={row.id} fill={chart.key === 'winRate' ? GREEN : scoreColor(row[chart.key])}/>)}</Bar></BarChart></ResponsiveContainer></JournalChart>)}</div></section>
}

export default function JournalAnalysis({ data }: { data: JournalAnalysisData | null }) {
  const charts = useMemo(() => {
    const trades = data?.trades || [], equity: Array<{label:string;equity:number}> = []; let total = 0
    for (const t of trades) { total += t.net_profit; equity.push({label:new Date(t.close_time_msc).toLocaleDateString(),equity:total}) }
    const monthlyMap = new Map<string,JournalTrade[]>(), weekday = DAYS.map(day=>({day,wins:0,count:0})), hourly = Array.from({length:24},(_,hour)=>({hour:`${String(hour).padStart(2,'0')}:00`,profit:0}))
    for (const t of trades) { const mk=monthKey(t.close_time_msc); monthlyMap.set(mk,[...(monthlyMap.get(mk)||[]),t]); const d=new Date(t.close_time_msc); weekday[d.getDay()].count++; if(t.net_profit>0) weekday[d.getDay()].wins++; hourly[d.getHours()].profit += t.net_profit }
    const monthly=[...monthlyMap].map(([month,items])=>({month,trades:items.length,drawdown:maxDrawdown(items)}))
    return {equity,monthly,weekday:weekday.map(x=>({...x,winRate:x.count ? x.wins/x.count*100 : 0})),hourly}
  }, [data])
  if (!data) return <div className="panel empty-state performance-empty">Loading journal analysis…</div>
  if (!data.trades.length) return <div className="panel empty-state performance-empty">No closed trades match the selected accounts and dates.</div>
  return <div className="journal-view"><div className="performance-intro"><div><span className="eyebrow">PORTFOLIO JOURNAL</span><h2>Journal analysis</h2></div><p>Current and historical trades attributed to the selected broker accounts.</p></div><div className="journal-grid"><JournalChart wide title="Equity curve — cumulative net profit"><ResponsiveContainer><AreaChart data={charts.equity}><defs><linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor={GREEN} stopOpacity={.35}/><stop offset="1" stopColor={GREEN} stopOpacity={0}/></linearGradient></defs><CartesianGrid stroke={GRID}/><XAxis dataKey="label" tick={{fill:'#74899a',fontSize:9}} minTickGap={30}/><YAxis tick={{fill:'#74899a',fontSize:9}}/><Tooltip content={<ChartTip/>}/><Area dataKey="equity" stroke={GREEN} fill="url(#equityFill)"/></AreaChart></ResponsiveContainer></JournalChart><JournalChart title="Trades executed by month"><ResponsiveContainer><BarChart data={charts.monthly}><CartesianGrid stroke={GRID}/><XAxis dataKey="month" tick={{fill:'#74899a',fontSize:9}}/><YAxis tick={{fill:'#74899a',fontSize:9}}/><Tooltip content={<ChartTip/>}/><Bar dataKey="trades" fill="#38bdf8"/></BarChart></ResponsiveContainer></JournalChart><JournalChart title="Win rate by weekday"><ResponsiveContainer><BarChart data={charts.weekday}><CartesianGrid stroke={GRID}/><XAxis dataKey="day" tick={{fill:'#74899a',fontSize:9}}/><YAxis domain={[0,100]} tick={{fill:'#74899a',fontSize:9}}/><Tooltip content={<ChartTip suffix="%"/>}/><Bar dataKey="winRate" fill={GREEN}/></BarChart></ResponsiveContainer></JournalChart><JournalChart title="Net profit by hour"><ResponsiveContainer><BarChart data={charts.hourly}><CartesianGrid stroke={GRID}/><XAxis dataKey="hour" interval={2} tick={{fill:'#74899a',fontSize:9}}/><YAxis tick={{fill:'#74899a',fontSize:9}}/><Tooltip content={<ChartTip/>}/><Bar dataKey="profit">{charts.hourly.map((x,i)=><Cell key={i} fill={x.profit >= 0 ? GREEN : RED}/>)}</Bar></BarChart></ResponsiveContainer></JournalChart><JournalChart title="Max drawdown by month"><ResponsiveContainer><LineChart data={charts.monthly}><CartesianGrid stroke={GRID}/><XAxis dataKey="month" tick={{fill:'#74899a',fontSize:9}}/><YAxis tick={{fill:'#74899a',fontSize:9}}/><Tooltip content={<ChartTip/>}/><Line dataKey="drawdown" stroke={RED} strokeWidth={2}/></LineChart></ResponsiveContainer></JournalChart></div><Calendar data={data}/></div>
}
