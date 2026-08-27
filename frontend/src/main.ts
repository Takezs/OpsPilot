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
app.use(pinia).use(router).use(ElementPlus)
configureApi(pinia, router)
app.mount('#app')
