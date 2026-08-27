import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'
import { sanitizeReturnUrl } from '../src/router/security'
import { useAuthStore } from '../src/stores/auth'

function token(role: string, exp = Math.floor(Date.now() / 1000) + 600): string {
  const part = (value: object) => btoa(JSON.stringify(value)).replaceAll('=', '')
  return `${part({ alg: 'HS256', typ: 'JWT' })}.${part({ sub: 'user-1', role, exp })}.signature`
}

describe('authenticated shell security', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
  })

  it('accepts only protected internal return URLs', () => {
    expect(sanitizeReturnUrl('/knowledge?tab=ready')).toBe('/knowledge?tab=ready')
    for (const unsafe of ['https://evil.example', '//evil.example', '/login', '/unknown', 'javascript:x']) {
      expect(sanitizeReturnUrl(unsafe)).toBe('/workspace')
    }
  })

  it('restores a valid session and rejects malformed or expired tokens', () => {
    localStorage.setItem('opspilot.access_token', token('REVIEWER'))
    const auth = useAuthStore()
    auth.restore()
    expect(auth.principal?.role).toBe('REVIEWER')
    expect(auth.token).not.toBeNull()
    auth.clearSession()
    localStorage.setItem('opspilot.access_token', token('USER', 1))
    auth.restore()
    expect(auth.token).toBeNull()
    expect(localStorage.getItem('opspilot.access_token')).toBeNull()
  })
})
