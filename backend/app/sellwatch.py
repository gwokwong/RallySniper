"""持仓卖出风控：仅对「已标记持有」的股票做盘中提醒。

用户选定方案：天量破位 + 20日线确认破位，盘中预警 + 尾盘确认。
- 天量破位：行情源量比 ≥ 阈值（默认5倍）且阴线、跌破前低或MA5；
- 20日线：MA20 五日斜率向下 + 跌破超 1% 缓冲；盘中触碰先预警，
  尾盘（默认14:50）要求今日与昨日均有效跌破才发确认。
统计依据见对话中的多源回测汇总；单一信号不是高胜率保证，故按「预警/确认」分级。
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

import pandas as pd

from . import datasource as ds
from .config import effective_config, load_config


class SellState:
    def __init__(self) -> None:
        self.signals: list[dict[str, Any]] = []
        self.sent_keys: set[str] = set()
        self.sent_date: str = ""
        self.last_scan: str | None = None
        self.last_mode: str | None = None
        # 确认冷却：{code: (峰值价, 类型)}。同一波下跌确认过一次后，只要股价未收复峰值
        # （上涨超 cooldown_reset_pct 视为创新高、开启新一波），后续急跌只盘中预警，不再尾盘确认。
        self.confirm_peak: dict[str, tuple[float, str]] = {}
        self._lock = threading.RLock()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": False,
                "last_scan": self.last_scan,
                "last_mode": self.last_mode,
                "signal_count": len(self.signals),
            }


state = SellState()


def _f(v: Any) -> float | None:
    try:
        f = float(v)
        return None if pd.isna(f) else f
    except (TypeError, ValueError):
        return None


def _completed_history(code: str) -> pd.DataFrame | None:
    hist = ds.stock_history(code, days=130)
    if hist is None or hist.empty:
        return None
    eff = ds.effective_trading_date()
    dates = [str(x)[:10].replace("-", "") for x in hist["date"]]
    # 只有盘中当天尚未收盘才剔除最后一根；休市时最近交易日已是完整收盘数据
    if not eff["is_closed"] and dates and dates[-1] == eff["date"] and len(hist) > 1:
        return hist.iloc[:-1].copy()
    return hist


def _evaluate(code: str, name: str, quote: dict[str, Any],
              rules: dict[str, Any], mode: str,
              cost: float | None = None) -> list[dict[str, Any]]:
    hist = _completed_history(code)
    if hist is None or len(hist) < 25:
        return []
    closes = [float(x) for x in hist["close"].astype(float)]
    lows = [float(x) for x in hist["low"].astype(float)]
    highs = [float(x) for x in hist["high"].astype(float)]
    cur = _f(quote.get("price"))
    op = _f(quote.get("open"))
    pct = _f(quote.get("change_pct"))
    low_now = _f(quote.get("low")) or cur
    vr = _f(quote.get("volume_ratio"))
    if cur is None:
        return []
    closes.append(cur)
    s = pd.Series(closes)
    ma = s.rolling(20).mean()
    ma20 = float(ma.iloc[-1]) if pd.notna(ma.iloc[-1]) else 0.0
    ma20_prev = float(ma.iloc[-2]) if len(ma) >= 2 and pd.notna(ma.iloc[-2]) else 0.0
    ma5 = float(s.rolling(5).mean().iloc[-1])
    ma10 = float(s.rolling(10).mean().iloc[-1]) if len(s) >= 10 else 0.0
    prev_close = closes[-2]
    prev_low = lows[-1]
    slope = (ma20 - float(ma.iloc[-6])) / float(ma.iloc[-6]) if (
        len(ma) >= 6 and pd.notna(ma.iloc[-6]) and float(ma.iloc[-6]) > 0
    ) else 0.0
    buffer = float(rules.get("ma20_buffer", 0.01))
    slope_limit = float(rules.get("ma20_slope", -0.005))
    surge_limit = float(rules.get("volume_surge_ratio", 5.0))

    if vr is None:
        vols = [float(x) for x in hist["volume"].astype(float)[-5:]]
        qvol = _f(quote.get("volume"))
        base = sum(vols) / len(vols) if vols else 0
        # 降级源无量比字段时按「已过交易时长占比」折算全天量：
        # 盘中 quote.volume 只是当日累计量（如10:30仅约1/4），直接除全天基期会
        # 低估3~4倍，导致天量破位盘中漏报（2026-11-21 审计确认的缺口）。
        frac = 1.0
        if mode == "intraday":
            now = datetime.now()
            t = now.time()
            if t >= datetime.strptime("13:00", "%H:%M").time():
                mins = 120 + (now.hour * 60 + now.minute - 13 * 60)
            elif t >= datetime.strptime("9:30", "%H:%M").time():
                mins = min(120, max(0, now.hour * 60 + now.minute - 9 * 60 - 30))
            else:
                mins = 0
            frac = max(0.15, min(1.0, mins / 240))
        vr = (qvol / (base * frac)) if (qvol and base) else 0.0

    out: list[dict[str, Any]] = []
    eff = ds.effective_trading_date()
    common = {
        "code": code, "name": name, "price": round(cur, 2),
        "change_pct": round(pct or 0, 2), "volume_ratio": round(vr, 2),
        # 信号日期按数据交易日，休市时不再是自然日
        "date": f"{eff['date'][:4]}-{eff['date'][4:6]}-{eff['date'][6:]}",
        "time": datetime.now().strftime("%H:%M"),
    }

    # 1) 天量破位：量比达标 + 阴线 + 破前低或MA5
    is_yin = bool((op and cur < op) or (pct is not None and pct < 0))
    broke = bool(cur < prev_low or (ma5 > 0 and cur < ma5))
    surge_hit = (vr >= surge_limit and is_yin and
                 (broke or (mode == "intraday" and (pct or 0) <= -1.5)))
    if surge_hit:
        reasons = [
            f"量比{vr:.1f}倍达天量阈值{surge_limit:g}",
            f"现价{cur:.2f}低于前低{prev_low:.2f}" if cur < prev_low else f"现价跌破MA5({ma5:.2f})",
            f"当日{(pct or 0):+.2f}%",
        ]
        if mode == "confirm":
            reasons.append("尾盘按近收盘价确认")
        else:
            reasons.append("盘中预警，尾盘再确认")
        out.append({**common, "strategy": "天量破位",
                    "level": "确认" if mode == "confirm" else "预警",
                    "reasons": reasons})

    # 2) 20日线：盘中触碰预警；尾盘两条通道取并集
    #    严格通道：今日+昨日均有效跌破且五日斜率向下（适合趋势走坏）；
    #    快速通道：单日有效跌破 MA20 超 fast_break_pct 且收阴线（适合快速杀跌，不必等斜率转负）。
    ma20_hit = ma20 > 0 and cur < ma20 * (1 - buffer)
    ma20_prev_hit = ma20_prev > 0 and prev_close < ma20_prev * (1 - buffer)
    strict_break = bool(ma20_hit and ma20_prev_hit and slope <= slope_limit)
    fast_enabled = bool(rules.get("fast_break_enabled", True))
    fast_pct = float(rules.get("fast_break_pct", 0.02))
    fast_break = bool(fast_enabled and ma20 > 0 and cur < ma20 * (1 - fast_pct) and is_yin)
    if mode == "confirm":
        ma_hit = strict_break or fast_break
    else:
        ma_hit = bool((ma20 > 0 and (cur < ma20 or low_now < ma20 * 0.995)) or ma20_hit)
    if ma_hit:
        reasons = [
            f"MA20={ma20:.2f}，现价{cur:.2f}" + (f"（跌破{buffer * 100:g}%缓冲）" if ma20_hit else "（盘中触碰）"),
            f"MA20五日斜率{slope * 100:+.2f}%",
        ]
        if mode == "confirm":
            if fast_break and not strict_break:
                reasons += [f"快速破位：单日跌破MA20超{fast_pct * 100:g}%且收阴", "尾盘确认，按纪律执行"]
            else:
                reasons += ["昨日亦有效跌破MA20", "尾盘确认，按纪律执行"]
        else:
            reasons.append("盘中预警，需尾盘确认")
        out.append({**common, "strategy": "20日线破位",
                    "level": "确认" if mode == "confirm" else "预警",
                    "reasons": reasons})

    # 3) 双通道：盘中急跌预警 + 尾盘破MA10确认
    #    背景：连板股急跌时 MA20 远在下方，等收盘破 MA20 已滞后2~3日、亏损扩大。
    #    盘中：当日跌幅≥rapid_drop_pct 立即预警（不等均线破位）；
    #    尾盘：① 急跌确认——当日跌幅≥rapid_drop_pct且收阴（急跌本身即信号，不必等MA10）；
    #          ② 破MA10确认——收盘破MA10且当日跌幅≥ma10_break_drop_pct且收阴。
    rapid_enabled = bool(rules.get("rapid_drop_enabled", True))
    rapid_pct = float(rules.get("rapid_drop_pct", 0.08))
    ma10_enabled = bool(rules.get("ma10_break_enabled", True))
    ma10_drop = float(rules.get("ma10_break_drop_pct", 0.05))
    drop = abs(pct) if pct is not None else 0.0

    # 浮盈分档（2026-11-21）：厚利票放宽急跌阈值 + 10日线移动止盈，让主升浪跑完。
    # 基准档：急跌8%、缩量跌停9.5%；厚利档（浮盈≥15%）：急跌12%、缩量11.5%、破MA10即确认。
    profit_pct = ((cur - cost) / cost * 100) if (cost and cost > 0) else 0.0
    tier_on = bool(rules.get("tier_enabled", True))
    thick = bool(tier_on and cost and cost > 0 and profit_pct >= float(rules.get("tier_profit_pct", 0.30)) * 100)
    if thick:
        rapid_pct = float(rules.get("tier_rapid_pct", 0.12))
        shrink_drop = float(rules.get("tier_shrink_drop", 0.115))
        trail_ma10 = bool(rules.get("tier_ma10_trail", True))
    else:
        shrink_drop = float(rules.get("shrink_limit_drop", 0.095))
        trail_ma10 = False

    if mode == "intraday":
        # 盘中急跌：跌幅达标即预警，与均线是否破位无关
        if rapid_enabled and drop >= rapid_pct * 100 and is_yin:
            out.append({**common, "strategy": "急跌破位",
                        "level": "预警",
                        "reasons": [
                            f"盘中急跌{(pct or 0):+.2f}%，达急跌阈值{rapid_pct * 100:g}%",
                            f"现价{cur:.2f}，MA10={ma10:.2f}，MA20={ma20:.2f}",
                            "盘中预警，尾盘再确认",
                        ]})
    else:  # confirm
        # 急跌确认收紧（用户 2026-11-21 决策）：放量急跌多为强势票洗盘，缩量跌停才是趋势末端。
        # 原「单日急跌≥8%且收阴即确认」对金健9/15、万向9/3/9/10/9/24等洗盘日误报，改为：
        #   缩量跌停通道（量比≤shrink_limit_ratio 且跌幅≥shrink_limit_drop 且收阴）或 破MA10确认。
        # 盘中 intraday 急跌预警保持原样（提前提示照发），只收紧尾盘确认。
        shrink_ratio = float(rules.get("shrink_limit_ratio", 0.5))
        shrink_limit_hit = bool(rapid_enabled and vr <= shrink_ratio
                                and drop >= shrink_drop * 100 and is_yin)
        ma10_confirm = bool(ma10_enabled and ma10 > 0 and cur < ma10
                            and drop >= ma10_drop * 100 and is_yin)
        # 厚利档移动止盈：浮盈≥15%时，收盘跌破MA10且收阴即确认，不等急跌8%。
        # 目的是让会稽山/万向这类主升浪在均线走坏时离场，而非被单日急跌洗出。
        trail_confirm = bool(thick and trail_ma10 and ma10 > 0 and cur < ma10 and is_yin)
        if shrink_limit_hit or ma10_confirm or trail_confirm:
            reasons = [f"尾盘{(pct or 0):+.2f}%，现价{cur:.2f}"]
            if trail_confirm and not shrink_limit_hit and not ma10_confirm:
                reasons.append(f"厚利档移动止盈：浮盈{profit_pct:.1f}%≥{float(rules.get('tier_profit_pct', 0.30)) * 100:g}%，"
                               f"收盘跌破MA10({ma10:.2f})")
            elif ma10_confirm:
                reasons.append(f"收盘跌破MA10({ma10:.2f})，跌幅达{ma10_drop * 100:g}%")
            else:
                reasons.append(f"缩量跌停：量比{vr:.2f}≤{shrink_ratio:g}、跌幅达{shrink_drop * 100:g}%，买盘枯竭非洗盘")
            reasons.append("尾盘确认，按纪律执行")
            # peak=本轮下跌前的近20日高点，供冷却期判断股价是否收复/创新高
            peak = max(highs[-21:-1]) if len(highs) >= 21 else (max(highs) if highs else cur)
            out.append({**common, "strategy": "急跌破位",
                        "level": "确认", "reasons": reasons, "peak": round(peak, 2)})
    # 4) MA10连续破位（第4通道）：连续N天收盘跌破MA10即确认卖出。
    #    比 MA20 三通道更快，比单日急跌更稳——两根K线确认趋势而非噪音。
    #    与 ma10_confirm（单日大跌破MA10）不同：不需要跌幅门槛，纯粹趋势跟随。
    ma10_consec_on = bool(rules.get("ma10_consec_enabled", True))
    ma10_consec_n = max(2, int(rules.get("ma10_consec_days", 2)))
    if ma10_consec_on and ma10 > 0:
        consec = 0
        for j in range(len(closes) - 1, max(0, len(closes) - ma10_consec_n - 2), -1):
            ma10_j = float(pd.Series(closes[:j + 1]).rolling(10).mean().iloc[-1]) if j >= 9 else None
            if ma10_j and closes[j] < ma10_j:
                consec += 1
            else:
                break
        if consec >= ma10_consec_n:
            reasons = [f"连续{consec}天收盘跌破MA10({ma10:.2f})，趋势转弱"]
            reasons.append("尾盘确认，按纪律执行")
            out.append({**common, "strategy": "MA10连续破位",
                        "level": "确认" if mode == "confirm" else "预警",
                        "reasons": reasons})

    return out


def scan(mode: str = "intraday") -> dict[str, Any]:
    """mode=intraday 盘中预警；mode=confirm 尾盘确认。"""
    cfg = effective_config()
    rules = cfg.sell.model_dump()
    holdings = [_normalize(c) for c in cfg.holdings]
    holdings = [c for c in holdings if len(c) == 6 and c.isdigit()]
    result: dict[str, Any] = {
        "success": True, "mode": mode, "holdings": len(holdings),
        "new_count": 0, "signals": [], "enabled": cfg.sell.enabled,
        "run_mode": cfg.run_mode,
    }
    if not cfg.sell.enabled or not holdings:
        result["message"] = "未开启卖出提醒或尚未标记持有股票"
        return result
    quotes = ds.spot_quote_map(holdings)
    names = ds.stock_code_names()
    # 去重键按数据交易日：休市多次扫描仍指向同一交易日，不会重复刷屏
    today = ds.effective_trading_date()["date"]
    found: list[dict[str, Any]] = []
    cooldown_on = bool(cfg.sell.cooldown_enabled)
    reset_pct = float(cfg.sell.cooldown_reset_pct)
    for code in holdings:
        q = quotes.get(code)
        if not q or not q.get("price"):
            continue
        # 每日检查冷却重置：只要现价较上次确认价上涨超 reset_pct（含盘中上涨日，
        # 即使当天没有确认信号），说明股价已反弹开启新一波，清除旧冷却。
        if cooldown_on and mode == "confirm" and code in state.confirm_peak:
            _p, base_price = state.confirm_peak[code]
            if float(q.get("price") or 0) > base_price * (1 + reset_pct):
                state.confirm_peak.pop(code, None)
        try:
            cost = cfg.holding_costs.get(code)
            found.extend(_evaluate(code, names.get(code, q.get("name", code)), q, rules, mode, cost=cost))
        except Exception:  # noqa: BLE001
            continue


    with state._lock:
        if state.sent_date != today:
            state.sent_keys.clear()
            state.sent_date = today
        fresh = []
        for sig in found:
            key = f"{today}:{mode}:{sig['code']}:{sig['strategy']}"
            if key in state.sent_keys:
                continue
            # 冷却期：尾盘确认信号若本票已确认过、且现价未收复下跌前峰值（即同一波下跌），
            # 只保留盘中预警、不再重复发尾盘确认（避免连续多日 2 站内+2 邮件轰炸）。
            # 股价较确认价上涨超 reset_pct 视为创新高/新一波，清除冷却允许再次确认。
            if mode == "confirm" and cooldown_on and sig.get("level") == "确认":
                code = sig["code"]
                prev = state.confirm_peak.get(code)
                price = float(sig.get("price") or 0)
                if prev is not None:
                    # 现价已在上方的每日检查里重置过；走到这里说明未创新高、同一波下跌 → 冷却跳过。
                    # 例：会稽山9/29确认37.66，10/8-10/9反弹至40.28(>38.79)已清除冷却，
                    # 故10/12(36.25)属新一波、放行；而10/13/10/14连续下跌不再重复发。
                    continue
                state.confirm_peak[code] = (float(sig.get("peak") or price), price)
            state.sent_keys.add(key)
            fresh.append(sig)
        state.signals = (fresh + state.signals)[:100]
        state.last_scan = datetime.now().isoformat(timespec="seconds")
        state.last_mode = mode
        result["new_count"] = len(fresh)
        result["signals"] = fresh
        result["degraded"] = ds.cache.is_degraded("stock_spot")
    return result


def _normalize(value: str) -> str:
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else digits.zfill(6) if digits else ""


def latest() -> list[dict[str, Any]]:
    with state._lock:
        return list(state.signals)


def status() -> dict[str, Any]:
    with state._lock:
        snapshot = state.snapshot()
    cfg = effective_config()
    return {**snapshot, "enabled": cfg.sell.enabled, "holdings": list(cfg.holdings),
            "rules": cfg.sell.model_dump(), "run_mode": cfg.run_mode,
            "times": ["盘中每5分钟", cfg.sell.confirm_time]}
