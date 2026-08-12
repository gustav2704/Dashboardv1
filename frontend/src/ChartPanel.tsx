import { useEffect, useRef, useState } from 'react'
import { createChart, CrosshairMode, type IChartApi, type Time } from 'lightweight-charts'
import { api } from './api'

type ChartPayload = {
  symbol?: string; timeframe?: string; message?: string; pending_refresh?: boolean; request_id?: string | null; response_request_id?: string | null; chart_ready?: boolean;
  candles: Array<{ time: number; open: number; high: number; low: number; close: number }>;
  markers: Array<{ time: number; position: 'aboveBar' | 'belowBar'; color: string; shape: 'arrowUp' | 'arrowDown' | 'circle'; text: string }>;
}

export default function ChartPanel({ strategyId, accountQuery }: { strategyId: number; accountQuery: string }) {
  const host = useRef<HTMLDivElement>(null)
  const chart = useRef<IChartApi | null>(null)
  const [timeframe, setTimeframe] = useState('H1')
  const [data, setData] = useState<ChartPayload | null>(null)
  const [message, setMessage] = useState('Loading chart…')

  async function load(refresh = false, requestId?: string) {
    const end = Math.floor(Date.now() / 1000)
    const start = end - 90 * 86400
    try {
      const params = new URLSearchParams(accountQuery)
      params.set('timeframe', timeframe)
      params.set('start', String(start))
      params.set('end', String(end))
      params.set('refresh', String(refresh))
      if (requestId) params.set('request_id', requestId)
      const payload = await api<ChartPayload>(`/api/chart/${strategyId}?${params}`)
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
    async function requestAndPoll() {
      // Render persisted candles first. Requesting a fresh snapshot can require
      // a round trip to a remote MT5 terminal, so it must not delay the chart.
      await load(false)
      if (cancelled) return
      const initial = await load(true)
      const requestId = initial?.request_id
      if (!requestId || cancelled) return
      for (let attempt = 0; attempt < 12 && !cancelled; attempt += 1) {
        await new Promise(resolve => { timer = window.setTimeout(resolve, 1000) })
        const next = await load(false, requestId)
        if (next?.chart_ready) return
      }
      if (!cancelled) setMessage('MT5 did not return candles in time. Check that the terminal and DashboardBridge are running, then retry.')
    }
    void requestAndPoll()
    return () => { cancelled = true; if (timer) window.clearTimeout(timer) }
  }, [strategyId, timeframe, accountQuery])
  useEffect(() => {
    if (!host.current || !data?.candles.length) return
    chart.current?.remove()
    const instance = createChart(host.current, {
      height: 470,
      layout: { background: { color: '#0b151f' }, textColor: '#8da1b3' },
      grid: { vertLines: { color: '#172633' }, horzLines: { color: '#172633' } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: '#263847' },
      timeScale: { borderColor: '#263847', timeVisible: true },
    })
    const candles = instance.addCandlestickSeries({ upColor: '#2dd4bf', downColor: '#fb7185', borderVisible: false, wickUpColor: '#2dd4bf', wickDownColor: '#fb7185' })
    candles.setData(data.candles.map(item => ({ ...item, time: item.time as Time })))
    candles.setMarkers(data.markers.map(item => ({ ...item, time: item.time as Time })))
    instance.timeScale().fitContent()
    chart.current = instance
    const observer = new ResizeObserver(() => host.current && instance.applyOptions({ width: host.current.clientWidth }))
    observer.observe(host.current)
    return () => { observer.disconnect(); instance.remove(); chart.current = null }
  }, [data])

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
