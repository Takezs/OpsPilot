<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api/client'
import { useAuthStore } from '../stores/auth'
const auth = useAuthStore()
const router = useRouter()
const canApprove = computed(() => ['REVIEWER', 'ADMIN'].includes(auth.principal?.role ?? ''))
async function checkSession(): Promise<void> {
  await Promise.allSettled([api.get('/knowledge-bases'), api.get('/knowledge-bases')])
}
function logout(): void { auth.clearSession(); void router.replace('/login') }
</script>
<template>
  <div class="app-shell">
    <aside><div class="brand">OpsPilot</div><nav>
      <router-link to="/workspace">Agent 工作台</router-link><router-link to="/knowledge">知识库</router-link>
      <router-link to="/retrieval">检索调试器</router-link><router-link v-if="canApprove" to="/approvals">审批中心</router-link>
      <router-link to="/runs/demo">Run 时间线</router-link>
    </nav></aside>
    <main><header><span>{{ auth.principal?.role }}</span><button data-testid="session-check" @click="checkSession">检查会话</button><button @click="logout">退出</button></header><router-view /></main>
  </div>
</template>
