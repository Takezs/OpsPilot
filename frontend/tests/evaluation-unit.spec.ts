import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import EvaluationDashboardView from '../src/views/EvaluationDashboardView.vue'

const { get } = vi.hoisted(() => ({ get: vi.fn(async (url: string) => {
  if (url === '/evaluations') return { data: [{ id: 'e1', dataset_version: 'v1', configuration_sha: 'abc', status: 'COMPLETED' }] }
  return { data: { configuration: { top_k: 5 }, metrics: { recall_at_5: { mean: 0.8, stddev: 0.1 }, p95_latency_ms: 42 }, failures: [] } }
}) }))
vi.mock('../src/api/client', () => ({ api: { get } }))

describe('evaluation dashboard', () => {
  it('renders persisted report metrics and experiment configuration', async () => {
    const wrapper = mount(EvaluationDashboardView)
    await flushPromises()
    expect(get).toHaveBeenCalledWith('/evaluations/e1/report.json')
    expect(wrapper.text()).toContain('0.8000')
    expect(wrapper.text()).toContain('top_k')
    expect(wrapper.text()).toContain('无失败案例')
  })
})
