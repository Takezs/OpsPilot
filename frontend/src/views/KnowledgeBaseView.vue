<script setup lang="ts">
import axios from 'axios'
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from '../api/client'
import type { DocumentRecord, KnowledgeBaseRecord, UploadResponse } from '../api/types'
import DocumentStatusTable from '../components/DocumentStatusTable.vue'
import { pollDocument } from '../composables/documentPolling'
const knowledgeBases = ref<KnowledgeBaseRecord[]>([]), selectedId = ref(''), documents = ref<DocumentRecord[]>([])
const loading = ref(true), error = ref(''), selectedFile = ref<File | null>(null), uploadProgress = ref(0), uploading = ref(false)
const controllers = new Map<string, AbortController>()
async function loadDocuments(): Promise<void> { if (!selectedId.value) { documents.value = []; return }; documents.value = (await api.get<DocumentRecord[]>(`/knowledge-bases/${selectedId.value}/documents`, { params: { limit: 100 } })).data }
async function load(): Promise<void> { loading.value = true; error.value = ''; try { knowledgeBases.value = (await api.get<KnowledgeBaseRecord[]>('/knowledge-bases', { params: { limit: 100 } })).data; selectedId.value = knowledgeBases.value[0]?.id ?? ''; await loadDocuments() } catch { error.value = '知识库加载失败' } finally { loading.value = false } }
function replaceDocument(document: DocumentRecord): void { const index = documents.value.findIndex((item) => item.id === document.id); if (index === -1) documents.value.unshift(document); else documents.value[index] = document }
async function fetchDocument(id: string, signal?: AbortSignal): Promise<DocumentRecord> { return (await api.get<DocumentRecord>(`/knowledge/documents/${id}`, { signal })).data }
async function upload(): Promise<void> {
  if (!selectedFile.value || !selectedId.value) return
  uploading.value = true; error.value = ''; uploadProgress.value = 0
  const file = selectedFile.value, form = new FormData(); form.append('title', file.name); form.append('file', file)
  try {
    const response = await api.post<UploadResponse>(`/knowledge/${selectedId.value}/documents`, form, { onUploadProgress: (event) => { uploadProgress.value = event.total ? Math.round(event.loaded * 100 / event.total) : 0 } })
    const placeholder: DocumentRecord = { id: response.data.document_id, knowledge_base_id: selectedId.value, title: file.name, version: response.data.version, effective_at: new Date().toISOString(), status: response.data.status, failure_message: null }
    replaceDocument(placeholder)
    const controller = new AbortController(); controllers.set(placeholder.id, controller)
    void pollDocument(placeholder.id, fetchDocument, replaceDocument, { signal: controller.signal }).catch((pollError: unknown) => { if (!axios.isCancel(pollError) && !(pollError instanceof DOMException && pollError.name === 'AbortError')) error.value = '文档状态更新失败' }).finally(() => controllers.delete(placeholder.id))
  } catch { error.value = '文档上传失败' } finally { uploading.value = false }
}
function chooseFile(event: Event): void { selectedFile.value = (event.target as HTMLInputElement).files?.[0] ?? null }
onMounted(load); onBeforeUnmount(() => { for (const controller of controllers.values()) controller.abort(); controllers.clear() })
</script>
<template><section class="page" v-loading="loading"><p class="eyebrow">KNOWLEDGE</p><h1>知识库</h1><el-alert v-if="error" :title="error" type="error" :closable="false" /><div v-if="knowledgeBases.length" class="toolbar"><el-select v-model="selectedId" aria-label="知识库" @change="loadDocuments"><el-option v-for="kb in knowledgeBases" :key="kb.id" :label="kb.name" :value="kb.id" /></el-select><label class="file-picker">选择文件<input aria-label="选择文件" type="file" accept=".md,.txt,.pdf,.docx" @change="chooseFile" /></label><el-button type="primary" :disabled="!selectedFile" :loading="uploading" @click="upload">上传文档</el-button><el-progress v-if="uploading" :percentage="uploadProgress" /></div><el-empty v-else-if="!loading" description="没有可访问的知识库" /><DocumentStatusTable v-if="knowledgeBases.length" :documents="documents" /></section></template>
