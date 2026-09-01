import { expect, test } from '@playwright/test'

test('reviewer sees persisted evaluation metrics and failures', async ({ page }) => {
  const token = `e30.${Buffer.from(JSON.stringify({ exp: Math.floor(Date.now() / 1000) + 3600 })).toString('base64url')}.sig`
  await page.addInitScript(value => localStorage.setItem('opspilot.access_token', value), token)
  await page.route('**/api/v1/auth/me', route => route.fulfill({ json: { user_id: 'reviewer-1', role: 'REVIEWER', allowed_departments: [], max_access_level: 1 } }))
  await page.route('**/api/v1/evaluations', route => route.fulfill({ json: [{ id: 'e1', evaluation_run_id: 'r1', dataset_version: 'v1', dataset_sha: 'a'.repeat(64), configuration_sha: 'b'.repeat(64), status: 'COMPLETED', frozen_at: '2026-08-31T00:00:00Z', started_at: '2026-08-31T00:00:01Z', completed_at: '2026-08-31T00:00:02Z', failure_summary: null }] }))
  await page.route('**/api/v1/evaluations/e1/report.json', route => route.fulfill({ json: { configuration: { top_k: 5, repetitions: 3 }, metrics: { recall_at_5: { mean: 0.8, stddev: 0.1 }, duplicate_side_effect_rate: 0 }, failures: [{ case_id: 'dev-7', repetition: 2, error: 'timeout' }] } }))
  await page.goto('/evaluations')
  await expect(page.getByRole('heading', { name: '评测看板' })).toBeVisible()
  await expect(page.getByText('0.8000')).toBeVisible()
  await expect(page.getByText(/dev-7 #2/)).toBeVisible()
})
