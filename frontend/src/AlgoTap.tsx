import { useEffect, useMemo, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { api } from './api'
import type { Strategy } from './types'
import type { AlgoTapEgt } from './algotapEgt'
import './AlgoTap.css'

type Num = number | null
export type AlgoTapAccount = {
  account_id: string; display_name: string; broker: string | null; server: string | null; platform: string | null; is_demo: boolean
  initial_balance: Num; balance: Num; equity: Num; margin_level: Num; live_at: string | null; floating_total: Num; current_month_profit: Num; total_profit: Num; total_trades: Num
  winrate: Num; profit_factor: Num; sharpe: Num; max_drawdown: Num; max_drawdown_pct: Num
  avg_win: Num; avg_loss: Num; avg_trade: Num; max_consec_wins: Num; max_consec_losses: Num; metric_confidence: string | null
  ea_count: Num; open_positions_count: Num; status_ok: Num; status_vigilancia: Num; status_alerta: Num
}
export type AlgoTapMagic = {
  account_id: string; magic: number | null; grade: string | null; score: Num; trade_count: Num
  profit_factor: Num; sharpe: Num; winrate: Num; ret_dd: Num; max_dd_pct: Num; stability: Num
  pillar_profit: Num; pillar_risk: Num; pillar_consistency: Num; computed_at: string | null
}
export type AlgoTapSnapshot = { synced_at: string; accounts: AlgoTapAccount[]; magics: AlgoTapMagic[]; egt?: AlgoTapEgt[]; warnings: string[] }
type AlgoTapStatus = { configured: boolean; snapshot: AlgoTapSnapshot | null }

const fmt = (value: Num, digits = 2) => value == null ? '—' : value.toLocaleString('es-ES', { minimumFractionDigits: digits, maximumFractionDigits: digits })
const pct = (value: Num) => value == null ? '—' : `${fmt(value, 1)}%`
const tone = (value: Num) => value == null ? '' : value >= 0 ? 'value-good' : 'value-bad'

/** Index dashboard strategies by "account|magic" so AlgoTAP rows show the local name. */
export function strategyIndex(strategies: Strategy[]) {
  const index = new Map<string, Strategy>()
  for (const strategy of strategies) {
    for (const magic of [...(strategy.magic_numbers || []), ...(strategy.historical_magic_numbers || [])]) {
      const key = `${strategy.account_login}|${magic}`
      if (!index.has(key)) index.set(key, strategy)
    }
  }
  return index
}

export default function AlgoTapPanel({ strategies, onSelect, onSnapshot }: { strategies: Strategy[]; onSelect: (strategyId: number) => void; onSnapshot?: (snapshot: AlgoTapSnapshot | null) => void }) {
  const [status, setStatus] = useState<AlgoTapStatus | null>(null)
  const [syncing, setSyncing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [accountFilter, setAccountFilter] = useState('all')

  useEffect(() => {
    api<AlgoTapStatus>('/api/algotap').then(setStatus).catch(err => setError(String(err?.message || err)))
  }, [])

  async function sync() {
    setSyncing(true)
    setError(null)
    try {
      const next = await api<AlgoTapStatus>('/api/algotap/sync', { method: 'POST', timeoutMs: 120_000 })
      setStatus(next)
      onSnapshot?.(next.snapshot)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setSyncing(false)
    }
  }

  const snapshot = status?.snapshot
  const byKey = useMemo(() => strategyIndex(strategies), [strategies])
  const accountName = useMemo(() => new Map((snapshot?.accounts || []).map(a => [a.account_id, a.display_name])), [snapshot])
  const magics = useMemo(() => (snapshot?.magics || [])
    .filter(m => accountFilter === 'all' || m.account_id === accountFilter)
    .sort((a, b) => (b.score ?? -Infinity) - (a.score ?? -Infinity)), [snapshot, accountFilter])

  return <section className="panel algotap-panel">
    <div className="panel-heading">
      <div>
        <span className="eyebrow">ALGOTAP ANALYZER · COPIA BAJO DEMANDA</span>
        <h2>KPIs de AlgoTAP</h2>
        <p className="help">{snapshot ? `Última copia: ${new Date(snapshot.synced_at).toLocaleString()}` : 'Aún no se ha copiado nada.'} Solo se consulta AlgoTAP al pulsar el botón.</p>
      </div>
      <button className="button refresh icon-command" onClick={() => { void sync() }} disabled={syncing || status?.configured === false}>
        <RefreshCw size={14} className={syncing ? 'spin' : undefined}/>{syncing ? 'Copiando…' : 'Sincronizar AlgoTAP'}
      </button>
    </div>
    {status?.configured === false && <div className="empty-state algotap-setup">
      Configura las credenciales en <code>data/algotap.env</code> y reinicia el backend:
      <pre>{'ALGOTAP_USER=tu_email_o_usuario\nALGOTAP_PASSWORD=tu_contraseña\n# o, si entras solo con Google:\n# ALGOTAP_REFRESH_TOKEN=...'}</pre>
    </div>}
    {error && <div className="error-banner">{error}</div>}
    {snapshot?.warnings?.length ? <div className="algotap-warnings">{snapshot.warnings.map(w => <div key={w}>⚠ {w}</div>)}</div> : null}

    {snapshot && <>
      <h3 className="algotap-section">Cuentas ({snapshot.accounts.length})</h3>
      <div className="guardian-table-wrap"><table className="guardian-table algotap-table">
        <thead><tr>
          <th>Cuenta</th><th>Tipo</th><th>Inicial</th><th title="Balance del último snapshot que AlgoTAP recibió del EA">Balance</th><th>Equity</th><th>Margen %</th><th>Flotante</th><th>Mes</th><th>Total</th><th>Trades</th><th>Win %</th><th>PF</th><th>Sharpe</th><th>Max DD</th><th>Max DD %</th><th>Racha −</th><th>EAs</th><th title="OK / vigilancia / alerta según AlgoTAP">Estado</th>
        </tr></thead>
        <tbody>{snapshot.accounts.map(a => <tr key={a.account_id}>
          <td><strong>{a.display_name}</strong><small>{a.account_id} · {a.broker || a.server || ''}</small></td>
          <td>{a.is_demo ? 'Demo' : 'Real'}</td>
          <td>{fmt(a.initial_balance)}</td>
          <td>{fmt(a.balance)}{a.live_at && <small>{new Date(a.live_at).toLocaleString()}</small>}</td>
          <td>{fmt(a.equity)}</td>
          <td>{a.margin_level == null ? '—' : `${fmt(a.margin_level, 0)}%`}</td>
          <td className={tone(a.floating_total)}>{fmt(a.floating_total)}</td>
          <td className={tone(a.current_month_profit)}>{fmt(a.current_month_profit)}</td>
          <td className={tone(a.total_profit)}>{fmt(a.total_profit)}</td>
          <td>{fmt(a.total_trades, 0)}</td>
          <td>{pct(a.winrate)}</td>
          <td>{fmt(a.profit_factor)}</td>
          <td>{fmt(a.sharpe)}</td>
          <td>{fmt(a.max_drawdown)}</td>
          <td>{pct(a.max_drawdown_pct)}</td>
          <td>{fmt(a.max_consec_losses, 0)}</td>
          <td>{fmt(a.ea_count, 0)}</td>
          <td className="algotap-status"><span className="ok">{a.status_ok ?? 0}</span>/<span className="watch">{a.status_vigilancia ?? 0}</span>/<span className="alert">{a.status_alerta ?? 0}</span></td>
        </tr>)}</tbody>
      </table></div>

      <div className="algotap-magic-heading">
        <h3 className="algotap-section">Score por magic ({magics.length})</h3>
        <select value={accountFilter} onChange={e => setAccountFilter(e.target.value)} aria-label="Filtrar por cuenta">
          <option value="all">Todas las cuentas</option>
          {snapshot.accounts.map(a => <option key={a.account_id} value={a.account_id}>{a.display_name}</option>)}
        </select>
      </div>
      {magics.length ? <div className="guardian-table-wrap"><table className="guardian-table algotap-table">
        <thead><tr>
          <th>Magic</th><th>Estrategia (dashboard)</th><th>Cuenta</th><th>Nota</th><th>Score</th><th>Trades</th><th>PF</th><th>Sharpe</th><th>Win %</th><th>Ret/DD</th><th>Max DD %</th><th>Estabilidad</th><th title="Pilares profit / riesgo / consistencia">Pilares P/R/C</th>
        </tr></thead>
        <tbody>{magics.map(m => {
          const local = byKey.get(`${m.account_id}|${m.magic}`)
          return <tr key={`${m.account_id}-${m.magic}`}>
            <td>{m.magic ?? '—'}</td>
            <td>{local ? <button type="button" className="link-button" onClick={() => onSelect(local.id)}>{local.display_name || local.sqx_name}</button> : <small>sin vincular</small>}</td>
            <td>{accountName.get(m.account_id) || m.account_id}</td>
            <td><span className={`algotap-grade grade-${(m.grade || '').charAt(0).toLowerCase()}`}>{m.grade || '—'}</span></td>
            <td>{fmt(m.score, 1)}</td>
            <td>{fmt(m.trade_count, 0)}</td>
            <td>{fmt(m.profit_factor)}</td>
            <td>{fmt(m.sharpe)}</td>
            <td>{pct(m.winrate)}</td>
            <td>{fmt(m.ret_dd)}</td>
            <td>{pct(m.max_dd_pct)}</td>
            <td>{fmt(m.stability)}</td>
            <td>{fmt(m.pillar_profit, 0)} / {fmt(m.pillar_risk, 0)} / {fmt(m.pillar_consistency, 0)}</td>
          </tr>
        })}</tbody>
      </table></div> : <div className="empty-state">Sin scores por magic para esta selección.</div>}
    </>}
  </section>
}
