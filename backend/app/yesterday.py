"""昨日涨停跟进：统计昨日封板个股今日表现，按专业术语分类。

每日 10:30 / 14:40 生成简报，写入站内通知并发送邮件。
分类：连板 / 炸板 / 高开低走 / 低开低走 / 下跌 / 跌停 / 强势溢价 / 平开震荡。
板块同时标注「行业板块」（涨停池所属行业）与「市场板块」（主板/科创/创业/北交）。
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from . import datasource as ds, mainwave


class YesterdayState:
    def __init__(self) -> None:
        self.running = False
        self.last_scan: str | None = None
        self.trade_date: str | None = None          # 兼容旧字段：涨停基准日
        self.base_date: str | None = None            # 涨停基准日
        self.observe_date: str | None = None         # 表现观察日
        self.base_label: str | None = None
        self.observe_label: str | None = None
        self.market_label: str | None = None
        self.rows: list[dict[str, Any]] = []
        self.summary: dict[str, int] = {}
        self._lock = threading.RLock()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self.running,
                "last_scan": self.last_scan,
                "trade_date": self.trade_date,
                "base_date": self.base_date,
                "observe_date": self.observe_date,
                "base_label": self.base_label,
                "observe_label": self.observe_label,
                "market_label": self.market_label,
                "total": len(self.rows),
                "summary": dict(self.summary),
            }


state = YesterdayState()


def _market_board(code: str) -> str:
    c = str(code).zfill(6)
    if c.startswith(("688", "689")):
        return "科创板"
    if c.startswith(("300", "301")):
        return "创业板"
    if c.startswith(("43", "83", "87", "92", "8")):
        return "北交所"
    if c.startswith(("60", "00")):
        return "主板"
    return "其他"


def _num(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def classify(code: str, name: str, prev: dict[str, Any],
             quote: dict[str, Any]) -> tuple[str, str]:
    """返回 (术语分类, 补充说明)。"""
    pct = _num(quote.get("change_pct"))
    price = _num(quote.get("price"))
    high = _num(quote.get("high"))
    low = _num(quote.get("low"))
    op = _num(quote.get("open"))
    pre = _num(quote.get("pre_close")) or prev.get("limit_price")
    if pct is None or price is None or not pre:
        return "无行情", "行情接口暂不可用"

    threshold = mainwave._limit_up_threshold(code, name)
    limit_price = pre * (1 + threshold / 100)
    touched = bool(high and high >= limit_price * 0.999)
    sealed = bool(high and price >= high * 0.995 and pct >= threshold - 0.4)
    streak = int(prev.get("streak") or 1)

    if sealed:
        return "连板", f"昨日{streak}连板，今日继续封板（约{streak + 1}板）"
    if touched:
        return "炸板", f"盘中触及涨停价{limit_price:.2f}但未能封住"
    if pct <= -threshold + 0.4:
        return "跌停", "昨日涨停今日跌停，情绪反转"
    if op and pre and op < pre * 0.99 and pct < 0:
        return "低开低走", f"低开{(op / pre - 1) * 100:+.1f}%后继续下挫"
    if op and pre and op > pre * 1.01 and price < op:
        return "高开低走", f"高开{(op / pre - 1) * 100:+.1f}%后回落"
    if pct < 0:
        return "下跌", "平开或小幅高开后翻绿"
    if pct >= 3:
        return "强势溢价", "未封板但维持强势溢价"
    return "平开震荡", "涨跌幅有限，暂无方向"


def scan_yesterday() -> dict[str, Any]:
    """拉取涨停基准日涨停池 + 表现观察日行情，生成分类明细与统计。

    口径：今天看昨天 —— 取「涨停基准日」的涨停池，观察这些票在「表现观察日」的走势。
    交易日：周四涨停 → 周五表现；周末：周四涨停 → 周五收盘表现（行情接口反映最近交易日）。
    """
    with state._lock:
        if state.running:
            return {"success": False, "error": "昨日涨停数据正在更新，请稍后", **state.snapshot()}
        state.running = True
    try:
        pair = ds.followup_date_pair()
        base_date = pair["base_date"]
        observe_date = pair["observe_date"]
        pool = ds.limit_up_pool_detail(base_date)
        if not pool:
            return {"success": False, "error": f"{base_date} 涨停池数据不可用（数据源限流），稍后重试"}
        quotes = ds.spot_quote_map([p["code"] for p in pool])
        rows: list[dict[str, Any]] = []
        for p in pool:
            code = p["code"]
            q = quotes.get(code, {})
            category, note = classify(code, p["name"], p, q)
            pre = _num(q.get("pre_close")) or p.get("limit_price")
            op = _num(q.get("open"))
            rows.append({
                "code": code,
                "name": q.get("name") or p["name"],
                "industry": p.get("industry") or "--",
                "market_board": _market_board(code),
                "prev_streak": int(p.get("streak") or 1),
                "category": category,
                "note": note,
                "price": _num(q.get("price")),
                "change_pct": _num(q.get("change_pct")),
                "open_pct": round((op / pre - 1) * 100, 2) if op and pre else None,
                "high_pct": round(((_num(q.get("high")) or 0) / pre - 1) * 100, 2)
                if pre and q.get("high") else None,
                "low_pct": round(((_num(q.get("low")) or 0) / pre - 1) * 100, 2)
                if pre and q.get("low") else None,
                "volume_ratio": _num(q.get("volume_ratio")),
                "turnover": _num(q.get("turnover")),
                "amount": _num(q.get("amount")),
            })
        order = ["连板", "炸板", "跌停", "低开低走", "下跌", "高开低走", "强势溢价", "平开震荡", "无行情"]
        rows.sort(key=lambda x: (order.index(x["category"]) if x["category"] in order else 99,
                                 -(x["change_pct"] or 0)))
        summary: dict[str, int] = {}
        for r in rows:
            summary[r["category"]] = summary.get(r["category"], 0) + 1
        now = datetime.now().isoformat(timespec="seconds")
        with state._lock:
            state.rows = rows
            state.summary = summary
            state.last_scan = now
            state.trade_date = base_date  # 兼容旧字段
            state.base_date = base_date
            state.observe_date = observe_date
            state.base_label = pair["base_label"]
            state.observe_label = pair["observe_label"]
        market = ds.market_status()
        with state._lock:
            state.market_label = market["label"]
        return {
            "success": True, "time": now,
            "trade_date": base_date,  # 兼容旧字段
            "base_date": base_date, "observe_date": observe_date,
            "base_label": pair["base_label"], "observe_label": pair["observe_label"],
            "total": len(rows), "summary": summary, "rows": rows,
            "market_status": market["label"],
            "degraded": ds.cache.is_degraded("stock_spot"),
        }
    finally:
        with state._lock:
            state.running = False


def latest() -> list[dict[str, Any]]:
    with state._lock:
        return list(state.rows)


def latest_snapshot() -> dict[str, Any]:
    return state.snapshot()


def brief_lines() -> tuple[str, str, list[dict[str, Any]]]:
    """生成简报标题/正文/邮件表格行。"""
    with state._lock:
        rows = list(state.rows)
        summary = dict(state.summary)
        base_label = state.base_label or state.trade_date or "--"
        observe_label = state.observe_label or "--"
    parts = [f"{k} {v}只" for k, v in summary.items() if v]
    title = f"【昨日涨停跟进】{base_label} 封板股在 {observe_label} 的表现：" + (" / ".join(parts) or "无数据")
    body = (
        f"统计口径：取 {base_label} 涨停池共 {len(rows)} 只，观察其在 {observe_label} 的表现。\n"
        + ("\n".join(parts) or "暂无分类统计")
        + "\n红涨绿跌；行业板块来自涨停池，市场板块按代码前缀判定。"
    )
    mail_rows = [{
        "code": r["code"],
        "name": r["name"],
        "stage": "trigger" if r["category"] == "连板" else ("down" if r["change_pct"] and r["change_pct"] < 0 else ""),
        "stage_label": r["category"],
        "direction": "down" if (r["change_pct"] or 0) < 0 else "up",
        "price": r["price"] or "--",
        "change_pct": r["change_pct"] or 0,
        "volume_ratio": r["volume_ratio"],
        "turnover": r["turnover"],
        "net_inflow": None,
        "reasons": [r["industry"], r["market_board"], r["note"]],
    } for r in rows]
    return title, body, mail_rows
