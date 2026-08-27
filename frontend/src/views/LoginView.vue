<script setup lang="ts">
import axios from 'axios'
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { sanitizeReturnUrl } from '../router/security'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
const form = reactive({ username: '', password: '' })
const errorMessage = ref('')
const loading = ref(false)

async function submit(): Promise<void> {
  errorMessage.value = ''
  loading.value = true
  try {
    await auth.login(form)
    await router.replace(sanitizeReturnUrl(route.query.returnUrl))
  } catch (error: unknown) {
    errorMessage.value = axios.isAxiosError(error) && error.response?.status === 401
      ? '用户名或密码错误' : '登录暂时不可用，请稍后重试'
  } finally {
    loading.value = false
  }
}
</script>
<template>
  <main class="login-page">
    <el-card class="login-card">
      <p class="eyebrow">OPSPILOT CONTROL PLANE</p><h1>欢迎回来</h1>
      <el-form @submit.prevent="submit">
        <el-form-item label="用户名"><el-input v-model="form.username" aria-label="用户名" autocomplete="username" /></el-form-item>
        <el-form-item label="密码"><el-input v-model="form.password" aria-label="密码" type="password" autocomplete="current-password" /></el-form-item>
        <p v-if="errorMessage" role="alert" class="error">{{ errorMessage }}</p>
        <el-button native-type="submit" type="primary" :loading="loading">登录</el-button>
      </el-form>
    </el-card>
  </main>
</template>
