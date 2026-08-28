import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import AppLayout from '../layout/AppLayout.vue'
import LoginView from '../views/LoginView.vue'
import ShellView from '../views/ShellView.vue'
import KnowledgeBaseView from '../views/KnowledgeBaseView.vue'
import RetrievalDebuggerView from '../views/RetrievalDebuggerView.vue'
import AgentWorkspaceView from '../views/AgentWorkspaceView.vue'
import ApprovalCenterView from '../views/ApprovalCenterView.vue'
import RunDetailView from '../views/RunDetailView.vue'
import { sanitizeReturnUrl } from './security'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/workspace' },
    { path: '/login', name: 'login', component: LoginView },
    {
      path: '/', component: AppLayout, meta: { requiresAuth: true }, children: [
        { path: 'workspace', name: 'workspace', component: AgentWorkspaceView, meta: { title: 'Agent 工作台' } },
        { path: 'knowledge', name: 'knowledge', component: KnowledgeBaseView, meta: { title: '知识库' } },
        { path: 'retrieval', name: 'retrieval', component: RetrievalDebuggerView, meta: { title: '检索调试器' } },
        { path: 'approvals', name: 'approvals', component: ApprovalCenterView, meta: { title: '审批中心', roles: ['REVIEWER', 'ADMIN'] } },
        { path: 'runs/:id', name: 'run', component: RunDetailView, meta: { title: 'Run 时间线' } },
      ],
    },
    { path: '/:pathMatch(.*)*', redirect: '/workspace' },
  ],
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  await auth.initialize()
  if (to.meta.requiresAuth && !auth.isAuthenticated) {
    return { name: 'login', query: { returnUrl: sanitizeReturnUrl(to.fullPath) } }
  }
  const roles = to.meta.roles as string[] | undefined
  if (roles && (!auth.principal || !roles.includes(auth.principal.role))) return '/workspace'
  if (to.name === 'login' && auth.isAuthenticated) return '/workspace'
  return true
})
