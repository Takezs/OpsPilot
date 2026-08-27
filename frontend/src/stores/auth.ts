import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import type { LoginRequest, Principal, SessionResponse } from '../api/types'
import { api, resetUnauthorizedRedirect } from '../api/client'

const TOKEN_KEY = 'opspilot.access_token'

function decodeBase64Url(value: string): string {
  const normalized = value.replaceAll('-', '+').replaceAll('_', '/')
  return atob(normalized.padEnd(Math.ceil(normalized.length / 4) * 4, '='))
}

function isPlausiblyUnexpired(token: string): boolean {
  try {
    const parts = token.split('.')
    if (parts.length !== 3) return false
    const payload = JSON.parse(decodeBase64Url(parts[1])) as Record<string, unknown>
    if (
      typeof payload.exp !== 'number' ||
      payload.exp <= Date.now() / 1000
    ) return false
    return true
  } catch {
    return false
  }
}

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string | null>(null)
  const principal = ref<Principal | null>(null)
  const initialized = ref(false)
  let initialization: Promise<void> | null = null
  const isAuthenticated = computed(() => token.value !== null && principal.value !== null)

  function clearSession(): void {
    token.value = null
    principal.value = null
    localStorage.removeItem(TOKEN_KEY)
  }

  async function loadPrincipal(): Promise<void> {
    const response = await api.get<SessionResponse>('/auth/me')
    const session = response.data
    if (
      typeof session.user_id !== 'string' ||
      !['USER', 'REVIEWER', 'ADMIN'].includes(session.role)
    ) throw new Error('invalid session response')
    principal.value = {
      userId: session.user_id,
      role: session.role,
      allowedDepartments: session.allowed_departments,
      maxAccessLevel: session.max_access_level,
    }
  }

  async function initialize(): Promise<void> {
    if (initialized.value) return
    if (initialization) return initialization
    initialization = (async () => {
      const stored = localStorage.getItem(TOKEN_KEY)
      if (!stored || !isPlausiblyUnexpired(stored)) {
        clearSession()
        initialized.value = true
        return
      }
      token.value = stored
      try {
        await loadPrincipal()
      } catch {
        clearSession()
      } finally {
        initialized.value = true
      }
    })()
    try {
      await initialization
    } finally {
      initialization = null
    }
  }

  async function login(credentials: LoginRequest): Promise<void> {
    const response = await api.post<{ access_token: string; token_type: string }>(
      '/auth/login', credentials,
    )
    if (!isPlausiblyUnexpired(response.data.access_token)) {
      throw new Error('invalid authentication response')
    }
    localStorage.setItem(TOKEN_KEY, response.data.access_token)
    token.value = response.data.access_token
    try {
      await loadPrincipal()
      initialized.value = true
      resetUnauthorizedRedirect()
    } catch (error) {
      clearSession()
      throw error
    }
  }

  return { token, principal, initialized, isAuthenticated, clearSession, initialize, login }
})
