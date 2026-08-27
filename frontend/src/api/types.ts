export type Role = 'USER' | 'REVIEWER' | 'ADMIN'

export interface Principal {
  userId: string
  role: Role
  allowedDepartments: string[]
  maxAccessLevel: number
}

export interface SessionResponse {
  user_id: string
  role: Role
  allowed_departments: string[]
  max_access_level: number
}

export interface LoginRequest {
  username: string
  password: string
}

export interface LoginResponse {
  access_token: string
  token_type: 'bearer'
}
