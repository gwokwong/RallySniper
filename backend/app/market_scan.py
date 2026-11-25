"""全市场「涨停回调 · 强势续攻」扫描引擎。

与标的池盯盘（monitor.py）不同，这里扫描全市场近 N 日涨停过的股票，
用「涨停回调 / 强势续攻」两套战法筛选可能启动主升浪的标的，并补齐量比、
换手率、资金流数据，供 14:00 / 14:40 两个时点推送。两战法分别受配置页
对应开关控制，命中信号合并推送。

数据源全部走 datasource 的缓存与降级链，东财限流时自动切腾讯/同花顺。
"""
from __future__ import annotations

import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any

from . import datasource as ds, mainwave, scanstore
from .config import effective_config, load_config


class MarketScanState:
    """扫描进度与结果（内存，重启后由 API 主动触发重扫）。"""

    def __init__(self) -> None:
        self.running = False
        self.last_scan: str | None = None
        self.last_duration = 0.0
        self.candidates = 0
        self.scanned = 0
        self.signals: list[dict[str, Any]] = []
        self.last_times: list[str] = []
        self.filtered_out = 0
        self.filter_reasons: dict[str, int] = {}
        # 行情实际对应的交易日：交易日=当日，休市/盘前=最近交易日
        self.scan_date: str | None = None
        self.scan_date_label: str | None = None
        self.market_label: str | None = None
        self.is_closed = False
        self.market_regime: dict[str, Any] = {}
        self.ignition: dict[str, Any] = {}
        self.run_mode: str = "stable"
        self.last_source: str | None = None   # 上一次扫描来源：手动扫描/手动推送/系统定时
        self.last_saved_at: str | None = None
        self._lock = threading.RLock()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self.running,
                "last_scan": self.last_scan,
                "last_duration": round(self.last_duration, 1),
                "candidates": self.candidates,
                "scanned": self.scanned,
                "filtered_out": self.filtered_out,
                "filter_reasons": dict(self.filter_reasons),
                "signal_count": len(self.signals),
                "last_times": list(self.last_times),
                "scan_date": self.scan_date,
                "scan_date_label": self.scan_date_label,
                "market_label": self.market_label,
                "is_closed": self.is_closed,
                "market_regime": dict(self.market_regime),
                "ignition": dict(self.ignition),
                "run_mode": self.run_mode,
                "last_source": self.last_source,
                "last_saved_at": self.last_saved_at,
            }


state = MarketScanState()


def persist_last_scan(result: dict[str, Any], source: str) -> None:
    """成功扫描后保存「上一次全市场机会」快照（含来源标注，重启不丢）。"""
    with state._lock:
        state.last_source = source
        state.last_saved_at = datetime.now().isoformat(timespec="seconds")
    try:
        scanstore.save("scan", {"source": source,
                                "snapshot": state.snapshot(),
                                "signals": result.get("signals", [])})
    except Exception:  # noqa: BLE001
        pass


def last_scan_persisted() -> dict[str, Any] | None:
    """落盘的上一次全市场机会（内存态为空时作回退）。"""
    return scanstore.load("scan")


def _date_fields() -> dict[str, Any]:
    """当前扫描数据对应的交易日标注，供各返回分支复用。"""
    with state._lock:
        return {
            "scan_date": state.scan_date,
            "scan_date_label": state.scan_date_label,
            "market_label": state.market_label,
            "is_closed": state.is_closed,
            "run_mode": state.run_mode,
        }


def _board_of(code: str) -> str:
    """按代码前缀判定板块：main/kcb/cyb/bj/other。"""
    c = str(code).zfill(6)
    if c.startswith(("688", "689")):
        return "kcb"
    if c.startswith(("300", "301")):
        return "cyb"
    if c.startswith(("43", "83", "87", "92", "8")):
        return "bj"
    if c.startswith(("60", "00")):
        return "main"
    return "other"


def pass_filter(code: str, name: str, price: float | None,
                f: dict[str, Any]) -> tuple[bool, str]:
    """按市场筛选配置判断候选股是否纳入扫描。返回 (是否通过, 淘汰原因)。"""
    name = str(name or "")
    # ST / *ST / SST / S*ST 一律按 ST 处理
    if "ST" in name.upper():
        if not f.get("include_st", False):
            return False, "ST"
    board = _board_of(code)
    board_keys = {"main": "main_board", "kcb": "kcb", "cyb": "cyb", "bj": "bj"}
    key = board_keys.get(board)
    if key and not f.get(key, True):
        return False, board
    # 其他未识别板块：只要不是 ST 就放行（避免误杀）
    # 价格区间（0 表示不限），用涨停池最新价粗筛
    try:
        lo = float(f.get("min_price", 0) or 0)
        hi = float(f.get("max_price", 0) or 0)
        if price is not None and lo > 0 and hi > 0 and lo <= hi:
            if not (lo <= float(price) <= hi):
                return False, "price"
        elif price is not None and lo > 0 and float(price) < lo:
            return False, "price"
        elif price is not None and hi > 0 and float(price) > hi:
            return False, "price"
    except (TypeError, ValueError):
        pass
    return True, ""


def _scan_one(code: str, name: str, rules: dict[str, Any],
              flows: dict[str, dict[str, float | str]],
              names: dict[str, str],
              enabled: dict[str, bool],
              mfilter: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """对单只候选股跑涨停回调 / 强势续攻战法，命中则补齐展示指标。

    受 enabled 开关控制（对应配置页「涨停回调」「强势续攻」），同一只票
    两个战法都命中时保留两条信号，由前端按战法区分。
    """
    hist = ds.stock_history(code, days=130)
    if hist is None or len(hist) < 40:
        return []
    display_name = names.get(code) or name or code
    d = mainwave._indicators(hist, code, display_name)
    checks: list[tuple[str, Any]] = []
    if enabled.get("涨停回调", True):
        checks.append(("涨停回调", mainwave._strategy_limitup_pullback))
    if enabled.get("强势续攻", True):
        checks.append(("强势续攻", mainwave._strategy_strong_continue))
    if enabled.get("洗盘反包", True):
        checks.append(("洗盘反包", mainwave._strategy_wash_reversal))
    if not checks:
        return []

    hits: list[tuple[str, dict[str, Any]]] = []
    for sname, fn in checks:
        try:
            res = fn(d, rules)
        except Exception:  # noqa: BLE001
            continue
        if res.get("hit"):
            hits.append((sname, res))
    if not hits:
        return []

    # A杀过滤：近N个交易日内出现冲高跳水长上影的股票不碰
    if mfilter and mfilter.get("a_kill_skip_enabled", True) \
            and mainwave.recent_a_kill(d, mfilter):
        return []

    metrics = ds.enrich_market_metrics(code, hist, flows, names)
    # 换手率×量比四象限入场过滤（C1 温和放量反包=最差象限，回测 19% 胜率）
    if mfilter and mainwave.vp_combo_should_skip(metrics.get("turnover"),
                                                 metrics.get("volume_ratio"), mfilter):
        return []
    cur = d.iloc[-1]
    # 强势续攻量比上限过滤（2026-11-21 回测）：高位放量(量比过大)的强势续攻
    # 多为出货/追高，10月样本量比<3时大亏笔数-43%且妖股保留85.7%。
    # 默认0=不过滤；妖股模式建议设3.0。只过滤强势续攻，不动洗盘反包/涨停回调。
    max_vr = float(rules.get("strong_max_vol_ratio", 0) or 0)
    if max_vr > 0 and metrics.get("volume_ratio") is not None:
        vr_val = float(metrics["volume_ratio"])
        if vr_val > max_vr:
            hits = [(sn, rs) for sn, rs in hits if sn != "强势续攻"]
    out: list[dict[str, Any]] = []
    for sname, res in hits:
        stage = res.get("stage", "")
        stage_label = {"trigger": "放量启动", "watch": "观察中"}.get(stage, "")
        out.append({
            "code": code,
            "name": metrics["name"] or display_name,
            "strategy": sname,
            "direction": "up",
            "stage": stage,
            "stage_label": stage_label,
            "date": str(cur["date"]),
            "price": round(float(cur["close"]), 2),
            "change_pct": round(float(cur["pct_change"]), 2) if "pct_change" in d.columns else metrics.get("change_pct", 0),
            "volume_ratio": metrics["volume_ratio"],
            "turnover": metrics["turnover"],
            "net_inflow": metrics["net_inflow"],
            "inflow": metrics["inflow"],
            "outflow": metrics["outflow"],
            "reasons": res["reasons"],
            "signal": f"{sname}·{stage_label or '放量启动'}：" + " + ".join(res["reasons"]),
            "strength": res.get("strength", 0),
        })
    return out


def scan_market(push_time: str | None = None, workers: int = 8) -> dict[str, Any]:
    """执行一次全市场扫描，返回命中的放量启动/观察中信号。

    push_time: 触发时点标记（如 '14:00'），仅用于记录，不影响筛选。
    """
    with state._lock:
        if state.running:
            return {"success": False, "error": "扫描进行中，请稍后", **state.snapshot()}
        state.running = True
        started = datetime.now()
        t0 = time.time()

    try:
        cfg = effective_config()
        with state._lock:
            state.run_mode = cfg.run_mode
        rules = cfg.strategy_rules.model_dump()
        # 数据交易日：交易日扫当日，休市/盘前自动落到最近一个交易日
        eff = ds.effective_trading_date()

        # 大盘环境闸门（2026-11-21）：弱市暂停/减半开新仓，战法权重倾斜。
        # 强市：全部启用战法；弱市 pause：直接返回空机会；
        # 弱市 half：仅保留主战法「洗盘反包」，屏蔽强势续攻。
        regime = ds.market_regime(
            ma_period=cfg.market_regime.ma_period,
            slope_lookback=cfg.market_regime.slope_lookback,
            index_code=cfg.market_regime.index_code,
        )
        with state._lock:
            state.market_regime = regime
        regime_enabled = bool(cfg.market_regime.enabled)
        if regime_enabled and not regime.get("bull") and not regime.get("degraded"):
            if cfg.market_regime.weak_action == "pause":
                return {
                    "success": True, "paused_by_regime": True, "candidates": 0,
                    "scanned": 0, "signal_count": 0, "trigger_count": 0,
                    "watch_count": 0, "signals": [], "market_regime": regime,
                    "note": f"大盘弱势（{regime['label']}，指数{regime['close']}<MA20{regime['ma']}），已暂停开新仓",
                    **_date_fields(),
                }
            # half：主战法优先，强势续攻仅强市开启
            if cfg.market_regime.strong_requires_bull:
                cfg.strategies.强势续攻 = False

        # 情绪点火闸门（2026-11-21 三个月全市场 2014 笔回测）：昨日涨停股今日
        # 涨幅中位数 ≥ 阈值视为「点火日」——点火日信号胜率 44.9%（基线 22.8%），
        # 非点火日追高信号系统性亏钱，暂停开新仓。妖股模式绕过（与弱市闸门同策略）。
            ignition: dict[str, Any] = {}
            if cfg.market_regime.ignition_enabled and cfg.run_mode != "monster":
                ignition = ds.ignition_premium(eff["date"])
                # 附带阈值与运行模式，供前端/通知层判断当前是否点火
                ignition["threshold"] = float(cfg.market_regime.ignition_threshold)
                ignition["ignited"] = bool(
                    not ignition.get("degraded") and ignition.get("median") is not None
                    and float(ignition["median"]) >= float(cfg.market_regime.ignition_threshold)
                )
                # 炸板闸门（2026-11-25 全网因子研究）：昨日市场炸板率>阈值=资金分歧极大
                # （情绪冰点），即使溢价达标也不点火。全部由昨日K线计算，开盘前已知。
                # 回测（7-11月）：叠加后总收益+5.99→+8.36万，胜率51.8%→57.5%。
                if ignition["ignited"] and cfg.market_regime.ignition_bomb_gate_enabled:
                    try:
                        from . import replay as _rp
                        dts = _rp.trading_dates()
                        pos = dts.index(eff["date"]) if eff["date"] in dts else -1
                        prev_d = dts[pos - 1] if pos >= 1 else None
                        br = (_rp.market_sentiment().get(prev_d, {}) or {}).get("bomb_rate") if prev_d else None
                        br_max = float(cfg.market_regime.ignition_bomb_rate_max)
                        ignition["bomb_rate_prev"] = br
                        ignition["bomb_rate_max"] = br_max
                        if br is not None and br > br_max:
                            ignition["ignited"] = False
                            with state._lock:
                                state.ignition = ignition
                            return {
                                "success": True, "paused_by_ignition": True, "candidates": 0,
                                "scanned": 0, "signal_count": 0, "trigger_count": 0,
                                "watch_count": 0, "signals": [], "ignition": ignition,
                                "market_regime": regime,
                                "note": (f"炸板闸门：昨日市场炸板率 {br:.1f}% > {br_max:g}%，"
                                         f"资金分歧极大（情绪冰点），暂停开新仓"),
                                **_date_fields(),
                            }
                    except Exception:  # noqa: BLE001
                        pass  # 情绪统计不可用时不拦截（降级）
            # 广度档位（2026-11-22 阶段研究）：点火日当日涨停家数≥全仓档阈值(默认100)
            # = 普涨高潮，信号胜率82.7%；家数不足 = 修复反弹，胜率19.8% → 建议试错1/4仓。
            # 纯提示不拦截；硬拦截用 ignition_min_breadth。
            today_zt = None
            size_tier = None
            if ignition["ignited"]:
                try:
                    detail = ds.limit_up_pool_detail(eff["date"], ttl=300)
                    today_zt = len(detail) if detail else 0
                except Exception:  # noqa: BLE001
                    today_zt = None
                tier_th = int(cfg.market_regime.ignition_tier_breadth or 100)
                ignition["tier_th"] = tier_th
                if today_zt is not None:
                    size_tier = "full" if today_zt >= tier_th else "probe"
            ignition["today_zt"] = today_zt
            ignition["size_tier"] = size_tier
            ignition["size_tier_label"] = {"full": "全仓档(广度普涨)", "probe": "试错档(建议1/4仓)"}.get(
                size_tier, "档位未知(广度取数失败)")
            # 广度确认（2026-11-22 阶段研究）：点火∧当日涨停家数≥阈值(高潮)胜率82.7%，
            # 点火∧家数不足(修复反弹)仅14.6%——溢价高但广度不够多为一日游反弹
            breadth = int(cfg.market_regime.ignition_min_breadth or 0)
            if breadth > 0 and ignition["ignited"]:
                ignition["min_breadth"] = breadth
                if today_zt is not None and today_zt < breadth:
                    ignition["ignited"] = False
                    with state._lock:
                        state.ignition = ignition
                    return {
                        "success": True, "paused_by_ignition": True, "candidates": 0,
                        "scanned": 0, "signal_count": 0, "trigger_count": 0,
                        "watch_count": 0, "signals": [], "ignition": ignition,
                        "market_regime": regime,
                        "note": (f"情绪点火但广度不足（今日涨停 {today_zt} 家 < {breadth}，"
                                 f"中位溢价 {ignition.get('median'):+.2f}% 属修复反弹非高潮），暂停开新仓"),
                        **_date_fields(),
                    }
            with state._lock:
                state.ignition = ignition
            median = ignition.get("median")
            if not ignition.get("degraded") and median is not None \
                    and float(median) < float(cfg.market_regime.ignition_threshold):
                return {
                    "success": True, "paused_by_ignition": True, "candidates": 0,
                    "scanned": 0, "signal_count": 0, "trigger_count": 0,
                    "watch_count": 0, "signals": [], "ignition": ignition,
                    "market_regime": regime,
                    "note": (f"情绪未点火（{ignition.get('prev_date', '')} 涨停股今日中位溢价 "
                             f"{median:+.2f}% < {cfg.market_regime.ignition_threshold:g}%），暂停开新仓"),
                    **_date_fields(),
                }
        else:
            with state._lock:
                state.ignition = ignition

        with state._lock:
            state.scan_date = eff["date"]
            state.scan_date_label = eff["label"]
            state.market_label = eff["market_label"]
            state.is_closed = eff["is_closed"]
        raw_codes, pool_names, pool_prices = ds.limit_up_pool_codes(
            days=int(rules.get("limitup_lookback", 20)), end_date=eff["date"]
        )
        if not raw_codes:
            return {
                "success": False, "error": f"{eff['label']} 涨停池数据不可用（数据源限流），稍后重试",
                "ignition": ignition,
                **_date_fields(),
            }

        # 板块 / 价格筛选
        mfilter = cfg.market_filter.model_dump()
        codes: list[str] = []
        reasons: dict[str, int] = {}
        for code in raw_codes:
            ok, why = pass_filter(code, pool_names.get(code, code), pool_prices.get(code), mfilter)
            if ok:
                codes.append(code)
            else:
                reasons[why] = reasons.get(why, 0) + 1
        with state._lock:
            state.candidates = len(codes)
            state.scanned = 0
            state.filtered_out = len(raw_codes) - len(codes)
            state.filter_reasons = reasons

        if not codes:
            return {
                "success": True, "duration": 0, "candidates": 0, "scanned": 0,
                "signal_count": 0, "trigger_count": 0, "watch_count": 0,
                "signals": [], "filtered_out": state.filtered_out,
                "filter_reasons": reasons,
                "degraded": ds.cache.is_degraded("fund_flow_map"),
                **_date_fields(),
            }

        flows = ds.fund_flow_map()
        names = ds.stock_code_names()
        enabled = cfg.strategies.as_dict()

        signals: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_scan_one, code, pool_names.get(code, code), rules, flows, names, enabled, mfilter): code
                for code in codes
            }
            for fut in as_completed(futures):
                try:
                    items = fut.result()
                    if items:
                        signals.extend(items)
                except Exception:  # noqa: BLE001
                    pass
                with state._lock:
                    state.scanned += 1

        # 基本面关联与筛选（2026-11-25）：营收同比≥阈值的信号才推送（业绩缺失保留并标注）
        if getattr(cfg, "fundamental_filter", None) and cfg.fundamental_filter.enabled:
            def _earnings_map():
                def _load(date):
                    key = f"yjbb_{date}"
                    def fetch():
                        import akshare as _ak
                        df = _ak.stock_yjbb_em(date=date)
                        out = {}
                        for _, r in df.iterrows():
                            code = str(r.get("股票代码", "")).strip().zfill(6)
                            try:
                                rev = float(r.get("营业总收入-同比增长"))
                            except (TypeError, ValueError):
                                rev = None
                            try:
                                np_ = float(r.get("净利润-净利润"))
                            except (TypeError, ValueError):
                                np_ = None
                            out[code] = {"rev_yoy": rev, "profit": np_,
                                         "industry": str(r.get("所处行业", ""))}
                        return out or None
                    return ds.cache.get_or_fetch(key, 43200, fetch) or {}
                # 无未来函数：按数据交易日选择最新已披露财报（11-12月→三季报，其余→中报）
                mm = int(eff["date"][4:6])
                report_date = "20260930" if mm >= 10 else "20260630"
                return _load(report_date)
            fmap = _earnings_map()
            kept = []
            for s in signals:
                f = fmap.get(s["code"])
                if f:
                    s["rev_yoy"] = f.get("rev_yoy")
                    s["profit_pos"] = (f.get("profit") is not None and f.get("profit") > 0) or None
                    s["industry"] = f.get("industry") or s.get("industry")
                    if s["rev_yoy"] is not None and s["rev_yoy"] < cfg.fundamental_filter.min_rev_yoy:
                        continue            # 营收增速低于阈值 → 过滤
                kept.append(s)
            filtered_n = len(signals) - len(kept)
            signals = kept
            if filtered_n:
                print(f"[market_scan] 基本面过滤: 剔除营收增速<{cfg.fundamental_filter.min_rev_yoy:g}% 信号 {filtered_n} 条")

        # 洗盘反包为主战法：同级信号中优先展示（主战法加权排在前面）
        primary = "洗盘反包" if cfg.market_regime.wash_primary else ""
        signals.sort(key=lambda x: (x["stage"] == "trigger",
                                    x.get("strategy") == primary,
                                    x["strength"]), reverse=True)
        # 每日信号上限：点火日信号可集中爆发（回测单日最多71只），随机等权
        # 抽签控制推送数量。不用强度排序挑选——回测证明强度Top反而更差。
        cap = int(cfg.market_filter.daily_signal_cap or 0)
        capped_note = ""
        if cap > 0 and len(signals) > cap:
            capped_note = f"信号 {len(signals)} 只超每日上限 {cap}，已随机等权抽取推送"
            signals = random.sample(signals, cap)
        duration = time.time() - t0
        with state._lock:
            state.signals = signals
            state.last_scan = started.isoformat(timespec="seconds")
            state.last_duration = duration
            if push_time:
                state.last_times.append(push_time)
                state.last_times = state.last_times[-10:]
        return {
            "success": True,
            "time": state.last_scan,
            "duration": round(duration, 1),
            "candidates": len(codes),
            "scanned": state.scanned,
            "filtered_out": state.filtered_out,
            "filter_reasons": dict(state.filter_reasons),
            "signal_count": len(signals),
            "trigger_count": sum(1 for s in signals if s["stage"] == "trigger"),
            "watch_count": sum(1 for s in signals if s["stage"] == "watch"),
            "strong_count": sum(1 for s in signals if s.get("strategy") == "强势续攻"),
            "pullback_count": sum(1 for s in signals if s.get("strategy") == "涨停回调"),
            "signals": signals,
            "capped_note": capped_note,
            "market_regime": regime,
            "ignition": ignition,
            "degraded": ds.cache.is_degraded("fund_flow_map"),
            **_date_fields(),
        }
    finally:
        with state._lock:
            state.running = False


def latest_signals() -> list[dict[str, Any]]:
    with state._lock:
        return list(state.signals)
