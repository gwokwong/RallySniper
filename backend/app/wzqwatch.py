"""弱转强观察池：收盘后筛出当日「巨量炸板」名单，供次日竞价人工判断。

口径（量化野哥弱转强的「弱」——纯观察提醒，不构成买卖信号）：
- 炸板：盘中触及涨停价（high≥昨收×(1+阈值)×0.999）但收盘未封住
- 烂板：收盘距当日最高 ≥1.5%（收盘离板，非贴板秒封回落）
- 巨量：量比 ≥2.5（行情源无量比且降级时退化为不过滤量，标注降级）
- 范围：随市场筛选配置（主板/科创/创业、价格区间、剔ST）

用法：每日收盘后（默认 15:20）自动推送站内+邮件；次日竞价按三问人工判断：
① 前一日为什么走弱？② 共识预期的正常剧本是什么？③ 竞价有没有资金抢筹反转？
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from . import datasource as ds, mainwave, notifications, notifier
from .config import load_config


class WzqState:
    def __init__(self) -> None:
        self.running = False
        self.last_scan: str | None = None
        self.trade_date: str | None = None
        self.rows: list[dict[str, Any]] = []
        self.degraded: bool = False
        self._lock = threading.RLock()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"running": self.running, "last_scan": self.last_scan,
                    "trade_date": self.trade_date, "total": len(self.rows),
                    "degraded": self.degraded}


state = WzqState()


def _market_board(code: str) -> str:
    c = str(code).zfill(6)
    if c.startswith(("688", "689")):
        return "科创"
    if c.startswith(("300", "301")):
        return "创业"
    if c.startswith(("60", "00")):
        return "主板"
    return "其他"


def scan_watch_pool(cfg=None) -> dict[str, Any]:
    """收盘后扫描当日巨量炸板名单（快照行情=当日收盘口径）。"""
    cfg = cfg or load_config()
    with state._lock:
        if state.running:
            return {"success": False, "error": "弱转强观察池正在更新，请稍后", **state.snapshot()}
        state.running = True
    try:
        eff = ds.effective_trading_date()
        mfilter = cfg.market_filter
        quotes = ds.spot_quote_map()
        if not quotes:
            return {"success": False, "error": "行情快照不可用，稍后重试"}
        rows: list[dict[str, Any]] = []
        degraded_vr = ds.cache.is_degraded("stock_spot")
        for code, q in quotes.items():
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
            if price is None or (mfilter.max_price and price > mfilter.max_price):
                continue
            if mfilter.min_price and price < mfilter.min_price:
                continue
            pre = q.get("pre_close")
            high = q.get("high")
            pct = q.get("change_pct")
            vr = q.get("volume_ratio")
            if not pre or not high or pct is None:
                continue
            th = mainwave._limit_up_threshold(code, nm)
            limit_price = pre * (1 + th / 100)
            touched = high >= limit_price * 0.999
            sealed = price >= high * 0.995 and pct >= th - 0.4
            if not touched or sealed:
                continue
            if price > high * 0.985:   # 收盘贴板，非烂板
                continue
            # 量比过滤：有量比须≥2.5；无量比且数据源正常 → 无法确认巨量，排除；
            # 整体降级时放行并标注（宁多勿漏，人工判断）
            if vr is not None:
                if vr < 2.5:
                    continue
            elif not degraded_vr:
                continue
            rows.append({
                "code": code, "name": nm,
                "market_board": _market_board(code),
                "price": round(price, 2),
                "change_pct": round(pct, 2),
                "high_pct": round((high / pre - 1) * 100, 2),
                "close_off_high": round((price / high - 1) * 100, 2),
                "volume_ratio": round(vr, 2) if vr is not None else None,
            })
        rows.sort(key=lambda x: -(x["volume_ratio"] or 0))
        now = datetime.now().isoformat(timespec="seconds")
        with state._lock:
            state.rows = rows
            state.last_scan = now
            state.trade_date = eff["date"]
            state.degraded = degraded_vr
        return {"success": True, "time": now, "trade_date": eff["date"],
                "total": len(rows), "rows": rows,
                "degraded_vr": degraded_vr,
                "market_status": ds.market_status()["label"]}
    finally:
        with state._lock:
            state.running = False


def run_watch(push: bool = True) -> dict[str, Any]:
    """扫描 + 站内/邮件推送观察池。"""
    r = scan_watch_pool()
    if not r.get("success"):
        return r
    rows = r.get("rows", [])
    eff_label = ds._date_label(r["trade_date"])
    if not rows:
        if push:
            _notify(r, "【弱转强观察池】今日无巨量炸板标的",
                    f"{eff_label} 收盘扫描：无符合条件的巨量炸板标的（量比≥2.5 触板未封且收盘离板）。")
        return {**r, "pushed_channels": 0}
    parts = [f"{x['name']}({x['code']}) 量比{x['volume_ratio'] or '--'} 收{round(x['close_off_high'], 1)}%"
             for x in rows[:10]]
    more = f" 等 {len(rows)} 只" if len(rows) > 10 else ""
    title = f"【弱转强观察池】{eff_label} 巨量炸板 {len(rows)} 只，明日竞价关注"
    body = (
        f"统计口径：{eff_label} 盘中触板未封 + 收盘离板≥1.5% + 量比≥2.5（{'行情降级，量比未过滤' if r.get('degraded_vr') else '行情正常'}）\n"
        f"名单：{'、'.join(parts)}{more}\n"
        f"次日竞价三问（野哥弱转强框架，人工判断）：\n"
        f"  ① 昨日为何走弱——真实多空分歧，还是跟大盘回调？\n"
        f"  ② 按共识明天正常剧本是什么——预期弱到什么程度？\n"
        f"  ③ 竞价有无资金抢筹反转——本该兑现却砸不动了吗？\n"
        f"提醒：纯观察池，不构成买卖信号；仅列入符合弱转强前置条件的标的。"
    )
    pushed = 0
    notifications.add(title, body, [], kind="wzq")
    cfg = load_config()
    if push and cfg.notification:
        results = notifier.broadcast(cfg.notification, title, body, [])
        pushed = sum(1 for x in results if x["success"])
    return {**r, "pushed_channels": pushed}


def _notify(title: str, body: str) -> None:
    notifications.add(title, body, [], kind="wzq")
    cfg = load_config()
    if cfg.notification:
        notifier.broadcast(cfg.notification, title, body, [])


def status() -> dict[str, Any]:
    cfg = load_config()
    return {"enabled": bool(cfg.scan.wzq_watch_enabled),
            "time": cfg.scan.wzq_watch_time,
            **state.snapshot()}


def latest() -> list[dict[str, Any]]:
    with state._lock:
        return list(state.rows)
