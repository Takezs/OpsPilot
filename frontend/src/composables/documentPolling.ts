import type { DocumentRecord } from '../api/types'

type Sleep = (delayMs: number, signal?: AbortSignal) => Promise<void>
interface PollOptions { initialDelayMs?: number; maxDelayMs?: number; signal?: AbortSignal; sleep?: Sleep }
const terminal = new Set<DocumentRecord['status']>(['READY', 'FAILED'])
const abortError = (): DOMException => new DOMException('Document polling aborted', 'AbortError')
const defaultSleep: Sleep = (delayMs, signal) => new Promise((resolve, reject) => {
  if (signal?.aborted) return reject(abortError())
  const timer = window.setTimeout(resolve, delayMs)
  signal?.addEventListener('abort', () => { window.clearTimeout(timer); reject(abortError()) }, { once: true })
})
function statusOf(error: unknown): number | undefined {
  if (typeof error !== 'object' || error === null) return undefined
  if ('status' in error && typeof error.status === 'number') return error.status
  if ('response' in error && typeof error.response === 'object' && error.response !== null && 'status' in error.response && typeof error.response.status === 'number') return error.response.status
  return undefined
}
export async function pollDocument(documentId: string, fetchDocument: (id: string) => Promise<DocumentRecord>, onUpdate: (document: DocumentRecord) => void, options: PollOptions = {}): Promise<DocumentRecord> {
  const initialDelay = options.initialDelayMs ?? 500
  const maxDelay = options.maxDelayMs ?? 4000
  const sleep = options.sleep ?? defaultSleep
  let delay = initialDelay
  while (true) {
    if (options.signal?.aborted) throw abortError()
    try {
      const document = await fetchDocument(documentId)
      onUpdate(document)
      if (terminal.has(document.status)) return document
      await sleep(delay, options.signal)
    } catch (error) {
      if (options.signal?.aborted) throw abortError()
      if (statusOf(error) === 401) throw error
      await sleep(delay, options.signal)
      delay = Math.min(delay * 2, maxDelay)
    }
  }
}
