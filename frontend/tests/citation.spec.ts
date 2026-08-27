import { describe, expect, it, vi } from 'vitest'
import { createLatestCitationLoader } from '../src/composables/citationLoading'

describe('citation request generations', () => {
  it('ignores late old and closed drawer responses', async () => {
    const requests = new Map<string, (value: string) => void>()
    const applied: string[] = []
    const fetch = vi.fn((id: string, _signal: AbortSignal) => new Promise<string>((resolve) => requests.set(id, resolve)))
    const loader = createLatestCitationLoader(fetch, (value) => applied.push(value), vi.fn())
    const old = loader.load('old')
    const latest = loader.load('latest')
    requests.get('latest')?.('latest detail')
    await latest
    requests.get('old')?.('old detail')
    await old
    expect(applied).toEqual(['latest detail'])

    const closing = loader.load('closing')
    loader.close()
    requests.get('closing')?.('closed detail')
    await closing
    expect(applied).toEqual(['latest detail'])
  })
})
