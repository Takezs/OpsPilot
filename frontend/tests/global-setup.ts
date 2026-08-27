import { createServer, type ViteDevServer } from 'vite'

let server: ViteDevServer | undefined

export default async function globalSetup(): Promise<() => Promise<void>> {
  server = await createServer({ server: { host: '127.0.0.1', port: 4173, strictPort: true } })
  await server.listen()
  return async () => {
    await server?.close()
    server = undefined
  }
}
