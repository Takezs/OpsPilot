<script setup lang="ts">
import type { RetrievalStageItem } from '../api/types'
defineProps<{ title: string; items: RetrievalStageItem[] }>()
defineEmits<{ select: [item: RetrievalStageItem] }>()
</script>
<template><section class="stage-column"><h2>{{ title }}</h2><el-empty v-if="!items.length" description="无结果" /><button v-for="item in items" :key="item.chunk_id" class="result-card" :aria-label="`${item.document_title} 版本 ${item.document_version}`" @click="$emit('select', item)"><strong>#{{ item.rank }} {{ item.document_title }}</strong><span>score {{ item.score.toFixed(4) }}</span><small>{{ item.chunk_id }}</small><p>{{ item.content_excerpt }}</p></button></section></template>
