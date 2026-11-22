/**
 * 本地缓存清理工具
 *
 * 主升浪捕手是从 Art Design Pro 模板精简而来。模板阶段浏览器 localStorage
 * 里可能残留大量旧版本的 store 数据、失效的工作标签页（worktab）与搜索历史，
 * 表现为「很多不是当前项目的菜单/页面还在」。本模块负责一次性清理这些残留，
 * 保证用户看到的是干净、只属于盯盘工具的界面。
 */
import { StorageConfig } from '@/utils/storage/storage-config'

/** 一次性清理标记：给旧缓存打上标记后不再重复清理 */
const CLEAN_FLAG = 'sys-mainwave-cleaned'

/** 当前项目允许的工作标签页路径前缀 */
const ALLOWED_TAB_PREFIX = '/monitor'

interface CleanResult {
  cleaned: boolean
  removedKeys: number
  removedTabs: number
}

/**
 * 清理模板遗留缓存。
 *
 * 通过 localStorage 标记保证只执行一次；配置页的「清理缓存」按钮可强制再执行。
 */
export function cleanStaleCache(force = false): CleanResult {
  const result: CleanResult = { cleaned: false, removedKeys: 0, removedTabs: 0 }
  try {
    if (!force && localStorage.getItem(CLEAN_FLAG) === '1') {
      return result
    }

    // 1) 删除非当前版本的 sys-v* 存储键（旧版本遗留的 user/setting/worktab/table）
    const keys = Object.keys(localStorage)
    keys.forEach((key) => {
      if (
        key.startsWith(StorageConfig.STORAGE_PREFIX) &&
        !StorageConfig.isCurrentVersionKey(key)
      ) {
        localStorage.removeItem(key)
        result.removedKeys += 1
      }
    })

    // 2) 过滤工作标签页：仅保留当前盯盘路由
    const worktabKey = StorageConfig.generateStorageKey('worktab')
    const worktabRaw = localStorage.getItem(worktabKey)
    if (worktabRaw) {
      try {
        const parsed = JSON.parse(worktabRaw)
        const opened = Array.isArray(parsed.opened) ? parsed.opened : []
        const kept = opened.filter((tab: any) =>
          typeof tab?.path === 'string' && tab.path.startsWith(ALLOWED_TAB_PREFIX)
        )
        result.removedTabs = opened.length - kept.length
        parsed.opened = kept
        if (kept.length === 0) {
          parsed.current = {}
        } else if (
          !kept.some((tab: any) => tab.path === parsed.current?.path)
        ) {
          parsed.current = kept[0]
        }
        localStorage.setItem(worktabKey, JSON.stringify(parsed))
      } catch {
        localStorage.removeItem(worktabKey)
      }
    }

    // 3) 清空搜索历史（其中引用的是模板示例页面）
    const userKey = StorageConfig.generateStorageKey('user')
    const userRaw = localStorage.getItem(userKey)
    if (userRaw) {
      try {
        const user = JSON.parse(userRaw)
        if (Array.isArray(user.searchHistory)) {
          user.searchHistory = []
          localStorage.setItem(userKey, JSON.stringify(user))
        }
      } catch {
        /* 忽略解析失败 */
      }
    }

    result.cleaned = true
    localStorage.setItem(CLEAN_FLAG, '1')
    console.info(
      `[CacheCleaner] 已清理模板遗留缓存：移除 ${result.removedKeys} 个旧键、${result.removedTabs} 个失效标签页`
    )
  } catch (error) {
    console.warn('[CacheCleaner] 清理缓存失败', error)
  }
  return result
}
