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
    let resolveFetch!: (value: DocumentRecord) => void
    let receivedSignal: AbortSignal | undefined
    const cancelledFetch = vi.fn((_id: string, signal?: AbortSignal) => {
      receivedSignal = signal
      return new Promise<DocumentRecord>((resolve) => { resolveFetch = resolve })
    })
    const onUpdate = vi.fn()
    const active = pollDocument('doc-1', cancelledFetch, onUpdate, {
      signal: controller.signal,
    })
    await vi.waitFor(() => expect(cancelledFetch).toHaveBeenCalledTimes(1))
    controller.abort()
    resolveFetch(document('READY'))
    await expect(active).rejects.toMatchObject({ name: 'AbortError' })
    expect(receivedSignal).toBe(controller.signal)
    expect(onUpdate).not.toHaveBeenCalled()
    expect(cancelledFetch).toHaveBeenCalledTimes(1)
  })
})
