/**
 * 快速入口配置
 *
 * 主升浪捕手仅保留盯盘业务，原模板的示例应用与快捷链接已清空。
 */
import type { FastEnterConfig } from '@/types/config'

const fastEnterConfig: FastEnterConfig = {
  // 显示条件（屏幕宽度）
  minWidth: 1200,
  // 应用列表：无模板示例应用
  applications: [],
  // 快速链接：无模板示例链接
  quickLinks: []
}

export default Object.freeze(fastEnterConfig)
