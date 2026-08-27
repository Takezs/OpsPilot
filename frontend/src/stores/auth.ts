import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import type { LoginRequest, Principal, Role } from '../api/types'
import { api, resetUnauthorizedRedirect } from '../api/client'

const TOKEN_KEY = 'opspilot.access_token'

function decodeBase64Url(value: string): string {
  const normalized = value.replaceAll('-', '+').replaceAll('_', '/')
  return atob(normalized.padEnd(Math.ceil(normalized.length / 4) * 4, '='))
}

function decodePrincipal(token: string): Principal | null {
  try {
    const parts = token.split('.')
    if (parts.length !== 3) return null
    const payload = JSON.parse(decodeBase64Url(parts[1])) as Record<string, unknown>
    if (
      typeof payload.sub !== 'string' ||
      !['USER', 'REVIEWER', 'ADMIN'].includes(String(payload.role)) ||
      typeof payload.exp !== 'number' ||
      payload.exp <= Date.now() / 1000
    ) return null
    return { userId: payload.sub, role: payload.role as Role, expiresAt: payload.exp }
  } catch {
    return null
  }
}

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string | null>(null)
  const principal = ref<Principal | null>(null)
  const initialized = ref(false)
  const isAuthenticated = computed(() => token.value !== null && principal.value !== null)

  function clearSession(): void {
    token.value = null
    principal.value = null
    localStorage.removeItem(TOKEN_KEY)
  }

  function restore(): void {
    const stored = localStorage.getItem(TOKEN_KEY)
    const restored = stored ? decodePrincipal(stored) : null
    if (!stored || !restored) clearSession()
    else {
      token.value = stored
      principal.value = restored
    }
    initialized.value = true
  }

  async function login(credentials: LoginRequest): Promise<void> {
    const response = await api.post<{ access_token: string; token_type: string }>(
      '/auth/login', credentials,
    )
    const restored = decodePrincipal(response.data.access_token)
    if (!restored) throw new Error('invalid authentication response')
    localStorage.setItem(TOKEN_KEY, response.data.access_token)
    token.value = response.data.access_token
    principal.value = restored
    initialized.value = true
    resetUnauthorizedRedirect()
  }

  return { token, principal, initialized, isAuthenticated, clearSession, restore, login }
})
