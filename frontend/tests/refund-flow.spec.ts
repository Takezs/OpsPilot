import { expect, test } from '@playwright/test'

const encode = (value: object) => Buffer.from(JSON.stringify(value)).toString('base64url')
const token = `${encode({ alg: 'HS256' })}.${encode({ sub: 'reviewer', role: 'REVIEWER', exp: Math.floor(Date.now() / 1000) + 600 })}.sig`

test('reviewer approval means allowed execution and timeline distinguishes uncertainty', async ({ page }) => {
  await page.addInitScript((value) => localStorage.setItem('opspilot.access_token', value), token)
  await page.route('**/api/v1/auth/me', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ user_id: 'reviewer', role: 'REVIEWER', allowed_departments: [], max_access_level: 1 }) }))
  let pending = true
  await page.route('**/api/v1/approval-requests?**', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(pending ? [{ id: 'approval-1', operation_id: 'operation-1', arguments_hash: 'a'.repeat(64), operation_version: 1, status: 'PENDING', operation_status: 'WAITING_APPROVAL', tool_name: 'refund_order', arguments: { order_number: 'ORD-002', amount: 350 }, expires_at: new Date(Date.now() + 60000).toISOString(), created_at: new Date().toISOString() }] : []) }))
  await page.route('**/api/v1/approval-requests/approval-1/decisions', async (route) => { pending = false; await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ already_processed: false, transitioned: true, approval_request_id: 'approval-1', operation_id: 'operation-1', operation_status: 'READY', operation_version: 2 }) }) })
  await page.goto('/approvals')
  const approve = page.getByRole('button', { name: '批准' })
  await expect(approve).toBeEnabled()
  await approve.click()
  await expect(page.getByRole('status')).toHaveText('已允许执行，等待 Worker 处理。')
  await expect(page.getByText('退款成功')).toHaveCount(0)

  await page.route('**/api/v1/runs/run-1', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ id: 'run-1', status: 'RUNNING', next_seq: 3, created_at: new Date().toISOString(), operations: [{ id: 'operation-1', tool_name: 'refund_order', status: 'OUTCOME_UNKNOWN', version: 4, policy_decision: 'REQUIRE_APPROVAL', provider_reference_id: null, attempts: [] }] }) }))
  await page.route('**/api/v1/runs/run-1/history**', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([{ run_id: 'run-1', seq: 1, event_type: 'approval_decided', payload: {}, created_at: new Date().toISOString() }, { run_id: 'run-1', seq: 2, event_type: 'operation_outcome_unknown', payload: {}, created_at: new Date().toISOString() }, { run_id: 'run-1', seq: 3, event_type: 'operation_reconciliation_started', payload: {}, created_at: new Date().toISOString() }]) }))
  await page.route('**/api/v1/runs/run-1/events', (route) => route.fulfill({ status: 200, contentType: 'text/event-stream', body: ': heartbeat\n\n' }))
  await page.goto('/runs/run-1')
  await expect(page.getByText('OUTCOME_UNKNOWN', { exact: true })).toBeVisible()
  await expect(page.getByText('结果暂未确认，系统不会自动重复退款。')).toBeVisible()
  await expect(page.getByText('#3 operation_reconciliation_started')).toBeVisible()
})
