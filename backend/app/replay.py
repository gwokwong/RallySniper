"""全市场机会历史回放：任意交易日按当前策略重建扫描结果（含卖出配对）。

数据源：backend/data/kline_cache/{code}.csv（腾讯前复权日线全市场缓存，
由 replay.refresh 增量更新；历史东财涨停池接口失效，候选池一律用
K线「收盘封板」重建，与回测口径一致）。

回放口径（与实盘扫描的差异在文档中标注）：
- 候选池 = 目标日之前20个交易日内有收盘封板的股票（含目标日）
- 点火读数 = 上一交易日封板池在目标日的收盘涨幅中位数（实盘为盘中快照+东财池含炸板）
- 广度 = 目标日收盘封板家数（实盘为盘中实时家数，略低于收盘值）
- 信号 = 截断到目标日的真实战法 trigger（策略开关随配置）
- 卖出 = 真实 sellwatch._evaluate(confirm) 首次确认；未触发按数据末日收盘浮动
- 行号对齐：每只股票用自身的日期列表定位行（bisect），停牌/缺口不影响
- 结果按 (日期, 配置指纹) 缓存于 data/replay_cache/
"""
from __future__ import annotations

import hashlib
import json
import threading
from bisect import bisect_left
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from . import datasource as ds
from . import mainwave as mw
from . import sellwatch as sw
from .config import effective_config

BASE = Path(__file__).resolve().parent.parent
CACHE_DIR = BASE / "data" / "kline_cache"
REPLAY_DIR = BASE / "data" / "replay_cache"
REF_CODE = "000001"  # 交易日历参考（平安银行，所有腾讯日线共享交易日）

_lock = threading.RLock()
_bars: dict[str, pd.DataFrame] = {}
_dlists: dict[str, list[str]] = {}   # code -> 升序日期列表（自身K线，停牌会有缺口）
_bars_loaded = False
_refreshing = False
_refresh_progress: dict[str, Any] = {"done": 0, "total": 0, "started": None, "finished": None, "ok": 0, "fail": 0}
_turnover_cache: dict[str, list[float] | None] = {}  # code -> 换手率序列（四象限过滤按需读取）


def _turnover_at(code: str, row: int) -> float | None:
    """按需读取该股换手率序列（内存瘦身索引不含此列，CSV 直读+缓存）。"""
    with _lock:
        if code not in _turnover_cache:
            f = CACHE_DIR / f"{code}.csv"
            try:
                df = pd.read_csv(f, usecols=["turnover"], dtype={"turnover": "float32"})
                _turnover_cache[code] = df["turnover"].astype(float).tolist()
            except Exception:  # noqa: BLE001
                _turnover_cache[code] = None
        arr = _turnover_cache[code]
    if arr is None or row < 0 or row >= len(arr):
        return None
    return arr[row]


def _cache_files() -> list[Path]:
    return sorted(CACHE_DIR.glob("*.csv"))


def _load_bars(force: bool = False) -> dict[str, pd.DataFrame]:
    """把缓存目录加载进内存（进程内复用，约 60MB），并建立个股日期索引。"""
    global _bars, _dlists, _bars_loaded
    with _lock:
        if _bars_loaded and not force:
            return _bars
        bars: dict[str, pd.DataFrame] = {}
        dlists: dict[str, list[str]] = {}
        # 内存优化：只读回放所需列 + float32（全列读 5200 只可达数 GB，小内存 VPS 会被 OOM 杀）
        usecols = ["date", "open", "close", "high", "low", "volume", "pct_change"]
        dtype = {c: "float32" for c in usecols if c != "date"}
        for p in _cache_files():
            try:
                df = pd.read_csv(p, usecols=usecols, dtype=dtype)
                if len(df) >= 60:
                    code = p.stem
                    bars[code] = df
                    dlists[code] = [str(x)[:10] for x in df["date"]]
            except Exception:  # noqa: BLE001
                continue
        _bars = bars
        _dlists = dlists
        _bars_loaded = True
        return _bars


def invalidate() -> None:
    """缓存文件更新后调用，强制下次重新加载。"""
    global _bars_loaded, _sentiment_cache
    with _lock:
        _bars_loaded = False
        _turnover_cache.clear()
        _sentiment_cache = None


def _row_of(code: str, date: str) -> int | None:
    """个股在 date 的行号（自身日期序列 bisect，缺口安全）；无该日返回 None。"""
    dl = _dlists.get(code)
    if not dl:
        return None
    i = bisect_left(dl, date)
    if i < len(dl) and dl[i] == date:
        return i
    return None


def trading_dates() -> list[str]:
    """可用交易日历（升序，取参考股的全部交易日）。"""
    bars = _load_bars()
    df = bars.get(REF_CODE)
    if df is None:
        for df_ in bars.values():
            df = df_
            break
    if df is None:
        return []
    return [str(x)[:10] for x in df["date"]]


def cache_status() -> dict[str, Any]:
    """轻量状态查询：只读参考股一个 CSV，绝不触发全量加载（轮询接口不能卡顿）。"""
    ref = CACHE_DIR / f"{REF_CODE}.csv"
    end = None
    if ref.exists():
        try:
            df = pd.read_csv(ref, usecols=["date"])
            end = str(df["date"].iloc[-1])[:10]
        except Exception:  # noqa: BLE001
            pass
    return {"codes": len(_cache_files()), "end_date": end,
            "bars_loaded": _bars_loaded,
            "refresh": dict(_refresh_progress)}


def refresh(end_date: str | None = None) -> dict[str, Any]:
    """增量刷新全市场K线缓存（后台线程，幂等）。"""
    global _refreshing
    with _lock:
        if _refreshing:
            return {"success": False, "message": "刷新进行中", "refresh": dict(_refresh_progress)}
        _refreshing = True
    from .config import load_config

    def _run():
        global _refreshing
        try:
            names = ds.stock_code_names()
            codes = sorted(set([p.stem for p in _cache_files()]) | set(names.keys()))
            codes = [c for c in codes if not c.startswith(("4", "8", "92"))]
            _refresh_progress.update({"done": 0, "total": len(codes),
                                      "started": datetime.now().isoformat(timespec="seconds"),
                                      "finished": None, "ok": 0, "fail": 0})
            ed = end_date or datetime.now().strftime("%Y%m%d")

            def fetch_one(code: str) -> bool:
                try:
                    hist = ds.stock_history(code, days=260)
                    if hist is None or hist.empty:
                        return False
                    hist.to_csv(CACHE_DIR / f"{code}.csv", index=False)
                    return True
                except Exception:  # noqa: BLE001
                    return False

            with ThreadPoolExecutor(max_workers=6) as pool:
                futs = {pool.submit(fetch_one, c): c for c in codes}
                for fut in as_completed(futs):
                    _refresh_progress["done"] += 1
                    if fut.result():
                        _refresh_progress["ok"] += 1
                    else:
                        _refresh_progress["fail"] += 1
            invalidate()
            # 后台线程内预热内存索引（约数十秒），避免下一个用户请求同步承担全量加载
            try:
                _load_bars(force=True)
            except Exception:  # noqa: BLE001
                pass
        finally:
            _refresh_progress["finished"] = datetime.now().isoformat(timespec="seconds")
            _refreshing = False

    threading.Thread(target=_run, daemon=True).start()
    return {"success": True, "message": "刷新已启动", "refresh": dict(_refresh_progress)}


def _cfg_fingerprint(cfg) -> str:
    raw = json.dumps({
        "v": 3,  # v3: 含卖出配对 + A杀买入过滤(market_filter)
        "strategies": cfg.strategies.model_dump(),
        "rules": cfg.strategy_rules.model_dump(),
        "sell": cfg.sell.model_dump(),
        "mfilter": cfg.market_filter.model_dump(),
        "ignition_threshold": cfg.market_regime.ignition_threshold,
        "tier": cfg.market_regime.ignition_tier_breadth,
        "breadth": cfg.market_regime.ignition_min_breadth,
        "ignition_enabled": cfg.market_regime.ignition_enabled,
        "bomb_gate": [cfg.market_regime.ignition_bomb_gate_enabled,
                      float(cfg.market_regime.ignition_bomb_rate_max)],
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(raw.encode()).hexdigest()[:10]


def replay(date: str) -> dict[str, Any]:
    """回放指定交易日的全市场扫描（含卖出配对）。date=YYYY-MM-DD。"""
    cfg = effective_config()
    date = str(date)[:10]
    dates = trading_dates()
    if date not in dates:
        return {"success": False, "error": f"{date} 不是缓存覆盖的交易日（可用范围 {dates[0]} ~ {dates[-1]}）"}

    REPLAY_DIR.mkdir(parents=True, exist_ok=True)
    fp = _cfg_fingerprint(cfg)
    cache_file = REPLAY_DIR / f"{date}_{fp}.json"
    if cache_file.exists():
        try:
            return {**json.loads(cache_file.read_text(encoding="utf-8")), "cached": True}
        except Exception:  # noqa: BLE001
            pass

    bars = _load_bars()
    di = dates.index(date)
    prev_day = dates[di - 1] if di >= 1 else None
    try:
        names = ds.stock_code_names()
    except Exception:  # noqa: BLE001
        names = {}

    def usable(code: str) -> bool:
        """与实盘 pass_filter 同口径：剔除北交/ST/退（include_st=False）。"""
        if code.startswith(("4", "8", "92")):
            return False
        nm = str(names.get(code, ""))
        return "ST" not in nm.upper() and "退" not in nm

    th = 3.0
    try:
        th = float(cfg.market_regime.ignition_threshold)
    except Exception:  # noqa: BLE001
        pass

    def is_lu(code: str, date: str) -> bool:
        """该股在 date 是否收盘封板（自身行号定位，停牌缺口安全）。"""
        k = _row_of(code, date)
        if k is None:
            return False
        df = bars[code]
        lth = 19.5 if code.startswith(("688", "689", "300", "301")) else 9.8
        try:
            return (float(df["pct_change"].iloc[k]) >= lth
                    and float(df["close"].iloc[k]) >= float(df["high"].iloc[k]) * 0.995)
        except Exception:  # noqa: BLE001
            return False

    # 1) 点火读数：上一交易日封板池在目标日的收盘涨幅中位数
    pool_prev = []
    if prev_day:
        for code in lu_codes_on(prev_day):
            k = _row_of(code, prev_day)
            k1 = k + 1 if k is not None else None
            df = bars.get(code)
            if k1 is not None and df is not None and len(df) > k1:
                c0, c1 = float(df["close"].iloc[k]), float(df["close"].iloc[k1])
                pool_prev.append((c1 / c0 - 1) * 100)
    premium = round(float(np.median(pool_prev)), 2) if len(pool_prev) >= 3 else None
    n_zt = len(lu_codes_on(date))
    tier_th = int(cfg.market_regime.ignition_tier_breadth or 100)
    size_tier = ("full" if n_zt >= tier_th else "probe") if premium is not None and premium >= th else None
    ignited = bool(premium is not None and premium >= th)
    blocked = ""
    if cfg.market_regime.ignition_enabled and not ignited:
        blocked = f"闸门未点火（{prev_day} 池在目标日中位溢价 {premium:+.2f}% < {th:g}%）"
    # 炸板闸门（2026-11-25 因子研究）：昨日市场炸板率>阈值=资金分歧极大（情绪冰点），
    # 即使溢价达标也不点火。7-11月回测：总收益 +5.99→+8.36万，胜率 51.8%→57.5%。
    if cfg.market_regime.ignition_enabled and ignited and cfg.market_regime.ignition_bomb_gate_enabled:
        br = market_sentiment().get(prev_day, {}).get("bomb_rate") if prev_day else None
        br_max = float(cfg.market_regime.ignition_bomb_rate_max)
        if br is not None and br > br_max:
            ignited = False
            blocked = f"炸板闸门（{prev_day} 市场炸板率 {br:.1f}% > {br_max:g}%，资金分歧极大）"
    elif cfg.market_regime.ignition_enabled and cfg.market_regime.ignition_min_breadth > 0 \
            and n_zt < int(cfg.market_regime.ignition_min_breadth):
        blocked = (f"情绪点火但广度不足（当日涨停 {n_zt} 家 < {cfg.market_regime.ignition_min_breadth}，"
                   f"属修复反弹非高潮）")

    signals: list[dict[str, Any]] = []
    cands: list[tuple[str, int]] = []
    if not blocked:
        # 2) 候选池：近20个交易日（日历窗口）内有收盘封板
        lo = max(0, di - 19)
        window = dates[lo:di + 1]
        for code in bars.keys():
            if not usable(code):
                continue
            if any(is_lu(code, d) for d in window):
                k = _row_of(code, date)
                if k is not None:
                    cands.append((code, k))
        # 3) 逐股跑启用战法（并行，纯买入判定）
        strats = [(n_, f_) for n_, f_ in (
            ("涨停回调", mw._strategy_limitup_pullback),
            ("强势续攻", mw._strategy_strong_continue),
            ("洗盘反包", mw._strategy_wash_reversal),
        ) if cfg.strategies.as_dict().get(n_)]
        rules = cfg.strategy_rules.model_dump()

        def run_one(item):
            code, i = item
            df = bars[code]
            d = mw._indicators(df.iloc[:i + 1], code, names.get(code, code))
            hits = []
            for sname, fn in strats:
                try:
                    res = fn(d, rules)
                except Exception:  # noqa: BLE001
                    continue
                if res.get("hit") and res.get("stage") == "trigger":
                    hits.append((sname, res))
            if not hits:
                return None
            # A杀过滤：与实盘 scan_market 同口径（近N交易日冲高跳水长上影不碰）
            mf = cfg.market_filter.model_dump()
            if mf.get("a_kill_skip_enabled", True) and mw.recent_a_kill(d, mf):
                return None
            # 换手率×量比四象限过滤（C1 温和放量反包=回测最差象限）：bars 内存索引
            # 无换手率列，按需读 CSV 补（带缓存，回放候选只有数百只）
            if mf.get("vp_combo_filter_enabled", False):
                vr_j = None
                try:
                    vols = df["volume"].to_numpy(dtype=float)
                    if i >= 5 and vols[i - 5:i].mean() > 0:
                        vr_j = float(vols[i]) / float(vols[i - 5:i].mean())
                except Exception:  # noqa: BLE001
                    vr_j = None
                to_j = _turnover_at(code, i)
                if mw.vp_combo_should_skip(to_j, vr_j, mf):
                    return None
            best = max(hits, key=lambda x: x[1].get("strength", 0))
            return {"code": code, "name": names.get(code, code), "strategy": best[0],
                    "price": round(float(d["close"].iloc[-1]), 2),
                    "strength": best[1].get("strength", 0), "stage": "trigger", "date": date,
                    "buy_row": i,
                    "reason": " / ".join(best[1].get("reasons", []))[:150],
                    "strategies": [h[0] for h in hits]}

        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = [pool.submit(run_one, it) for it in cands]
            for fut in as_completed(futs):
                try:
                    s = fut.result()
                    if s:
                        signals.append(s)
                except Exception:  # noqa: BLE001
                    continue
        signals.sort(key=lambda x: -x["strength"])

        # 4) 卖出配对（串行：monkey-patch 全局非线程安全）——真实 _evaluate 首确认
        sell_rules = cfg.sell.model_dump()
        for s in signals:
            code, buy_i = s["code"], s["buy_row"]
            df = bars[code]
            n = len(df)
            sell_date = sell_price = None
            for j in range(buy_i + 1, n):
                window = df.iloc[:j].copy()
                sw._completed_history = lambda c, w=window: w
                row = df.iloc[j]
                quote = {"price": float(row["close"]), "open": float(row["open"]),
                         "change_pct": float(row["pct_change"]), "low": float(row["low"]),
                         "volume": float(row["volume"])}
                try:
                    found = sw._evaluate(code, s["name"], quote, sell_rules, "confirm", cost=s["price"])
                except Exception:  # noqa: BLE001
                    found = []
                if found:
                    sell_date, sell_price = dates[j], round(float(row["close"]), 2)
                    break
            sw._completed_history = _orig_completed
            s["sell_date"] = sell_date or dates[-1]
            s["sell_price"] = sell_price if sell_price is not None else round(float(df["close"].iloc[-1]), 2)
            s["sold"] = sell_date is not None
            s["r_sell"] = round((s["sell_price"] / s["price"] - 1) * 100, 2)
            bi = bisect_left(dates, date)
            ei = bisect_left(dates, s["sell_date"])
            s["hold_nat"] = (datetime.fromisoformat(s["sell_date"] or dates[-1])
                             - datetime.fromisoformat(date)).days
            s["hold_trd"] = max(0, ei - bi)
        sw._completed_history = _orig_completed

    sold_n = sum(1 for s in signals if s.get("sold"))
    rets = [s["r_sell"] for s in signals if s.get("r_sell") is not None]
    sell_stats = {
        "sold": sold_n, "open": len(signals) - sold_n,
        "win": round(sum(1 for x in rets if x > 0) / len(rets) * 100, 1) if rets else None,
        "avg": round(float(np.mean(rets)), 2) if rets else None,
        "total": round(sum(10000 * x / 100 for x in rets)) if rets else 0,
    }

    result = {
        "success": True, "date": date, "replay": True,
        "gate": {"premium": premium, "threshold": th, "ignited": ignited,
                 "n_zt": n_zt, "pool_prev_n": len(pool_prev),
                 "size_tier": size_tier,
                 "size_tier_label": {"full": "全仓档(广度普涨)", "probe": "试错档(建议1/4仓)"}.get(size_tier, "—")},
        "blocked_reason": blocked,
        "candidates": len(cands) if not blocked else 0,
        "signal_count": len(signals),
        "sell_stats": sell_stats,
        "signals": signals,
        "cached_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        cache_file.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return result


_orig_completed = sw._completed_history


_sentiment_cache: dict[str, dict[str, float]] | None = None


def market_sentiment() -> dict[str, dict[str, float]]:
    """每交易日市场短线情绪统计（全部来自K线缓存，开盘前即可知，零盘中接口依赖）。

    - bomb_rate 炸板率(%)：盘中触板未封 ÷ 盘中触板；触板=高点≥昨收+(涨停阈值-0.3pp)。
      情绪周期经典指标：>45% 资金分歧极大（7-11月回测：该环境下反包入场胜率仅38.1%）。
    - max_streak 市场最高连板高度（情绪高度）。
    """
    global _sentiment_cache
    with _lock:
        if _sentiment_cache is not None:
            return _sentiment_cache
    bars = _load_bars()
    touched_n: dict[str, int] = {}
    sealed_n: dict[str, int] = {}
    max_streak: dict[str, int] = {}
    for code, df in bars.items():
        if code.startswith(("4", "8", "92")):
            continue  # 北交所（30cm 涨跌幅会虚增触板口径），与候选池 usable() 同口径剔除
        c = df["close"].to_numpy(dtype=float)
        h = df["high"].to_numpy(dtype=float)
        p = df["pct_change"].to_numpy(dtype=float)
        n = len(c)
        th = 19.5 if code.startswith(("688", "689", "300", "301")) else 9.8
        pc = np.empty(n)
        pc[0] = np.nan
        pc[1:] = c[:-1]
        sealed = (p >= th) & (c >= h * 0.995)
        touched = (~np.isnan(pc)) & ((h / pc - 1) * 100 >= (th - 0.3))
        dt = [str(x)[:10] for x in df["date"]]
        st = 0
        for j in range(n):
            d = dt[j]
            st = st + 1 if sealed[j] else 0
            if d < "2026-06-01":
                continue
            if touched[j]:
                touched_n[d] = touched_n.get(d, 0) + 1
            if sealed[j]:
                sealed_n[d] = sealed_n.get(d, 0) + 1
                if st > max_streak.get(d, 0):
                    max_streak[d] = st
    out: dict[str, dict[str, float]] = {}
    for d in set(touched_n) | set(sealed_n):
        t, s = touched_n.get(d, 0), sealed_n.get(d, 0)
        out[d] = {"bomb_rate": round((t - s) / t * 100, 2) if t > 0 else None,
                  "touched": t, "sealed": s, "max_streak": max_streak.get(d, 0)}
    with _lock:
        _sentiment_cache = out
    return out


def lu_codes_on(date: str) -> list[str]:
    """某交易日收盘封板的可用股票列表（自身行号定位）。"""
    bars = _load_bars()
    out = []
    for code in bars.keys():
        if _row_of(code, date) is not None and _lu_check(code, date):
            out.append(code)
    return out


def _lu_check(code: str, date: str) -> bool:
    k = _row_of(code, date)
    if k is None:
        return False
    df = _bars[code]
    lth = 19.5 if code.startswith(("688", "689", "300", "301")) else 9.8
    try:
        return (float(df["pct_change"].iloc[k]) >= lth
                and float(df["close"].iloc[k]) >= float(df["high"].iloc[k]) * 0.995)
    except Exception:  # noqa: BLE001
        return False
