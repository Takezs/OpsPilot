const protectedPath = /^\/(workspace|knowledge|retrieval|approvals)(?:[/?#]|$)|^\/runs\/[^/?#]+(?:[/?#]|$)/

export function sanitizeReturnUrl(value: unknown): string {
  if (typeof value !== 'string' || !value.startsWith('/') || value.startsWith('//')) {
    return '/workspace'
  }
  return protectedPath.test(value) ? value : '/workspace'
}
