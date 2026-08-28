import { onBeforeUnmount, ref, type Ref } from 'vue'
import { api, authorizedStreamFetch } from '../api/client'
import type { RunEventRecord } from '../api/types'

export interface RunEventFrame {
  id: number
  event: string
  data: Record<string, unknown>
}

type FetchRunEventPage = (afterSeq: number, limit: number) => Promise<RunEventRecord[]>

export interface RunEventFactSynchronizer {
  readonly lastSentId: number
  hint(frame: RunEventFrame): Promise<void>
  syncAll(): Promise<void>
}

/**
 * Redis/SSE frames are notifications only. Rendered facts always come from the
 * authorized PostgreSQL history endpoint, through one serial paginated drain.
 */
export function createRunEventFactSynchronizer(
  fetchPage: FetchRunEventPage,
  onReady: (rows: RunEventRecord[]) => void,
  pageSize = 500,
): RunEventFactSynchronizer {
  let last = 0
  let targetWatermark = 0
  let syncPromise: Promise<void> | null = null
  const facts = new Map<number, RunEventRecord>()

  function drain(): void {
    const ready: RunEventRecord[] = []
    while (facts.has(last + 1)) {
      const fact = facts.get(last + 1)!
      facts.delete(last + 1)
      last += 1
      ready.push(fact)
    }
    if (ready.length > 0) onReady(ready)
  }

  function synchronize(untilHistoryEnd: boolean): Promise<void> {
    if (syncPromise) return syncPromise
    syncPromise = (async () => {
      while (true) {
        const rows = await fetchPage(last, pageSize)
        for (const row of rows) {
          if (Number.isSafeInteger(row.seq) && row.seq > last) facts.set(row.seq, row)
        }
        drain()
        if (rows.length === 0) return
        if (untilHistoryEnd) {
          if (rows.length < pageSize) return
        } else if (last >= targetWatermark && rows.length < pageSize) {
          return
        }
      }
    })().finally(() => { syncPromise = null })
    return syncPromise
  }

  return {
    get lastSentId() { return last },
    async hint(frame: RunEventFrame): Promise<void> {
      if (Number.isSafeInteger(frame.id) && frame.id > targetWatermark) {
        targetWatermark = frame.id
      }
      await synchronize(false)
    },
    syncAll: () => synchronize(true),
  }
}

export interface RunEventSequencer {
  readonly lastSentId: number
  readonly bufferedCount: number
  accept(frame: RunEventFrame): RunEventFrame[]
}

export function createRunEventSequencer(lastSentId = 0): RunEventSequencer {
  let last = lastSentId
  const buffer = new Map<number, RunEventFrame>()
  return {
    get lastSentId() { return last },
    get bufferedCount() { return buffer.size },
    accept(frame: RunEventFrame): RunEventFrame[] {
      if (!Number.isSafeInteger(frame.id) || frame.id <= last) return []
      if (buffer.size >= 256 && !buffer.has(frame.id)) {
        throw new Error('run event buffer limit exceeded')
      }
      buffer.set(frame.id, frame)
      const ready: RunEventFrame[] = []
      while (buffer.has(last + 1)) {
        const next = buffer.get(last + 1)!
        buffer.delete(last + 1)
        last += 1
        ready.push(next)
      }
      return ready
    },
  }
}

function decodeFrame(raw: string): RunEventFrame | null {
  let id: number | null = null
  let event = 'message'
  const data: string[] = []
  for (const line of raw.split('\n')) {
    if (!line || line.startsWith(':')) continue
    const separator = line.indexOf(':')
    const field = separator < 0 ? line : line.slice(0, separator)
    let value = separator < 0 ? '' : line.slice(separator + 1)
    if (value.startsWith(' ')) value = value.slice(1)
    if (field === 'id') id = Number(value)
    else if (field === 'event') event = value
    else if (field === 'data') data.push(value)
  }
  if (id === null || !Number.isSafeInteger(id) || id < 1 || data.length === 0) return null
  const parsed: unknown = JSON.parse(data.join('\n'))
  if (parsed === null || Array.isArray(parsed) || typeof parsed !== 'object') return null
  return { id, event, data: parsed as Record<string, unknown> }
}

export async function parseSseStream(
  stream: ReadableStream<Uint8Array>,
  onFrame: (frame: RunEventFrame) => void,
): Promise<void> {
  const reader = stream.getReader()
  const decoder = new TextDecoder()
  let pending = ''
  try {
    while (true) {
      const { done, value } = await reader.read()
      pending += decoder.decode(value, { stream: !done }).replace(/\r\n/g, '\n').replace(/\r/g, '\n')
      let boundary = pending.indexOf('\n\n')
      while (boundary >= 0) {
        const raw = pending.slice(0, boundary)
        pending = pending.slice(boundary + 2)
        const frame = decodeFrame(raw)
        if (frame) onFrame(frame)
        boundary = pending.indexOf('\n\n')
      }
      if (done) break
    }
  } finally {
    reader.releaseLock()
  }
}

interface StreamOptions {
  runId: string
  lastEventId: number
  signal: AbortSignal
  authorizedFetch: (path: string, init: RequestInit) => Promise<Response>
  onFrame: (frame: RunEventFrame) => void
  onUnauthorized: () => void | Promise<void>
}

export async function streamRunEvents(options: StreamOptions): Promise<void> {
  const response = await options.authorizedFetch(`/runs/${options.runId}/events`, {
    headers: { 'Last-Event-ID': String(options.lastEventId) },
    signal: options.signal,
  })
  if (response.status === 401) {
    await options.onUnauthorized()
    throw new Error('unauthorized run event stream')
  }
  if (!response.ok || !response.body) throw new Error('run event stream unavailable')
  await parseSseStream(response.body, options.onFrame)
}

export function useRunEvents(runId: string): {
  events: Ref<RunEventRecord[]>
  connection: Ref<'CONNECTING' | 'CONNECTED' | 'RECONNECTING'>
  start: () => Promise<void>
} {
  const events = ref<RunEventRecord[]>([])
  const connection = ref<'CONNECTING' | 'CONNECTED' | 'RECONNECTING'>('CONNECTING')
  const controller = new AbortController()
  let stopped = false
  const synchronizer = createRunEventFactSynchronizer(
    async (afterSeq, limit) => (await api.get<RunEventRecord[]>(`/runs/${runId}/history`, {
      params: { after_seq: afterSeq, limit }, signal: controller.signal,
    })).data,
    (rows) => events.value.push(...rows),
  )

  async function start(): Promise<void> {
    await synchronizer.syncAll()
    let delay = 250
    while (!stopped) {
      try {
        connection.value = connection.value === 'CONNECTING' ? 'CONNECTING' : 'RECONNECTING'
        await streamRunEvents({
          runId, lastEventId: synchronizer.lastSentId, signal: controller.signal,
          authorizedFetch: authorizedStreamFetch, onUnauthorized: () => undefined,
          onFrame: (frame) => {
            void synchronizer.hint(frame)
            connection.value = 'CONNECTED'
          },
        })
        if (!stopped) throw new Error('stream ended')
      } catch (error) {
        if (controller.signal.aborted) return
        connection.value = 'RECONNECTING'
        await new Promise((resolve) => setTimeout(resolve, delay))
        delay = Math.min(delay * 2, 4000)
      }
    }
  }
  onBeforeUnmount(() => { stopped = true; controller.abort() })
  return { events, connection, start }
}
