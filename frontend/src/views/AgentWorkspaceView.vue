<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api/client'
const router = useRouter(); const message = ref(''); const busy = ref(false); const error = ref('')
async function submit(): Promise<void> { busy.value = true; error.value = ''; try { const run = await api.post<{run_id:string}>('/runs'); await api.post(`/runs/${run.data.run_id}/messages`, { content: message.value }); await router.push(`/runs/${run.data.run_id}`) } catch { error.value = '无法启动任务，请稍后重试。' } finally { busy.value = false } }
</script>
<template><section><h1>Agent 工作台</h1><textarea v-model="message" aria-label="任务内容" /><button :disabled="busy || !message.trim()" @click="submit">{{ busy ? '提交中…' : '开始运行' }}</button><p v-if="error" role="alert">{{ error }}</p></section></template>
