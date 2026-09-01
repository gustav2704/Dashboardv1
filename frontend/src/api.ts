const GET_RETRY_DELAYS_MS = [300, 900, 2_000]
const TRANSIENT_HTTP_STATUSES = new Set([502, 503, 504])
export type ApiOptions = RequestInit & { timeoutMs?: number }

function waitForRetry(delay: number, signal?: AbortSignal | null) {
  return new Promise<void>((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException('The operation was aborted', 'AbortError'))
      return
    }
    const timer = globalThis.setTimeout(resolve, delay)
    signal?.addEventListener('abort', () => {
      globalThis.clearTimeout(timer)
      reject(new DOMException('The operation was aborted', 'AbortError'))
    }, { once: true })
  })
}

export async function api<T>(url: string, options?: ApiOptions): Promise<T> {
  const method = String(options?.method || 'GET').toUpperCase()
  const retryDelays = method === 'GET' || method === 'HEAD' ? GET_RETRY_DELAYS_MS : []
  const timeoutMs = options?.timeoutMs ?? ((method === 'GET' || method === 'HEAD') ? 5_000 : 0)
  const timeoutController = new AbortController()
  let timedOut = false
  const externalSignal = options?.signal
  const relayAbort = () => timeoutController.abort(externalSignal?.reason)
  externalSignal?.addEventListener('abort', relayAbort, { once: true })
  const timeout = timeoutMs > 0 ? globalThis.setTimeout(() => {
    timedOut = true
    timeoutController.abort(new DOMException('Request timed out', 'TimeoutError'))
  }, timeoutMs) : null
  const { timeoutMs: _timeoutMs, ...requestOptions } = options || {}
  let attempt = 0

  try {
    while (true) {
      try {
        const response = await fetch(url, {
          ...requestOptions,
          signal: timeoutController.signal,
          headers: { 'Content-Type': 'application/json', ...(options?.headers || {}) },
        })
        if (TRANSIENT_HTTP_STATUSES.has(response.status) && attempt < retryDelays.length) {
          await waitForRetry(retryDelays[attempt++], timeoutController.signal)
          continue
        }
        if (!response.ok) {
          const body = await response.json().catch(() => ({ detail: response.statusText }))
          throw new Error(body.detail || response.statusText)
        }
        return response.json()
      } catch (error) {
        if (timedOut) throw new Error(`La carga superó ${Math.round(timeoutMs / 1000)} segundos. Conservamos los últimos datos; pulsa Refresh para reintentar.`)
        if (externalSignal?.aborted || (error instanceof DOMException && error.name === 'AbortError')) throw error
        if (!(error instanceof TypeError) || attempt >= retryDelays.length) {
          if (error instanceof TypeError) {
            throw new Error('El servidor no respondió. Conservamos los últimos datos; pulsa Refresh para reintentar.')
          }
          throw error
        }
        await waitForRetry(retryDelays[attempt++], timeoutController.signal)
      }
    }
  } finally {
    if (timeout !== null) globalThis.clearTimeout(timeout)
    externalSignal?.removeEventListener('abort', relayAbort)
  }
}
