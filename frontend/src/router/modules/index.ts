import { AppRouteRecord } from '@/types/router'
import { monitorRoutes } from './monitor'

/**
 * 导出所有模块化路由
 *
 * 主升浪捕手仅保留盯盘业务菜单，所有模板示例与帮助路由已移除。
 */
export const routeModules: AppRouteRecord[] = [monitorRoutes]
