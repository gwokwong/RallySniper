"""通达信指标日K预警引擎（无未来函数口径）。

来源：用户提供的三个通达信副图（主力洗盘/鬼手抄底/三线金点）。
忠实度说明：
- 鬼手抄底（买进A/买进B/抄底/减仓）可完整无未来复现 ✓
- 三线金点的 TROUGHBARS/PEAKBARS/ZIG 为未来函数（当日信号次日会消失，回测虚高），
  用无未来近似替代：「超卖首现」（VAR4首次触发）、「金叉反弹」（DIF上穿DEA+10日新低附近）、
  「CCI超卖」（CCI14<-110）；「格外关注/逃命」依赖未来函数，不做。
- 主力洗盘原文缺 VAR1 定义，无法忠实复现，未实现。
全部基于日K收盘计算（尾盘 14:45 触发时以实时快照近似当日收盘，收盘后为最终值）。
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

import numpy as np
import pandas as pd

from . import datasource as ds
from .config import load_config

_signals_cache: dict[str, dict[str, list[int]]] | None = None
# {signal_name: {code: [触发日行号列表]}}（最新一次全市场扫描结果）


def _tdx_sma(s: pd.Series, n: int, m: int) -> pd.Series:
    """通达信 SMA(X,N,M)：Y=(X*M+Y'*(N-M))/N，等价 ewm(alpha=M/N)。"""
    return s.ewm(alpha=m / n, adjust=False).mean()


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _compute_signals(df: pd.DataFrame) -> dict[str, np.ndarray]:
    """对单只股票计算全部信号布尔序列（对齐 df 行）。"""
    c = df["close"].astype(float)
    o = df["open"].astype(float)
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    v = df["volume"].astype(float)
    n = len(df)
    ref_c = c.shift(1)
    down = c < ref_c

    # ---- 鬼手抄底 ----
    a7 = (2 * c + h + l) / 4
    v1 = a7.rolling(21).min()
    v2 = a7.rolling(21).max()
    rng = (v2 - v1).replace(0, np.nan)
    sk = _ema((a7 - v1) / rng * 100, 7)
    sd = _ema(0.667 * sk.shift(1) + 0.333 * sk, 5)
    ma_v5 = v.rolling(5).mean()
    llv120_prev = l.shift(1).rolling(120).min()

    buy_a = (
        (down.rolling(8).sum() > 4.8)          # 8日中收阴>6/10
        & (v >= 1.5 * ma_v5)                   # 放量
        & ((sk >= sd).rolling(3).max().astype(bool))  # 近3日SK曾≥SD
        & (l.shift(1) <= llv120_prev)          # 昨日最低=120日最低
    )
    cond13 = down.rolling(13).sum() > 7.8      # 13日中收阴>6/10
    sk_gt6 = (sk > sd).rolling(6).min().astype(bool)  # 连续6日SK>SD
    llv5_prev = l.shift(5).rolling(115).min()
    yang452 = (
        (c.shift(4) >= o.shift(4)) & (c.shift(3) > o.shift(3)) & (c.shift(2) > o.shift(2))
    )
    buy_b = (
        cond13 & sk_gt6 & (l.shift(5) <= llv5_prev)
        & yang452 & (o.shift(1) > c.shift(1)) & (o > ref_c)
    )
    chao_di = (
        (c < c.rolling(80).mean() - c.rolling(10).mean() / 3)
        & (v > ma_v5)
        & (c > ref_c * 1.08)
        & (c < c.rolling(13).mean() * 1.3)
    )
    diff_rsi = c - ref_c
    rsi6 = _tdx_sma(diff_rsi.clip(lower=0), 6, 1) / _tdx_sma(diff_rsi.abs(), 6, 1) * 100
    jian_cang = (rsi6.shift(1) >= 82) & (rsi6 < 82)  # RSI6 高位跌破82 → 减仓

    # ---- 三线金点（无未来替代）----
    up1 = _tdx_sma((c - ref_c).clip(lower=0), 34, 1) / _tdx_sma((c - ref_c).abs(), 7, 1) * 100
    up2 = _tdx_sma((c - ref_c).clip(lower=0), 13, 1) / _tdx_sma((c - ref_c).abs(), 13, 1) * 100
    oversold = (up1 < 20) & (up2 < 23)
    first_os = oversold & ~oversold.shift(1).fillna(False).astype(bool)
    dif = _ema(c, 21) - _ema(c, 8)
    dea = _ema(dif, 5)
    cross_dif = (dif.shift(1) <= dea.shift(1)) & (dif > dea)
    near_low10 = l <= l.rolling(10).min()
    golden = cross_dif & near_low10
    tp = (h + l + c) / 3
    cci = (tp - tp.rolling(14).mean()) / (0.015 * tp.rolling(14).apply(lambda x: np.mean(np.abs(x - x.mean()))))
    cci_os = cci < -110

    out = {
        "鬼手买进A": buy_a.fillna(False).to_numpy(),
        "鬼手买进B": buy_b.fillna(False).to_numpy(),
        "鬼手抄底": chao_di.fillna(False).to_numpy(),
        "鬼手减仓": jian_cang.fillna(False).to_numpy(),
        "三线超卖首现": first_os.fillna(False).to_numpy(),
        "三线金叉反弹": golden.fillna(False).to_numpy(),
        "三线CCI超卖": cci_os.fillna(False).to_numpy(),
    }
    return out


def scan_day(date: str) -> list[dict[str, Any]]:
    """扫描某交易日全市场的指标信号（日K收盘口径）。date=YYYY-MM-DD。"""
    global _signals_cache
    cfg = load_config()
    from . import replay as rp
    dates = rp.trading_dates()
    if date not in dates:
        return []
    j = dates.index(date)
    bars = rp._load_bars()
    names = {}
    try:
        names = ds.stock_code_names()
    except Exception:  # noqa: BLE001
        pass
    out: list[dict[str, Any]] = []
    for code, df in bars.items():
        if code.startswith(("4", "8", "92")):
            continue
        nm = str(names.get(code, ""))
        if "ST" in nm.upper() or "退" in nm or len(df) < 45:
            continue
        if str(df["date"].iloc[-1])[:10] < date:
            continue  # 当日停牌
        sigs = _compute_signals(df)
        for name, arr in sigs.items():
            if len(arr) > j and arr[j]:
                out.append({"code": code, "name": nm or code, "signal": name,
                            "price": round(float(df["close"].iloc[j]), 2),
                            "pct": round(float(df["pct_change"].iloc[j]), 2)})
    return out


def scan_today() -> dict[str, Any]:
    """尾盘预警入口：今日信号 + 表格行（供调度器发送）。"""
    cfg = load_config()
    eff = ds.effective_trading_date()
    d = str(eff["date"])
    if "-" not in d:
        d = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    items = scan_day(d)
    rows = [{"strategy": x["signal"], "stage": "trigger" if "减仓" not in x["signal"] else "watch",
             "stage_label": x["signal"] + ("（⚠️抄底类信号回测期望为负，勿盲从）"
                                           if "减仓" not in x["signal"] else "（卖出预警）"),
             "code": x["code"], "name": x["name"], "price": x["price"],
             "change_pct": x["pct"],
             "reasons": [f"通达信指标日K信号：{x['signal']}"]}
            for x in items[:30]]
    downs = [x for x in items if "减仓" in x["signal"]]
    ups = [x for x in items if "减仓" not in x["signal"]]
    return {"success": True, "date": d, "n": len(items),
            "items": items, "rows": rows, "n_down": len(downs), "n_up": len(ups),
            "ups": ups, "downs": downs}
