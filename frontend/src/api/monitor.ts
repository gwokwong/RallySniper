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

/** 信号验证：单条实盘信号记录 */
export interface SignalOutcomeRecord {
  id: string
  tdate: string
  code: string
  name: string
  strategy: string
  direction: 'up' | 'down'
  stage_label: string
  entry_price: number | null
  change_pct: number | null
  reasons: string[]
  source: string
  recorded_at: string
  pushed_at: string[]
  outcome: {
    status: 'pending' | 'resolved' | 'no_data'
    r_t1?: number
    r_t5?: number
    r_t10?: number
    win_t1?: boolean
    win_t5?: boolean
    win_t10?: boolean
    mfe_t10?: number
    mae_t10?: number
  }
}

/** 信号验证：统计汇总 */
export interface HorizonStat {
  n: number
  win_rate?: number
  avg?: number
  med?: number
  sum_wan?: number
}

export interface SignalOutcomeSummary {
  total: number
  resolved: number
  pending: number
  no_data: number
  horizons: { t1: HorizonStat; t5: HorizonStat; t10: HorizonStat }
  by_direction: { up: Record<string, HorizonStat>; down: Record<string, HorizonStat> }
  by_strategy: Array<{
    strategy: string
    direction: 'up' | 'down'
    total: number
    resolved: number
    t1: HorizonStat
    t5: HorizonStat
    t10: HorizonStat
  }>
}

/** ETF 池内标的（含纸面持仓状态） */
export interface EtfPoolItem {
  code: string
  name: string
  price: number | null
  date: string | null
  in_position: boolean
  entry_date?: string
  entry_price?: number | null
  life_line?: number | null
  pnl_pct?: number | null
  flags?: {
    squeeze?: boolean
    freeze?: boolean
    in_pos?: boolean
    z20?: number
    r5?: number
    vr?: number
  }
}

/** ETF 今日扫描行（含 watch 级：冰点/收缩观察——不进验证闭环） */
export interface EtfScanSignal {
  code: string
  name: string
  strategy: string
  stage: 'trigger' | 'watch'
  stage_label: string
  direction: 'up' | 'down'
  price: number
  change_pct: number
  reasons: string[]
}

export interface EtfScanItem {
  code: string
  name: string
  price: number
  chg: number
  date: string
  flags: { squeeze: boolean; freeze: boolean; in_pos: boolean }
  readings: Record<string, any>
  signals: EtfScanSignal[]
}

/** ETF trigger 信号（含验证结果） */
export interface EtfSignal {
  id: string
  tdate: string
  code: string
  name: string
  strategy: string
  direction: 'up' | 'down'
  entry_price: number | null
  reasons: string[]
  source: string
  outcome: {
    status: 'pending' | 'resolved' | 'no_data'
    r_t1?: number
    r_t5?: number
    r_t10?: number
  }
}

/** 全市场 ETF 搜索/热榜行 */
export interface EtfSearchItem {
  code: string
  name: string
  price: number | null
  pct: number | null
  volume: number | null
  turnover?: number | null
}

export interface EtfStatus {
  enabled: boolean
  pool: EtfPoolItem[]
  last_scan: any
  cache_n: number
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
  lanbanCandidates: () => request.get<{ date: string; n: number; candidates: any[] }>({ url: '/api/lanban/candidates' }),
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
  noticeClear: () => request.del<any>({ url: '/api/notifications' }),

  /** 信号验证闭环（实盘信号 T+1/5/10 结果回填） */
  signalOutcomesSummary: () => request.get<SignalOutcomeSummary>({ url: '/api/signaloutcomes/summary' }),
  signalOutcomesSignals: (status = 'all', limit = 100, strategy?: string) =>
    request.get<{ items: SignalOutcomeRecord[] }>({
      url: '/api/signaloutcomes/signals',
      params: { status, limit, strategy }
    }),
  signalOutcomesRun: () => request.post<SignalOutcomeSummary & { last_run: any }>({ url: '/api/signaloutcomes/run' }),

  /** ETF 监控（S4 波动收缩突破，T+1 收盘确认口径） */
  etfStatus: () => request.get<EtfStatus>({ url: '/api/etf/status' }),
  etfSignals: (limit = 50) => request.get<{ items: EtfSignal[] }>({ url: '/api/etf/signals', params: { limit } }),
  etfSearch: (q: string, limit = 30) => request.get<{ items: EtfSearchItem[] }>({ url: '/api/etf/search', params: { q, limit } }),
  etfHotlist: (limit = 15) => request.get<{ pct: EtfSearchItem[]; volume: EtfSearchItem[]; n: number }>({ url: '/api/etf/hotlist', params: { limit } }),
  etfPoolAdd: (code: string, name = '') => request.post<any>({ url: '/api/etf/pool', data: { code, name } }),
  etfPoolRemove: (code: string) => request.del<any>({ url: `/api/etf/pool/${code}` }),
  etfHoldingsToggle: (code: string) => request.post<any>({ url: `/api/etf/holdings/${code}` }),
  etfScan: () => request.post<any>({ url: '/api/etf/scan' }),
  etfRefresh: () => request.post<any>({ url: '/api/etf/refresh' })
}
