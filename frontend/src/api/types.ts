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

export type OperationStatus = 'CREATED' | 'WAITING_APPROVAL' | 'READY' | 'EXECUTING' | 'RETRYING' | 'OUTCOME_UNKNOWN' | 'RECONCILING' | 'SUCCEEDED' | 'FAILED' | 'DENIED' | 'REJECTED' | 'MANUAL_REVIEW'
export interface RunEventRecord { run_id: string; seq: number; event_type: string; payload: Record<string, unknown>; created_at: string }
export interface AttemptRecord { id: string; attempt_number: number; kind: string; status: string; error: string | null; completed_at: string | null }
export interface OperationRecord { id: string; tool_name: string; status: OperationStatus; version: number; policy_decision: string | null; provider_reference_id: string | null; attempts: AttemptRecord[] }
export interface RunDetail { id: string; status: string; next_seq: number; created_at: string; operations: OperationRecord[] }
export interface ApprovalRecord { id: string; operation_id: string; arguments_hash: string; operation_version: number; status: string; operation_status: OperationStatus; tool_name: string; arguments: Record<string, unknown>; expires_at: string; created_at: string }
