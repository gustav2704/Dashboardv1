import { describe, expect, it } from 'vitest'
import { TERMINAL_STALE_MS, terminalUpdateLabel } from './terminalUpdateAge'

const now = Date.parse('2026-11-18T12:00:00Z')

describe('terminalUpdateLabel', () => {
  it('reports a fresh connected terminal as not stale', () => {
    const label = terminalUpdateLabel({ status: 'connected', last_seen: '2026-11-18T11:59:30Z' }, now)
    expect(label.stale).toBe(false)
    expect(label.text).toContain('hace menos de 1 min')
  })

  it('shows minutes and hours for older updates', () => {
    expect(terminalUpdateLabel({ status: 'connected', last_seen: '2026-11-18T11:55:00Z' }, now).text).toContain('hace 5 min')
    expect(terminalUpdateLabel({ status: 'connected', last_seen: '2026-11-18T09:30:00Z' }, now).text).toContain('hace 2 h 30 min')
  })

  it('marks updates older than the threshold as stale', () => {
    const old = new Date(now - TERMINAL_STALE_MS - 1000).toISOString()
    expect(terminalUpdateLabel({ status: 'connected', last_seen: old }, now).stale).toBe(true)
  })

  it('marks a non-connected terminal as stale even when recent', () => {
    expect(terminalUpdateLabel({ status: 'connecting', last_seen: '2026-11-18T11:59:30Z' }, now).stale).toBe(true)
  })

  it('falls back to last_sync when last_seen is missing', () => {
    const label = terminalUpdateLabel({ status: 'connected', last_seen: null, last_sync: '2026-11-18T11:58:00Z' }, now)
    expect(label.atMs).toBe(Date.parse('2026-11-18T11:58:00Z'))
    expect(label.text).toContain('hace 2 min')
  })

  it('reports missing or invalid timestamps as stale', () => {
    expect(terminalUpdateLabel({ status: 'connected' }, now)).toMatchObject({ stale: true, atMs: null })
    expect(terminalUpdateLabel({ status: 'connected', last_seen: 'not-a-date' }, now)).toMatchObject({ stale: true, atMs: null })
  })
})
