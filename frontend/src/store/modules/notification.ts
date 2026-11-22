/** 站内通知全局状态：未读数 + 通知列表（铃铛与通知面板共享）。 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import { monitorApi, type NoticeItem } from '@/api/monitor'

export const useNotificationStore = defineStore('notification', () => {
  const items = ref<NoticeItem[]>([])
  const unread = ref(0)
  const loaded = ref(false)
  let timer: ReturnType<typeof setInterval> | null = null

  async function load(limit = 100, silent = true) {
    try {
      const res = await monitorApi.notices(limit)
      items.value = res?.items || []
      unread.value = res?.unread || 0
      loaded.value = true
    } catch {
      if (!silent) throw new Error('加载通知失败')
    }
  }

  async function refreshUnread() {
    try {
      const res = await monitorApi.noticeUnread()
      unread.value = res?.count || 0
    } catch {
      /* 静默 */
    }
  }

  async function markRead(id?: string) {
    try {
      await monitorApi.noticeRead(id)
      if (id) {
        const item = items.value.find((x) => x.id === id)
        if (item) item.read = true
      } else {
        items.value.forEach((x) => (x.read = true))
      }
      await refreshUnread()
    } catch {
      /* 静默 */
    }
  }

  async function clear() {
    try {
      await monitorApi.noticeClear()
      items.value = []
      unread.value = 0
    } catch {
      /* 静默 */
    }
  }

  /** 启动轮询未读数（60 秒），组件卸载时调用 stop。 */
  function startPolling(intervalMs = 60000) {
    stopPolling()
    refreshUnread()
    timer = setInterval(refreshUnread, intervalMs)
  }

  function stopPolling() {
    if (timer) {
      clearInterval(timer)
      timer = null
    }
  }

  return { items, unread, loaded, load, refreshUnread, markRead, clear, startPolling, stopPolling }
})
