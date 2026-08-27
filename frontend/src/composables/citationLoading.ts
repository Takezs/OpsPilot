export interface LatestCitationLoader {
  load(key: string): Promise<void>
  close(): void
}

export function createLatestCitationLoader<T>(
  fetchValue: (key: string, signal: AbortSignal) => Promise<T>,
  applyValue: (value: T) => void,
  applyError: (error: unknown) => void,
  setLoading?: (loading: boolean) => void,
): LatestCitationLoader {
  let generation = 0
  let controller: AbortController | null = null

  async function load(key: string): Promise<void> {
    generation += 1
    const current = generation
    controller?.abort()
    controller = new AbortController()
    const signal = controller.signal
    setLoading?.(true)
    try {
      const value = await fetchValue(key, signal)
      if (signal.aborted || current !== generation) return
      applyValue(value)
    } catch (error) {
      if (signal.aborted || current !== generation) return
      applyError(error)
    } finally {
      if (current === generation) {
        controller = null
        setLoading?.(false)
      }
    }
  }

  function close(): void {
    generation += 1
    controller?.abort()
    controller = null
    setLoading?.(false)
  }

  return { load, close }
}
