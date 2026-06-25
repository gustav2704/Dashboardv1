import { useEffect, useRef, useState } from 'react'
import { createChart, CrosshairMode, type IChartApi, type Time } from 'lightweight-charts'
import { api } from './api'

type ChartPayload = {
  symbol?: string; timeframe?: string; message?: string; pending_refresh?: boolean;
  candles: Array<{ time: number; open: number; high: number; low: number; close: number }>;
  markers: Array<{ time: number; position: 'aboveBar' | 'belowBar'; color: string; shape: 'arrowUp' | 'arrowDown' | 'circle'; text: string }>;
}

export default function ChartPanel({ strategyId }: { strategyId: number }) {
  const host = useRef<HTMLDivElement>(null)
  const chart = useRef<IChartApi | null>(null)
  const [timeframe, setTimeframe] = useState('H1')
  const [data, setData] = useState<ChartPayload | null>(null)
  const [message, setMessage] = useState('Cargando gráfico…')

  async function load(refresh = false) {
    const end = Math.floor(Date.now() / 1000)
    const start = end - 90 * 86400
    try {
      const payload = await api<ChartPayload>(`/api/chart/${strategyId}?timeframe=${timeframe}&start=${start}&end=${end}&refresh=${refresh}`)
      setData(payload)
      setMessage(payload.message || (payload.candles.length ? '' : refresh ? 'Solicitud enviada a MT5. Vuelve a actualizar en unos segundos.' : 'Aún no hay velas en caché.'))
    } catch (error) { setMessage(error instanceof Error ? error.message : 'No se pudo cargar el gráfico') }
  }

  useEffect(() => { load(false) }, [strategyId, timeframe])
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
      <div><span className="eyebrow">PRECIO DEL BROKER</span><h2>{data?.symbol || 'Gráfico de estrategia'}</h2></div>
      <div className="actions">
        <select value={timeframe} onChange={event => setTimeframe(event.target.value)}>{['M1','M5','M15','M30','H1','H4','D1'].map(tf => <option key={tf}>{tf}</option>)}</select>
        <button className="button" onClick={() => load(true)}>Solicitar a MT5</button>
      </div>
    </div>
    {message && <div className="empty-state">{message}</div>}
    <div ref={host} className="chart-host" />
    <div className="legend"><span><i className="dot long" /> Entrada long</span><span><i className="dot short" /> Entrada short</span><span><i className="dot exit" /> Salida / P&amp;L</span></div>
  </section>
}

