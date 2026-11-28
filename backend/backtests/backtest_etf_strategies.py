"""ETF 策略回测：在长历史日K上验证 4 个候选策略，结论决定 etfwatch 默认开关。

口径：东财 qfq 日K（被限流时新浪不复权兜底，标记 degraded）；收盘价成交、次日执行滞后
（S2 轮动）、双边 10bp 成本；参数扫描防过拟合；空仓去处两版（现金 vs 货币ETF 511260）。
策略（候选规则，详见计划）：
  S1 趋势生命线：上穿 MA20(+放量变体) 买入；MA20 破位（斜率向下+1%缓冲+2日确认）卖出
  S2 双动量轮动：池内 N 日动量 top-1，绝对动量≤0 → 安全资产；周/月调仓、buffer 降换手
  S3 情绪联动：个股点火闸门(premium≥3)持有、退潮(premium<0 连续2日)离场（窗口仅 2025-07+）
  S4 波动收缩突破：布林带宽 squeeze 后放量突破上轨；跌回中轨/下轨离场
指标：CAGR / 最大回撤 / Calmar / 月度胜率 / 交易次数 / 年换手。基线：买入持有、等权池。
注意：ETF 代码与名称对应关系待 universe 数据源恢复后核对（当前东财限流），本报告按代码输出。
"""
import sys, json, time
sys.path.insert(0, "/Users/xxwei/myspace/gupiao/backend")
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app import datasource as ds
from app import replay as rp
from app.config import effective_config

ARCHIVE = "/Users/xxwei/myspace/gupiao/backend/backtests/etf_strategies_result.json"
ARCHIVE_DIR = "/Users/xxwei/myspace/gupiao/backend/backtests/etf_hist"
COST = 0.001  # 双边 10bp

POOL = [
    ("588000", "科创50ETF"),
    ("588200", "科创芯片ETF"),
    ("512480", "半导体ETF"),
    ("515880", "通信ETF"),
    ("159326", "电网设备ETF"),
    ("512000", "券商ETF"),
    ("512660", "军工ETF"),
    ("515030", "新能源车ETF"),
    ("512010", "医药ETF"),
    ("518880", "黄金ETF"),
]
SAFE_ASSETS = {"cash": None, "511260": ("511260", "国债ETF")}
DAYS = 1600

cfg = effective_config()
reg = cfg.market_regime
dates_stock = rp.trading_dates()
bars_stock = rp._load_bars()

# ===== 数据获取（东财 qfq 优先，新浪兜底；本地归档避免重复拉取）=====
import pathlib
Path(ARCHIVE_DIR).mkdir(parents=True, exist_ok=True)
hist: dict[str, pd.DataFrame] = {}
degraded: dict[str, bool] = {}
for code, name in POOL + [SAFE_ASSETS["511260"]]:
    f = pathlib.Path(ARCHIVE_DIR) / f"{code}.csv"
    if f.exists():
        df = pd.read_csv(f)
        df["date"] = df["date"].astype(str)
        hist[code] = df
        degraded[code] = bool(df.get("degraded", pd.Series([0])).iloc[-1])
        continue
    df = ds.etf_history(code, days=DAYS)
    if df is None or len(df) < 100:
        print(f"[warn] {code} {name} 历史不足({0 if df is None else len(df)}行)，跳过")
        continue
    df["degraded"] = 1 if bool(df["turnover"].isna().all()) else 0
    df.to_csv(f, index=False)
    hist[code] = df
    degraded[code] = bool(df["degraded"].iloc[-1])
    time.sleep(0.8)
print(f"获取 {len(hist)} 只ETF历史：", {c: len(df) for c, df in hist.items()}, flush=True)

# ===== S3 所需：点火 premium 序列（来自个股K线缓存，2025-07+）=====
pool_pct: dict[str, list[float]] = defaultdict(list)
for code, df in bars_stock.items():
    if code.startswith(("4", "8", "92")):
        continue
    p = df["pct_change"].to_numpy(dtype=float)
    c = df["close"].to_numpy(dtype=float)
    h = df["high"].to_numpy(dtype=float)
    th = 19.5 if code.startswith(("688", "689", "300", "301")) else 9.8
    sealed = (p >= th) & (c >= h * 0.995)
    dtl = [str(x)[:10] for x in df["date"]]
    for j in range(1, len(sealed)):
        if sealed[j - 1]:
            pool_pct[dtl[j]].append(float(p[j]))
premium = {d: float(np.median(v)) for d, v in pool_pct.items() if len(v) >= 3}


# ===== 指标 =====
def metrics(equity: pd.Series, trades: int, turnover_y: float) -> dict[str, Any]:
    eq = equity.dropna()
    if len(eq) < 30:
        return {"n": len(eq)}
    years = len(eq) / 244.0
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / max(years, 1e-9)) - 1
    dd = eq / eq.cummax() - 1
    mdd = float(dd.min())
    ret = eq.pct_change().dropna()
    sharpe = float(ret.mean() / ret.std() * np.sqrt(244)) if ret.std() > 0 else 0
    monthly = eq.resample("ME").last().pct_change().dropna()
    return {"n": len(eq), "cagr": round(cagr * 100, 1), "mdd": round(mdd * 100, 1),
            "calmar": round(cagr / abs(mdd), 2) if mdd < 0 else None,
            "sharpe": round(sharpe, 2),
            "monthly_win": round(float((monthly > 0).mean() * 100), 1),
            "trades": trades, "turnover_y": round(turnover_y, 1)}


def run_single(code: str, rule: str, variant: str = "") -> dict[str, Any]:
    """S1/S4 单标的收盘买卖回测（信号日收盘成交，无滞后——与个股回测口径一致）。"""
    df = hist[code]
    c = df["close"].to_numpy(dtype=float)
    o = df["open"].to_numpy(dtype=float)
    v = df["volume"].to_numpy(dtype=float)
    dtl = [str(x)[:10] for x in df["date"]]
    ma20 = pd.Series(c).rolling(20).mean().to_numpy()
    vma5 = pd.Series(v).rolling(5).mean().shift(1).to_numpy()
    ma20_prev = pd.Series(ma20).shift(1).to_numpy()
    ma20_slope = pd.Series(ma20).diff(5).to_numpy()
    close_s = pd.Series(c)
    bb_mid = close_s.rolling(20).mean()
    bb_std = close_s.rolling(20).std(ddof=0)
    bb_up = (bb_mid + 2 * bb_std).to_numpy()
    bw = ((bb_mid + 2 * bb_std) - (bb_mid - 2 * bb_std)) / bb_mid * 100
    bw_q25 = bw.rolling(60, min_periods=10).quantile(0.25).shift(1).to_numpy()
    squeeze_prev = (bw.shift(1).to_numpy() <= np.maximum(bw_q25 * 1.1, 6.0))
    n = len(c)
    pos = False
    eq = 1.0
    eqs, trades = [], 0
    for i in range(20, n):
        r = c[i] / c[i - 1] - 1
        if pos:
            eq *= (1 + r)
        if not pos:
            if rule == "S1" and np.isfinite(ma20[i]) and c[i] > ma20[i] and c[i - 1] <= ma20_prev[i]:
                if variant == "vol" and not (np.isfinite(vma5[i]) and v[i] >= 1.2 * vma5[i]):
                    pass
                else:
                    pos, trades = True, trades + 1
            elif rule == "S4" and squeeze_prev[i] and c[i] > bb_up[i] and \
                    np.isfinite(vma5[i]) and v[i] >= 1.2 * vma5[i]:
                pos, trades = True, trades + 1
        else:
            if rule == "S1" and np.isfinite(ma20[i]) and c[i] < ma20[i] * 0.99 \
                    and ma20_slope[i] < 0 and c[i - 1] < ma20[i - 1] * 0.99:
                # 连续2日确认完成，确认日尾盘卖出（与 14:50 确认口径一致）
                pos, trades = False, trades + 1
            elif rule == "S4" and c[i] < bb_mid.iloc[i]:
                pos, trades = False, trades + 1
        eqs.append(eq)
    idx = pd.to_datetime(pd.Series(dtl[20:n]))
    return metrics(pd.Series(eqs, index=idx), trades, 0)


def run_rotation(lb: int, freq: str, safe_code: str | None, buffer_pct: float = 0.2) -> dict[str, Any]:
    """S2 双动量轮动：信号日收盘算分，次日收盘执行（滞后1日），双边10bp。"""
    codes = [c for c, _ in POOL if c in hist]
    closes = pd.DataFrame({c: hist[c].set_index(pd.to_datetime(hist[c]["date"].str[:10]))["close"]
                           for c in codes}).sort_index().ffill(limit=5)
    safe_close = None
    if safe_code and safe_code in hist:
        safe_close = hist[safe_code].set_index(pd.to_datetime(hist[safe_code]["date"].str[:10]))["close"]
    mom = closes / closes.shift(lb) - 1
    # 调仓信号日：周/月最后交易日
    key = closes.index.to_series()
    grp = key.dt.isocalendar() if freq == "weekly" else None
    if freq == "weekly":
        is_sig = key.dt.dayofweek.eq(4) | (key.shift(-1).dt.isocalendar().week != key.dt.isocalendar().week)
    else:
        is_sig = key.dt.month.ne(key.shift(-1).dt.month)
    eq, holding, trades, turnover_sum = 1.0, None, 0, 0.0
    eqs = []
    pending = None
    idx = closes.index
    for i, d in enumerate(idx):
        r = closes.iloc[i] / closes.iloc[i - 1] - 1 if i > 0 else 0.0
        if holding:
            if holding in closes.columns:
                eq *= (1 + float(r[holding]))
            elif safe_close is not None and d in safe_close.index:
                prev_d = idx[i - 1]
                if prev_d in safe_close.index:
                    eq *= (1 + float(safe_close[d] / safe_close[prev_d] - 1))
        elif safe_close is not None and d in safe_close.index:
            prev_d = idx[i - 1]
            if prev_d in safe_close.index:
                eq *= (1 + float(safe_close[d] / safe_close[prev_d] - 1))
        if pending is not None:
            tgt = pending
            if tgt != holding:
                trades += 1
                turnover_sum += 1.0
                eq *= (1 - COST)
            holding = tgt if tgt else None
            pending = None
        if bool(is_sig.iloc[i]) and i > lb and np.isfinite(mom.iloc[i]).sum() >= 3:
            scores = mom.iloc[i].dropna().sort_values(ascending=False)
            best = scores.index[0] if len(scores) else None
            best_v = float(scores.iloc[0]) if len(scores) else None
            if best_v is not None and best_v > 0:
                # buffer：在位资产仍居前且得分不劣于 best×(1-buffer) 则保留
                if holding and holding in scores.index and scores[holding] >= best_v * (1 - buffer_pct) \
                        and scores[holding] > 0:
                    tgt = holding
                else:
                    tgt = best
            else:
                tgt = safe_code  # 绝对动量过滤 → 安全资产/现金
            pending = tgt
        eqs.append(eq)
    return metrics(pd.Series(eqs, index=idx), trades, 0)


def run_s3(code: str) -> dict[str, Any]:
    """S3 情绪联动：点火闸门开(≥3%)持有、premium<0 连续2日离场；窗口 2025-07+。"""
    df = hist[code]
    dtl = [str(x)[:10] for x in df["date"]]
    c = df["close"].to_numpy(dtype=float)
    n = len(c)
    pos, neg_run, eq, trades = False, 0, 1.0, 0
    eqs = []
    for i in range(1, n):
        r = c[i] / c[i - 1] - 1
        if pos:
            eq *= (1 + r)
        prem = premium.get(dtl[i])
        if not pos and prem is not None and prem >= float(reg.ignition_threshold):
            pos, trades = True, trades + 1
        elif pos and prem is not None and prem < 0:
            neg_run += 1
            if neg_run >= 2:
                pos, trades, neg_run = False, trades + 1, 0
        elif prem is not None and prem >= 0:
            neg_run = 0
        eqs.append(eq)
    idx = pd.to_datetime(pd.Series(dtl[1:n]))
    return metrics(pd.Series(eqs, index=idx), trades, 0)


arch: dict[str, Any] = {"pool": [list(x) for x in POOL], "degraded": degraded, "sections": {}}

# ===== S1/S4 逐标的 =====
print("===== S1 趋势生命线（上穿MA20买 / 破位2日确认卖）=====", flush=True)
s1, s1v, s4 = {}, {}, {}
for code, name in POOL:
    if code not in hist:
        continue
    a = run_single(code, "S1")
    b = run_single(code, "S1", variant="vol")
    d = run_single(code, "S4")
    s1[code], s1v[code], s4[code] = a, b, d
    bh_idx = pd.to_datetime(hist[code]["date"].str[:10].to_numpy()[20:])
    bh = metrics(pd.Series(hist[code]["close"].to_numpy()[20:], index=bh_idx), 1, 0)
    print(f"  {name}({code}) S1: CAGR{a.get('cagr')}% MDD{a.get('mdd')}% Cal{a.get('calmar')} "
          f"交易{a.get('trades')} ｜ S1+量: {b.get('cagr')}%/{b.get('mdd')}% ｜ S4: {d.get('cagr')}%/{d.get('mdd')}% "
          f"｜ 买入持有: {bh.get('cagr')}%/{bh.get('mdd')}%")
arch["sections"]["S1"] = s1
arch["sections"]["S1_vol"] = s1v
arch["sections"]["S4"] = s4
print()

# ===== S2 轮动 + 参数扫描 =====
print("===== S2 双动量轮动（top-1，参数扫描）=====", flush=True)
rot = {}
for lb in (5, 10, 15, 20):
    for freq in ("weekly", "monthly"):
        for safe_lab, safe_code in (("cash", None), ("511260", "511260")):
            if safe_code and safe_code not in hist:
                continue
            key = f"lb{lb}_{freq}_{safe_lab}"
            rot[key] = run_rotation(lb, freq, safe_code)
print("  参数扫描（CAGR%/MDD%/Calmar/月胜率/交易/年换手）:")
for key, m in sorted(rot.items(), key=lambda x: -(x[1].get("cagr") or -99)):
    print(f"  {key:<22} CAGR {m.get('cagr'):>6}%  MDD {m.get('mdd'):>6}%  Cal {m.get('calmar')}  "
          f"月胜 {m.get('monthly_win')}%  交易{m.get('trades')}  换手{m.get('turnover_y')}x/年")
# 等权池基准
codes = [c for c, _ in POOL if c in hist]
closes = pd.DataFrame({c: hist[c].set_index(pd.to_datetime(hist[c]["date"].str[:10]))["close"] for c in codes}).sort_index().ffill(limit=5)
ew = closes.pct_change().mean(axis=1).add(1).cumprod()
rot["bench_equal"] = metrics(ew, 0, 0)
arch["sections"]["S2_rotation"] = rot
print()

# ===== S3 情绪联动 =====
print("===== S3 情绪联动（点火持有/退潮离场，窗口 2025-07+，仅约16个月）=====", flush=True)
s3 = {}
for code, name in POOL:
    if code not in hist:
        continue
    a = run_s3(code)
    s3[code] = a
    print(f"  {name}({code}): CAGR {a.get('cagr')}% MDD {a.get('mdd')}% 月胜 {a.get('monthly_win')}% 交易 {a.get('trades')}")
arch["sections"]["S3"] = s3

try:
    json.dump(arch, open(ARCHIVE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n归档: {ARCHIVE}")
except Exception as exc:  # noqa: BLE001
    print(f"\n归档失败: {exc}")
