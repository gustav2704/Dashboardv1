import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from './api'

describe('api transient recovery', () => {
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('retries a transient GET network failure', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }))

    const result = api<{ ok: boolean }>('/api/test')
    await vi.advanceTimersByTimeAsync(300)

    await expect(result).resolves.toEqual({ ok: true })
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('does not retry a write request', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))

    await expect(api('/api/test', { method: 'POST' })).rejects.toThrow('El servidor no respondió')
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('bounds a stalled filter read and keeps a useful retry message', async () => {
    vi.useFakeTimers()
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation((_url, options) => (
      new Promise((_resolve, reject) => {
        options?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true })
      })
    ))

    const result = api('/api/dashboard', { timeoutMs: 100 })
    const assertion = expect(result).rejects.toThrow('La carga superó')
    await vi.advanceTimersByTimeAsync(100)

    await assertion
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })
})
