import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './tests',
  testIgnore: [
    '**/auth.spec.ts',
    '**/citation.spec.ts',
    '**/knowledge.spec.ts',
    '**/evaluation-unit.spec.ts',
    '**/run-events-red.spec.ts',
  ],
  globalSetup: './tests/global-setup.ts',
  use: { baseURL: 'http://127.0.0.1:4173' },
  reporter: 'line',
})
