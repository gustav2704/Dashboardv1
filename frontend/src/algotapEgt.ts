// AlgoTAP incubator EGT per (account, magic), joined onto dashboard strategies.
export type AlgoTapEgt = {
  account_id: string
  magic: number
  egt_overall_score: number | null
  egt_grade: string | null
  trend_direction: string | null
  trend_slope: number | null
}

export type EgtIndex = Map<string, AlgoTapEgt>

type StrategyKeys = { account_login: string | null; primary_magic_number: number | null; magic_numbers?: number[] | null }

export function buildEgtIndex(rows: AlgoTapEgt[] | null | undefined): EgtIndex {
  const index: EgtIndex = new Map()
  for (const row of rows || []) index.set(`${row.account_id}|${row.magic}`, row)
  return index
}

/** Primary magic first, then any other current magic of the strategy. */
export function egtForStrategy(index: EgtIndex, strategy: StrategyKeys): AlgoTapEgt | undefined {
  if (!strategy.account_login || index.size === 0) return undefined
  const magics = [strategy.primary_magic_number, ...(strategy.magic_numbers || [])]
  for (const magic of magics) {
    if (magic == null) continue
    const hit = index.get(`${strategy.account_login}|${magic}`)
    if (hit) return hit
  }
  return undefined
}

export const egtTrendLabels: Record<string, string> = { IMPROVING: 'Mejora', STABLE: 'Estable', DEGRADING: 'Empeora' }
export const egtTrendArrows: Record<string, string> = { IMPROVING: '▲', STABLE: '■', DEGRADING: '▼' }
