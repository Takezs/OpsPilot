import axios from 'axios'
import type { Pinia } from 'pinia'
import type { Router } from 'vue-router'
import { useAuthStore } from '../stores/auth'

export const api = axios.create({ baseURL: '/api/v1', timeout: 15000 })

let unauthorizedRedirecting = false
let configuredPinia: Pinia | null = null
let configuredRouter: Router | null = null

export function resetUnauthorizedRedirect(): void {
  unauthorizedRedirecting = false
}

export function configureApi(pinia: Pinia, router: Router): void {
  configuredPinia = pinia
  configuredRouter = router
  api.interceptors.request.use((config) => {
    const auth = useAuthStore(pinia)
    if (auth.token) config.headers.Authorization = `Bearer ${auth.token}`
    return config
  })
  api.interceptors.response.use(
    (response) => response,
    async (error: unknown) => {
      if (axios.isAxiosError(error) && error.response?.status === 401) {
        const auth = useAuthStore(pinia)
        auth.clearSession()
        if (!unauthorizedRedirecting) {
          unauthorizedRedirecting = true
          const current = router.currentRoute.value.fullPath
          await router.replace({ name: 'login', query: { returnUrl: current } })
        }
      }
      return Promise.reject(error)
    },
  )
}

export async function authorizedStreamFetch(path: string, init: RequestInit): Promise<Response> {
  if (!configuredPinia) throw new Error('API client is not configured')
  const auth = useAuthStore(configuredPinia)
  const headers = new Headers(init.headers)
  if (auth.token) headers.set('Authorization', `Bearer ${auth.token}`)
  const response = await fetch(`/api/v1${path}`, { ...init, headers })
  if (response.status === 401) {
    auth.clearSession()
    if (!unauthorizedRedirecting && configuredRouter) {
      unauthorizedRedirecting = true
      await configuredRouter.replace({ name: 'login', query: { returnUrl: configuredRouter.currentRoute.value.fullPath } })
    }
  }
  return response
}
