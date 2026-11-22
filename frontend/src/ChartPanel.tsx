import { useEffect, useRef, useState } from 'react'
import { subscribeTheme, themeColor } from './ThemeControls'
import { createChart, CrosshairMode, type IChartApi, type ISeriesApi, type Time } from 'lightweight-charts'
import { api } from './api'

type ChartPayload = {
  symbol?: string; timeframe?: string; message?: string; pending_refresh?: boolean; request_id?: string | null; response_request_id?: string | null; chart_ready?: boolean;
  candles: Array<{ time: number; open: number; high: number; low: number; close: number }>;
  markers: Array<{ time: number; position: 'aboveBar' | 'belowBar'; color: string; shape: 'arrowUp' | 'arrowDown' | 'circle'; text: string }>;
}

export default function ChartPanel({ strategyId, accountQuery }: { strategyId: number; accountQuery: string }) {
  const host = useRef<HTMLDivElement>(null)
  const chart = useRef<IChartApi | null>(null)
  const candles = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const fittedSource = useRef<string | null>(null)
  const requestSequence = useRef(0)
  const [timeframe, setTimeframe] = useState('H1')
  const [data, setData] = useState<ChartPayload | null>(null)
  const [message, setMessage] = useState('Loading chart…')

  async function load(refresh = false, requestId?: string, requestedTimeframe = timeframe, isCurrent = () => true) {
    const end = Math.floor(Date.now() / 1000)
    const start = end - 90 * 86400
    try {
      const params = new URLSearchParams(accountQuery)
      params.set('timeframe', requestedTimeframe)
      params.set('start', String(start))
      params.set('end', String(end))
      params.set('refresh', String(refresh))
      if (requestId) params.set('request_id', requestId)
      const payload = await api<ChartPayload>(`/api/chart/${strategyId}?${params}`)
      if (!isCurrent()) return
      setData(payload)
      if (payload.message) setMessage(payload.message)
      else if (requestId && !payload.chart_ready) setMessage('Requesting candles from MT5…')
      else if (payload.candles.length) setMessage('')
      else setMessage('No candles are available for this symbol and period.')
      return payload
    } catch (error) { setMessage(error instanceof Error ? error.message : 'Could not load the chart') }
  }

  useEffect(() => {
    let cancelled = false
    let timer: number | undefined
    const sequence = ++requestSequence.current
    const isCurrent = () => !cancelled && requestSequence.current === sequence
    async function requestAndPoll() {
      // Render persisted candles first. Requesting a fresh snapshot can require
      // a round trip to a remote MT5 terminal, so it must not delay the chart.
      setData(null)
      setMessage(`Loading ${timeframe} chart…`)
      await load(false, undefined, timeframe, isCurrent)
      if (cancelled) return
      const initial = await load(true, undefined, timeframe, isCurrent)
      const requestId = initial?.request_id
      if (!requestId || cancelled) return
      for (let attempt = 0; attempt < 12 && !cancelled; attempt += 1) {
        await new Promise(resolve => { timer = window.setTimeout(resolve, 1000) })
        const next = await load(false, requestId, timeframe, isCurrent)
        if (next?.chart_ready) return
      }
      if (!cancelled) setMessage('MT5 did not return candles in time. Check that the terminal and DashboardBridge are running, then retry.')
    }
    void requestAndPoll()
    return () => { cancelled = true; if (timer) window.clearTimeout(timer) }
  }, [strategyId, timeframe, accountQuery])
  useEffect(() => {
    if (!host.current) return
    const palette = () => ({
      layout: { background: { color: themeColor('--tt-surface') }, textColor: themeColor('--tt-muted') },
      grid: { vertLines: { color: themeColor('--tt-raised') }, horzLines: { color: themeColor('--tt-raised') } },
      rightPriceScale: { borderColor: themeColor('--tt-line') },
      timeScale: { borderColor: themeColor('--tt-line'), timeVisible: true },
    })
    const candleColors = () => {
      const up = themeColor('--tt-good'), down = themeColor('--tt-bad')
      return { upColor: up, downColor: down, wickUpColor: up, wickDownColor: down }
    }
    const instance = createChart(host.current, { height: 470, crosshair: { mode: CrosshairMode.Normal }, ...palette() })
    candles.current = instance.addCandlestickSeries({ borderVisible: false, ...candleColors() })
    const unsubscribeTheme = subscribeTheme(() => { instance.applyOptions(palette()); candles.current?.applyOptions(candleColors()) })
    chart.current = instance
    const observer = new ResizeObserver(() => host.current && instance.applyOptions({ width: host.current.clientWidth }))
    observer.observe(host.current)
    return () => { unsubscribeTheme(); observer.disconnect(); instance.remove(); chart.current = null; candles.current = null; fittedSource.current = null }
  }, [])

  useEffect(() => {
    if (!chart.current || !candles.current) return
    if (!data?.candles.length) {
      candles.current.setData([])
      candles.current.setMarkers([])
      return
    }
    candles.current.setData(data.candles.map(item => ({ ...item, time: item.time as Time })))
    candles.current.setMarkers(data.markers.map(item => ({ ...item, time: item.time as Time })))

    // MT5 replies can arrive just after the user releases the wheel.  Updating
    // the existing series preserves the visible range instead of recreating the
    // chart and fitting all candles again, which was the source of the jump.
    const source = `${strategyId}:${timeframe}`
    if (fittedSource.current !== source) {
      chart.current.timeScale().fitContent()
      fittedSource.current = source
    }
  }, [data, strategyId, timeframe])

  return <section className="panel chart-panel">
    <div className="panel-heading">
      <div><span className="eyebrow">BROKER PRICE</span><h2>{data?.symbol || 'Strategy chart'}</h2></div>
      <div className="actions">
        <select value={timeframe} onChange={event => setTimeframe(event.target.value)}>{['M1','M5','M15','M30','H1','H4','D1'].map(tf => <option key={tf}>{tf}</option>)}</select>
        <button className="button" onClick={() => { void load(true) }}>Request from MT5</button>
      </div>
    </div>
    {message && <div className="empty-state">{message}</div>}
    <div ref={host} className="chart-host" />
    <div className="legend"><span><i className="dot long" /> Long entry</span><span><i className="dot short" /> Short entry</span><span><i className="dot exit" /> Exit / P&amp;L</span></div>
  </section>
}
