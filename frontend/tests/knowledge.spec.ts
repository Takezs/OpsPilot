import { describe, expect, it, vi } from 'vitest'
import { pollDocument } from '../src/composables/documentPolling'
import type { DocumentRecord } from '../src/api/types'

function document(status: DocumentRecord['status']): DocumentRecord {
  return {
    id: 'doc-1',
    knowledge_base_id: 'kb-1',
    title: 'Guide',
    version: 1,
    effective_at: '2026-08-27T00:00:00Z',
    status,
    failure_message: status === 'FAILED' ? 'Document processing failed' : null,
  }
}

describe('document ingestion polling', () => {
  it('backs off and stops at READY', async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(document('UPLOADED'))
      .mockRejectedValueOnce(new Error('temporary'))
      .mockResolvedValueOnce(document('INDEXING'))
      .mockResolvedValueOnce(document('READY'))
    const delays: number[] = []
    const updates: string[] = []
    const result = await pollDocument('doc-1', fetch, (item) => updates.push(item.status), {
      initialDelayMs: 100,
      maxDelayMs: 400,
      sleep: async (delay) => { delays.push(delay) },
    })
    expect(result.status).toBe('READY')
    expect(updates).toEqual(['UPLOADED', 'INDEXING', 'READY'])
    expect(delays).toEqual([100, 100, 200])
  })

  it('stops on 401 and cancellation without scheduling another request', async () => {
    const unauthorized = Object.assign(new Error('unauthorized'), { status: 401 })
    const fetch = vi.fn().mockRejectedValue(unauthorized)
    await expect(pollDocument('doc-1', fetch, vi.fn(), {
      initialDelayMs: 1, maxDelayMs: 2, sleep: async () => undefined,
    })).rejects.toBe(unauthorized)
    expect(fetch).toHaveBeenCalledTimes(1)

    const controller = new AbortController()
    const cancelledFetch = vi.fn().mockResolvedValue(document('INDEXING'))
    const active = pollDocument('doc-1', cancelledFetch, vi.fn(), {
      signal: controller.signal,
      sleep: async (_delay, signal) => new Promise<void>((_resolve, reject) => {
        signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), { once: true })
      }),
    })
    await vi.waitFor(() => expect(cancelledFetch).toHaveBeenCalledTimes(1))
    controller.abort()
    await expect(active).rejects.toMatchObject({ name: 'AbortError' })
    expect(cancelledFetch).toHaveBeenCalledTimes(1)
  })
})
