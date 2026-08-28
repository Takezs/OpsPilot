export interface RunEventFrame {
  id: number
  event: string
  data: Record<string, unknown>
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
  const sequencer = createRunEventSequencer(0)
  let stopped = false

  async function backfill(afterSeq: number): Promise<void> {
    const rows = (await api.get<RunEventRecord[]>(`/runs/${runId}/history`, {
      params: { after_seq: afterSeq, limit: 500 }, signal: controller.signal,
    })).data
    for (const row of rows) {
      for (const applied of sequencer.accept({ id: row.seq, event: row.event_type, data: row.payload })) {
        const fact = rows.find((candidate) => candidate.seq === applied.id)
        if (fact && !events.value.some((item) => item.seq === fact.seq)) events.value.push(fact)
      }
    }
  }

  async function start(): Promise<void> {
    await backfill(0)
    let delay = 250
    while (!stopped) {
      try {
        connection.value = connection.value === 'CONNECTING' ? 'CONNECTING' : 'RECONNECTING'
        await streamRunEvents({
          runId, lastEventId: sequencer.lastSentId, signal: controller.signal,
          authorizedFetch: authorizedStreamFetch, onUnauthorized: () => undefined,
          onFrame: (frame) => {
            const ready = sequencer.accept(frame)
            if (ready.length === 0 && frame.id > sequencer.lastSentId + 1) void backfill(sequencer.lastSentId)
            for (const item of ready) events.value.push({ run_id: runId, seq: item.id, event_type: item.event, payload: item.data, created_at: new Date().toISOString() })
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
import { onBeforeUnmount, ref, type Ref } from 'vue'
import { api, authorizedStreamFetch } from '../api/client'
import type { RunEventRecord } from '../api/types'
