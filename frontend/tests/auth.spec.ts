import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../src/api/client'
import { sanitizeReturnUrl } from '../src/router/security'
import { useAuthStore } from '../src/stores/auth'

function token(role: string, exp = Math.floor(Date.now() / 1000) + 600): string {
  const part = (value: object) => btoa(JSON.stringify(value)).replaceAll('=', '')
  return `${part({ alg: 'HS256', typ: 'JWT' })}.${part({ sub: 'user-1', role, exp })}.signature`
}

describe('authenticated shell security', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    localStorage.clear()
    setActivePinia(createPinia())
  })

  it('accepts only protected internal return URLs', () => {
    expect(sanitizeReturnUrl('/knowledge?tab=ready')).toBe('/knowledge?tab=ready')
    for (const unsafe of ['https://evil.example', '//evil.example', '/login', '/unknown', 'javascript:x']) {
      expect(sanitizeReturnUrl(unsafe)).toBe('/workspace')
    }
  })

  it('trusts only /auth/me and shares concurrent initialization', async () => {
    localStorage.setItem('opspilot.access_token', token('ADMIN'))
    const get = vi.spyOn(api, 'get').mockResolvedValue({
      data: { user_id: 'user-1', role: 'USER', allowed_departments: [], max_access_level: 1 },
    })
    const auth = useAuthStore()
    await Promise.all([auth.initialize(), auth.initialize()])
    expect(get).toHaveBeenCalledTimes(1)
    expect(auth.principal?.role).toBe('USER')
    expect(auth.token).not.toBeNull()

    setActivePinia(createPinia())
    localStorage.setItem('opspilot.access_token', token('USER', 1))
    const expiredAuth = useAuthStore()
    await expiredAuth.initialize()
    expect(expiredAuth.token).toBeNull()
    expect(localStorage.getItem('opspilot.access_token')).toBeNull()
  })
})
