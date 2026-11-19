export const TERMINAL_STALE_MS = 10 * 60_000

export type TerminalUpdateSource = {
  status: string
  last_seen?: string | null
  last_sync?: string | null
}

export type TerminalUpdateLabel = { text: string; stale: boolean; atMs: number | null }

function ageText(ageMs: number) {
  const minutes = Math.floor(ageMs / 60_000)
  if (minutes < 1) return 'hace menos de 1 min'
  if (minutes < 60) return `hace ${minutes} min`
  if (minutes < 1440) return `hace ${Math.floor(minutes / 60)} h ${minutes % 60} min`
  return `hace ${Math.floor(minutes / 1440)} d`
}

// Describes when a broker terminal last reported to the dashboard. The bridge
// sets last_seen from its payload time and last_sync when it ingests data, so
// last_seen wins and last_sync is the fallback.
export function terminalUpdateLabel(terminal: TerminalUpdateSource, nowMs: number): TerminalUpdateLabel {
  const iso = terminal.last_seen || terminal.last_sync
  const atMs = iso ? Date.parse(iso) : NaN
  if (!iso || !Number.isFinite(atMs)) {
    return { text: 'Sin actualización registrada', stale: true, atMs: null }
  }
  const ageMs = Math.max(0, nowMs - atMs)
  const time = new Date(atMs).toLocaleTimeString('es-ES', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  return {
    text: `Actualizado ${ageText(ageMs)} · ${time}`,
    stale: ageMs > TERMINAL_STALE_MS || terminal.status !== 'connected',
    atMs,
  }
}
