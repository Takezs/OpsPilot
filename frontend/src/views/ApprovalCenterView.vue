<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '../api/client'
import type { ApprovalRecord } from '../api/types'
const approvals = ref<ApprovalRecord[]>([]); const pending = ref(new Set<string>()); const notice = ref('')
async function load(): Promise<void> { approvals.value = (await api.get<ApprovalRecord[]>('/approval-requests', { params: { status: 'PENDING' } })).data }
async function decide(id: string, decision: 'APPROVE'|'REJECT'): Promise<void> { if (pending.value.has(id)) return; pending.value.add(id); pending.value = new Set(pending.value); try { await api.post(`/approval-requests/${id}/decisions`, { decision }); notice.value = decision === 'APPROVE' ? '已允许执行，等待 Worker 处理。' : '已拒绝执行。'; await load() } catch (error: unknown) { const status = (error as {response?:{status?:number}}).response?.status; notice.value = status === 409 ? '审批已处理、已过期或参数已变化，请刷新。' : '审批提交失败。' } finally { pending.value.delete(id); pending.value = new Set(pending.value) } }
onMounted(() => { void load() })
</script>
<template><section><h1>审批中心</h1><p v-if="notice" role="status">{{ notice }}</p><article v-for="item in approvals" :key="item.id"><h2>{{ item.tool_name }}</h2><pre>{{ JSON.stringify(item.arguments, null, 2) }}</pre><button :disabled="pending.has(item.id)" @click="decide(item.id,'APPROVE')">批准</button><button :disabled="pending.has(item.id)" @click="decide(item.id,'REJECT')">拒绝</button></article><p v-if="!approvals.length">暂无待审批请求</p></section></template>
