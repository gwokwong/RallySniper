"""应用配置与持久化。

系统定位：主升浪捕手 —— 标的池股票命中主升浪战法即发邮件提醒。
通知渠道仅保留 SMTP 邮件，其余渠道已移除。

配置项（标的池、战法开关与阈值、邮件、扫描节奏）保存在
backend/data/config.json，支持运行时通过 API 修改并立即生效。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
CONFIG_FILE = DATA_DIR / "config.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)


# ----------------------------- 数据模型 -----------------------------

STRATEGY_NAMES = ["量价突破", "三重共振", "四特征共振", "龙出水", "三线底部启动", "涨停回调", "强势续攻", "洗盘反包"]
BEAR_STRATEGY_NAMES = ["双线死叉", "三死叉见顶", "生命线破位", "MACD高位死叉", "量价背离"]


class StrategyRules(BaseModel):
    """战法量化阈值（可通过界面调整）。"""

    volume_ratio: float = Field(default=1.5, ge=1.0, le=5, description="放量倍数阈值")
    resonance_window: int = Field(default=3, ge=1, le=10, description="三重共振同步窗口(交易日)")
    feature_window: int = Field(default=20, ge=5, le=60, description="四特征回看窗口(交易日)")
    box_days: int = Field(default=40, ge=20, le=120, description="龙出水横盘天数")
    box_max_range: float = Field(default=0.35, ge=0.05, le=1.0, description="龙出水箱体最大振幅")
    ma_spread: float = Field(default=0.10, ge=0.01, le=0.5, description="均线粘合离散度")
    box_break_vol: float = Field(default=2.0, ge=1.0, le=5, description="龙出水突破放量倍数")
    cross_window: int = Field(default=5, ge=1, le=15, description="三线金叉回看窗口")
    # 涨停回调（低吸）
    limitup_lookback: int = Field(default=20, ge=5, le=60, description="涨停回看窗口(交易日，含连板)")
    pullback_max_days: int = Field(default=8, ge=2, le=20, description="涨停后最长回调天数")
    pullback_shrink_days: int = Field(default=3, ge=2, le=10, description="回调缩量观察天数")
    pullback_shrink_ratio: float = Field(default=0.7, ge=0.2, le=1.0, description="回调均量/涨停日量上限")
    pullback_vol_ratio: float = Field(default=1.5, ge=1.0, le=5, description="放量启动量比阈值")
    # 强势续攻（涨停后缩量整理/直接续板，不必跌破涨停收盘价）
    strong_max_days: int = Field(default=10, ge=2, le=20, description="强势续攻涨停后观察天数")
    strong_vol_ratio: float = Field(default=1.2, ge=1.0, le=5, description="缩量转折放量阈值(默认1.2,抓缩量后首波启动)")
    strong_shrink_ratio: float = Field(default=1.0, ge=0.3, le=1.5, description="转折前一日量比上限(缩量确认)")
    strong_accel_ratio: float = Field(default=2.0, ge=1.0, le=8, description="连续板直接加速放量阈值")
    strong_max_vol_ratio: float = Field(default=3.0, ge=0.0, le=20.0,
                                         description="强势续攻量比上限(0=不限)。10月回测:量比<3时大亏-43%、妖股保留85.7%，"
                                                     "monster模式强制开启强势续攻，默认3.0保护追高")
    # 洗盘反包（大跌洗盘后首板反包，抓连板股启动初期）
    wash_lookback: int = Field(default=8, ge=3, le=15, description="洗盘反包回看窗口(交易日)")
    wash_drop_pct: float = Field(default=7.0, ge=3.0, le=15.0, description="洗盘大跌门槛(跌幅百分比,默认7%)")
    # 下跌战法
    death_cross_window: int = Field(default=5, ge=1, le=15, description="死叉回看窗口(交易日)")
    death_high_pct: float = Field(default=0.5, ge=0, le=1, description="双线死叉相对高位分位门槛")
    triple_death_window: int = Field(default=5, ge=1, le=10, description="三死叉同步窗口(交易日)")
    break_vol: float = Field(default=1.5, ge=1.0, le=5, description="破位放量增强倍数")
    divergence_window: int = Field(default=30, ge=10, le=90, description="背离/新高回看窗口(交易日)")


class StrategySwitches(BaseModel):
    """上涨战法开关。

    默认按 2026-11-21 三个月全市场回测冠军方案：洗盘反包单战法 +
    情绪点火闸门（胜率 64.5%、平均 +5.36%，基线 22.8%/-3.92%）。
    涨停回调（34笔/胜率8.8%/妖股0）与强势续攻（胜率17.6%，仅强市
    &量比<3 下可用）默认关闭，可在配置页随时开启。
    """
    量价突破: bool = False
    三重共振: bool = False
    四特征共振: bool = False
    龙出水: bool = False
    三线底部启动: bool = False
    涨停回调: bool = False
    强势续攻: bool = False
    洗盘反包: bool = True

    def as_dict(self) -> dict[str, bool]:
        return {name: getattr(self, name) for name in STRATEGY_NAMES}


class BearStrategySwitches(BaseModel):
    """下跌/离场战法开关。默认只保留双线死叉与生命线破位两个核心危险信号。"""
    双线死叉: bool = True
    三死叉见顶: bool = False
    生命线破位: bool = True
    MACD高位死叉: bool = False
    量价背离: bool = False

    def as_dict(self) -> dict[str, bool]:
        return {name: getattr(self, name) for name in BEAR_STRATEGY_NAMES}


class AlertRules(BaseModel):
    """保留的通用异动阈值（页面仍展示，但不用于主升浪推送）。"""

    price_change_pct: float = Field(default=4.0, ge=0.1, le=20, description="个股日内涨跌幅阈值(%)")
    volume_ratio: float = Field(default=2.0, ge=1.0, le=10, description="放量倍数阈值")
    fund_flow_amount: float = Field(default=50_000_000, ge=0, description="主力资金净额阈值(元)")
    sector_change_pct: float = Field(default=3.0, ge=0.1, le=15, description="板块涨跌幅阈值(%)")
    lhb_net_buy: float = Field(default=20_000_000, ge=0, description="龙虎榜净买入阈值(元)")
    watchlist_only: bool = Field(default=True, description="异动是否仅针对标的池")


class NotifierConfig(BaseModel):
    """邮件通知渠道配置（唯一支持的渠道）。"""

    id: str = "email"
    type: Literal["email"] = "email"
    name: str = "邮件通知"
    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    mail_from: str = ""
    mail_to: str = ""


class ScanConfig(BaseModel):
    """扫描节奏配置。"""

    enabled: bool = Field(default=True, description="是否开启后台自动盯盘（默认开启）")
    trading_interval: int = Field(default=300, ge=30, description="盘中扫描间隔(秒)")
    lunch_interval: int = Field(default=600, ge=30, description="午休扫描间隔(秒)")
    closed_interval: int = Field(default=1800, ge=60, description="收盘后扫描间隔(秒)")
    only_trading_days: bool = Field(default=True, description="仅交易日扫描")
    only_trading_hours: bool = Field(default=True,
                                     description="仅盘中推送标的池信号（9:30-11:30/13:00-15:00）。"
                                                 "关闭后凌晨/周末也会拿最近交易日数据扫描推送——"
                                                 "2026-11-23 凌晨误推【主升浪提醒】的根因，默认开")
    cooldown_seconds: int = Field(default=86400, ge=0,
                                  description="同一股票同一战法告警冷却时间(秒)，默认24小时避免重复提醒")
    market_push_times: list[str] = Field(
        default=["14:00", "14:40"],
        description="全市场扫描推送/买进时点(HH:MM 列表，支持全天任意交易时段)。"
                    "默认 14:00/14:40 尾盘双时点；改动后由 scheduler.refresh 立即重排，无需重启",
    )
    kline_refresh_time: str = Field(
        default="15:10",
        description="收盘后自动刷新全市场K线缓存的时点(HH:MM，回放功能数据源；休市日自动跳过)",
    )
    daily_check_time: str = Field(
        default="09:20",
        description="每日系统自检邮件时点(HH:MM)：检查数据源/K线缓存/调度任务/邮件渠道/持仓并邮件汇报",
    )
    wzq_watch_enabled: bool = Field(
        default=True,
        description="弱转强观察池：收盘后筛当日巨量炸板（触板未封+收盘离板≥1.5%+量比≥2.5），"
                    "纯观察提醒供次日竞价人工判断，不构成买卖信号",
    )
    wzq_watch_time: str = Field(
        default="15:20",
        description="弱转强观察池扫描推送时点(HH:MM，收盘后行情快照口径；休市日跳过)",
    )
    pattern_watch_enabled: bool = Field(
        default=True,
        description="盘中形态监控：全市场「洗盘反包·进行中」与「N日新高突破」实时提醒（每票每日每形态一次）",
    )
    pattern_poll_minutes: int = Field(default=3, ge=1, le=15,
                                      description="形态监控轮询间隔(分钟，仅盘中)")
    pattern_break_high_days: int = Field(default=20, ge=5, le=120,
                                         description="新高突破的N日窗口")
    pattern_break_vr: float = Field(default=1.5, ge=1.0, le=10,
                                    description="新高突破的量比门槛")
    pattern_max_alerts: int = Field(default=20, ge=1, le=100,
                                    description="每日形态提醒总量上限（防刷屏）")
    pattern_board_alert: bool = Field(default=True,
                                      description="板块异动提醒：行业聚合涨跌达到阈值时提醒拉涨/砸盘（每板块每日各一次）")
    pattern_board_anomaly_pct: float = Field(default=3.0, ge=1.0, le=10,
                                             description="板块异动阈值(%)：行业聚合涨跌中位达到±此值判定异动")


class SellConfig(BaseModel):
    """持仓卖出风控：天量破位 + 20日线确认破位，盘中预警 + 尾盘确认。"""

    enabled: bool = Field(default=True, description="是否开启持仓卖出提醒")
    volume_surge_ratio: float = Field(default=4.5, ge=1.0, le=20,
                                      description="天量破位量比阈值（盘中用行情源量比，默认4.5）")
    ma20_buffer: float = Field(default=0.01, ge=0, le=0.1,
                               description="20日线有效跌破缓冲比例")
    ma20_slope: float = Field(default=-0.005, ge=-0.05, le=0.05,
                              description="MA20 五日斜率门槛，向下才算破位")
    fast_break_enabled: bool = Field(default=True, description="是否启用快速破位确认通道")
    fast_break_pct: float = Field(default=0.02, ge=0.005, le=0.1,
                                  description="快速破位：单日收盘低于MA20的比例(默认2%)")
    # 双通道：盘中急跌预警 + 尾盘破MA10确认（针对连板急跌，MA20 严重滞后的问题）
    rapid_drop_enabled: bool = Field(default=True, description="是否启用盘中急跌预警通道")
    rapid_drop_pct: float = Field(default=0.08, ge=0.02, le=0.2,
                                  description="盘中急跌预警：当日跌幅达此比例即预警(默认8%)")
    ma10_break_enabled: bool = Field(default=True, description="是否启用尾盘破MA10确认通道")
    ma10_break_drop_pct: float = Field(default=0.05, ge=0.01, le=0.2,
                                       description="破MA10确认：当日跌幅达此比例(默认5%)")
    # 缩量跌停确认：放量急跌多为洗盘，缩量跌停才是趋势末端。盘中急跌预警仍照发，
    # 但尾盘「急跌破位」确认要求破MA10 或 缩量跌停，避免强势票洗盘被反复确认轰炸。
    shrink_limit_ratio: float = Field(default=0.5, ge=0.1, le=3.0,
                                      description="缩量跌停量比上限(默认0.5)，量比≤此值且跌幅达标才算缩量跌停")
    shrink_limit_drop: float = Field(default=0.095, ge=0.05, le=0.2,
                                     description="缩量跌停跌幅门槛(默认9.5%)")
    ma10_consec_enabled: bool = Field(default=True,
                                      description="MA10连续破位卖出：连续N天收盘跌破MA10即确认卖出（第4通道）")
    ma10_consec_days: int = Field(default=2, ge=2, le=10,
                                  description="MA10连续破位天数：连续N天收盘<MA10即触发")
    touch_ma10_exit_enabled: bool = Field(default=True,
                                          description="30日内首板后第4次回踩MA10预警卖出（第5通道）：涨停后每次回踩到MA10附近均视为一次考验，第4次说明主力反复护盘已到极限")
    touch_ma10_count: int = Field(default=4, ge=2, le=10,
                                  description="回踩MA10次数阈值：达到此次数即预警卖出")
    touch_ma10_window: int = Field(default=30, ge=10, le=60,
                                   description="首板回看窗口(交易日)：从最近30日内首个涨停日起算")
    touch_strong_reset_enabled: bool = Field(default=False,
                                             description="第5通道回踩计数从最近一次强势日(大阳线)起算：洗盘反包的入场日即反包大阳，等价于从买入日起算，避免入场前洗盘段的回踩把计数用满")
    touch_strong_pct: float = Field(default=5.0, ge=3.0, le=15.0,
                                    description="强势日判定：当日涨幅≥此值(%)视为强势确认日，回踩计数从其次日重新开始")
    cooldown_enabled: bool = Field(default=True, description="是否启用确认冷却期(同一波下跌不重复发确认邮件)")
    cooldown_reset_pct: float = Field(default=0.03, ge=0.005, le=0.2,
                                      description="冷却重置：股价较上次确认价上涨超此比例视为创新高，允许新一轮确认")
    intraday_interval_minutes: int = Field(default=5, ge=1, le=30,
                                           description="盘中预警扫描间隔(分钟)")
    confirm_time: str = Field(default="14:50", description="尾盘确认时点(HH:MM)")
    notify_times: int = Field(default=2, ge=1, le=4,
                              description="同一破位交易日尾盘确认后追加提醒次数(至少2次通知)")
    # 浮盈分档止损：2026-11-21 回测显示持有>16天胜率48.8%、>30天100%，
    # 一律8%急跌砍仓会误伤主升浪。浮盈垫厚的票放宽阈值，让利润奔跑。
    tier_enabled: bool = Field(default=False,
                               description="是否启用浮盈分档止损（8-10月下跌市回测未证明有效，默认关闭）")
    tier_profit_pct: float = Field(default=0.30, ge=0.05, le=0.5,
                                   description="浮盈达标比例(默认30%)，超过则放宽止损")
    tier_rapid_pct: float = Field(default=0.12, ge=0.08, le=0.3,
                                  description="厚利档急跌预警阈值(默认12%，基准档8%)")
    tier_shrink_drop: float = Field(default=0.115, ge=0.08, le=0.25,
                                    description="厚利档缩量跌停确认门槛(默认11.5%，基准档9.5%)")
    tier_ma10_trail: bool = Field(default=True,
                                  description="厚利档启用10日线移动止盈：跌破MA10且收阴即确认，不等急跌")


class FundamentalFilterConfig(BaseModel):
    """基本面筛选（2026-11-25 研究落地）：全市场信号关联业绩报表后按营收增速过滤。

    7-11月回测：营收同比>10% 的信号 117 笔胜率 53.0%、平均 +2.73%、总营收 +31,905 元，
    优于全部信号（276 笔 46.0%、+0.99%、+27,189 元）——少一半交易反而多赚钱。
    注意：7 月曾反向（31% vs 53%），因子中等强度非铁律；业绩缺失（未披露/新股）默认保留。
    """

    enabled: bool = Field(default=True, description="是否启用基本面筛选（营收增速）")
    min_rev_yoy: float = Field(default=10.0, ge=-100, le=1000,
                               description="最低营收同比增长(%)：低于此值的信号被过滤；业绩缺失的保留并标注")
    pick_max_float_cap: float = Field(default=150.0, ge=0, le=10000,
                                      description="精选候选流通市值上限(亿元)，0=不限。资金有限优选中小盘")
    pick_per_industry: int = Field(default=2, ge=1, le=10,
                                   description="精选名单每行业最多几只（龙头+龙二）")




class MarketRegimeConfig(BaseModel):
    """大盘环境过滤：指数在MA20下方或MA20向下时暂停/减半开新仓。

    2026-11-21 回测结论：8/10-8/30 的 508 个买入信号全程处于上证-9.7%的
    下跌市，逆势开仓是最大亏损来源。强市（指数站上MA20且MA20不向下）才
    全力开新仓；弱市暂停或减半，避免信号被系统性下跌带垮。

    情绪点火闸门（2026-11-21 三个月全市场回测新增）：昨日涨停股今日的
    涨幅中位数 ≥ 阈值视为「情绪点火日」。8/9/10 月 2014 笔信号回测：
    基线胜率 22.8%，点火闸门(中位数≥3%)后 44.9%；配合洗盘反包单战法
    64.5%、平均收益 +5.36%（8月弱市洗盘反包胜率高达 60.6%）。阈值
    2.0~4.0 全区间稳健（55%~65.5%），非过拟合孤点。
    """

    enabled: bool = Field(default=True, description="是否启用大盘环境过滤")
    index_code: str = Field(default="sh000001", description="大盘指数代码（腾讯/sina 格式）")
    ma_period: int = Field(default=20, ge=5, le=60, description="大盘均线周期(默认20日)")
    slope_lookback: int = Field(default=5, ge=2, le=20, description="均线斜率回看交易日(默认5日)")
    weak_action: str = Field(default="half", pattern="^(pause|half)$",
                             description="弱市动作：pause=暂停开新仓，half=减半（仅保留主战法）。"
                                         "三个月回测显示弱市里洗盘反包胜率 42.4%(8月弱市 60.6%)，"
                                         "half 优于 pause")
    # 战法权重倾斜：洗盘反包为主战法；强势续攻仅在强市开启
    wash_primary: bool = Field(default=True, description="洗盘反包设为主战法（通知/排序优先）")
    strong_requires_bull: bool = Field(default=True,
                                       description="强势续攻仅在强市开启（弱市自动屏蔽该战法）")
    # 情绪点火闸门：昨日涨停股今日溢价决定今日能否开新仓（妖股模式自动绕过）
    ignition_enabled: bool = Field(default=True,
                                   description="是否启用情绪点火闸门：昨日涨停股今日涨幅中位数低于阈值时暂停开新仓")
    ignition_threshold: float = Field(default=3.0, ge=0.0, le=10.0,
                                      description="点火阈值(%)：昨日涨停股今日涨幅中位数≥此值视为点火日，"
                                                  "三个月回测 3.0 最优(2.0~4.0 均稳健)")
    # 点火盘中监控：确认点火立即提醒（稳妥版：连续确认+退潮预警+一日两条封顶）
    ignition_watch_enabled: bool = Field(default=True,
                                         description="盘中点火监控：连续 ignition_sustain_checks 次达标即提醒点火，跌破0提醒退潮")
    ignition_watch_minutes: int = Field(default=5, ge=1, le=30,
                                        description="点火监控检查间隔(分钟)，默认5分钟")
    ignition_sustain_checks: int = Field(default=3, ge=1, le=10,
                                         description="确认点火需要的连续达标次数(默认3次=15分钟持续)，过滤开盘冲高假点火")
    ignition_min_pool: int = Field(default=10, ge=1, le=100,
                                   description="点火监控最小样本池(昨日涨停家数)，过小不可信则跳过")
    ignition_auto_scan: bool = Field(default=False,
                                     description="点火确认后是否立即执行一次全市场扫描推送（默认关，仅提醒；稳妥起见信号仍按推送时点出）")
    ignition_min_breadth: int = Field(default=0, ge=0, le=1000,
                                      description="点火日广度确认：当日实时涨停家数≥此值才开仓（0=关）。"
                                                  "回测：按阶段拆分，点火∧高潮(家数≥100)信号胜率82.7%/+8.3万，"
                                                  "点火∧修复(家数不足)仅14.6%/-5.1万；默认关，建议12月前瞻验证后开启")
    ignition_tier_breadth: int = Field(default=100, ge=1, le=1000,
                                       description="全仓档广度阈值：点火日当日涨停家数≥此值为全仓档，低于为试错档"
                                                   "(建议1/4仓位)。纯提示不拦截——回测全仓档胜率82.7%、试错档19.8%，"
                                                   "阶梯仓位样本内营收+7.1万 vs 全等仓+3.4万，资金敞口降43%")


class MarketFilterConfig(BaseModel):
    """全市场涨停回调扫描的板块与价格筛选。"""

    include_st: bool = Field(default=False, description="是否纳入 ST / *ST 股票")
    main_board: bool = Field(default=True, description="主板（60/00 开头）")
    kcb: bool = Field(default=True, description="科创板（688/689 开头）")
    cyb: bool = Field(default=True, description="创业板（300/301 开头）")
    bj: bool = Field(default=False, description="北交所（43/83/87/92 开头）")
    min_price: float = Field(default=0.0, ge=0, description="最低股价(元)，0 表示不限")
    max_price: float = Field(default=0.0, ge=0, description="最高股价(元)，0 表示不限")
    daily_signal_cap: int = Field(default=0, ge=0, le=200,
                                  description="每次扫描最多推送的信号数(0=不限)。回测:点火日信号可多达71只集中爆发"
                                              "(单只1万需71万资金)，按资金量设10~20可控制占用；随机等权抽签，"
                                              "强度排序已被回测证伪(高分=追高)不采用")
    a_kill_skip_enabled: bool = Field(default=False,
                                      description="A杀过滤：多日连跌(≥5天且累计≥15%)触底后N个交易日内的股票不产生买入信号。"
                                                  "7-11月回测证伪：被过滤的56笔反而更强(胜率57.1%/+3.09% vs 其余49.3%/+0.77%)——"
                                                  "深跌后的反包涨停是超跌深度洗盘形态，质量更高；默认关闭供实盘对照")
    a_kill_days: int = Field(default=3, ge=0, le=10,
                             description="触底后不碰交易日数：A杀触底日及随后N-1个交易日内不碰(默认3)")
    a_kill_down_days: int = Field(default=5, ge=2, le=20,
                                  description="A杀判定：连续下跌交易日数≥此值(默认5天连跌)")
    a_kill_cum_drop: float = Field(default=15.0, ge=5.0, le=50.0,
                                   description="A杀判定：连跌段累计跌幅≥此值(%)，从段前收盘到段底(默认15%)")
    vp_combo_filter_enabled: bool = Field(default=True,
                                          description="换手率×量比四象限过滤：剔除C1（换手>5%且量比2~3的温和放量反包）。"
                                                      "7-11月回测：C1入场21笔胜率19%/均-2.76%，为全样本最差象限（经典口径反向）；剔除后总收益4.52→5.10万")
    vp_combo_exclude_c4: bool = Field(default=False,
                                      description="四象限过滤同时剔除C4（换手>10%且量比<1，无量诱多）：入场45.9%偏弱但合计仍+0.13万，剔除约中性，默认不剔")


class AppConfig(BaseModel):
    run_mode: str = Field(
        default="stable", pattern="^(stable|monster)$",
        description="运行模式：stable=稳健（大盘过滤+战法倾斜+快止损）；"
                    "monster=抓妖股（弱市照开+连板加速+厚利放宽止损且不移动止盈）",
    )
    watchlist: list[str] = Field(default_factory=lambda: ["600519", "000858", "300750"])
    holdings: list[str] = Field(default_factory=list, description="已标记为持有的股票代码")
    holding_costs: dict[str, float] = Field(default_factory=dict,
                                            description="持仓成本价 {code: 买入价}，用于浮盈分档止损")
    sell: SellConfig = Field(default_factory=SellConfig)
    strategies: StrategySwitches = Field(default_factory=StrategySwitches)
    bear_strategies: BearStrategySwitches = Field(default_factory=BearStrategySwitches)
    strategy_rules: StrategyRules = Field(default_factory=StrategyRules)
    market_filter: MarketFilterConfig = Field(default_factory=MarketFilterConfig)
    fundamental_filter: FundamentalFilterConfig = Field(default_factory=FundamentalFilterConfig)
    market_regime: MarketRegimeConfig = Field(default_factory=MarketRegimeConfig)
    rules: AlertRules = Field(default_factory=AlertRules)
    notification: list[NotifierConfig] = Field(
        default_factory=lambda: [NotifierConfig()]
    )
    scan: ScanConfig = Field(default_factory=ScanConfig)


# ----------------------------- 默认配置 -----------------------------

def default_config() -> AppConfig:
    return AppConfig()


_lock = threading.RLock()
_config: AppConfig | None = None


def _migrate(raw: dict[str, Any]) -> dict[str, Any]:
    """兼容旧配置：移除其他通知渠道，仅保留 email；补齐新字段。"""
    raw = dict(raw)
    # 运行模式：旧配置默认稳健模式
    raw.setdefault("run_mode", "stable")
    # 通知渠道只保留 email，并规范化字段
    old_channels = raw.get("notification") or []
    emails = [c for c in old_channels if isinstance(c, dict) and c.get("type") == "email"]
    if emails:
        kept = []
        for c in emails:
            kept.append({
                "id": c.get("id", "email"),
                "type": "email",
                "name": c.get("name", "邮件通知"),
                "enabled": c.get("enabled", False),
                "smtp_host": c.get("smtp_host", ""),
                "smtp_port": c.get("smtp_port", 465),
                "smtp_user": c.get("smtp_user", ""),
                "smtp_password": c.get("smtp_password", ""),
                "mail_from": c.get("mail_from", ""),
                "mail_to": c.get("mail_to", ""),
            })
        raw["notification"] = kept
    else:
        raw["notification"] = [NotifierConfig().model_dump()]

    # 旧版 rules 里的 cooldown_seconds(15分钟)不适合主升浪（会重复提醒），
    # 不继承，直接用 scan.cooldown_seconds 的新默认值(24小时)。
    old_rules = dict(raw.get("rules") or {})
    old_rules.pop("cooldown_seconds", None)
    raw["rules"] = old_rules

    # 聚焦模式一次性迁移：旧配置没有 pullback_vol_ratio 时，
    # 将战法开关重置为新默认值（只留核心友好/危险信号），
    # 之后再手工开启的战法不会被覆盖。
    rules_raw = raw.get("strategy_rules") or {}
    if "pullback_vol_ratio" not in rules_raw:
        raw["strategies"] = StrategySwitches().model_dump()
        raw["bear_strategies"] = BearStrategySwitches().model_dump()
    return raw


def load_config() -> AppConfig:
    """加载配置（带内存缓存）"""
    global _config
    with _lock:
        if _config is not None:
            return _config
        if CONFIG_FILE.exists():
            try:
                raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                migrated = _migrate(raw)
                cfg = AppConfig.model_validate(migrated)
                if migrated != raw:
                    save_config(cfg)
                _config = cfg
                return _config
            except Exception as exc:  # noqa: BLE001
                print(f"[config] 配置文件解析失败，使用默认配置: {exc}")
        _config = default_config()
        save_config(_config)
        return _config


def save_config(cfg: AppConfig) -> None:
    """保存配置到磁盘并更新缓存"""
    global _config
    with _lock:
        CONFIG_FILE.write_text(
            json.dumps(cfg.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _config = cfg


def update_config(patch: dict[str, Any]) -> AppConfig:
    """局部更新配置"""
    with _lock:
        current = load_config().model_dump()
        _deep_merge(current, patch)
        cfg = AppConfig.model_validate(_migrate(current))
        save_config(cfg)
        return cfg


def effective_config() -> AppConfig:
    """返回按「运行模式」投影后的生效配置，不改动磁盘上的用户配置。

    - stable（稳健）：原样返回，维持「洗盘反包 + 情绪点火闸门」冠军方案与
      大盘过滤 + 分档默认关的既有行为。
    - monster（抓妖股）：放开弱市闸门与点火闸门，并强制开启强势续攻
      （连板加速分支是抓妖股的发动机——洗盘反包只抓反包首板，抓不到
      连板股行进中段，如会稽山这类弱市连板启动），让妖股能进入信号池；
      卖出端强制启用厚利分档，但关闭 MA10 移动止盈——回测里移动止盈把
      002667 从 +45.9% 提前砍在 +15.8%，是抓妖股的大忌。

    用 model_copy 生成副本，绝不写回 load_config 的缓存对象，避免一次扫描
    把用户持久化配置里的战法开关改掉（旧代码曾在 half 模式直接改 cfg）。
    """
    cfg = load_config()
    # 始终返回副本：market_scan 在 half 模式会就地关掉强势续攻，
    # 若直接返回缓存对象会污染持久化配置，导致一次弱市扫描后战法被永久改写。
    eff = cfg.model_copy(deep=True)
    if cfg.run_mode != "monster":
        return eff
    # 扫描端：无视大盘弱市闸门与点火闸门，并强制开启强势续攻
    # （连板加速引擎，妖股行进中段的唯一来源；洗盘反包只抓反包首板）
    eff.market_regime.enabled = False
    eff.market_regime.strong_requires_bull = False
    eff.market_regime.ignition_enabled = False
    eff.strategies.强势续攻 = True
    # 卖出端：厚利档放宽止损，但绝不移动止盈，让主升浪跑完
    eff.sell.tier_enabled = True
    eff.sell.tier_ma10_trail = False
    return eff


def _deep_merge(target: dict, patch: dict) -> None:
    for key, value in patch.items():
        if key not in target:
            target[key] = value
        elif isinstance(target[key], dict) and isinstance(value, dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value
