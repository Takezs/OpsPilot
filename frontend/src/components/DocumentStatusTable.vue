<script setup lang="ts">
import type { DocumentRecord } from '../api/types'
defineProps<{ documents: DocumentRecord[] }>()
</script>
<template><div class="table-wrap"><table v-if="documents.length" class="document-table"><thead><tr><th>文档</th><th>版本</th><th>状态</th><th>说明</th></tr></thead><tbody><tr v-for="document in documents" :key="document.id"><td>{{ document.title }}</td><td>v{{ document.version }}</td><td><el-tag :type="document.status === 'READY' ? 'success' : document.status === 'FAILED' ? 'danger' : 'warning'">{{ document.status }}</el-tag></td><td>{{ document.failure_message ?? '—' }}</td></tr></tbody></table><el-empty v-else description="暂无文档" /></div></template>
