"""场内板块 ETF 监控与策略引擎（etfwatch）。

设计依据（2026-11-28 长历史回测，结论固化于 STRATEGY_BOUNDARY.md ETF 节）：
- 主策略 S4「波动收缩突破」：布林带宽 squeeze（≤ max(6%, 前60日带宽25分位×1.1)）后
  放量突破上轨买入，收盘跌回中轨（MA20）卖出——12 组参数扫描 9~10/10 正收益、无悬崖，
  均 CAGR 9.8% / 均MDD -19.7% / Cal 0.54，显著优于买入持有与 MA20 趋势（后者 whipsaw 严重）。
- S1 趋势 / S2 轮动 / S3 情绪联动：回测证伪或过拟合，开关保留默认关。
- 约束：ETF 为 T+1（当日买入不可当日卖出），所有信号收盘确认、次日执行；
  仓位建议接入现有点火档位（全仓档 1x / 试错档 1/4x × available_capital，整百份额）。

数据：datasource.etf_history（东财 qfq → 新浪兜底）落盘 data/etf_cache/{code}.csv，
刷新 15:12（晚于个股 kline_refresh），扫描 15:20，复用 mainwave._indicators（含 VCP/布林列）。
持仓状态为纸面跟踪（state 自动按买卖信号翻转；人工实际成交以自身券商为准）。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from . import datasource as ds, mainwave as mw, notifications, notifier, scanstore, signalstore
from .config import load_config

BASE = Path(__file__).resolve().parent.parent
CACHE_DIR = BASE / "data" / "etf_cache"
STATE_FILE = BASE / "data" / "etf_state.json"

_lock = threading.RLock()
_bars: dict[str, pd.DataFrame] = {}
_bars_loaded = False
_last_scan: dict[str, Any] | None = None


# ----------------------------- 状态持久化（纸面持仓） -----------------------------

def _load_state() -> dict[str, Any]:
    try:
        d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            return d
    except Exception:  # noqa: BLE001
        pass
    return {}


def _persist_state(state: dict[str, Any]) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


# ----------------------------- K线缓存 -----------------------------

def _held_codes() -> set[str]:
    """持仓 ETF = 手动登记(etf_holdings) ∪ 纸面状态 in_position（系统信号买入）。"""
    cfg = load_config()
    held = {str(c).zfill(6) for c in (cfg.etf.etf_holdings or [])}
    for code, rec in _load_state().items():
        if rec.get("in_position"):
            held.add(str(code).zfill(6))
    return held


def _pool_codes() -> list[tuple[str, str]]:
    cfg = load_config()
    codes = [(it.code.zfill(6), it.name) for it in cfg.etf.pool]
    safe = str(cfg.etf.safe_asset or "").strip()
    if safe and safe != "cash":
        if not any(c == safe.zfill(6) for c, _ in codes):
            codes.append((safe.zfill(6), f"安全资产{safe}"))
    return codes


def _load_bars(force: bool = False) -> dict[str, pd.DataFrame]:
    global _bars, _bars_loaded
    with _lock:
        if _bars_loaded and not force:
            return _bars
        bars: dict[str, pd.DataFrame] = {}
        for f in CACHE_DIR.glob("*.csv"):
            try:
                df = pd.read_csv(f)
                if len(df) >= 25:
                    df["date"] = df["date"].astype(str)
                    bars[f.stem] = df
            except Exception:  # noqa: BLE001
                continue
        _bars = bars
        _bars_loaded = True
        return _bars


def invalidate() -> None:
    global _bars_loaded
    with _lock:
        _bars_loaded = False


def refresh(force: bool = False) -> dict[str, Any]:
    """刷新池内 ETF 日K缓存（收盘后 15:12 定时；休市日跳过由调度器负责）。"""
    codes = _pool_codes()
    ok, fail = [], []

    def fetch_one(item: tuple[str, str]):
        code, _name = item
        f = CACHE_DIR / f"{code}.csv"
        if f.exists() and not force:
            try:
                last = pd.read_csv(f).tail(1)["date"].iloc[0]
                if str(last)[:10] >= ds.effective_trading_date()["date"]:
                    ok.append(code)
                    return
            except Exception:  # noqa: BLE001
                pass
        df = ds.etf_history(code, days=400)
        if df is None or len(df) < 25:
            fail.append(code)
            return
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        df.to_csv(f, index=False)
        ok.append(code)

    # 串行抓取（池子仅几只，秒级完成；akshare 新浪源在多线程下会崩，勿并发）
    for it in codes:
        try:
            fetch_one(it)
        except Exception:  # noqa: BLE001
            fail.append("unknown")
    invalidate()
    return {"ok": ok, "fail": fail, "total": len(codes),
            "time": datetime.now().isoformat(timespec="seconds")}


# ----------------------------- 策略判定 -----------------------------

def _squeeze(d: pd.DataFrame, cfg_etf: Any, idx: int) -> bool:
    """带宽 squeeze：带宽 ≤ max(6%, 前 win 日带宽25分位×1.1)，取 idx-1 值防前视。"""
    cs = d["close"]
    mid = cs.rolling(20).mean()
    bw = ((mid + cfg_etf.bb_std * cs.rolling(20).std(ddof=0))
          - (mid - cfg_etf.bb_std * cs.rolling(20).std(ddof=0))) / mid * 100
    q25 = bw.rolling(cfg_etf.squeeze_window, min_periods=10).quantile(0.25).shift(1)
    if idx < 1:
        return False
    v, th = float(bw.iloc[idx - 1]), float(q25.iloc[idx - 1])
    return np.isfinite(v) and (v <= max(6.0, th * 1.1) if np.isfinite(th) else v <= 6.0)


def _eval_item(code: str, name: str, df: pd.DataFrame, today: str,
               ec: Any, in_pos: bool) -> dict[str, Any] | None:
    """单标的策略评估（scan 与 status 共用，保证页面/推送/判定口径一致）。

    返回 {code,name,price,chg,date,signals,readings,flags}；数据缺失或过期返回 None。
    flags: squeeze/freeze/z20/r5/vr——页面状态标签与推送共用。
    """
    nm = name
    try:
        d = mw._indicators(df, code, nm)
    except Exception:  # noqa: BLE001
        return None
    i = len(d) - 1
    if str(d["date"].iloc[i])[:10] < today:
        return None  # 数据未到今日
    cur = d.iloc[i]
    vma5 = float(cur.get("vma5") or 0)
    vr = float(cur["volume"] / vma5) if vma5 > 0 else None
    sq = _squeeze(d, ec, i)
    breakout = bool(cur["close"] > cur["bb_up"]) and (vr is not None and vr >= ec.vol_ratio_min)
    readings = {"vcp_ratio": None if pd.isna(cur.get("vcp_ratio")) else round(float(cur["vcp_ratio"]), 2),
                "squeeze": sq, "vr": round(vr, 2) if vr else None,
                "bb_up": round(float(cur["bb_up"]), 3), "bb_mid": round(float(cur["bb_mid"]), 3)}
    sigs: list[dict[str, Any]] = []
    if ec.squeeze_enabled:
        if not in_pos and sq and breakout:
            sigs.append({"strategy": "波动收缩突破", "stage": "trigger", "stage_label": "买入",
                         "direction": "up",
                         "reasons": [f"带宽收缩后放量突破上轨{cur['bb_up']:.3f}（量比{vr}）",
                                     "回测：12组参数9-10/10正收益，均CAGR 9.8%/MDD-19.7%"]})
        elif not in_pos and sq and not breakout:
            sigs.append({"strategy": "波动收缩突破", "stage": "watch", "stage_label": "收缩观察",
                         "direction": "up",
                         "reasons": [f"带宽收缩中（未突破），站上{cur['bb_up']:.3f}且量比≥{ec.vol_ratio_min}触发"]})
        if in_pos and float(cur["close"]) < float(cur["bb_mid"]):
            sigs.append({"strategy": "波动收缩突破", "stage": "trigger", "stage_label": "卖出",
                         "direction": "down",
                         "reasons": [f"收盘跌回中轨{cur['bb_mid']:.3f}（回测离场口径）"]})
    # 读数面板（z20/r5 恒计算；冰点与趋势标注共用）
    ma20 = float(cur["bb_mid"])
    sd20 = float(d["close"].rolling(20).std(ddof=0).iloc[i])
    r5 = float(d["close"].pct_change(5).iloc[i] * 100) if i >= 5 else 0.0
    z20 = (float(cur["close"]) - ma20) / sd20 if sd20 > 0 else 0.0
    readings["z20"] = round(z20, 2)
    readings["r5"] = round(r5, 2)
    readings["pct"] = round(float(cur["pct_change"]), 2)
    # 情绪冰点状态提醒（防抄底，非买入信号）：z20 与 5日跌幅双确认。
    # 2026-11-28 回测：冰点组 T+5 仅 +0.1~0.25pp 微增量、T+10 平均 -0.4~-0.6%——冰点后大概率继续弱。
    freeze = False
    if ec.freeze_note_enabled:
        freeze = bool(z20 <= float(ec.freeze_z) and r5 <= float(ec.freeze_r5))
        if freeze:
            sigs.append({"strategy": "情绪冰点", "stage": "watch", "stage_label": "冰点·勿抄底",
                         "direction": "down",
                         "reasons": [f"z20={z20:.2f}≤{ec.freeze_z} 且 5日{r5:+.1f}%≤{ec.freeze_r5}%——情绪冰点",
                                     "回测：冰点后 T+10 平均 -0.4~-0.6%，是弱不是买点，勿急于接刀"]})
    chg = float(cur["pct_change"])
    return {"code": code, "name": nm, "price": round(float(cur["close"]), 3),
            "chg": round(chg, 2), "date": str(d["date"].iloc[i])[:10],
            "signals": sigs, "readings": readings,
            "flags": {"squeeze": sq, "freeze": freeze, "in_pos": in_pos}}


def scan(push: bool = True) -> dict[str, Any]:
    """收盘后扫描（S4 主策略 + 情绪冰点状态提醒）。

    提示链路三处到位：①返回值供页面「今日扫描」卡片 ②站内通知（kind=etf）
    ③邮件广播——**无论有无信号每日必推简报**（无信号日给池子快照，避免静默）。
    trigger 级经 signalstore 落盘验证；watch 级（冰点/收缩观察）仅提示不入验证闭环。
    """
    cfg = load_config()
    ec = cfg.etf
    if not ec.enabled:
        return {"success": False, "error": "ETF 监控未启用", "signals": [], "items": []}
    bars = _load_bars()
    state = _load_state()
    # 日期归一化：effective_trading_date 可能返回 YYYYMMDD（无横杠），K线日期均为 YYYY-MM-DD；
    # premium 接口（涨停池）要求 YYYYMMDD 原始格式，两个格式各自传对。
    today_raw = str(ds.effective_trading_date()["date"])
    today = today_raw if "-" in today_raw else f"{today_raw[:4]}-{today_raw[4:6]}-{today_raw[6:8]}"
    names = {}
    try:
        names = ds.stock_code_names()
    except Exception:  # noqa: BLE001
        pass
    ign = ds.ignition_premium(today_raw, ttl=600)
    ignited = bool(ign.get("median") is not None
                   and ign.get("median") >= float(cfg.market_regime.ignition_threshold))
    tier_factor = 1.0 if ignited else 0.25

    signals: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    for code, name in _pool_codes():
        df = bars.get(code)
        if df is None:
            df = ds.etf_history(code, days=400)
            if df is None or len(df) < 25:
                continue
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            df.to_csv(CACHE_DIR / f"{code}.csv", index=False)
        rec = state.get(code) or {}
        held = code in _held_codes()
        r = _eval_item(code, name, df, today, ec, bool(rec.get("in_position")) or held)
        if r is None:
            continue
        items.append(r)
        for sig in r["signals"]:
            row = {"code": code, "name": r["name"], "strategy": sig["strategy"],
                   "stage": sig["stage"], "stage_label": sig["stage_label"],
                   "direction": sig["direction"], "price": r["price"],
                   "change_pct": r["chg"], "reasons": sig["reasons"],
                   "readings": r["readings"], "date": today}
            signals.append(row)
            if sig["stage"] == "trigger":
                rows.append(row)
        # 纸面持仓状态翻转（按信号口径，人工实际成交以券商为准）
        for sig in r["signals"]:
            if sig["stage"] == "trigger":
                if sig["direction"] == "up" and not r["flags"]["in_pos"]:
                    state[code] = {"in_position": True, "entry_date": today,
                                   "entry_price": r["price"],
                                   "life_line": r["readings"]["bb_mid"]}
                    r["flags"]["in_pos"] = True
                elif sig["direction"] == "down" and r["flags"]["in_pos"]:
                    state[code] = {"in_position": False,
                                   "last_exit_date": today,
                                   "last_exit_price": r["price"]}
                    r["flags"]["in_pos"] = False
    _persist_state(state)

    # 池内 5 日动量排名（读数面板：rank5 = 第几强/共几只）
    ranked = sorted([it for it in items if it["readings"].get("r5") is not None],
                    key=lambda x: -x["readings"]["r5"])
    for k, it in enumerate(ranked, 1):
        it["readings"]["rank5"] = f"{k}/{len(ranked)}"

    # 仓位建议（点火档位 × available_capital，整百份额）——仅对买入 trigger
    cap = float(getattr(cfg, "available_capital", 0) or 0)
    for sig in signals:
        if sig["stage"] == "trigger" and sig["direction"] == "up" and cap > 0:
            amount = cap * tier_factor
            sig["alloc"] = {"tier_factor": tier_factor, "amount": round(amount, 0),
                            "shares": int(amount / sig["price"] // 100) * 100,
                            "note": "T+1：信号收盘确认、次日可买；份额按整百取整"}

    if push:
        buy = [s for s in signals if s["stage"] == "trigger" and s["direction"] == "up"]
        sell = [s for s in signals if s["stage"] == "trigger" and s["direction"] == "down"]
        watch = [s for s in signals if s["stage"] == "watch"]
        frz = [s for s in signals if s["strategy"] == "情绪冰点"]
        title = (f"【ETF 监控】{today} 买入 {len(buy)} / 卖出 {len(sell)} / "
                 f"观察 {len(watch) - len(frz)} / 冰点 {len(frz)}"
                 if watch else f"【ETF 监控】{today} 买入 {len(buy)} / 卖出 {len(sell)} / 无观察信号")
        body = ""
        for s in signals:
            body += (f"\n- {s['name']}({s['code']})【{s['stage_label']}】现价{s['price']}"
                     f"：{'；'.join(s['reasons'])}")
            if s.get("alloc"):
                body += f"｜建议 {s['alloc']['shares']}股≈{s['alloc']['amount']:g}元（档位×{s['alloc']['tier_factor']}）"
        if not signals:
            body += "\n- 今日无买卖/观察信号"
        # 池子状态快照（每日必推，保证链路可见）
        body += "\n\n【池子状态·战法命中】"
        for it in items:
            tags = []
            if it["flags"]["in_pos"]:
                tags.append("持仓中")
            if it["flags"]["freeze"]:
                tags.append("❄️冰点")
            elif it["flags"]["squeeze"]:
                tags.append("收缩中")
            hits = [f"{x['stage_label']}" for x in signals if x["code"] == it["code"]]
            rk = it["readings"].get("rank5", "")
            body += (f"\n- {it['name']}({it['code']}) {it['price']} {it['chg']:+.1f}%"
                     + (f"｜动量排名{rk}" if rk else "")
                     + (f"｜{'/'.join(tags)}" if tags else "")
                     + (f"｜命中:{'/'.join(hits[:4])}" if hits else "｜无命中"))
        body += ("\n\n点火档位：" + ("全仓档×1.0" if tier_factor == 1.0 else "试错档×0.25")
                 + f"（点火溢价 {ign.get('median')}%）")
        body += "\nT+1：信号收盘确认、次日执行；纸面跟踪非实际持仓，不构成投资建议。"
        try:
            notifications.add(title, body, signals, kind="etf")
            if cfg.notification:
                notifier.broadcast(cfg.notification, title, body, signals)
        except Exception as exc:  # noqa: BLE001
            print(f"[etfwatch] 推送失败: {exc}")
        try:
            signalstore.record_batch(rows, source="etf")
        except Exception as exc:  # noqa: BLE001
            print(f"[etfwatch] 信号落盘失败: {exc}")
    result = {"success": True, "date": today, "signals": signals, "items": items,
              "tier_factor": tier_factor, "state": state,
              "time": datetime.now().isoformat(timespec="seconds")}
    global _last_scan
    with _lock:
        _last_scan = result
    try:
        scanstore.save("etf", {"date": today, "signals": signals, "items": items,
                               "tier_factor": tier_factor})
    except Exception:  # noqa: BLE001
        pass
    return result


# ----------------------------- 池子 / 搜索 / 热榜 -----------------------------

def pool_add(code: str, name: str = "") -> dict[str, Any]:
    from .config import update_config
    code = str(code).zfill(6)
    cfg = load_config()
    if any(it.code == code for it in cfg.etf.pool):
        return {"success": False, "error": "已在池内"}
    if not name:
        u = ds.etf_universe()
        name = next((x["name"] for x in u if x["code"] == code), "")
    update_config({"etf": {"pool": [it.model_dump() for it in cfg.etf.pool] + [{"code": code, "name": name}]}})
    return {"success": True, "code": code, "name": name}


def pool_remove(code: str) -> dict[str, Any]:
    from .config import update_config
    code = str(code).zfill(6)
    cfg = load_config()
    remain = [it.model_dump() for it in cfg.etf.pool if it.code != code]
    if len(remain) == len(cfg.etf.pool):
        return {"success": False, "error": "不在池内"}
    update_config({"etf": {"pool": remain}})
    return {"success": True}


def search_universe(q: str, limit: int = 30) -> list[dict[str, Any]]:
    u = ds.etf_universe()
    q = str(q).strip().lower()
    if not q:
        return u[:limit]
    return [x for x in u if q in x["code"] or q in x["name"].lower()][:limit]


def hotlist(limit: int = 15) -> dict[str, Any]:
    """全市场 ETF 当日热榜（只荐不入池）：涨幅榜 + 成交量榜。"""
    u = [x for x in ds.etf_spot(3600) if x.get("price")]
    by_pct = sorted([x for x in u if x.get("pct") is not None], key=lambda x: -x["pct"])[:limit]
    by_vol = sorted([x for x in u if x.get("volume") is not None], key=lambda x: -x["volume"])[:limit]
    return {"pct": by_pct, "volume": by_vol, "n": len(u)}


def status() -> dict[str, Any]:
    cfg = load_config()
    state = _load_state()
    bars = _load_bars()
    today_raw = str(ds.effective_trading_date()["date"])
    today = today_raw if "-" in today_raw else f"{today_raw[:4]}-{today_raw[4:6]}-{today_raw[6:8]}"
    ec = cfg.etf
    pool = []
    held_all = _held_codes()
    for it in cfg.etf.pool:
        code = it.code.zfill(6)
        df = bars.get(code)
        st = state.get(code) or {}
        last_px = float(df["close"].iloc[-1]) if df is not None else None
        last_dt = str(df["date"].iloc[-1])[:10] if df is not None else None
        in_pos = bool(st.get("in_position")) or code in held_all
        flags = {}
        if df is not None:
            r = _eval_item(code, it.name, df, today, ec, in_pos)
            if r:
                flags = {k: r["flags"][k] for k in ("squeeze", "freeze", "in_pos")}
                flags.update({k: r["readings"].get(k) for k in ("z20", "r5", "vr")})
                last_px, last_dt = r["price"], r["date"]
        pool.append({
            "code": code, "name": it.name, "price": last_px, "date": last_dt,
            "in_position": in_pos,
            "entry_date": st.get("entry_date"), "entry_price": st.get("entry_price"),
            "life_line": st.get("life_line"),
            "pnl_pct": (round((last_px / st["entry_price"] - 1) * 100, 2)
                        if last_px and st.get("entry_price") else None),
            "flags": flags,
        })
    last = _last_scan or scanstore.load("etf")
    return {"enabled": cfg.etf.enabled, "pool": pool, "last_scan": last,
            "cache_n": len(bars), "state": state}


# ----------------------------- 盘中预警（每5分钟） -----------------------------

_intraday_alerted: set[str] = set()
_intraday_date: str | None = None


def intraday_check() -> dict[str, Any]:
    """盘中预警层（预警≠确认，执行以 15:20 收盘复核为准）：

    - 卖出预警：纸面持仓标的盘中跌破中轨（昨日锚定口径）——持仓者当日可卖，高价值；
    - 买入预警：未持仓标的盘中触及突破位（现价 ≥ 上轨×99.5%）——当日买入 T+1 次日起可卖。
    每标的每方向每日至多一次（防刷屏）； Bands 锚定昨日收盘，与 freeze_intraday 同口径。
    """
    global _intraday_date
    cfg = load_config()
    ec = cfg.etf
    today_raw = str(ds.effective_trading_date()["date"])
    today = today_raw if "-" in today_raw else f"{today_raw[:4]}-{today_raw[4:6]}-{today_raw[6:8]}"
    if _intraday_date != today:
        _intraday_alerted.clear()
        _intraday_date = today
    spot = {x["code"]: x for x in ds.etf_spot(30)}
    bars = _load_bars()
    state = _load_state()
    alerts: list[dict[str, Any]] = []
    for code, name in _pool_codes():
        q = spot.get(code)
        df = bars.get(code)
        if not q or not q.get("price") or df is None:
            continue
        hist = df[df["date"].astype(str).str[:10] < today]  # 昨日锚定
        if len(hist) < 20:
            continue
        c = hist["close"].astype(float)
        mid = float(c.rolling(20).mean().iloc[-1])
        up = float((c.rolling(20).mean() + ec.bb_std * c.rolling(20).std(ddof=0)).iloc[-1])
        live = float(q["price"])
        rec = state.get(code) or {}
        in_pos = bool(rec.get("in_position")) or code in _held_codes()
        if in_pos and live < mid and f"{code}:sell" not in _intraday_alerted:
            _intraday_alerted.add(f"{code}:sell")
            alerts.append({"code": code, "name": name or q.get("name", code), "direction": "down",
                           "kind": "卖出预警", "price": live, "line": round(mid, 3),
                           "note": f"盘中跌破中轨{mid:.3f}（持仓者当日可卖）；收盘复核确认后次日执行"})
        elif not in_pos and live >= up * 0.995 and f"{code}:buy" not in _intraday_alerted:
            _intraday_alerted.add(f"{code}:buy")
            alerts.append({"code": code, "name": name or q.get("name", code), "direction": "up",
                           "kind": "买入预警", "price": live, "line": round(up, 3),
                           "note": f"盘中触及突破位{up:.3f}；若收盘确认（≥上轨）次日执行买入"})
    if alerts and push_notify_enabled(cfg):
        title = f"【ETF 盘中预警】{datetime.now().strftime('%H:%M')} {len(alerts)} 条（预警≠确认，收盘复核为准）"
        body = ""
        for a in alerts:
            body += (f"\n- {a['name']}({a['code']})【{a['kind']}】现价{a['price']}｜阈值{a['line']}｜{a['note']}")
        body += "\n\n执行纪律不变：以 15:20 收盘复核为准，T+1 次日执行。"
        try:
            notifications.add(title, body, [], kind="etf")
            if cfg.notification:
                notifier.broadcast(cfg.notification, title, body, [])
        except Exception as exc:  # noqa: BLE001
            print(f"[etfwatch] 盘中预警推送失败: {exc}")
    return {"success": True, "date": today, "alerts": alerts, "checked": len(_pool_codes())}


def push_notify_enabled(cfg: Any) -> bool:
    try:
        return bool(cfg.etf.enabled)
    except Exception:  # noqa: BLE001
        return True


# ----------------------------- 盘中冰点快报（14:40） -----------------------------

def freeze_intraday() -> dict[str, Any]:
    """盘中冰点快报：实时价近似 z20/5日涨幅，双确认即推送（收盘复核为准）。

    指标口径与收盘版一致，但均线/标准差锚定昨日收盘（不含今日盘中行）：
      z20 = (现价 - MA20昨锚) / SD20昨锚；r5 = 现价 / 5个交易日前收盘 - 1
    每日最多推送一次（claim_once 防重），通知+邮件双通道；状态提醒非买入信号。
    """
    cfg = load_config()
    ec = cfg.etf
    if not ec.enabled or not ec.freeze_intraday_enabled:
        return {"success": False, "error": "未启用", "hits": []}
    today_raw = str(ds.effective_trading_date()["date"])
    today = today_raw if "-" in today_raw else f"{today_raw[:4]}-{today_raw[4:6]}-{today_raw[6:8]}"
    spot = {x["code"]: x for x in ds.etf_spot(30)}
    bars = _load_bars()
    hits: list[dict[str, Any]] = []
    for code, name in _pool_codes():
        df = bars.get(code)
        q = spot.get(code)
        if df is None or q is None or not q.get("price"):
            continue
        hist = df[df["date"].astype(str).str[:10] < today]  # 锚定昨日收盘
        if len(hist) < 25:
            continue
        c = hist["close"].astype(float)
        ma20, sd20 = float(c.rolling(20).mean().iloc[-1]), float(c.rolling(20).std(ddof=0).iloc[-1])
        if sd20 <= 0:
            continue
        live = float(q["price"])
        z20 = (live - ma20) / sd20
        r5 = (live / float(c.iloc[-5]) - 1) * 100 if len(c) >= 5 else 0.0
        if z20 <= float(ec.freeze_z) and r5 <= float(ec.freeze_r5):
            hits.append({"code": code, "name": name or q.get("name", code), "price": live,
                         "z20": round(z20, 2), "r5": round(r5, 2)})
    if not hits:
        return {"success": True, "date": today, "hits": [], "checked": True}
    title = f"【ETF 盘中冰点快报·14:40】{today} {len(hits)} 只触及冰点（收盘复核为准）"
    body = ""
    for h in hits:
        body += (f"\n- {h['name']}({h['code']}) 现价{h['price']}｜z20 {h['z20']:+.2f}≤{ec.freeze_z}｜"
                 f"盘中5日 {h['r5']:+.1f}%≤{ec.freeze_r5}%")
    body += ("\n\n⚠️ 冰点是状态提醒非买入信号（回测：冰点后 T+10 平均 -0.4~-0.6%）；"
             "收盘后以 15:20 扫描复核。")
    try:
        notifications.add(title, body, [], kind="etf")
        if cfg.notification:
            notifier.broadcast(cfg.notification, title, body, [])
    except Exception as exc:  # noqa: BLE001
        print(f"[etfwatch] 盘中冰点推送失败: {exc}")
    return {"success": True, "date": today, "hits": hits}
