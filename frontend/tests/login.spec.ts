import { expect, test } from '@playwright/test'

function token(role: 'USER' | 'REVIEWER' | 'ADMIN'): string {
  const encode = (value: object) => Buffer.from(JSON.stringify(value)).toString('base64url')
  return `${encode({ alg: 'HS256', typ: 'JWT' })}.${encode({ sub: 'e2e-user', role, exp: Math.floor(Date.now() / 1000) + 600 })}.signature`
}

async function mockLogin(page: import('@playwright/test').Page, role: 'USER' | 'REVIEWER' = 'USER') {
  await page.route('**/api/v1/auth/login', async (route) => {
    const body = route.request().postDataJSON()
    if (body.password === 'wrong') {
      await route.fulfill({ status: 401, contentType: 'application/json', body: JSON.stringify({ detail: 'invalid credentials', stack: 'secret-stack' }) })
      return
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ access_token: token(role), token_type: 'bearer' }) })
  })
  await page.route('**/api/v1/auth/me', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      user_id: 'e2e-user', role, allowed_departments: [], max_access_level: 1,
    }),
  }))
}

test('wrong password shows a safe error', async ({ page }) => {
  await mockLogin(page)
  await page.goto('/login')
  await page.getByLabel('用户名').fill('alice')
  await page.getByLabel('密码').fill('wrong')
  await page.getByRole('button', { name: '登录' }).click()
  await expect(page.getByRole('alert')).toHaveText('用户名或密码错误')
  await expect(page.getByText('secret-stack')).toHaveCount(0)
})

test('success redirects, refresh restores, and USER cannot access approvals', async ({ page }) => {
  await mockLogin(page, 'USER')
  await page.goto('/login?returnUrl=%2Fknowledge')
  await page.getByLabel('用户名').fill('alice')
  await page.getByLabel('密码').fill('correct')
  await page.getByRole('button', { name: '登录' }).click()
  await expect(page).toHaveURL(/\/knowledge$/)
  await page.reload()
  await expect(page).toHaveURL(/\/knowledge$/)
  await expect(page.getByRole('link', { name: '审批中心' })).toHaveCount(0)
  await page.goto('/approvals')
  await expect(page).toHaveURL(/\/workspace/)
})

test('401 atomically clears session and returns to login once', async ({ page }) => {
  await mockLogin(page, 'REVIEWER')
  await page.route('**/api/v1/knowledge-bases', (route) => route.fulfill({ status: 401, body: '{}' }))
  await page.goto('/login')
  await page.getByLabel('用户名').fill('reviewer')
  await page.getByLabel('密码').fill('correct')
  await page.getByRole('button', { name: '登录' }).click()
  await expect(page.getByRole('link', { name: '审批中心' })).toBeVisible()
  await page.getByTestId('session-check').click()
  await expect(page).toHaveURL(/\/login/)
  expect(await page.evaluate(() => localStorage.getItem('opspilot.access_token'))).toBeNull()
  await page.getByLabel('用户名').fill('reviewer')
  await page.getByLabel('密码').fill('correct')
  await page.getByRole('button', { name: '登录' }).click()
  await page.getByTestId('session-check').click()
  await expect(page).toHaveURL(/\/login/)
})

test('forged ADMIN payload stays untrusted while /me restores database USER', async ({ page }) => {
  let releaseMe: (() => void) | undefined
  const waitForRelease = new Promise<void>((resolve) => { releaseMe = resolve })
  const forged = token('ADMIN')
  await page.addInitScript((value) => localStorage.setItem('opspilot.access_token', value), forged)
  await page.route('**/api/v1/auth/me', async (route) => {
    await waitForRelease
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        user_id: 'e2e-user', role: 'USER', allowed_departments: [], max_access_level: 1,
      }),
    })
  })
  const meRequest = page.waitForRequest('**/api/v1/auth/me')
  const navigation = page.goto('/approvals')
  await meRequest
  await expect(page.getByText('OpsPilot')).toHaveCount(0)
  releaseMe?.()
  await navigation
  await expect(page).toHaveURL(/\/workspace$/)
  await expect(page.getByRole('link', { name: '审批中心' })).toHaveCount(0)
})
