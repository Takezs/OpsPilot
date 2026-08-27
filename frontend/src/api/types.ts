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

export type DocumentStatus = 'UPLOADED' | 'PARSING' | 'CHUNKING' | 'INDEXING' | 'READY' | 'FAILED'
export interface KnowledgeBaseRecord { id: string; name: string; department: string; access_level: number }
export interface DocumentRecord { id: string; knowledge_base_id: string; title: string; version: number; effective_at: string; status: DocumentStatus; failure_message: string | null }
export interface UploadResponse { document_id: string; version: number; status: DocumentStatus }
export type RetrievalSource = 'dense' | 'fts' | 'rrf' | 'reranker'
export interface RetrievalStageItem { chunk_id: string; document_id: string; document_version: number; document_title: string; section_path: string[]; page: number | null; rank: number; score: number; source: RetrievalSource; content_excerpt: string }
export interface RetrievalDebugResponse { dense: RetrievalStageItem[]; fts: RetrievalStageItem[]; rrf: RetrievalStageItem[]; reranker: RetrievalStageItem[]; reranker_status: 'ok' | 'degraded' }
export interface CitationSnapshot { document_id: string; document_version: number; chunk_id: string; section_path: string[]; page: number | null }
export interface CitationDetail extends CitationSnapshot { document_title: string; content: string; effective_at: string }
