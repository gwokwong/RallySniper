"""主升浪捕手后端入口。

启动：uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import math
import threading
from contextlib import asynccontextmanager
import sys

# 服务器 locale 常为 POSIX/ASCII：中文 print（含异常消息）会触发 UnicodeEncodeError
# 直接把进程搞崩（2026-11-23 部署实测）。启动时强制 stdout/stderr 走 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

from pathlib import Path
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import (
    analyzer, datasource as ds, ignitionwatch, lanban, mainwave, monitor, market_scan, patternwatch, replay, tdxwatch, wzqwatch,
    notifications, notifier as notifier_mod, scheduler, sellwatch, yesterday,
)
from .config import AppConfig, load_config, save_config, update_config
from .notifier import broadcast


def _warm_market_cache() -> None:
    """后台预热全市场行情/资金流缓存，避免前端首屏冷启动请求超时。

    数据源被限流时降级链可能耗时 10~20s，若在请求线程里同步拉取会超过
    前端 15s 超时；启动时预热一次，页面打开即可命中缓存快速返回。
    """
    for label, fn in (
        ("实时行情", ds.stock_spot),
        ("个股资金流", ds.individual_fund_flow),
    ):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            print(f"[预热] {label}失败：{str(exc)[:80]}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_config()
    if cfg.scan.enabled:
        monitor.start_monitor()
    # 回放K线索引后台预热（约数千个CSV，避免首个用户请求同步承担全量加载）
    try:
        import threading as _th
        _th.Thread(target=replay._load_bars, kwargs={"force": True}, daemon=True).start()
    except Exception as exc:  # noqa: BLE001
        print(f"[启动] 回放缓存预热失败：{exc}")
    scheduler.start()  # 全市场 14:00 / 14:40 定时推送
    threading.Thread(target=_warm_market_cache, name="warm-market-cache", daemon=True).start()
    yield
    monitor.stop_monitor()
    scheduler.stop()


app = FastAPI(title="主升浪捕手 API", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def ok(data: Any, msg: str = "success") -> dict[str, Any]:
    """兼容 Art Design Pro 的响应结构"""
    return {"code": 200, "msg": msg, "data": data}


# ----------------------------- 基础 -----------------------------

@app.get("/api/health")
def health() -> dict[str, Any]:
    return ok({
        "status": "ok",
        "time": datetime.now().isoformat(timespec="seconds"),
        "datasource": ds.health(),
        "monitor": monitor.status(),
    })


@app.get("/api/market/status")
def market_status() -> dict[str, Any]:
    return ok(ds.market_status())


# ----------------------------- 鉴权兼容（本地工具，免密） -----------------------------

@app.post("/api/auth/login")
def login() -> dict[str, Any]:
    return ok({"token": "local-monitor-token", "refreshToken": "local-monitor-refresh"})


@app.get("/api/user/info")
def user_info() -> dict[str, Any]:
    return ok({
        "buttons": [],
        "roles": ["R_SUPER"],
        "userId": 1,
        "userName": "主升浪捕手",
        "email": "local@monitor",
    })


# ----------------------------- 主升浪（核心） -----------------------------

@app.get("/api/mainwave/overview")
def mainwave_overview() -> dict[str, Any]:
    """总览：标的池规模 + 战法命中统计 + Top 信号"""
    cfg = load_config()
    enabled = {**cfg.strategies.as_dict(), **cfg.bear_strategies.as_dict()}
    signals = mainwave.scan_watchlist(
        cfg.watchlist, cfg.strategy_rules.model_dump(), enabled
    )
    by_strategy: dict[str, int] = {}
    for s in signals:
        by_strategy[s["strategy"]] = by_strategy.get(s["strategy"], 0) + 1
    up_n = sum(1 for s in signals if s.get("direction") == "up")
    down_n = sum(1 for s in signals if s.get("direction") == "down")
    return ok({
        "market": ds.market_status(),
        "watchlist_size": len(cfg.watchlist),
        "enabled_strategies": [k for k, v in cfg.strategies.as_dict().items() if v],
        "enabled_bear_strategies": [k for k, v in cfg.bear_strategies.as_dict().items() if v],
        "hit_count": len(signals),
        "up_count": up_n,
        "down_count": down_n,
        "by_strategy": by_strategy,
        "top_signals": signals[:12],
        "monitor": monitor.status(),
    })


@app.get("/api/mainwave/scan")
def mainwave_scan(refresh: bool = False) -> dict[str, Any]:
    """扫描标的池主升浪信号。refresh=true 强制刷新历史数据缓存。"""
    if refresh:
        ds.cache.clear()  # 清空缓存以拉取最新K线
    cfg = load_config()
    enabled = {**cfg.strategies.as_dict(), **cfg.bear_strategies.as_dict()}
    signals = mainwave.scan_watchlist(
        cfg.watchlist, cfg.strategy_rules.model_dump(), enabled
    )
    return ok({
        "signals": signals,
        "scanned": len(cfg.watchlist),
        "time": datetime.now().isoformat(timespec="seconds"),
    })


@app.get("/api/mainwave/stock/{code}")
def mainwave_stock(code: str) -> dict[str, Any]:
    """单只股票的全部战法命中详情（上涨+下跌）。"""
    cfg = load_config()
    signals = mainwave.analyze_stock(code, cfg.strategy_rules.model_dump())
    return ok({"code": code, "signals": signals})


@app.get("/api/mainwave/strategies")
def mainwave_strategies() -> dict[str, Any]:
    """战法说明清单 + 当前开关状态（上涨+下跌，带 direction）。"""
    cfg = load_config()
    items = mainwave.strategy_catalog()
    switches = {**cfg.strategies.as_dict(), **cfg.bear_strategies.as_dict()}
    for it in items:
        it["enabled"] = switches.get(it["key"], False)
    return ok(items)


# ----------------------------- 图表数据 -----------------------------

def _chart_num(value: Any, digits: int = 2) -> float | None:
    """把 DataFrame 里的数值转成可 JSON 序列化的数字，NaN 转 None。"""
    try:
        f = float(value)
        return None if math.isnan(f) else round(f, digits)
    except (TypeError, ValueError):
        return None


@app.get("/api/stock/kline/{code}")
def stock_kline(code: str, days: int = 120) -> dict[str, Any]:
    """个股日K线 + MA5/MA10/MA20（供标的池详情图表使用）。"""
    code = str(code).strip().zfill(6)
    hist = ds.stock_history(code, days=max(60, min(days, 250)))
    name = ds.stock_code_names().get(code, code)
    if hist is None or hist.empty:
        return ok({"code": code, "name": name, "rows": []})
    d = mainwave._indicators(hist, code, name)
    # 量比：当日成交量 / 前5日均量（收盘后与标准量比一致）
    vol_ratio = d["volume"].astype(float) / d["volume"].astype(float).shift(1).rolling(5).mean()
    turnover_series = d["turnover"] if "turnover" in d.columns else None
    cols = ["date", "open", "high", "low", "close", "volume", "ma5", "ma10", "ma20"]
    rows = []
    for idx, r in d[cols].iterrows():
        rows.append({
            "date": str(r["date"]),
            "open": _chart_num(r["open"]),
            "high": _chart_num(r["high"]),
            "low": _chart_num(r["low"]),
            "close": _chart_num(r["close"]),
            "volume": _chart_num(r["volume"], 0),
            "turnover": _chart_num(turnover_series.loc[idx]) if turnover_series is not None else None,
            "volume_ratio": _chart_num(vol_ratio.loc[idx]),
            "ma5": _chart_num(r["ma5"]),
            "ma10": _chart_num(r["ma10"]),
            "ma20": _chart_num(r["ma20"]),
        })
    return ok({"code": code, "name": name, "rows": rows})


@app.get("/api/stock/intraday/{code}")
def stock_intraday(code: str, period: str = "5") -> dict[str, Any]:
    """个股日内分钟K线（新浪优先，东财降级）。"""
    code = str(code).strip().zfill(6)
    df = ds.stock_intraday_minutes(code, period)
    rows: list[dict[str, Any]] = []
    if df is not None and not df.empty:
        for _, r in df.iterrows():
            rows.append({
                "time": str(r["time"]),
                "open": _chart_num(r["open"]),
                "high": _chart_num(r["high"]),
                "low": _chart_num(r["low"]),
                "close": _chart_num(r["close"]),
                "volume": _chart_num(r["volume"], 0),
            })
    return ok({
        "code": code,
        "period": period,
        "date": str(df.iloc[-1]["date"]) if rows and df is not None else None,
        "source": "sina/em" if rows else "unavailable",
        "rows": rows,
    })


# ----------------------------- 保留的展示型数据接口 -----------------------------

@app.get("/api/monitor/overview")
def overview() -> dict[str, Any]:
    """兼容旧前端：复用主升浪总览。"""
    return mainwave_overview()


@app.get("/api/monitor/stock-anomalies")
def stock_anomalies() -> dict[str, Any]:
    cfg = load_config()
    return ok(analyzer.stock_anomalies(cfg.rules, cfg.watchlist))


@app.get("/api/monitor/fund-anomalies")
def fund_anomalies() -> dict[str, Any]:
    cfg = load_config()
    return ok(analyzer.fund_flow_anomalies(cfg.rules, cfg.watchlist))


@app.get("/api/monitor/stock-changes")
def stock_changes(limit: int = 50) -> dict[str, Any]:
    return ok(analyzer.intraday_changes(limit))


@app.get("/api/monitor/limit-up")
def limit_up(date: str | None = None) -> dict[str, Any]:
    return ok(analyzer.limit_up_stocks(date))


@app.get("/api/monitor/sector-anomalies")
def sector_anomalies() -> dict[str, Any]:
    cfg = load_config()
    return ok(analyzer.sector_anomalies(cfg.rules))


@app.get("/api/monitor/sectors")
def sectors(board_type: str = "行业") -> dict[str, Any]:
    cfg = load_config()
    return ok(analyzer.sector_board_overview(cfg.rules, board_type))


@app.get("/api/monitor/dragon-tiger")
def dragon_tiger(start_date: str | None = None, end_date: str | None = None) -> dict[str, Any]:
    cfg = load_config()
    return ok(analyzer.dragon_tiger(cfg.rules, start_date, end_date))


@app.get("/api/monitor/watchlist")
def get_watchlist() -> dict[str, Any]:
    cfg = load_config()
    df = ds.stock_spot()
    quotes: dict[str, dict[str, Any]] = {}
    if df is not None and not df.empty:
        import pandas as pd
        code_col = next((c for c in ("代码", "股票代码", "symbol") if c in df.columns), None)
        if code_col:
            for _, row in df[df[code_col].astype(str).isin(cfg.watchlist)].iterrows():
                quotes[str(row[code_col])] = {
                    k: (None if pd.isna(v) else v) for k, v in row.items()
                }
    return ok({"codes": cfg.watchlist, "quotes": quotes})


# ----------------------------- 标的池（自选股式管理） -----------------------------

def _normalize_code(value: str) -> str:
    """从用户输入中提取 6 位 A 股代码，兼容 sh600519 / 600519.SH 等写法。"""
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else digits.zfill(6) if digits else ""


def _watchlist_rows() -> dict[str, Any]:
    """标的池明细：行情 + 当前战法信号（行情失败时价格留空，不阻断页面）。"""
    cfg = load_config()
    codes = [_normalize_code(c) for c in cfg.watchlist]
    codes = list(dict.fromkeys(c for c in codes if len(c) == 6))
    enabled = {**cfg.strategies.as_dict(), **cfg.bear_strategies.as_dict()}
    signals = mainwave.scan_watchlist(
        codes, cfg.strategy_rules.model_dump(), enabled
    ) if codes else []
    by_code: dict[str, list[dict[str, Any]]] = {}
    for s in signals:
        by_code.setdefault(s["code"], []).append(s)

    names = ds.stock_code_names()
    rows: list[dict[str, Any]] = []
    for code in codes:
        price: float | None = None
        change: float | None = None
        turnover: float | None = None
        volume_ratio: float | None = None
        date = ""
        hist = ds.stock_history(code)
        if hist is not None and not hist.empty:
            cur = hist.iloc[-1]
            try:
                price = round(float(cur["close"]), 2)
                change = round(float(cur["pct_change"]), 2)
                date = str(cur["date"])
            except (TypeError, ValueError, KeyError):
                pass
            if "turnover" in hist.columns:
                turnover = _chart_num(cur["turnover"])
            if "volume" in hist.columns and len(hist) >= 6:
                vols = hist["volume"].astype(float)
                base = vols.iloc[-6:-1].mean()
                if base > 0:
                    volume_ratio = _chart_num(vols.iloc[-1] / base)
        sigs = by_code.get(code, [])
        rows.append({
            "code": code,
            "name": names.get(code, code),
            "price": price,
            "change_pct": change,
            "turnover": turnover,
            "volume_ratio": volume_ratio,
            "date": date,
            "signals": [{
                "strategy": s["strategy"],
                "direction": s["direction"],
                "strength": s["strength"],
            } for s in sigs],
            "up_count": sum(1 for s in sigs if s.get("direction") == "up"),
            "down_count": sum(1 for s in sigs if s.get("direction") == "down"),
        })
    return {
        "rows": rows,
        "total": len(rows),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


@app.get("/api/watchlist")
def watchlist_detail() -> dict[str, Any]:
    return ok(_watchlist_rows())


@app.get("/api/watchlist/search")
def watchlist_search(q: str = "", limit: int = 12) -> dict[str, Any]:
    """本地代码/名称模糊搜索，不依赖实时行情接口。"""
    needle = q.strip()
    if not needle:
        return ok([])
    upper = needle.upper()
    items = [
        {"code": code, "name": name}
        for code, name in ds.stock_code_names().items()
        if needle in code or needle in name or upper in name.upper()
    ]
    return ok(items[:max(1, min(limit, 50))])


class WatchlistAddRequest(BaseModel):
    code: str
    name: str | None = None
    cost: float | None = None  # 持仓成本价（标记持有时可选录入）


class HoldingCostRequest(BaseModel):
    cost: float | None = None


@app.post("/api/watchlist")
def watchlist_add(req: WatchlistAddRequest) -> dict[str, Any]:
    code = _normalize_code(req.code)
    if len(code) != 6 or not code.isdigit():
        raise HTTPException(status_code=400, detail="请输入有效的6位股票代码")
    cfg = load_config()
    if code in cfg.watchlist:
        return ok({"codes": cfg.watchlist, "code": code}, "该股票已在标的池中")
    names = ds.stock_code_names()
    if names and code not in names:
        raise HTTPException(status_code=400, detail="未找到该股票代码，请检查后重试")
    cfg.watchlist.append(code)
    save_config(cfg)
    return ok(
        {"codes": cfg.watchlist, "code": code, "name": names.get(code, req.name or code)},
        "已加入标的池",
    )


@app.get("/api/watchlist/codes")
def watchlist_codes() -> dict[str, Any]:
    """轻量查询标的池代码列表（机会页快速加池用，不触发行情扫描）。"""
    return ok({"codes": list(load_config().watchlist)})


class WatchlistBatchRequest(BaseModel):
    codes: list[str]


@app.post("/api/watchlist/batch")
def watchlist_add_batch(req: WatchlistBatchRequest) -> dict[str, Any]:
    """批量加入标的池：已是标的的自动跳过，不报错。"""
    cfg = load_config()
    existing = set(cfg.watchlist)
    added: list[str] = []
    skipped: list[str] = []
    invalid: list[str] = []
    for raw in req.codes:
        code = _normalize_code(raw)
        if len(code) != 6 or not code.isdigit():
            invalid.append(str(raw))
            continue
        if code in existing:
            skipped.append(code)
            continue
        cfg.watchlist.append(code)
        existing.add(code)
        added.append(code)
    if added:
        save_config(cfg)
    return ok(
        {"codes": list(cfg.watchlist), "added": added, "skipped": skipped, "invalid": invalid},
        f"已加入 {len(added)} 只，已存在 {len(skipped)} 只",
    )


@app.delete("/api/watchlist/{code}")
def watchlist_remove(code: str) -> dict[str, Any]:
    code = _normalize_code(code)
    cfg = load_config()
    if code not in cfg.watchlist:
        return ok({"codes": cfg.watchlist}, "该股票不在标的池中")
    cfg.watchlist = [c for c in cfg.watchlist if c != code]
    save_config(cfg)
    return ok({"codes": cfg.watchlist, "code": code}, "已从标的池移除")


class WatchlistOrderRequest(BaseModel):
    codes: list[str]


@app.put("/api/watchlist/order")
def watchlist_reorder(req: WatchlistOrderRequest) -> dict[str, Any]:
    cfg = load_config()
    original = list(cfg.watchlist)
    ordered: list[str] = []
    for raw in req.codes:
        code = _normalize_code(raw)
        if code in original and code not in ordered:
            ordered.append(code)
    ordered.extend(c for c in original if c not in ordered)
    cfg.watchlist = ordered
    save_config(cfg)
    return ok({"codes": ordered}, "顺序已保存")


# ----------------------------- 持有标记 -----------------------------

@app.get("/api/holdings")
def holdings_detail() -> dict[str, Any]:
    cfg = load_config()
    if not cfg.holdings:
        return ok({"rows": [], "codes": []})
    quotes = ds.spot_quote_map(cfg.holdings)
    names = ds.stock_code_names()
    rows = []
    for code in cfg.holdings:
        q = quotes.get(code, {})
        cost = cfg.holding_costs.get(code)
        price = q.get("price")
        profit_pct = round((price - cost) / cost * 100, 2) if (cost and price and cost > 0) else None
        rows.append({
            "code": code,
            "name": names.get(code, q.get("name", code)),
            "price": price,
            "cost": cost,
            "profit_pct": profit_pct,
            "change_pct": q.get("change_pct"),
            "volume_ratio": q.get("volume_ratio"),
        })
    return ok({"rows": rows, "codes": list(cfg.holdings), "costs": cfg.holding_costs})


@app.post("/api/holdings")
def holdings_add(req: WatchlistAddRequest) -> dict[str, Any]:
    code = _normalize_code(req.code)
    if len(code) != 6 or not code.isdigit():
        raise HTTPException(status_code=400, detail="请输入有效的6位股票代码")
    cfg = load_config()
    if code not in cfg.watchlist:
        cfg.watchlist.append(code)
    if code not in cfg.holdings:
        cfg.holdings.append(code)
    if req.cost is not None and req.cost > 0:
        cfg.holding_costs[code] = round(float(req.cost), 3)
    save_config(cfg)
    return ok({"codes": list(cfg.holdings), "costs": cfg.holding_costs},
              "已标记为持有" + (f"，成本价{req.cost:g}" if req.cost else ""))


@app.put("/api/holdings/{code}/cost")
def holdings_cost(code: str, req: HoldingCostRequest) -> dict[str, Any]:
    """补录/修改持仓成本价，供浮盈分档止损使用。"""
    code = _normalize_code(code)
    cfg = load_config()
    if code not in cfg.holdings:
        raise HTTPException(status_code=400, detail="该股票未标记为持有")
    if req.cost is None or req.cost <= 0:
        cfg.holding_costs.pop(code, None)
    else:
        cfg.holding_costs[code] = round(float(req.cost), 3)
    save_config(cfg)
    return ok({"costs": cfg.holding_costs}, "成本价已更新")


@app.delete("/api/holdings/{code}")
def holdings_remove(code: str) -> dict[str, Any]:
    code = _normalize_code(code)
    cfg = load_config()
    cfg.holdings = [c for c in cfg.holdings if c != code]
    cfg.holding_costs.pop(code, None)
    save_config(cfg)
    return ok({"codes": list(cfg.holdings)}, "已取消持有标记")


# ----------------------------- 配置 -----------------------------

@app.get("/api/config")
def get_config() -> dict[str, Any]:
    return ok(load_config().model_dump())


@app.put("/api/config")
def put_config(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        cfg = update_config(payload)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"配置校验失败: {exc}") from exc
    if cfg.scan.enabled:
        monitor.start_monitor()
    else:
        monitor.stop_monitor()
    scheduler.refresh()
    return ok(cfg.model_dump(), "配置已保存")


@app.post("/api/config/reset")
def reset_config() -> dict[str, Any]:
    from .config import default_config
    cfg = default_config()
    save_config(cfg)
    monitor.stop_monitor()
    return ok(cfg.model_dump(), "已恢复默认配置")


# ----------------------------- 通知（仅邮件） -----------------------------

class NotifyTestRequest(BaseModel):
    channel_id: str | None = None
    message: str = "这是一条来自主升浪捕手的测试邮件"


@app.post("/api/notify/test")
def notify_test(req: NotifyTestRequest) -> dict[str, Any]:
    cfg = load_config()
    channels = [c for c in cfg.notification if c.enabled and (not req.channel_id or c.id == req.channel_id)]
    if not channels:
        return ok({"results": []}, "没有启用的邮件渠道，请先在配置页填写并启用")
    results = broadcast(channels, "【主升浪捕手】邮件测试", req.message, [])
    return ok({"results": results})


@app.post("/api/monitor/scan")
def manual_scan() -> dict[str, Any]:
    """手动触发一次扫描（按当前规则推送邮件）"""
    return ok(monitor.scan_once(push=True))


@app.post("/api/monitor/start")
def start() -> dict[str, Any]:
    cfg = load_config()
    cfg.scan.enabled = True
    save_config(cfg)
    return ok(monitor.start_monitor())


@app.post("/api/monitor/stop")
def stop() -> dict[str, Any]:
    cfg = load_config()
    cfg.scan.enabled = False
    save_config(cfg)
    return ok(monitor.stop_monitor())


@app.get("/api/monitor/status")
def monitor_status() -> dict[str, Any]:
    return ok(monitor.status())


@app.get("/api/monitor/history")
def history(limit: int = 50) -> dict[str, Any]:
    return ok(monitor.state.get_history(limit))


@app.delete("/api/monitor/history")
def clear_history() -> dict[str, Any]:
    monitor.state.clear_history()
    return ok({}, "已清空")


# ----------------------------- 全市场扫描与推送 -----------------------------

@app.get("/api/lanban/candidates")
def lanban_candidates() -> dict[str, Any]:
    """烂板接力候选（T-2涨停→T-1开盘无板+触板+收盘封板+放量换手）。"""
    return ok(lanban.candidates())


@app.get("/api/positions/plan")
def positions_plan() -> dict[str, Any]:
    """持仓操作纪律卡：每只持仓的关键价位 + 四条纪律。"""
    cfg_v = load_config()
    out = []
    for h in cfg_v.holdings:
        if not (len(h) == 6 and h.isdigit()):
            continue
        cost = cfg_v.holding_costs.get(h)
        card = sellwatch.position_plan(h, float(cost or 0))
        if card.get("success"):
            out.append(card)
        else:
            out.append({"code": h, "success": False, "error": card.get("error")})
    return ok({"plans": out, "time": datetime.now().isoformat(timespec="seconds")})


@app.get("/api/positions/plan")
def positions_plan() -> dict[str, Any]:
    """持仓操作纪律卡：每只持仓的关键价位 + 四条纪律。"""
    cfg_v = load_config()
    out = []
    for h in cfg_v.holdings:
        if not (len(h) == 6 and h.isdigit()):
            continue
        cost = cfg_v.holding_costs.get(h)
        out.append(sellwatch.position_plan(h, float(cost or 0)))
    return ok({"plans": out, "time": datetime.now().isoformat(timespec="seconds")})


@app.get("/api/tdx/latest")
def tdx_latest() -> dict[str, Any]:
    """最近一个已完成交易日的通达信指标信号（按代码聚合）。"""
    return ok(tdxwatch.latest_signals())


@app.get("/api/market/scan-status")
def market_scan_status() -> dict[str, Any]:
    """全市场扫描与双时点定时推送状态。"""
    return ok(scheduler.status())


@app.post("/api/market/scan")
def market_scan_now(push: bool = False) -> dict[str, Any]:
    """手动触发一次全市场扫描。push=true 时同时发站内通知与邮件。"""
    if push:
        return ok(scheduler.run_market_push(datetime.now().strftime("%H:%M"), source="手动推送"))
    result = market_scan.scan_market()
    if result.get("success"):
        market_scan.persist_last_scan(result, "手动扫描")
    return ok(result)


@app.get("/api/market/signals")
def market_signals() -> dict[str, Any]:
    """返回最近一次全市场扫描的命中信号（内存态为空时回退到落盘快照，重启不丢）。"""
    snap = market_scan.state.snapshot()
    signals = market_scan.latest_signals()
    last: dict[str, Any] | None = None
    if not signals:
        persisted = market_scan.last_scan_persisted()
        if persisted:
            signals = persisted.get("signals") or []
            snap = {**snap, **(persisted.get("snapshot") or {})}
            last = {"saved_at": persisted.get("saved_at"), "source": persisted.get("source")}
    else:
        last = {"saved_at": snap.get("last_saved_at"), "source": snap.get("last_source")}
    return ok({"snapshot": snap, "signals": signals, "last": last})


@app.post("/api/market/push-now")
def market_push_now() -> dict[str, Any]:
    """立即按双时点推送逻辑执行一次（用于验证站内+邮件链路）。"""
    return ok(scheduler.run_market_push(datetime.now().strftime("%H:%M"), source="手动推送"))


# ----------------------------- 昨日涨停跟进 -----------------------------

@app.get("/api/yesterday/snapshot")
def yesterday_snapshot() -> dict[str, Any]:
    """昨日涨停跟进快照（内存态为空时回退到落盘快照，重启不丢）。"""
    snap = yesterday.latest_snapshot()
    rows = yesterday.latest()
    last: dict[str, Any] | None = None
    if not rows:
        persisted = yesterday.last_persisted()
        if persisted:
            rows = persisted.get("rows") or []
            last = {"saved_at": persisted.get("saved_at"), "source": persisted.get("source"),
                    "base_label": persisted.get("base_label"),
                    "observe_label": persisted.get("observe_label")}
    else:
        last = {"saved_at": snap.get("saved_at"), "source": snap.get("source"),
                "base_label": snap.get("base_label"), "observe_label": snap.get("observe_label")}
    return ok({
        "snapshot": snap, "rows": rows, "last": last,
        "times": ["10:30", "14:40"],
    })


@app.post("/api/yesterday/scan")
def yesterday_scan() -> dict[str, Any]:
    return ok(yesterday.scan_yesterday(source="手动扫描"))


@app.post("/api/yesterday/push-now")
def yesterday_push_now() -> dict[str, Any]:
    return ok(scheduler.run_yesterday_push(datetime.now().strftime("%H:%M"), source="手动推送"))


# ----------------------------- 持仓卖出风控 -----------------------------

@app.get("/api/sell/status")
def sell_status() -> dict[str, Any]:
    return ok(sellwatch.status())


@app.get("/api/sell/signals")
def sell_signals() -> dict[str, Any]:
    return ok({"signals": sellwatch.latest()})


@app.post("/api/sell/scan")
def sell_scan(confirm: bool = False) -> dict[str, Any]:
    return ok(sellwatch.scan("confirm" if confirm else "intraday"))


@app.post("/api/sell/push-now")
def sell_push_now(confirm: bool = False) -> dict[str, Any]:
    result = scheduler.run_sell_alert("confirm" if confirm else "intraday")
    return ok(result)


# ----------------------------- 站内通知 -----------------------------

@app.get("/api/notifications")
def notification_list(limit: int = 100) -> dict[str, Any]:
    return ok({
        "items": notifications.list_all(limit),
        "unread": notifications.unread_count(),
        "total": notifications.status()["total"],
    })


@app.get("/api/notifications/unread")
def notification_unread() -> dict[str, Any]:
    return ok({"count": notifications.unread_count()})


@app.post("/api/notifications/read")
def notification_mark_read(notify_id: str | None = None) -> dict[str, Any]:
    return ok(notifications.mark_read(notify_id))


@app.delete("/api/notifications")
def notification_clear() -> dict[str, Any]:
    return ok(notifications.clear_all(), "已清空站内通知")


@app.get("/api/market/replay/dates")
def market_replay_dates() -> dict[str, Any]:
    """回放可用交易日历 + K线缓存状态。"""
    return ok({"dates": replay.trading_dates(), "cache": replay.cache_status()})


@app.get("/api/market/replay")
def market_replay(date: str) -> dict[str, Any]:
    """按任意交易日回放全市场扫描（当前策略+当前配置，无未来函数）。"""
    return ok(replay.replay(date))


@app.post("/api/market/replay/refresh")
def market_replay_refresh() -> dict[str, Any]:
    """增量刷新全市场K线缓存（后台执行）。"""
    return ok(replay.refresh())


@app.get("/api/pattern/status")
def pattern_status() -> dict[str, Any]:
    """盘中形态监控状态（当日候选/已提醒/最近触发）。"""
    return ok(patternwatch.status())


@app.post("/api/pattern/scan")
def pattern_scan() -> dict[str, Any]:
    """手动执行一次盘中形态监控。"""
    return ok(patternwatch.poll())


@app.get("/api/wzq/watch")
def wzq_watch() -> dict[str, Any]:
    """弱转强观察池：最近一次巨量炸板扫描结果。"""
    return ok({"status": wzqwatch.status(), "rows": wzqwatch.latest()})


@app.post("/api/wzq/scan")
def wzq_scan(push: bool = False) -> dict[str, Any]:
    """手动扫描弱转强观察池（push=True 时同时推送）。"""
    return ok(wzqwatch.run_watch(push=push))


@app.post("/api/system/check")
def system_check() -> dict[str, Any]:
    """手动触发一次系统自检并发送邮件（部署验收/排障用）。"""
    return ok(scheduler.run_daily_check())


@app.get("/api/ignition/status")
def ignition_status() -> dict[str, Any]:
    """点火盘中监控状态（状态机/当日读数/提醒情况）。"""
    return ok(ignitionwatch.status())


@app.post("/api/ignition/check")
def ignition_check() -> dict[str, Any]:
    """手动触发一次点火监控检查（调试/验收用）。"""
    return ok(ignitionwatch.check())


# ----------------------------- 前端静态托管 -----------------------------

# 存在前端构建产物时由后端直接托管 UI（前端路由为 hash 模式，无需 SPA 回退）：
# 单端口 8000 同时提供页面与 /api，服务器部署无需 nginx/node。
# 挂载在所有路由之后，/api、/docs 等已注册路由优先匹配。
_UI_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
if _UI_DIST.is_dir():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(_UI_DIST), html=True), name="ui")
