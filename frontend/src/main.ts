import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import './style.css'
import App from './App.vue'
import { router } from './router'
import { configureApi } from './api/client'

const app = createApp(App)
const pinia = createPinia()
app.use(pinia)
configureApi(pinia, router)
app.use(router).use(ElementPlus)
app.mount('#app')
