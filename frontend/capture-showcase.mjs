import { chromium } from '@playwright/test'
import { readFile, mkdir } from 'node:fs/promises'

// Read an existing local login file; never persist tokens or browser storage.
const credentials = JSON.parse(await readFile(process.env.OPSPILOT_SCREENSHOT_CREDENTIALS, 'utf8'))
const output = new URL('../docs/images/', import.meta.url)
await mkdir(output, { recursive: true })
const browser = await chromium.launch()
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 960 }, deviceScaleFactor: 1 })
  await page.goto('http://127.0.0.1:8088/login')
  await page.getByLabel('用户名').fill(credentials.username)
  await page.getByLabel('密码').fill(credentials.password)
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await page.waitForURL('**/workspace')
  await page.goto('http://127.0.0.1:8088/retrieval')
  await page.getByLabel('检索查询').fill('refund approval policy')
  await page.getByRole('button', { name: '运行调试' }).click()
  await page.locator('.retrieval-grid').waitFor({ timeout: 90000 })
  await page.screenshot({ path: new URL('retrieval.png', output).pathname.replace(/^\/([A-Z]:)/, '$1') })
  await page.goto('http://127.0.0.1:8088/knowledge')
  await page.locator('.el-loading-mask').waitFor({ state: 'hidden' })
  await page.screenshot({ path: new URL('knowledge.png', output).pathname.replace(/^\/([A-Z]:)/, '$1'), fullPage: true })
  console.log('Captured real knowledge and retrieval UI; no credentials or browser state exported.')
} finally {
  await browser.close()
}
