<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { api } from '../api/client'
import type { CitationDetail, CitationSnapshot } from '../api/types'
import { createLatestCitationLoader } from '../composables/citationLoading'
const props = defineProps<{ modelValue: boolean; snapshot: CitationSnapshot | null; highlight: string }>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()
const detail = ref<CitationDetail | null>(null)
const loading = ref(false)
const error = ref('')
const loader = createLatestCitationLoader<CitationDetail>(
  async (path, signal) => (await api.get<CitationDetail>(path, { signal })).data,
  (value) => { detail.value = value },
  () => { error.value = '引用快照不可用或无权访问' },
  (value) => { loading.value = value },
)
watch(() => [props.modelValue, props.snapshot] as const, async ([open, snapshot]) => {
  detail.value = null; error.value = ''
  if (!open || !snapshot) { loader.close(); return }
  await loader.load(`/knowledge/documents/${snapshot.document_id}/versions/${snapshot.document_version}/chunks/${snapshot.chunk_id}`)
}, { immediate: true })
onBeforeUnmount(loader.close)
const segments = computed(() => {
  if (!detail.value || !props.highlight.trim()) return [{ text: detail.value?.content ?? '', hit: false }]
  const needle = props.highlight.trim()
  const escaped = needle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return detail.value.content.split(new RegExp(`(${escaped})`, 'gi')).filter(Boolean).map((text) => ({ text, hit: text.toLowerCase() === needle.toLowerCase() }))
})
</script>
<template><el-drawer :model-value="modelValue" title="引用快照" size="min(560px, 92vw)" @close="emit('update:modelValue', false)"><div v-loading="loading"><el-alert v-if="error" :title="error" type="error" :closable="false" /><template v-else-if="detail"><h3>{{ detail.document_title }} · v{{ detail.document_version }}</h3><p>{{ detail.section_path.length ? detail.section_path.join(' / ') : '无章节信息' }} · {{ detail.page === null ? '无页码' : `第 ${detail.page} 页` }}</p><pre class="citation-content"><template v-for="(segment, index) in segments" :key="index"><mark v-if="segment.hit">{{ segment.text }}</mark><template v-else>{{ segment.text }}</template></template></pre></template></div></el-drawer></template>
