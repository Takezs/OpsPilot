export type Role = 'USER' | 'REVIEWER' | 'ADMIN'

export interface Principal {
  userId: string
  role: Role
  expiresAt: number
}

export interface LoginRequest {
  username: string
  password: string
}

export interface LoginResponse {
  access_token: string
  token_type: 'bearer'
}
