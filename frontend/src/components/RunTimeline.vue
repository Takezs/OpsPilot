<script setup lang="ts">
import type { RunEventRecord } from '../api/types'
defineProps<{ events: RunEventRecord[] }>()
</script>
<template><ol class="timeline"><li v-for="item in events" :key="item.seq"><strong>#{{ item.seq }} {{ item.event_type }}</strong><pre>{{ JSON.stringify(item.payload, null, 2) }}</pre></li></ol></template>
