import type { JournalAnalysisData as BaseJournalAnalysisData, JournalTrade as BaseJournalTrade } from './types'

export type JournalTrade = BaseJournalTrade & {
  magic_number: number
  open_time_msc: number
  direction: string
}

export type JournalBalancePoint = {
  time_msc: number
  balance: number
}

export type JournalBalanceBoundary = {
  time_msc: number | null
  total_balance: number | null
  accounts: Record<string, JournalBalancePoint>
  complete: boolean
  max_age_msc?: number | null
}

export type JournalAnalysisData = Omit<BaseJournalAnalysisData, 'trades'> & {
  range_start_msc: number
  range_end_msc: number
  trades: JournalTrade[]
  daily_balances: Array<{
    day: string
    time_msc: number
    total_balance: number
    accounts: Record<string, JournalBalancePoint>
    complete: boolean
  }>
  capital_coverage: {
    accounts: string[]
    start: JournalBalanceBoundary
    end: JournalBalanceBoundary
  }
}
