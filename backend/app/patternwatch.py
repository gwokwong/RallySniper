"""盘中形态监控：全市场「洗盘反包·进行中」与「N日新高突破」实时提醒。

- 反包·进行中：近 wash_lookback 日有洗盘大跌（≥wash_drop_pct），今日价格已收复
  洗盘日收盘且涨幅≥5% 或盘中触板——冠军战法的实时前置版（不等封板确认）。
- N日新高突破：现价 > 前 pattern_break_high_days 日最高×1.002 且量比≥1.5
  （形态突破；注意：历史回测中突破形态的隔夜持有期望为负，本提醒仅作盘中观察/决策参考）。

防刷屏：每票每日每形态一次、每日总量上限 pattern_max_alerts、同轮多票合并一封邮件。
数据：前一日基准来自 kline_cache（replay._load_bars 瘦身索引），盘中用实时行情快照判定。
"""
from __future__ import annotations

import threading
from collections import defaultdict
from bisect import bisect_left
from datetime import datetime
from typing import Any

import numpy as np

from . import datasource as ds, notifications, notifier, replay
from .config import load_config


class PatternState:
    def __init__(self) -> None:
        self.date: str | None = None
        self.prepared: bool = False            # 当日候选/基准是否已就绪
        self.rev_candidates: set[str] = set()  # 反包候选（昨日收盘判定）
        self.high_base: dict[str, float] = {}  # 突破基准：代码→前N日最高
        self.wzt_base: dict[str, dict] = {}    # 涨停缩量突破：代码→{low, ph, ldate}
        self.names: dict[str, str] = {}        # 代码→名称
        self.industry_map: dict[str, str] = {}  # 代码→行业
        self.board_alerted: set[str] = set()   # 已提醒的板块异动（ind:方向）
        self.alerted: set[str] = set()         # 已提醒 code:pattern
        self.day_alerts: int = 0
        self.last_poll: str | None = None
        self.last_new: list[dict[str, Any]] = []
        self._lock = threading.RLock()

    def reset_for(self, date: str) -> None:
        self.date = date
        self.prepared = False
        self.rev_candidates = set()
        self.high_base = {}
        self.wzt_base = {}
        self.names = {}
        self.industry_map = {}
        self.board_alerted = set()
        self.alerted = set()
        self.day_alerts = 0
        self.last_poll = None
        self.last_new = []


state = PatternState()


def _prev_day_row(code: str, prev_day: str) -> int | None:
    dl = replay._dlists.get(code)
    if not dl:
        return None
    i = bisect_left(dl, prev_day)
    if i < len(dl) and dl[i] == prev_day:
        return i
    return None


def _prepare_day(prev_day: str, names: dict[str, str] | None = None,
                 industry_map: dict[str, str] | None = None) -> None:
    """开盘前一次性预计算：反包候选 + 突破基准（全部基于前一日及更早，无未来函数）。"""
    cfg = load_config()
    rules = cfg.strategy_rules
    lookback = int(rules.wash_lookback)
    drop = float(rules.wash_drop_pct)
    bars = replay._load_bars()
    rev: set[str] = set()
    high_base: dict[str, float] = {}
    wzt_base: dict[str, dict] = {}
    mfilter = cfg.market_filter
    for code, df in bars.items():
        if not code.startswith(("60", "00", "68", "30")):
            continue
        if mfilter.main_board is False and code.startswith(("60", "00")):
            continue
        if mfilter.kcb is False and code.startswith(("688", "689")):
            continue
        if mfilter.cyb is False and code.startswith(("300", "301")):
            continue
        k = _prev_day_row(code, prev_day)
        if k is None:
            continue
        pc = df["pct_change"].iloc[max(0, k - lookback + 1): k + 1]
        # 反包候选：近 N 日有洗盘大跌，且昨日未封板（封板属连板场景，非反包前置）
        if bool((pc <= -drop).any()):
            th = 19.5 if code.startswith(("688", "689", "300", "301")) else 9.8
            if not (float(df["pct_change"].iloc[k]) >= th - 0.4
                    and float(df["close"].iloc[k]) >= float(df["high"].iloc[k]) * 0.995):
                rev.add(code)
        # 突破基准：前 N 日最高（不含当日）
        lo = max(0, k - int(cfg.scan.pattern_break_high_days) + 1)
        high_base[code] = float(df["high"].iloc[lo:k + 1].max())
        # 涨停缩量突破基准（用户战法）：近10个交易日内最近一次收盘封板 L，
        # 涨停后从次日起连续≥3日量能逐日递减（持续缩量），回调段收盘均不破 L 最低价
        th = 19.5 if code.startswith(("688", "689", "300", "301")) else 9.8
        L = None
        for j in range(k, max(0, k - 9) - 1, -1):          # 近10个交易日（含昨日）找最近封板日
            if float(df["pct_change"].iloc[j]) >= th and float(df["close"].iloc[j]) >= float(df["high"].iloc[j]) * 0.995:
                L = j
                break
        if L is not None:
            pull = k - L                                    # 涨停日后的回调天数
            if pull >= 3:
                vols = df["volume"].iloc[L:k + 1].astype(float).tolist()
                shrink3 = vols[1] > vols[2] > vols[3]       # 涨停次日起连续3日缩量
                low_l = float(df["low"].iloc[L])
                holds = bool((df["close"].iloc[L + 1: k + 1] >= low_l).all())
                if shrink3 and holds:
                    ph = float(df["high"].iloc[L:k + 1].max())   # 区间最高（涨停日至今）
                    wzt_base[code] = {"low": low_l, "ph": round(ph, 3),
                                      "ldate": str(df["date"].iloc[L])[:10]}
    with state._lock:
        state.rev_candidates = rev
        state.high_base = high_base
        state.wzt_base = wzt_base
        state.names = dict(names or {})
        state.industry_map = dict(industry_map or {})
        state.prepared = True


def poll() -> dict[str, Any]:
    """盘中执行一次形态监控（调度器每 N 分钟调用）。返回本轮新增触发。"""
    cfg = load_config()
    if not cfg.scan.pattern_watch_enabled:
        return {"checked": False, "reason": "未启用"}
    status = ds.market_status()["status"]
    if status not in ("trading",):
        return {"checked": False, "reason": f"非盘中({status})"}

    eff = ds.effective_trading_date()
    today = eff["date"]
    now_hm = datetime.now().strftime("%H:%M")
    with state._lock:
        if state.date != today:
            state.reset_for(today)
        state.last_poll = now_hm

    if not state.prepared:
        dates = replay.trading_dates()
        pos = [k for k, d in enumerate(dates) if d < today]
        if not pos:
            return {"checked": False, "reason": "无前置交易日"}
        _prepare_day(dates[pos[-1]])

    quotes = ds.spot_quote_map()
    if not quotes:
        with state._lock:
            state.alerted.clear()
        return {"checked": True, "reason": "行情快照不可用，跳过本轮"}

    mfilter = cfg.market_filter
    vr_min = float(cfg.scan.pattern_break_vr)
    cap = int(cfg.scan.pattern_max_alerts)
    new: list[dict[str, Any]] = []
    with state._lock:
        alerted = state.alerted
        day_alerts = state.day_alerts
        industry_map = dict(state.industry_map)

    # 板块异动检测（个股聚合中位口径）：拉涨≥阈值 / 砸盘≤-阈值，每板块每日各一次
    board_th = float(cfg.scan.pattern_board_anomaly_pct)
    ind_pcts = defaultdict(list)
    for code, q in quotes.items():
        ind = industry_map.get(code)
        pct = q.get("change_pct")
        if ind and pct is not None:
            ind_pcts[ind].append(float(pct))
    board_now = {ind: float(np.median(vals)) for ind, vals in ind_pcts.items() if len(vals) >= 3}
    import sys as _s
    print(f"[debug] board段: quotes={len(quotes)} industry_map={len(industry_map)} ind_pcts={dict(ind_pcts)}", file=_s.stderr)
    for ind, bp in sorted(board_now.items(), key=lambda kv: -kv[1]):
        if bp >= board_th and f"{ind}:拉涨" not in state.board_alerted:
            state.board_alerted.add(f"{ind}:拉涨")
            new.append({"code": "--", "name": f"板块·{ind}", "pattern": "板块异动·拉涨",
                        "price": None, "pct": round(bp, 2), "vr": None,
                        "detail": f"行业聚合涨跌中位 {bp:+.1f}%（板块内 {len(ind_pcts) and ''}{sum(1 for c, ic in industry_map.items() if ic == ind)} 只成分），"
                                  f"该板块候选优先关注"})
            if state.day_alerts < cap:
                state.day_alerts += 1
                title = f"【板块异动·拉涨】{ind} {bp:+.1f}%"
                body = (f"{now_hm} 行业聚合涨跌中位 {bp:+.1f}%（成分 {sum(1 for c, ic in industry_map.items() if ic == ind)} 只）\n"
                        f"该板块反包候选将优先进入主推精选；追高注意兑现节奏。")
                notifications.add(title, body, [], kind="pattern")
                if cfg.notification:
                    notifier.broadcast(cfg.notification, title, body, [])
        elif bp <= -board_th and f"{ind}:砸盘" not in state.board_alerted:
            state.board_alerted.add(f"{ind}:砸盘")
            new.append({"code": "--", "name": f"板块·{ind}", "pattern": "板块异动·砸盘",
                        "price": None, "pct": round(bp, 2), "vr": None,
                        "detail": f"行业聚合涨跌中位 {bp:+.1f}%，该板块持仓注意风险，候选暂停追入"})
            if state.day_alerts < cap:
                state.day_alerts += 1
                title = f"【板块异动·砸盘】{ind} {bp:+.1f}%"
                body = f"{now_hm} 行业聚合涨跌中位 {bp:+.1f}%（成分 {sum(1 for c, ic in industry_map.items() if ic == ind)} 只）——该板块候选暂停追入，持仓注意风险。"
                notifications.add(title, body, [], kind="pattern")
                if cfg.notification:
                    notifier.broadcast(cfg.notification, title, body, [])

    for code, q in quotes.items():
        if day_alerts >= cap:
            break
        nm = str(q.get("name") or code)
        if "ST" in nm.upper() or "退" in nm:
            continue
        if not code.startswith(("60", "00", "68", "30")):
            continue
        if mfilter.main_board is False and code.startswith(("60", "00")):
            continue
        if mfilter.kcb is False and code.startswith(("688", "689")):
            continue
        if mfilter.cyb is False and code.startswith(("300", "301")):
            continue
        price = q.get("price")
        pct = q.get("change_pct")
        vr = q.get("volume_ratio")
        pre = q.get("pre_close")
        if price is None or pct is None or not pre:
            continue
        if mfilter.max_price and price > mfilter.max_price:
            continue
        if mfilter.min_price and price < mfilter.min_price:
            continue
        th = 19.5 if code.startswith(("688", "689", "300", "301")) else 9.8
        high = q.get("high") or 0

        # 形态1：洗盘反包·进行中（候选 + 涨幅≥5%或触板 + 价格守在洗盘日收盘上方）
        if code in state.rev_candidates and f"{code}:rev" not in alerted:
            near_board = pct >= min(5.0, th - 0.5) or (high and high >= pre * (1 + th / 100) * 0.999)
            if near_board:
                # 守住洗盘日收盘：近 lookback 日最近一个洗盘日的收盘
                df = replay._bars.get(code)
                wash_close = None
                if df is not None:
                    drop = float(cfg.strategy_rules.wash_drop_pct)
                    lb = int(cfg.strategy_rules.wash_lookback)
                    di = _prev_day_row(code, today)
                    start = max(0, (di if di is not None else len(df)) - lb)
                    seg = df.iloc[start:(di if di is not None else len(df))]
                    if not seg.empty:
                        wash_idx = seg["pct_change"].idxmin()
                        wash_close = float(df["close"].iloc[wash_idx])
                if wash_close is not None and price >= wash_close:
                    alerted.add(f"{code}:rev")
                    day_alerts += 1
                    new.append({"code": code, "name": nm, "pattern": "洗盘反包·进行中",
                                "price": round(price, 2), "pct": round(pct, 2),
                                "vr": round(vr, 2) if vr is not None else None,
                                "detail": f"今日涨幅{pct:+.1f}%、守在洗盘日收盘{wash_close:.2f}上方"})

        # 形态2：N日新高突破（首次越线 + 量比≥阈值）
        if f"{code}:brk" not in alerted:
            base = state.high_base.get(code)
            if base and base > 0 and price > base * 1.002 and vr is not None and vr >= vr_min:
                alerted.add(f"{code}:brk")
                day_alerts += 1
                new.append({"code": code, "name": nm, "pattern": f"新高突破",
                            "price": round(price, 2), "pct": round(pct, 2),
                            "vr": round(vr, 2),
                            "detail": f"突破前{cfg.scan.pattern_break_high_days}日最高{base:.2f}，量比{vr:.1f}"})

        # 形态3：涨停缩量突破（用户战法：近10日有涨停→缩量≥3日不破涨停日最低→放量突破区间最高）
        if f"{code}:wzt" not in alerted:
            wb = state.wzt_base.get(code)
            if wb and price > wb["ph"] and vr is not None and vr >= vr_min:
                low_today = q.get("low")
                if low_today is None or low_today >= wb["low"] * 0.995:   # 当日也未破涨停日最低
                    alerted.add(f"{code}:wzt")
                    day_alerts += 1
                    new.append({"code": code, "name": nm, "pattern": "涨停缩量突破",
                                "price": round(price, 2), "pct": round(pct, 2),
                                "vr": round(vr, 2),
                                "detail": (f"突破{wb['ldate'][5:]}涨停以来区间最高{wb['ph']:.2f}"
                                           f"（缩量≥3日不破{wb['low']:.2f}），量比{vr:.1f}")})

    with state._lock:
        state.last_new = new
        state.day_alerts = day_alerts

    if new:
        title = f"【形态突破】{now_hm} 新增 {len(new)} 条（今日累计 {state.day_alerts}）"
        lines = "\n".join(
            f"- {x['name']}({x['code']})【{x['pattern']}】现价{x['price']} {x['pct']:+.1f}%"
            f"（量比{x['vr'] if x['vr'] is not None else '--'}）：{x['detail']}"
            for x in new[:20])
        body = f"触发时间：{now_hm}\n{lines}\n\n提醒：盘中形态信息，仅供观察与决策参考，不构成买卖信号。"
        notifications.add(title, body, [], kind="pattern")
        if cfg.notification:
            notifier.broadcast(cfg.notification, title, body, [])

    return {"checked": True, "new": new, "day_alerts": state.day_alerts}


def ignition_picks(top_n: int = 4) -> dict[str, Any]:
    """点火日精选候选：反包候选池按「盈利优先→营收增速→行业分散」取 Top N。

    回测证明点火日内按任何单一维度精选都不优于等权——本名单是资金有限时的
    结构化分散参考（非 alpha 承诺），配合实时「反包·进行中」提醒与生命线止损使用。
    """
    cfg = load_config()
    eff = ds.effective_trading_date()
    today = eff["date"]
    with state._lock:
        need_prep = state.date != today or not state.prepared
    if need_prep:
        dates = replay.trading_dates()
        pos = [k for k, d in enumerate(dates) if d < today]
        if not pos:
            return {"picks": [], "reason": "无前置交易日"}
        try:
            names = ds.stock_code_names()
        except Exception:  # noqa: BLE001
            names = {}
        industry_map = {}
        try:
            ydf = ds.cache.get_or_fetch(
                "yjbb_picks_ind", 43200,
                lambda: __import__("akshare").stock_yjbb_em(date="20260930")) or None
            if ydf is not None:
                for _, rr in ydf.iterrows():
                    industry_map[str(rr.get("股票代码", "")).strip().zfill(6)] = str(rr.get("所处行业", ""))
        except Exception:  # noqa: BLE001
            pass
        _prepare_day(dates[pos[-1]], names, industry_map)
    codes = sorted(state.rev_candidates)
    if not codes:
        return {"picks": [], "reason": "今日反包候选为空"}

    mm = int(today[4:6])
    report = "20260930" if mm >= 10 else "20260630"

    def _fund_map():
        def fetch():
            import akshare as _ak
            df = _ak.stock_yjbb_em(date=report)
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
                out[code] = {"rev_yoy": rev, "profit": np_, "industry": str(r.get("所处行业", ""))}
            return out or None
        return ds.cache.get_or_fetch(f"yjbb_picks_{report}", 43200, fetch) or {}

    fmap = _fund_map()
    cap_yi = float(cfg.fundamental_filter.pick_max_float_cap)
    per_ind = max(1, int(cfg.fundamental_filter.pick_per_industry))
    quotes = ds.spot_quote_map(codes)

    cands = []
    for code in codes:
        q = quotes.get(code) or {}
        f = fmap.get(code, {})
        rev = f.get("rev_yoy")
        prof = f.get("profit")
        fc = q.get("float_cap")   # 流通市值（元）
        cap_ok = fc is None or fc <= cap_yi * 1e8
        if not cap_ok:
            continue
        cands.append({"code": code, "name": str(q.get("name") or state.names.get(code, code)),
                      "price": q.get("price"), "pct": q.get("change_pct"),
                      "float_cap": round(fc / 1e8, 1) if fc is not None else None,
                      "rev_yoy": rev, "profit_pos": (prof is not None and prof > 0) or None,
                      "industry": f.get("industry", "")})
    picks, seen_cnt = [], {}
    for s in sorted(cands, key=lambda x: (not x["profit_pos"], -(x["rev_yoy"] if x["rev_yoy"] is not None else -999),
                                          -(x["pct"] if x["pct"] is not None else -999))):
        ind = s["industry"] or s["code"]
        if seen_cnt.get(ind, 0) >= per_ind:
            continue
        seen_cnt[ind] = seen_cnt.get(ind, 0) + 1
        picks.append(s)
        if len(picks) >= top_n:
            break
    return {"picks": picks, "total_candidates": len(cands),
            "filtered_by_cap": len(cands) < len(codes)}


def status() -> dict[str, Any]:
    cfg = load_config()
    with state._lock:
        return {
            "enabled": bool(cfg.scan.pattern_watch_enabled),
            "minutes": int(cfg.scan.pattern_poll_minutes),
            "high_days": int(cfg.scan.pattern_break_high_days),
            "vr_min": float(cfg.scan.pattern_break_vr),
            "cap": int(cfg.scan.pattern_max_alerts),
            "date": state.date,
            "prepared": state.prepared,
            "rev_candidates": len(state.rev_candidates),
            "day_alerts": state.day_alerts,
            "last_poll": state.last_poll,
            "last_new": state.last_new,
        }
