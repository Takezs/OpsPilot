import { expect, test, type Page } from '@playwright/test'

function token(): string {
  const encode = (value: object) => Buffer.from(JSON.stringify(value)).toString('base64url')
  return `${encode({ alg: 'HS256' })}.${encode({ exp: Math.floor(Date.now() / 1000) + 600 })}.signature`
}

async function authenticate(page: Page): Promise<void> {
  await page.addInitScript((value) => localStorage.setItem('opspilot.access_token', value), token())
  await page.route('**/api/v1/auth/me', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      user_id: 'user-1', role: 'USER', allowed_departments: ['support'], max_access_level: 1,
    }),
  }))
}

test('upload polls the exact document to READY and renders FAILED safely', async ({ page }) => {
  await authenticate(page)
  const kb = { id: 'kb-1', name: 'Support', department: 'support', access_level: 1 }
  const base = {
    knowledge_base_id: kb.id, title: 'Guide', version: 1,
    effective_at: '2026-08-27T00:00:00Z', failure_message: null,
  }
  let detailCalls = 0
  let uploadCalls = 0
  await page.route(/\/api\/v1\/knowledge-bases(?:\?.*)?$/, (route) => route.fulfill({ json: [kb] }))
  await page.route(/\/api\/v1\/knowledge-bases\/kb-1\/documents/, (route) => route.fulfill({
    json: [{ ...base, id: 'failed-doc', status: 'FAILED', failure_message: 'Document processing failed' }],
  }))
  await page.route('**/api/v1/knowledge/kb-1/documents', (route) => {
    uploadCalls += 1
    return route.fulfill({ status: 202, json: {
      document_id: uploadCalls === 1 ? 'new-doc' : 'new-failed-doc', version: 1, status: 'UPLOADED',
    } })
  })
  await page.route('**/api/v1/knowledge/documents/new-doc', (route) => {
    detailCalls += 1
    const status = detailCalls > 1 ? 'READY' : 'INDEXING'
    return route.fulfill({ json: { ...base, id: 'new-doc', status } })
  })
  await page.route('**/api/v1/knowledge/documents/new-failed-doc', (route) => route.fulfill({
    json: { ...base, id: 'new-failed-doc', title: 'failed.md', status: 'FAILED', failure_message: 'Document processing failed' },
  }))
  await page.goto('/knowledge')
  await expect(page.getByText('Document processing failed')).toBeVisible()
  await page.getByLabel('选择文件').setInputFiles({
    name: 'guide.md', mimeType: 'text/markdown', buffer: Buffer.from('# Guide'),
  })
  await page.getByRole('button', { name: '上传文档' }).click()
  await expect(page.getByText('READY')).toBeVisible({ timeout: 5000 })
  expect(detailCalls).toBe(2)
  await page.getByLabel('选择文件').setInputFiles({
    name: 'failed.md', mimeType: 'text/markdown', buffer: Buffer.from('# Failed'),
  })
  await page.getByRole('button', { name: '上传文档' }).click()
  await expect(page.getByRole('row').filter({ hasText: 'failed.md' })).toContainText('FAILED')
})

test('shows four retrieval stages, degraded state, and exact-version citation drawer', async ({ page }) => {
  await authenticate(page)
  const item = {
    chunk_id: 'chunk-1', document_id: 'doc-1', document_version: 3,
    document_title: 'Refund policy', section_path: ['Policy', 'Refund'], page: 7,
    rank: 1, score: 0.8123, source: 'dense', content_excerpt: 'refund policy excerpt',
  }
  await page.route('**/api/v1/retrieval/debug', (route) => route.fulfill({ json: {
    dense: [{ ...item, source: 'dense' }], fts: [{ ...item, source: 'fts' }],
    rrf: [{ ...item, source: 'rrf' }], reranker: [{ ...item, source: 'reranker' }],
    reranker_status: 'degraded',
  } }))
  await page.route('**/api/v1/knowledge/documents/doc-1/versions/3/chunks/chunk-1', (route) => route.fulfill({ json: {
    document_id: 'doc-1', document_version: 3, chunk_id: 'chunk-1',
    document_title: 'Refund policy', section_path: ['Policy', 'Refund'], page: 7,
    content: '<script>alert(1)</script> exact snapshot', effective_at: '2026-08-27T00:00:00Z',
  } }))
  await page.goto('/retrieval')
  await page.getByLabel('检索查询').fill('refund policy')
  await page.getByRole('button', { name: '运行调试' }).click()
  for (const heading of ['Dense', 'PostgreSQL FTS', 'RRF', 'Reranker']) {
    await expect(page.getByRole('heading', { name: heading })).toBeVisible()
  }
  await expect(page.getByText('Reranker 已降级')).toBeVisible()
  await expect(page.getByText('BM25')).toHaveCount(0)
  await page.getByRole('button', { name: /Refund policy.*版本 3/ }).first().click()
  await expect(page.getByRole('dialog')).toContainText('exact snapshot')
  await expect(page.getByRole('dialog').locator('script')).toHaveCount(0)
  await expect(page.getByRole('dialog')).toContainText('第 7 页')
  expect(await page.evaluate(() => document.querySelector('[role="dialog"]')?.contains(document.activeElement))).toBe(true)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await page.setViewportSize({ width: 390, height: 844 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
})
