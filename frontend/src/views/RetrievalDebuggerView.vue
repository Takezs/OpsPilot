<script setup lang="ts">
import { ref } from 'vue'
import { api } from '../api/client'
import type { CitationSnapshot, RetrievalDebugResponse, RetrievalStageItem } from '../api/types'
import CitationDrawer from '../components/CitationDrawer.vue'
import RetrievalStageColumn from '../components/RetrievalStageColumn.vue'
const query = ref(''), loading = ref(false), error = ref(''), result = ref<RetrievalDebugResponse | null>(null)
const selected = ref<CitationSnapshot | null>(null), drawerOpen = ref(false)
const stages: Array<{ key: 'dense' | 'fts' | 'rrf' | 'reranker'; title: string }> = [{ key: 'dense', title: 'Dense' }, { key: 'fts', title: 'PostgreSQL FTS' }, { key: 'rrf', title: 'RRF' }, { key: 'reranker', title: 'Reranker' }]
async function run(): Promise<void> { if (!query.value.trim()) return; loading.value = true; error.value = ''; try { result.value = (await api.post<RetrievalDebugResponse>('/retrieval/debug', { query: query.value, top_k: 10 })).data } catch { result.value = null; error.value = '检索调试失败或无权访问' } finally { loading.value = false } }
function openCitation(item: RetrievalStageItem): void { selected.value = { document_id: item.document_id, document_version: item.document_version, chunk_id: item.chunk_id, section_path: item.section_path, page: item.page }; drawerOpen.value = true }
</script>
<template><section class="page"><p class="eyebrow">RETRIEVAL PIPELINE</p><h1>检索调试器</h1><div class="toolbar"><el-input v-model="query" aria-label="检索查询" placeholder="输入查询" @keyup.enter="run" /><el-button type="primary" :loading="loading" @click="run">运行调试</el-button></div><el-alert v-if="error" :title="error" type="error" :closable="false" /><el-alert v-if="result?.reranker_status === 'degraded'" title="Reranker 已降级，当前保留 RRF 顺序" type="warning" :closable="false" /><div v-if="result" class="retrieval-grid"><RetrievalStageColumn v-for="stage in stages" :key="stage.key" :title="stage.title" :items="result[stage.key]" @select="openCitation" /></div><el-empty v-else-if="!loading && !error" description="运行查询以查看四阶段结果" /><CitationDrawer v-model="drawerOpen" :snapshot="selected" :highlight="query" /></section></template>
