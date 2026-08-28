<script setup lang="ts">
import type { OperationRecord } from '../api/types'
defineProps<{ operation: OperationRecord }>()
</script>
<template><article class="tool-card"><header><strong>{{ operation.tool_name }}</strong><span :class="`state state-${operation.status.toLowerCase()}`">{{ operation.status }}</span></header><p v-if="operation.status === 'OUTCOME_UNKNOWN'">结果暂未确认，系统不会自动重复退款。</p><p v-else-if="operation.status === 'RECONCILING'">正在向支付服务核对真实结果。</p><p v-else-if="operation.status === 'MANUAL_REVIEW'">需要管理员人工处理。</p><p v-else-if="operation.status === 'EXECUTING'">正在执行退款。</p></article></template>
