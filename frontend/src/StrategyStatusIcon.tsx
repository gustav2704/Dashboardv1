import { Frown, Smile } from 'lucide-react'
import type { StrategyStatus } from './strategyStatusFilter'

export const strategyStatusLabels: Record<StrategyStatus, string> = {
  '': 'Sin categoría',
  live: 'Darwinex live',
  monitor: 'Revisar',
  candidate_live: 'Candidata live',
  axi_live: 'Axi live',
}

export function StrategyStatusIcon({ status, size = 16 }: { status: StrategyStatus; size?: number }) {
  if (status === 'live') return <span aria-label="Darwinex live" style={{ fontSize: size, lineHeight: 1 }}>D</span>
  if (status === 'monitor') return <Frown size={size} aria-label="Revisar" />
  if (status === 'candidate_live') return <Smile size={size} aria-label="Candidata live" />
  if (status === 'axi_live') return <span aria-label="Axi live" style={{ fontSize: size, lineHeight: 1 }}>A</span>
  return null
}

export function PositiveStrategyStatusIcon({ status, size = 14 }: { status: StrategyStatus; size?: number }) {
  return status === 'live' || status === 'candidate_live' || status === 'axi_live'
    ? <StrategyStatusIcon status={status} size={size} />
    : null
}

export function StrategyStatusIcons({ statuses, size = 16, positiveOnly = false }: { statuses: StrategyStatus[]; size?: number; positiveOnly?: boolean }) {
  return <span className="strategy-status-icons">{statuses.map(status => positiveOnly && status === 'monitor' ? null : <span className={`strategy-status-indicator ${status}`} key={status}><StrategyStatusIcon status={status} size={size} /></span>)}</span>
}
