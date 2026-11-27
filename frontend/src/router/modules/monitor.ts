import { AppRouteRecord } from '@/types/router'

/** 主升浪捕手业务路由 */
export const monitorRoutes: AppRouteRecord = {
  name: 'Monitor',
  path: '/monitor',
  component: '/index/index',
  redirect: '/monitor/overview',
  meta: {
    title: 'menus.monitor.title',
    icon: 'mainwave:menu',
    roles: ['R_SUPER']
  },
  children: [
    {
      path: 'overview',
      name: 'MonitorOverview',
      component: '/monitor/overview',
      meta: {
        title: 'menus.monitor.overview',
        icon: 'ri:dashboard-line',
        fixedTab: true
      }
    },
    {
      path: 'watchlist',
      name: 'Watchlist',
      component: '/monitor/watchlist',
      meta: {
        title: 'menus.monitor.watchlist',
        icon: 'ri:star-line'
      }
    },
    {
      path: 'opportunity',
      name: 'MarketOpportunity',
      component: '/monitor/opportunity',
      meta: {
        title: 'menus.monitor.opportunity',
        icon: 'ri:fire-line'
      }
    },
    {
      path: 'yesterday',
      name: 'YesterdayFollow',
      component: '/monitor/yesterday',
      meta: {
        title: 'menus.monitor.yesterday',
        icon: 'ri:bar-chart-box-line'
      }
    },
    {
      path: 'sell',
      name: 'SellRiskControl',
      component: '/monitor/sell',
      meta: {
        title: 'menus.monitor.sell',
        icon: 'ri:shield-line'
      }
    },
    {
      path: 'strategies',
      name: 'WaveStrategies',
      component: '/monitor/strategies',
      meta: {
        title: 'menus.monitor.strategies',
        icon: 'ri:lightbulb-line'
      }
    },
    {
      path: 'lanban',
      name: 'LanbanRelay',
      component: '/monitor/lanban',
      meta: {
        title: 'menus.monitor.lanban',
        icon: 'ri:rocket-line'
      }
    },
    {
      path: 'settings',
      name: 'MonitorSettings',
      component: '/monitor/settings',
      meta: {
        title: 'menus.monitor.settings',
        icon: 'ri:settings-3-line'
      }
    }
  ]
}
