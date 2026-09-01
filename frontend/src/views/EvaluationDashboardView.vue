<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '../api/client'
import MetricComparisonChart from '../components/MetricComparisonChart.vue'
type Execution = { id:string; dataset_version:string; configuration_sha:string; status:string }
type Report = { configuration:Record<string,unknown>; metrics:Record<string,unknown>; failures:Array<{case_id:string; repetition:number; error:string|null}> }
const executions = ref<Execution[]>([]); const selected = ref(''); const report = ref<Report|null>(null); const error = ref('')
async function loadReport(): Promise<void> {
  if (!selected.value) return
  error.value=''; report.value=null
  try { report.value=(await api.get<Report>(`/evaluations/${selected.value}/report.json`)).data }
  catch { error.value='评测报告暂不可用' }
}
async function download(format:'json'|'csv'|'html'): Promise<void> {
  const response=await api.get(`/evaluations/${selected.value}/report.${format}`,{responseType:'blob'})
  const url=URL.createObjectURL(response.data as Blob); const link=document.createElement('a'); link.href=url; link.download=`evaluation.${format}`; link.click(); URL.revokeObjectURL(url)
}
onMounted(async()=>{ try { executions.value=(await api.get<Execution[]>('/evaluations')).data; selected.value=executions.value[0]?.id??''; await loadReport() } catch { error.value='无法读取评测记录' } })
</script>
<template><section class="evaluation-dashboard"><h1>评测看板</h1>
  <label>执行记录 <select v-model="selected" @change="loadReport"><option v-for="item in executions" :key="item.id" :value="item.id">{{ item.dataset_version }} · {{ item.status }}</option></select></label>
  <p v-if="error" role="alert">{{ error }}</p>
  <template v-if="report"><MetricComparisonChart :metrics="report.metrics"/><h2>实验配置</h2><pre>{{ JSON.stringify(report.configuration,null,2) }}</pre>
    <h2>失败案例</h2><p v-if="!report.failures.length">无失败案例</p><ul><li v-for="item in report.failures" :key="`${item.case_id}-${item.repetition}`">{{ item.case_id }} #{{ item.repetition }} — {{ item.error ?? '指标未达标' }}</li></ul>
    <nav class="downloads"><button @click="download('json')">JSON</button><button @click="download('csv')">CSV</button><button @click="download('html')">HTML</button></nav>
  </template></section></template>
<style scoped>.evaluation-dashboard{max-width:1100px;margin:auto}.evaluation-dashboard label{display:flex;gap:10px;margin-bottom:18px}.evaluation-dashboard pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f6f8fa;padding:12px}.downloads{display:flex;gap:16px}</style>
