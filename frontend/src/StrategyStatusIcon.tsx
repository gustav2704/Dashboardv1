import { Frown, Smile, Star } from 'lucide-react'
import type { StrategyStatus } from './strategyStatusFilter'

export const strategyStatusLabels: Record<StrategyStatus, string> = {
  '': 'Sin categoria',
  live: 'Live',
  monitor: 'Revisar',
  candidate_live: 'Candidata live',
}

export function StrategyStatusIcon({ status, size = 16 }: { status: StrategyStatus; size?: number }) {
  if (status === 'live') return <Star size={size} fill="currentColor" aria-label="Live" />
  if (status === 'monitor') return <Frown size={size} aria-label="Revisar" />
  if (status === 'candidate_live') return <Smile size={size} aria-label="Candidata live" />
  return null
}

export function PositiveStrategyStatusIcon({ status, size = 14 }: { status: StrategyStatus; size?: number }) {
  return status === 'live' || status === 'candidate_live'
    ? <StrategyStatusIcon status={status} size={size} />
    : null
}
