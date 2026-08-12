import { api } from './api'
import type { PanelHistory, Trade } from './types'

export type HistoryPriority = 'selected' | 'hover' | 'idle'

const MAX_CACHE_ENTRIES = 50
const MAX_CONCURRENT_REQUESTS = 2
const priorityValue: Record<HistoryPriority, number> = { selected: 0, hover: 1, idle: 2 }
const cache = new Map<string, PanelHistory>()

type QueueTask = {
  key: string
  strategyId: number
  query: string
  priority: HistoryPriority
  promise: Promise<PanelHistory>
  resolve: (value: PanelHistory) => void
  reject: (reason: unknown) => void
}

const queued = new Map<string, QueueTask>()
const running = new Map<string, Promise<PanelHistory>>()
let queue: QueueTask[] = []
let activeRequests = 0

export function panelHistoryKey(strategyId: number, query: string) {
  return `${strategyId}?${new URLSearchParams(query).toString()}`
}

function touch(key: string, value: PanelHistory) {
  cache.delete(key)
  cache.set(key, value)
  while (cache.size > MAX_CACHE_ENTRIES) {
    const oldest = cache.keys().next().value as string | undefined
    if (!oldest) break
    cache.delete(oldest)
  }
}

export function getCachedPanelHistory(strategyId: number, query: string) {
  const key = panelHistoryKey(strategyId, query)
  const value = cache.get(key) || null
  if (value) touch(key, value)
  return value
}

function tradeKey(trade: Trade) {
  return trade.trade_key || `${trade.source_role || 'live'}:${trade.source_account || ''}:${trade.deal_ticket}`
}

export function mergePanelHistory(previous: PanelHistory | null, incoming: PanelHistory): PanelHistory {
  if (!previous || incoming.reset || incoming.scope_version !== previous.scope_version) return incoming
  const merged = new Map(previous.trades.map(trade => [tradeKey(trade), trade]))
  incoming.trades.forEach(trade => merged.set(tradeKey(trade), trade))
  const trades = [...merged.values()]
    .sort((left, right) => left.close_time_msc - right.close_time_msc || left.deal_ticket - right.deal_ticket)
    .slice(-80)
  return {
    ...incoming,
    reset: false,
    trades,
    equity_curve: incoming.equity_curve.length ? incoming.equity_curve : previous.equity_curve,
    mappings: incoming.mappings.length ? incoming.mappings : previous.mappings,
  }
}

async function fetchPanelHistory(strategyId: number, query: string): Promise<PanelHistory> {
  const key = panelHistoryKey(strategyId, query)
  const previous = cache.get(key) || null
  const params = new URLSearchParams(query)
  params.set('limit', '80')
  if (previous) {
    params.set('max_deal_id', String(previous.cursor.max_deal_id))
    params.set('max_import_id', String(previous.cursor.max_import_id))
    params.set('scope_version', previous.scope_version)
  }
  const incoming = await api<PanelHistory>(`/api/strategies/${strategyId}/panel-history?${params}`)
  const merged = mergePanelHistory(previous, incoming)
  touch(key, merged)
  return merged
}

function pumpQueue() {
  queue.sort((left, right) => priorityValue[left.priority] - priorityValue[right.priority])
  while (activeRequests < MAX_CONCURRENT_REQUESTS && queue.length) {
    const task = queue.shift()!
    queued.delete(task.key)
    activeRequests += 1
    const request = fetchPanelHistory(task.strategyId, task.query)
    running.set(task.key, request)
    request.then(task.resolve, task.reject).finally(() => {
      running.delete(task.key)
      activeRequests -= 1
      pumpQueue()
    })
  }
}

export function requestPanelHistory(
  strategyId: number,
  query: string,
  priority: HistoryPriority = 'selected',
): Promise<PanelHistory> {
  const key = panelHistoryKey(strategyId, query)
  const active = running.get(key)
  if (active) return active
  const pending = queued.get(key)
  if (pending) {
    if (priorityValue[priority] < priorityValue[pending.priority]) pending.priority = priority
    pumpQueue()
    return pending.promise
  }
  let resolve!: (value: PanelHistory) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<PanelHistory>((accept, decline) => {
    resolve = accept
    reject = decline
  })
  const task = { key, strategyId, query, priority, promise, resolve, reject }
  queued.set(key, task)
  queue.push(task)
  pumpQueue()
  return promise
}

export function prefetchPanelHistory(strategyId: number, query: string, priority: HistoryPriority = 'idle') {
  if (getCachedPanelHistory(strategyId, query)) return
  void requestPanelHistory(strategyId, query, priority).catch(() => undefined)
}

export function clearPanelHistoryCacheForTests() {
  cache.clear()
  queue = []
  queued.clear()
}
