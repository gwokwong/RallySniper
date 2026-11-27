"""烂板接力：连板换手烂板次日秒板/封板接力（用户战法，回测验证版）。

形态（T-1 烂板日，日K口径）：
- T-2 收盘涨停
- T-1 开盘未涨停（开盘没有板）+ 盘中触板 + 收盘封板 + 放量（量≥1.5×5日均量）
- 局限：日K无法区分「上午封盘」，以收盘封板近似

接力纪律（回测 6-11 月实证，次日开盘买持有5日）：
- 二进三 + 次日封住：可控 +8.65% / 分歧大 +2.58% —— 核心盈利来源
- 二进三 + 次日未封：-3.89% / -8.36% —— 封不住必须走（纪律）
- 高度≥4 未封（-3~3.8%）：高位低开必死（规则6）验证 ✓

用户九条纪律：
1 只做连板核心 2 只做情绪反转节点 3 放量换手板才有合力 4 无量上板不碰
5 二进三选换手二板次日高开秒板进 6 高位三次放量后低开必死
7 缩量不打、下午板不打、一字上来的不打 8 等待 9 跟随主力顺势
"""
from __future__ import annotations

import threading
from collections import Counter
from typing import Any

import numpy as np
import pandas as pd

from . import datasource as ds
from .config import load_config

_lock = threading.Lock()
_cand_cache: dict[str, Any] | None = None   # {date, candidates}


def _detect_candidates(date: str) -> list[dict[str, Any]]:
    """扫描 date 收盘后的烂板接力候选（基于日K缓存）。"""
    from . import replay as rp
    dates = rp.trading_dates()
    if date not in dates:
        return []
    j_date = dates.index(date)
    if j_date < 2:
        return []
    d_t2 = dates[j_date - 2]   # T-2
    bars = rp._load_bars()
    names = {}
    try:
        names = ds.stock_code_names()
    except Exception:  # noqa: BLE001
        pass
    imap = {}
    try:
        imap = ds.industry_map_full()
    except Exception:  # noqa: BLE001
        pass
    out: list[dict[str, Any]] = []
    for code, df in bars.items():
        if code.startswith(("4", "8", "92")) or len(df) < 25:
            continue
        nm = str(names.get(code, ""))
        if "ST" in nm.upper() or "退" in nm:
            continue
        c = df["close"].astype(float)
        o = df["open"].astype(float)
        h = df["high"].astype(float)
        v = df["volume"].astype(float)
        p = df["pct_change"].astype(float)
        n = len(dt:=[str(x)[:10] for x in df["date"]])
        if n < 25 or dt[-1] < date:
            continue  # 停牌/数据陈旧
        th = 19.5 if code.startswith(("688", "689", "300", "301")) else 9.8
        sealed = (p >= th) & (c >= h * 0.995)
        # 连板高度（截至 date）
        run = 0
        for j in range(n):
            run = run + 1 if bool(sealed.iloc[j]) else 0
        if run < 1:
            continue
        # T-2(j-1) 收盘涨停、T-1(j=last) 烂板特征
        j = n - 1
        if j < 1:
            continue
        if not bool(sealed.iloc[j - 1]):
            continue
        limit_px = round(float(c.iloc[j - 1]) * (1 + th / 100), 2)
        if float(o.iloc[j]) >= limit_px * 0.995:
            continue  # 一字/开盘有板 → 排除（规则7：一字上来的不打）
        if float(h.iloc[j]) < limit_px * 0.995:
            continue  # 盘中未触板
        if not (float(p.iloc[j]) >= th and float(c.iloc[j]) >= float(h.iloc[j]) * 0.995):
            continue  # 收盘未封住
        v_ma5 = float(v.rolling(5).mean().iloc[j - 1]) if j >= 5 else 0
        if not (float(v.iloc[j]) >= 1.5 * max(v_ma5, 1)):
            continue  # 未放量（规则3/4）
        out.append({
            "code": code, "name": nm or code,
            "industry": imap.get(code, "--"),
            "streak": run,                    # T-1 收盘连板高度（2=二进三候选）
            "limit_px": limit_px,             # 今日接力确认价（触此价=封板）
            "t1_close": round(float(c.iloc[j]), 2),
            "turnover_pct": round(float(hist_turnover(df).iloc[j]), 2) if len(hist_turnover(df)) > j else None,
            "vol_ratio_1d": round(float(v.iloc[j]) / max(v_ma5, 1), 2),
            "date": date,
        })
    out.sort(key=lambda x: (-x["streak"], -x.get("vol_ratio_1d", 0)))
    return out


def hist_turnover(df: pd.DataFrame) -> pd.Series:
    if "turnover" in df.columns:
        return df["turnover"].astype(float)
    return pd.Series([np.nan] * len(df))


def candidates() -> dict[str, Any]:
    """今日烂板接力候选（按最近已完成交易日缓存）。"""
    global _cand_cache
    from . import replay as rp
    dates = rp.trading_dates()
    d = dates[-1]
    with _lock:
        if _cand_cache and _cand_cache.get("date") == d:
            return _cand_cache
    cands = _detect_candidates(d)
    out = {"date": d, "n": len(cands), "candidates": cands}
    with _lock:
        _cand_cache = out
    return out


def intraday_check(snap: dict[str, dict[str, Any]], now_hour: int) -> list[dict[str, Any]]:
    """盘中接力判定：候选股今日上午封板（规则7：下午板不打）→ 触发提醒。

    - 秒板/封板确认：现价 ≥ 接力确认价（=封板）
    - 排除一字上来（开盘即有板）
    - 规则6：高度≥4 且今日低开 → 标记「必死预警」不提醒买入
    """
    cands = candidates().get("candidates", [])
    fired: list[dict[str, Any]] = []
    for c in cands:
        q = snap.get(c["code"]) or {}
        price = q.get("price")
        pct = q.get("change_pct")
        if price is None or pct is None:
            continue
        p = float(pct)
        limit_px = c["limit_px"]
        sealed_now = float(price) >= limit_px * 0.995
        opened_at_limit = float(q.get("open") or 0) >= limit_px * 0.995
        low_open = (q.get("open") is not None
                    and float(q["open"]) < float(c["t1_close"]))
        note = None
        if c["streak"] >= 4 and low_open:
            note = "高位低开（规则6：三次放量后低开必死）——不接力"
        elif opened_at_limit:
            note = "一字上来（规则7）——不打"
        if not sealed_now:
            continue
        if now_hour >= 11 and now_hour < 13:
            note = note or "午间封板"
        if now_hour >= 13:
            note = note or "下午板（规则7：下午板不打）"
        fired.append({**c, "price": round(float(price), 2), "pct": round(p, 1),
                      "skip": bool(note), "note": note})
    fired.sort(key=lambda x: (x.get("skip"), -(x.get("streak", 0))))
    return fired
