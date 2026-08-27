import axios from 'axios'
import type { Pinia } from 'pinia'
import type { Router } from 'vue-router'
import { useAuthStore } from '../stores/auth'

export const api = axios.create({ baseURL: '/api/v1', timeout: 15000 })

let unauthorizedRedirecting = false

export function resetUnauthorizedRedirect(): void {
  unauthorizedRedirecting = false
}

export function configureApi(pinia: Pinia, router: Router): void {
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
