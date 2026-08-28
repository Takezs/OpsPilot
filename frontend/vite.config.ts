import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: { port: 4173 },
  test: { environment: 'jsdom', globals: true, include: ['tests/*.spec.ts'], exclude: ['tests/*-retrieval.spec.ts', 'tests/login.spec.ts', 'tests/refund-flow.spec.ts'] },
})
