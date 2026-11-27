/**
 * 主升浪捕手 API 封装
 * 统一走模板的 request（含 /api 前缀代理，自动解包 data）
 */
import request from '@/utils/http'

export interface WaveSignal {
  code: string
  name: string
  strategy: string
  direction: 'up' | 'down'
  stage?: string
  stage_label?: string
  date: string
  price: number
  change_pct: number
  reasons: string[]
  signal: string
  strength: number
  type: string
}

export interface MarketStatus {
  status: string
  label: string
  date: string
  time: string
  timezone: string
}

/** 全市场扫描信号 */
export interface MarketSignal {
  code: string
  name: string
  strategy: string
  direction: 'up' | 'down'
  stage: string
  stage_label: string
  date: string
  price: number
  change_pct: number
  volume_ratio: number | null
  turnover: number | null
  net_inflow: number | null
  inflow: number | null
  outflow: number | null
  reasons: string[]
  signal: string
  strength: number
}

/** 站内通知 */
/** 昨日涨停跟进 */
export interface YesterdayRow {
  code: string
  name: string
  industry: string
  market_board: string
  prev_streak: number
  category: string
  note: string
  price: number | null
  change_pct: number | null
  open_pct: number | null
  high_pct: number | null
  low_pct: number | null
  volume_ratio: number | null
  turnover: number | null
  amount: number | null
}

/** 持仓卖出信号 */
export interface SellSignal {
  code: string
  name: string
  strategy: string
  level: '预警' | '确认'
  price: number
  change_pct: number
  volume_ratio: number
  reasons: string[]
  date: string
  time: string
}

export interface NoticeItem {
  id: string
  time: string
  title: string
  body: string
  kind: string
  read: boolean
  signals: MarketSignal[]
}

export interface WatchlistSignal {
  strategy: string
  direction: 'up' | 'down'
  strength: number
}

export interface WatchlistRow {
  code: string
  name: string
  price: number | null
  change_pct: number | null
  turnover: number | null
  volume_ratio: number | null
  date: string
  signals: WatchlistSignal[]
  up_count: number
  down_count: number
}

export const monitorApi = {
  health: () => request.get({ url: '/api/health' }),
  marketStatus: () => request.get<MarketStatus>({ url: '/api/market/status' }),

  /** 主升浪总览 */
  mainwaveOverview: () => request.get<any>({ url: '/api/mainwave/overview' }),

  /** 扫描标的池主升浪信号（refresh=true 强制刷新K线缓存） */
  mainwaveScan: (refresh = false) =>
    request.get<{ signals: WaveSignal[]; scanned: number; time: string }>({
      url: '/api/mainwave/scan',
      params: { refresh }
    }),

  /** 单只股票战法详情 */
  mainwaveStock: (code: string) =>
    request.get<{ code: string; signals: WaveSignal[] }>({ url: `/api/mainwave/stock/${code}` }),

  /** 个股日K线 + MA5/10/20 */
  stockKline: (code: string, days = 120) =>
    request.get<{ code: string; name: string; rows: any[] }>({
      url: `/api/stock/kline/${code}`,
      params: { days }
    }),

  /** 个股日内分钟K线 */
  stockIntraday: (code: string, period = '5') =>
    request.get<{ code: string; period: string; date: string | null; source: string; rows: any[] }>({
      url: `/api/stock/intraday/${code}`,
      params: { period }
    }),

  /** 战法清单与开关 */
  strategies: () => request.get<any[]>({ url: '/api/mainwave/strategies' }),

  /** 标的池（自选股式管理） */
  watchlist: () => request.get<{ rows: WatchlistRow[]; total: number; updated_at: string }>({
    url: '/api/watchlist'
  }),
  watchlistSearch: (q: string, limit = 12) =>
    request.get<Array<{ code: string; name: string }>>({
      url: '/api/watchlist/search',
      params: { q, limit }
    }),
  watchlistAdd: (code: string) =>
    request.post<any>({ url: '/api/watchlist', data: { code } }),
  watchlistCodes: () => request.get<{ codes: string[] }>({ url: '/api/watchlist/codes' }),
  watchlistAddBatch: (codes: string[]) =>
    request.post<{ codes: string[]; added: string[]; skipped: string[]; invalid: string[] }>({
      url: '/api/watchlist/batch',
      data: { codes }
    }),
  watchlistRemove: (code: string) =>
    request.del<any>({ url: `/api/watchlist/${code}` }),
  watchlistReorder: (codes: string[]) =>
    request.put<any>({ url: '/api/watchlist/order', data: { codes } }),

  /** 配置 */
  getConfig: () => request.get<any>({ url: '/api/config' }),
  putConfig: (data: any) => request.put<any>({ url: '/api/config', data }),
  resetConfig: () => request.post<any>({ url: '/api/config/reset' }),

  /** 邮件测试 */
  notifyTest: (channelId?: string) =>
    request.post<any>({ url: '/api/notify/test', data: { channel_id: channelId } }),

  /** 手动扫描并推送邮件 */
  scan: () => request.post<any>({ url: '/api/monitor/scan' }),

  /** 自动盯盘开关 */
  status: () => request.get<any>({ url: '/api/monitor/status' }),
  start: () => request.post<any>({ url: '/api/monitor/start' }),
  stop: () => request.post<any>({ url: '/api/monitor/stop' }),

  /** 告警历史 */
  history: (limit = 50) => request.get<any[]>({ url: '/api/monitor/history', params: { limit } }),
  clearHistory: () => request.del<any>({ url: '/api/monitor/history' }),

  /** 全市场扫描与双时点推送 */
  marketScan: (push = false) =>
    request.post<any>({ url: `/api/market/scan?push=${push}` }),
  marketSignals: () =>
    request.get<{ snapshot: any; signals: MarketSignal[]; last?: { saved_at?: string; source?: string } | null }>({ url: '/api/market/signals' }),
  marketScanStatus: () => request.get<any>({ url: '/api/market/scan-status' }),
  marketReplay: (date: string) =>
    request.get<any>({ url: `/api/market/replay?date=${date}`, timeout: 120000 }),
  marketReplayDates: () =>
    request.get<any>({ url: '/api/market/replay/dates', timeout: 120000 }),
  marketReplayRefresh: () =>
    request.post<any>({ url: '/api/market/replay/refresh' }),
  marketPushNow: () => request.post<any>({ url: '/api/market/push-now' }),

  /** 昨日涨停跟进 */
  yesterdaySnapshot: () => request.get<{
    snapshot: any; rows: YesterdayRow[]; times: string[]; last?: { saved_at?: string; source?: string } | null
  }>({ url: '/api/yesterday/snapshot' }),
  yesterdayScan: () => request.post<any>({ url: '/api/yesterday/scan' }),
  tdxLatest: () => request.get<{ date: string; window?: number; signals: Record<string, { signal: string; days_ago: number }[]> }>({ url: '/api/tdx/latest' }),
  positionsPlan: () => request.get<{ plans: any[]; time: string }>({ url: '/api/positions/plan', timeout: 60000 }),
  yesterdayPush: () => request.post<any>({ url: '/api/yesterday/push-now' }),

  /** 持有标记 */
  holdings: () => request.get<{ rows: any[]; codes: string[]; costs: Record<string, number> }>({ url: '/api/holdings' }),
  holdingsAdd: (code: string, cost?: number) => request.post<any>({ url: '/api/holdings', data: { code, cost } }),
  holdingsCost: (code: string, cost: number | null) => request.put<any>({ url: `/api/holdings/${code}/cost`, data: { cost } }),
  holdingsRemove: (code: string) => request.del<any>({ url: `/api/holdings/${code}` }),

  /** 持仓卖出风控 */
  sellStatus: () => request.get<any>({ url: '/api/sell/status' }),
  sellSignals: () => request.get<{ signals: SellSignal[] }>({ url: '/api/sell/signals' }),
  sellScan: (confirm = false) => request.post<any>({ url: `/api/sell/scan?confirm=${confirm}` }),
  sellPush: (confirm = false) => request.post<any>({ url: `/api/sell/push-now?confirm=${confirm}` }),

  /** 站内通知 */
  notices: (limit = 100) =>
    request.get<{ items: NoticeItem[]; unread: number; total: number }>({
      url: '/api/notifications', params: { limit }
    }),
  noticeUnread: () => request.get<{ count: number }>({ url: '/api/notifications/unread' }),
  noticeRead: (id?: string) =>
    request.post<any>({ url: `/api/notifications/read${id ? `?notify_id=${id}` : ''}` }),
  noticeClear: () => request.del<any>({ url: '/api/notifications' })
}
