<script setup lang="ts">
defineProps<{ metrics: Record<string, unknown> }>()
function valueOf(value: unknown): string {
  if (typeof value === 'number') return value.toFixed(4)
  if (value && typeof value === 'object' && 'mean' in value) {
    const mean = (value as { mean?: unknown }).mean
    return typeof mean === 'number' ? mean.toFixed(4) : '—'
  }
  return '—'
}
</script>
<template>
  <div class="metric-grid" aria-label="评测指标">
    <article v-for="(value, name) in metrics" :key="name" class="metric-card">
      <span>{{ name }}</span><strong>{{ valueOf(value) }}</strong>
    </article>
  </div>
</template>
<style scoped>
.metric-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px }
.metric-card { border:1px solid #dfe4ea; border-radius:10px; padding:14px; display:flex; flex-direction:column; gap:8px }
.metric-card span { color:#637083; font-size:12px; overflow-wrap:anywhere }
.metric-card strong { font-size:22px }
</style>
