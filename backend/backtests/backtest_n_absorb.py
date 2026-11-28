"""分歧强承接 / N型突破 战法回测（日线近似版·回踩窗口口径）。

教学原版是分时级（分时均线/早盘高点/盘口），离线日K只能做骨架近似——所有近似项
在结论中标注"日线代理"。形态（回踩窗口口径，教学"短暂击穿后快速收回"允许 2% 内击穿）：
  D1 放量上攻日：阳线、涨幅≥3%、量比≥1.5
  回踩段（D1 后 1~4 个交易日）：每日低点 ≥ D1收盘×0.98（生命线，短暂击穿可收回）、
        段内收盘不创 D1 新高、段内均量 < 0.8×D1量（缩量回踩）
  D3 突破日：收盘 > D1 最高（日线代理"突破早盘高点"）、阳线、量比≥1.2、开盘 < D1最高（排除一字开盘）
  入场 = D3 收盘确认（教学为盘中突破即买，日线口径偏保守——突破后追入成本更高）。
回测内容：
  A. 全市场 2025-08 起 T+1/5/10 前向收益；2026-06 起叠加现行闸门对照；2026-07 起子样本卖出配对。
  B. 三防线作入场日因子（红盘/开盘支撑/站稳日内上沿）：N型入场集 + 洗盘反包入场集（能否给现有战法加分）。
  C. 突破日量比分桶（温和放量 vs 过度放量）。
"""
import sys, json
sys.path.insert(0, "/Users/xxwei/myspace/gupiao/backend")
from collections import defaultdict
from typing import Any

import numpy as np
import pandas as pd

from app import replay as rp
from app import datasource as ds
from app import sellwatch as sw
from app.config import effective_config

ARCHIVE = "/Users/xxwei/myspace/gupiao/backend/backtests/n_absorb_result.json"
START = "2025-08-01"

dates = rp.trading_dates()
last_date = dates[-1]
bars = rp._load_bars()
cfg = effective_config()
reg = cfg.market_regime
try:
    names = ds.stock_code_names()
except Exception:
    names = {}
try:
    imap = ds.industry_map_full()
except Exception:
    imap = {}

# ===== 闸门复刻（与 replay 同口径，2026-06-01 起可用）=====
sent = rp.market_sentiment()
mom = rp.market_momentum(5)
pool_pct: dict[str, list[float]] = defaultdict(list)
for code, df in bars.items():
    if code.startswith(("4", "8", "92")):
        continue
    p = df["pct_change"].to_numpy(dtype=float)
    c = df["close"].to_numpy(dtype=float)
    h = df["high"].to_numpy(dtype=float)
    th = 19.5 if code.startswith(("688", "689", "300", "301")) else 9.8
    sealed = (p >= th) & (c >= h * 0.995)
    dtl = [str(x)[:10] for x in df["date"]]
    for j in range(1, len(sealed)):
        if sealed[j - 1] and dtl[j] >= "2026-06-01":
            pool_pct[dtl[j]].append(float(p[j]))
ignition = {d: float(np.median(v)) for d, v in pool_pct.items() if len(v) >= 3}


def gate_of(d: str) -> str:
    dtl = dates
    if d not in dtl:
        return ""
    k = dtl.index(d)
    if k < 1:
        return "闸门未点火"
    prev_d = dtl[k - 1]
    prem = ignition.get(d)
    if prem is None or prem < float(reg.ignition_threshold):
        return "闸门未点火"
    br = (sent.get(prev_d) or {}).get("bomb_rate")
    if br is not None and reg.ignition_bomb_gate_enabled and br > float(reg.ignition_bomb_rate_max):
        return "炸板闸门"
    m = mom.get(prev_d)
    if reg.momentum_dead_zone_enabled and m is not None \
            and float(reg.momentum_dead_low) <= m < float(reg.momentum_dead_high):
        return "动量死区"
    return ""


# ===== A. N型突破全市场扫描（回踩窗口口径）=====
events: list[dict[str, Any]] = []
seen: set[tuple[str, str]] = set()
for code, df in bars.items():
    if code.startswith(("4", "8", "92")) or len(df) < 40:
        continue
    nm = str(names.get(code, ""))
    if "ST" in nm.upper() or "退" in nm:
        continue
    c = df["close"].to_numpy(dtype=float)
    o = df["open"].to_numpy(dtype=float)
    h = df["high"].to_numpy(dtype=float)
    l = df["low"].to_numpy(dtype=float)
    v = df["volume"].to_numpy(dtype=float)
    p = df["pct_change"].to_numpy(dtype=float)
    dtl = [str(x)[:10] for x in df["date"]]
    n = len(c)
    vma5 = pd.Series(v).rolling(5).mean().shift(1).to_numpy()
    vr = np.where(vma5 > 0, v / vma5, np.nan)
    d1_mask = (c > o) & (p >= 3.0) & (vr >= 1.5)
    for i1 in np.where(d1_mask[: n - 12])[0]:
        i1 = int(i1)
        high1, close1, vol1 = h[i1], c[i1], v[i1]
        breakout_j, pull_ok, pull_vols = None, True, []
        for j in range(i1 + 1, min(i1 + 6, n - 10)):
            if l[j] < close1 * 0.98:       # 生命线击穿超 2% → 形态失败
                pull_ok = False
                break
            if c[j] > high1:               # 突破日
                breakout_j = j
                break
            pull_vols.append(v[j])
        if not pull_ok or breakout_j is None or breakout_j < i1 + 2 or not pull_vols:
            continue                       # 至少 1 天缩量回踩（次日直接新高=连续攻击非N型）
        j = breakout_j
        if float(np.mean(pull_vols)) >= vol1 * 0.8:
            continue                       # 回踩未缩量
        if max(c[i1 + 1: j]) >= high1:
            continue                       # 回踩段已创新高
        if not (c[j] > o[j] and o[j] < high1 and np.isfinite(vr[j]) and vr[j] >= 1.2):
            continue                       # 突破日须阳线+非一字+放量
        d3_date = dtl[j]
        if d3_date < START or (code, d3_date) in seen:
            continue
        seen.add((code, d3_date))
        rng = h[j] - l[j]
        has_next = j + 10 < n
        events.append({
            "code": code, "name": nm or code, "industry": imap.get(code, ""),
            "date": d3_date, "pull_days": j - i1 - 1,
            "pct": round(float(p[j]), 2),
            "r_t1": round((c[j + 1] / c[j] - 1) * 100, 3) if j + 1 < n else None,
            "r_t5": round((c[j + 5] / c[j] - 1) * 100, 3) if j + 5 < n else None,
            "r_t10": round((c[j + 10] / c[j] - 1) * 100, 3) if j + 10 < n else None,
            "f_red": bool(c[j] > c[j - 1]),
            "f_open": bool(l[j] >= o[j]),
            "f_vwap": bool(rng > 0 and (c[j] - l[j]) / rng >= 0.6),
            "vr3": round(float(vr[j]), 2) if np.isfinite(vr[j]) else None,
            "gate": gate_of(d3_date) if d3_date >= "2026-06-01" else "六月前(闸门未标定)",
        })
print(f"N型突破事件 {len(events)} 件（{START} ~ {last_date}）", flush=True)
ev = pd.DataFrame(events)

arch: dict[str, Any] = {"events_total": len(events), "sections": {}}


def agg(g, col):
    rs = g[col].dropna() if len(g) else g[col]
    if len(g) == 0 or len(rs) == 0:
        return {"n": 0}
    return {"n": int(len(rs)), "win": round(float((rs > 0).mean() * 100), 1),
            "avg": round(float(rs.mean()), 2), "med": round(float(rs.median()), 2),
            "sum_wan": round(float(rs.sum() / 100), 2)}


# ===== A1 全样本前向收益 =====
print("===== A1 N型全样本前向收益（D3收盘买入）=====")
for col, lab in (("r_t1", "T+1"), ("r_t5", "T+5"), ("r_t10", "T+10")):
    a = agg(ev, col)
    print(f"  {lab}: {a['n']}件 胜率{a.get('win', 0):>5.1f}% 均{a.get('avg', 0):>+6.2f}% 中位{a.get('med', 0):>+6.2f}% 合{a.get('sum_wan', 0):>+7.2f}万")
arch["sections"]["forward_all"] = {c: agg(ev, c) for c in ("r_t1", "r_t5", "r_t10")}
print()

# ===== A2 闸门叠加对照（2026-06 起）=====
print("===== A2 闸门叠加（2026-06 起样本）=====")
sub6 = ev[ev["date"] >= "2026-06-01"]
for g_lab, g in (("开闸日", sub6[sub6["gate"] == ""]), ("拦截日", sub6[sub6["gate"] != ""])):
    a = agg(g, "r_t5")
    b = agg(g, "r_t10")
    print(f"  {g_lab}: {a['n']}件 T5胜率{a.get('win', 0):>5.1f}% 均{a.get('avg', 0):>+6.2f}% ｜ T10均{b.get('avg', 0):>+6.2f}%")
gate_open = sub6[sub6["gate"] == ""]
arch["sections"]["gate_split"] = {
    "open": {c: agg(gate_open, c) for c in ("r_t1", "r_t5", "r_t10")},
    "blocked": {c: agg(sub6[sub6["gate"] != ""], c) for c in ("r_t1", "r_t5", "r_t10")},
}
print()

# ===== B1 三防线因子（N型入场集）=====
print("===== B1 三防线因子 × N型入场 =====")
fac_rows = {}
for fac, lab in (("f_red", "红盘(close>昨收)"), ("f_open", "开盘支撑(low≥open)"), ("f_vwap", "站稳日内上沿(≥60%)")):
    a = agg(ev[ev[fac]], "r_t10")
    b = agg(ev[~ev[fac]], "r_t10")
    fac_rows[fac] = {"yes_t10": a, "no_t10": b}
    print(f"  {lab}: 成立 {a['n']}件 T10胜率{a.get('win', 0):>5.1f}% 均{a.get('avg', 0):>+6.2f}%"
          f" ｜ 不成立 {b['n']}件 {b.get('win', 0):>5.1f}% / {b.get('avg', 0):>+6.2f}%")
arch["sections"]["lines_n"] = fac_rows
print()

# ===== B2 三防线因子 × 洗盘反包入场集（能否给现有战法加分）=====
print("===== B2 三防线因子 × 洗盘反包入场集（2026-07起，r_sell 同口径）====="
      "（已在前一轮报告过：红盘/上沿由反包定义隐含，开盘支撑不成立组更强）", flush=True)
run_dates = [d for d in dates if d >= "2026-07-01"]
rp.effective_config = lambda: cfg
_names = None
def cached_names():
    global _names
    if _names is None:
        try:
            _names = ds.stock_code_names()
        except Exception:
            _names = {}
    return _names
rp.ds.stock_code_names = cached_names
wb_rows = []
for d in run_dates:
    res = rp.replay(d)
    if not res.get("success"):
        continue
    for s in res.get("signals", []):
        if int(s.get("hold_trd") or 0) <= 0:
            continue
        df = bars.get(s["code"])
        if df is None:
            continue
        dtl = [str(x)[:10] for x in df["date"]]
        if s["date"] not in dtl:
            continue
        i = dtl.index(s["date"])
        if i < 1:
            continue
        c_, o_, h_, l_ = (df[k].to_numpy(dtype=float) for k in ("close", "open", "high", "low"))
        rng = h_[i] - l_[i]
        wb_rows.append({"code": s["code"], "date": s["date"], "r_sell": s.get("r_sell"),
                        "f_red": bool(c_[i] > c_[i - 1]), "f_open": bool(l_[i] >= o_[i]),
                        "f_vwap": bool(rng > 0 and (c_[i] - l_[i]) / rng >= 0.6)})
wb = pd.DataFrame(wb_rows)
wb_fac = {}
for fac, lab in (("f_red", "红盘"), ("f_open", "开盘支撑"), ("f_vwap", "站稳日内上沿")):
    a = agg(wb[wb[fac]], "r_sell")
    b = agg(wb[~wb[fac]], "r_sell")
    wb_fac[fac] = {"yes": a, "no": b}
    print(f"  {lab}: 成立 {a['n']}笔 胜率{a.get('win', 0):>5.1f}% 均{a.get('avg', 0):>+6.2f}%"
          f" ｜ 不成立 {b['n']}笔 {b.get('win', 0):>5.1f}% / {b.get('avg', 0):>+6.2f}%")
arch["sections"]["lines_wash"] = wb_fac
arch["wash_entries"] = len(wb)
print()

# ===== C. 突破日量比分桶 =====
print("===== C 突破日量比分桶（温和放量 vs 过度放量）=====")
subv = ev.dropna(subset=["vr3"])
vr_stats = []
for b, lab in zip([(1.2, 2), (2, 3), (3, 99)], ["1.2~2温和", "2~3", ">3过度"]):
    g = subv[subv["vr3"].between(*b)]
    a = agg(g, "r_t10")
    vr_stats.append({"label": lab, **a})
    print(f"  突破量比{lab:<10} {a['n']:>4}件 T10胜率{a.get('win', 0):>5.1f}% 均{a.get('avg', 0):>+6.2f}%")
arch["sections"]["vr_buckets"] = vr_stats

# ===== A3 卖出配对子样本（2026-07 起开闸事件，与 replay 同口径）=====
print("===== A3 N型卖出配对子样本（2026-07 起开闸日，D3收盘买入+现行5通道）=====")
sell_rules = cfg.sell.model_dump()
_orig_completed = sw._completed_history
pair_rows = []
sub_pair = ev[(ev["date"] >= "2026-07-01") & (ev["gate"] == "")]
print(f"开闸 N型事件 {len(sub_pair)} 件，逐笔配对中…", flush=True)
for e in sub_pair.itertuples():
    df = bars.get(e.code)
    if df is None:
        continue
    dtl = [str(x)[:10] for x in df["date"]]
    if e.date not in dtl:
        continue
    i = dtl.index(e.date)
    cost = float(df["close"].iloc[i])
    n = len(df)
    r_sell, hold, why = None, 0, "持有至期末"
    for j in range(i + 1, n):
        window = df.iloc[:j].copy()
        sw._completed_history = lambda cc, w=window: w
        row = df.iloc[j]
        quote = {"price": float(row["close"]), "open": float(row["open"]),
                 "change_pct": float(row["pct_change"]), "low": float(row["low"]),
                 "volume": float(row["volume"])}
        try:
            found = sw._evaluate(e.code, e.name, quote, sell_rules, "confirm", cost=cost)
        except Exception:  # noqa: BLE001
            found = []
        if found:
            sw._completed_history = _orig_completed
            r_sell = (float(row["close"]) / cost - 1) * 100
            hold, why = j - i, str(found[0].get("strategy", "?"))
            break
    sw._completed_history = _orig_completed
    if r_sell is None:
        r_sell = (float(df["close"].iloc[-1]) / cost - 1) * 100
        hold = n - 1 - i
    pair_rows.append({"code": e.code, "date": e.date, "r_sell": round(r_sell, 2), "hold": hold, "why": why})
pw = pd.DataFrame(pair_rows)
a = agg(pw, "r_sell")
print(f"  配对 {a['n']} 件: 胜率{a.get('win', 0):>5.1f}% 均{a.get('avg', 0):>+6.2f}% 中位{a.get('med', 0):>+6.2f}% 合{a.get('sum_wan', 0):>+6.2f}万")
arch["sections"]["pair_gated"] = a

try:
    json.dump(arch, open(ARCHIVE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n归档: {ARCHIVE}")
except Exception as exc:  # noqa: BLE001
    print(f"\n归档失败: {exc}")
