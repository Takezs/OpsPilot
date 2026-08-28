import { describe, expect, it, vi } from 'vitest'
import {
  createRunEventSequencer,
  parseSseStream,
  streamRunEvents,
  type RunEventFrame,
} from '../src/composables/useRunEvents'

function event(seq: number): RunEventFrame {
  return { id: seq, event: 'operation_updated', data: { seq } }
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
