import { describe, expect, it } from 'vitest'
import { isCurrentOverviewScope } from './overviewDataScope'

describe('Overview data scope', () => {
  it('accepts a response only when it belongs to the active Monitoring query', () => {
    const query = 'window=all&filter_accounts=true&accounts=7396577'
    expect(isCurrentOverviewScope(query, query)).toBe(true)
  })

  it('rejects old account or asset data while the new filter is loading', () => {
    expect(isCurrentOverviewScope(
      'window=all&filter_accounts=true&accounts=7396577',
      'window=all&filter_accounts=true&accounts=3000097316',
    )).toBe(false)
    expect(isCurrentOverviewScope(
      'window=all&filter_accounts=true&accounts=7396577',
      'window=all&filter_accounts=true&accounts=7396577&filter_assets=true&assets=eurusd',
    )).toBe(false)
  })

  it('does not treat an unloaded response as current', () => {
    expect(isCurrentOverviewScope('', 'window=all')).toBe(false)
  })
})
