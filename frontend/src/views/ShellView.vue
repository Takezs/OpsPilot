<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
const route = useRoute()
const title = computed(() => String(route.meta.title ?? 'OpsPilot'))
</script>
<template><section class="shell"><p class="eyebrow">OpsPilot</p><h1>{{ title }}</h1><p>页面功能将在后续任务中接入。</p></section></template>
