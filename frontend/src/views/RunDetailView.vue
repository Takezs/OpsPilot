<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../api/client'
import type { RunDetail } from '../api/types'
import RunTimeline from '../components/RunTimeline.vue'; import ToolCallCard from '../components/ToolCallCard.vue'
import { useRunEvents } from '../composables/useRunEvents'
const route = useRoute(); const id=String(route.params.id); const run = ref<RunDetail|null>(null); const { events, connection, start } = useRunEvents(id)
async function load(): Promise<void> { run.value=(await api.get<RunDetail>(`/runs/${id}`)).data; await start() }
onMounted(() => { void load().catch(() => { connection.value='RECONNECTING' }) })
</script>
<template><section><h1>Run 时间线</h1><p>连接：{{ connection }}</p><template v-if="run"><ToolCallCard v-for="operation in run.operations" :key="operation.id" :operation="operation" /><RunTimeline :events="events" /></template></section></template>
