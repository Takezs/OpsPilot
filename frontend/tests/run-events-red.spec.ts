import { describe, expect, it, vi } from 'vitest'
import {
  createRunEventFactSynchronizer,
  createRunEventSequencer,
  consumeRunEventStream,
  parseSseStream,
  runEventConnectionLoop,
  streamRunEvents,
  type RunEventFrame,
} from '../src/composables/useRunEvents'
import type { RunEventRecord } from '../src/api/types'

function event(seq: number): RunEventFrame {
  return { id: seq, event: 'operation_updated', data: { seq } }
}

function fact(seq: number, createdAt = `2026-08-29T00:00:${String(seq).padStart(2, '0')}Z`): RunEventRecord {
  return {
    run_id: 'run-1',
    seq,
    event_type: 'operation_updated',
    payload: { seq, source: 'postgres' },
    created_at: createdAt,
  }
}

function byteStream(parts: Uint8Array[]): ReadableStream<Uint8Array> {
  return new ReadableStream({
    start(controller) {
      for (const part of parts) controller.enqueue(part)
      controller.close()
    },
  })
}

describe('Task 15 reliable run event client', () => {
  it('observes a hint history failure and aborts the current stream for reconnect', async () => {
    const historyError = new Error('history unavailable')
    let emit!: (frame: RunEventFrame) => void
    const stream = vi.fn(async (signal: AbortSignal, onFrame: (frame: RunEventFrame) => void) => {
      emit = onFrame
      await new Promise<void>((resolve) => signal.addEventListener('abort', () => resolve(), { once: true }))
    })
    const hint = vi.fn().mockRejectedValue(historyError)
    const pending = consumeRunEventStream({
      signal: new AbortController().signal,
      stream,
      hint,
      onSynchronized: vi.fn(),
    })

    emit(event(1))

    await expect(pending).rejects.toBe(historyError)
    expect(stream.mock.calls[0]?.[0].aborted).toBe(true)
    expect(hint).toHaveBeenCalledOnce()
  })

  it('propagates a history 401 once so the caller can stop reconnecting', async () => {
    const unauthorized = Object.assign(new Error('unauthorized'), { response: { status: 401 } })
    let emit!: (frame: RunEventFrame) => void
    const stream = async (signal: AbortSignal, onFrame: (frame: RunEventFrame) => void) => {
      emit = onFrame
      await new Promise<void>((resolve) => signal.addEventListener('abort', () => resolve(), { once: true }))
    }
    const hint = vi.fn().mockRejectedValue(unauthorized)
    const pending = consumeRunEventStream({
      signal: new AbortController().signal,
      stream,
      hint,
      onSynchronized: vi.fn(),
    })

    emit(event(1))

    await expect(pending).rejects.toBe(unauthorized)
    expect(hint).toHaveBeenCalledOnce()
  })

  it('does not update connected state when unmounted while history sync is pending', async () => {
    const controller = new AbortController()
    let emit!: (frame: RunEventFrame) => void
    let release!: () => void
    const history = new Promise<void>((resolve) => { release = resolve })
    const onSynchronized = vi.fn()
    const pending = consumeRunEventStream({
      signal: controller.signal,
      stream: async (signal, onFrame) => {
        emit = onFrame
        await new Promise<void>((resolve) => signal.addEventListener('abort', () => resolve(), { once: true }))
      },
      hint: vi.fn(() => history),
      onSynchronized,
    })

    emit(event(1))
    controller.abort()
    release()
    await pending

    expect(onSynchronized).not.toHaveBeenCalled()
  })

  it('moves to reconnecting when hint history fails, then recovers exactly once', async () => {
    const controller = new AbortController()
    const states: string[] = []
    const synchronizer = {
      lastSentId: 0,
      syncAll: vi.fn().mockResolvedValue(undefined),
      hint: vi.fn()
        .mockRejectedValueOnce(new Error('history network failure'))
        .mockResolvedValueOnce(undefined),
    }
    const openStream = vi.fn(async (signal: AbortSignal, onFrame: (frame: RunEventFrame) => void) => {
      onFrame(event(1))
      await new Promise<void>((resolve) => signal.addEventListener('abort', () => resolve(), { once: true }))
    })

    await runEventConnectionLoop({
      signal: controller.signal,
      synchronizer,
      openStream,
      wait: vi.fn().mockResolvedValue(undefined),
      onConnection: (state) => {
        states.push(state)
        if (state === 'CONNECTED') controller.abort()
      },
    })

    expect(states).toEqual(['RECONNECTING', 'CONNECTED'])
    expect(openStream).toHaveBeenCalledTimes(2)
    expect(synchronizer.hint).toHaveBeenCalledTimes(2)
  })

  it('stops after a history 401 without entering a reconnect loop', async () => {
    const unauthorized = Object.assign(new Error('unauthorized'), { response: { status: 401 } })
    const synchronizer = {
      lastSentId: 0,
      syncAll: vi.fn().mockRejectedValue(unauthorized),
      hint: vi.fn(),
    }
    const openStream = vi.fn()
    const onConnection = vi.fn()
    const wait = vi.fn()

    await runEventConnectionLoop({
      signal: new AbortController().signal,
      synchronizer,
      openStream,
      onConnection,
      wait,
    })

    expect(synchronizer.syncAll).toHaveBeenCalledOnce()
    expect(openStream).not.toHaveBeenCalled()
    expect(onConnection).not.toHaveBeenCalled()
    expect(wait).not.toHaveBeenCalled()
  })

  it('retains an early seq 3 hint while PostgreSQL history returns seq 1 and 2 first', async () => {
    const pages = [[fact(1), fact(2)], [fact(3, '2026-08-29T03:03:03Z')]]
    const fetchPage = vi.fn(async () => pages.shift() ?? [])
    const rendered: RunEventRecord[] = []
    const synchronizer = createRunEventFactSynchronizer(fetchPage, (rows) => rendered.push(...rows), 2)

    await synchronizer.hint(event(3))

    expect(rendered.map((row) => row.seq)).toEqual([1, 2, 3])
    expect(rendered[2]?.created_at).toBe('2026-08-29T03:03:03Z')
    expect(rendered[2]?.payload).toEqual({ seq: 3, source: 'postgres' })
  })

  it('coalesces concurrent hints into one backfill and drains duplicate out-of-order hints once', async () => {
    let release!: () => void
    const gate = new Promise<void>((resolve) => { release = resolve })
    const fetchPage = vi.fn(async (afterSeq: number, _limit: number) => {
      await gate
      return Array.from({ length: 5 - afterSeq }, (_, offset) => fact(afterSeq + offset + 1))
    })
    const rendered: RunEventRecord[] = []
    const synchronizer = createRunEventFactSynchronizer(fetchPage, (rows) => rendered.push(...rows))

    const pending = [3, 1, 2, 3, 5, 4].map((seq) => synchronizer.hint(event(seq)))
    expect(fetchPage).toHaveBeenCalledOnce()
    release()
    await Promise.all(pending)

    expect(rendered.map((row) => row.seq)).toEqual([1, 2, 3, 4, 5])
    expect(fetchPage.mock.calls.every((call) => call[1] === 500)).toBe(true)
  })

  it('paginates beyond 500 rows until the hinted PostgreSQL watermark is closed', async () => {
    const allRows = Array.from({ length: 1201 }, (_, offset) => fact(offset + 1))
    const fetchPage = vi.fn(async (afterSeq: number, limit: number) =>
      allRows.slice(afterSeq, afterSeq + limit),
    )
    const rendered: RunEventRecord[] = []
    const synchronizer = createRunEventFactSynchronizer(fetchPage, (rows) => rendered.push(...rows))

    await synchronizer.hint(event(1201))

    expect(fetchPage).toHaveBeenCalledTimes(3)
    expect(fetchPage.mock.calls.map((call) => call[0])).toEqual([0, 500, 1000])
    expect(rendered).toHaveLength(1201)
    expect(rendered.at(-1)?.seq).toBe(1201)
  })

  it('renders an out-of-order duplicate sequence exactly once and in order', () => {
    const sequencer = createRunEventSequencer(0)
    const rendered = [1, 3, 2, 3, 5, 4].flatMap((seq) => sequencer.accept(event(seq)))

    expect(rendered.map((item) => item.id)).toEqual([1, 2, 3, 4, 5])
    expect(sequencer.lastSentId).toBe(5)
    expect(sequencer.bufferedCount).toBe(0)
  })

  it('parses CRLF frames split across chunks and inside a UTF-8 code point', async () => {
    const encoded = new TextEncoder().encode(
      'id: 7\r\nevent: assistant_message\r\ndata: {"content":"退款完成😀"}\r\n\r\n',
    )
    const emojiStart = encoded.findIndex((value) => value === 0xf0)
    const stream = byteStream([
      encoded.slice(0, 5),
      encoded.slice(5, emojiStart + 2),
      encoded.slice(emojiStart + 2, encoded.length - 1),
      encoded.slice(encoded.length - 1),
    ])
    const frames: RunEventFrame[] = []

    await parseSseStream(stream, (frame) => frames.push(frame))

    expect(frames).toEqual([
      { id: 7, event: 'assistant_message', data: { content: '退款完成😀' } },
    ])
  })

  it('uses the authorized transport with Last-Event-ID and caller abort signal', async () => {
    const controller = new AbortController()
    const authorizedFetch = vi.fn().mockResolvedValue(
      new Response(byteStream([]), {
        status: 200,
        headers: { 'Content-Type': 'text/event-stream' },
      }),
    )

    await streamRunEvents({
      runId: 'run-1',
      lastEventId: 12,
      signal: controller.signal,
      authorizedFetch,
      onFrame: vi.fn(),
      onUnauthorized: vi.fn(),
    })

    expect(authorizedFetch).toHaveBeenCalledOnce()
    expect(authorizedFetch).toHaveBeenCalledWith('/runs/run-1/events', {
      headers: { 'Last-Event-ID': '12' },
      signal: controller.signal,
    })
  })

  it('propagates AbortError without treating cancellation as unauthorized', async () => {
    const controller = new AbortController()
    const aborted = new DOMException('cancelled', 'AbortError')
    const authorizedFetch = vi.fn(async (_path: string, init: RequestInit) => {
      await new Promise<void>((_resolve, reject) => {
        init.signal?.addEventListener('abort', () => reject(aborted), { once: true })
      })
      throw new Error('unreachable')
    })
    const onUnauthorized = vi.fn()
    const pending = streamRunEvents({
      runId: 'run-1',
      lastEventId: 0,
      signal: controller.signal,
      authorizedFetch,
      onFrame: vi.fn(),
      onUnauthorized,
    })

    controller.abort()

    await expect(pending).rejects.toBe(aborted)
    expect(onUnauthorized).not.toHaveBeenCalled()
  })

  it('routes a 401 through the single session invalidation callback', async () => {
    const onUnauthorized = vi.fn().mockResolvedValue(undefined)
    const authorizedFetch = vi.fn().mockResolvedValue(new Response(null, { status: 401 }))

    await expect(
      streamRunEvents({
        runId: 'run-1',
        lastEventId: 4,
        signal: new AbortController().signal,
        authorizedFetch,
        onFrame: vi.fn(),
        onUnauthorized,
      }),
    ).rejects.toThrow(/unauthorized/i)

    expect(onUnauthorized).toHaveBeenCalledOnce()
  })
})
