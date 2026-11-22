import App from './App.vue'
import { createApp } from 'vue'
import { initStore, store } from './store'          // Store
import { useUserStore } from './store/modules/user' // 用户状态
import { initRouter } from './router'               // Router
import language from './locales'                    // 国际化
import '@styles/core/tailwind.css'                  // tailwind
import '@styles/index.scss'                         // 样式
import '@utils/sys/console.ts'                      // 控制台输出内容
import '@utils/ui/iconify-loader'                   // 注册本地自定义图标（主升浪菜单）
import { setupGlobDirectives } from './directives'
import { setupErrorHandle } from './utils/sys/error-handle'
import { cleanStaleCache } from './utils/sys/cache-cleaner' // 清理模板遗留缓存

document.addEventListener(
  'touchstart',
  function () {},
  { passive: false }
)

// 在 store 初始化前清理模板遗留缓存，避免旧标签页/搜索历史被载入
cleanStaleCache()

const app = createApp(App)
initStore(app)

// 本地盯盘工具：路由初始化前自动登录，跳过登录页
useUserStore(store).autoLogin()

initRouter(app)
setupGlobDirectives(app)
setupErrorHandle(app)

app.use(language)
app.mount('#app')