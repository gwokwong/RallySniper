"""新因子快筛第二批（F7~F11）：在 factor_screen 同口径入场集上检验 5 个此前未测因子。

全部因子只使用入场日收盘前已知信息（无前视），全部来自 K线缓存，零盘中接口依赖：
  F7 入场日量比 = 当日量 / 前5日均量（shifted 口径，README 约定）
  F8 入场日开盘溢价 = 当日开盘 / 昨收 - 1（开盘即已知）
  F9 入场日涨幅 = 当日 pct_change（收盘确认口径，与 F5/F6 一致）
  F10 个股连板高度 = 截至昨日的连续封板数（封板 = pct≥板块阈值 且 close≥high×0.995）
  F11 洗盘深度 = 近5日最大单日跌幅（含当日，全样本代理）
对照口径：r_sell（replay 卖出配对，全样本 435 笔量级）+ r_t10（T+10 前向，深样本）。
"""
import sys, json
sys.path.insert(0, "/Users/xxwei/myspace/gupiao/backend")
from typing import Any
import numpy as np
import pandas as pd
from app import replay as rp
from app import datasource as ds
from app import mainwave as mw
from app.config import effective_config

ARCHIVE = "/Users/xxwei/myspace/gupiao/backend/backtests/factor_screen2_result.json"
START = "2026-07-01"

dates = rp.trading_dates()
run_dates = [d for d in dates if d >= START]
bars = rp._load_bars()

cfg = effective_config()
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

# 卫生规则：hold_trd=0 = 数据末端买入的未结算信号（0% 伪收益），剔除
entries = []
dropped_unsettled = 0
for d in run_dates:
    res = rp.replay(d)
    if not res.get("success"):
        continue
    for s in res.get("signals", []):
        if int(s.get("hold_trd") or 0) <= 0:
            dropped_unsettled += 1
            continue
        entries.append({"code": s["code"], "name": s["name"], "date": s["date"],
                        "strategy": s["strategy"], "r_sell": s.get("r_sell")})
print(f"入场 {len(entries)} 笔（剔除数据末端未结算 {dropped_unsettled} 笔）", flush=True)

codes = sorted({e["code"] for e in entries})
ind_frames: dict[str, pd.DataFrame] = {}
for code in codes:
    df = bars.get(code)
    if df is None:
        continue
    try:
        ind_frames[code] = mw._indicators(df, code, cached_names().get(code, code))
    except Exception:
        continue

rows = []
for e in entries:
    code, d = e["code"], e["date"]
    df = ind_frames.get(code)
    if df is None:
        continue
    j = rp._row_of(code, d)
    if j is None or j < 21:
        continue
    n = len(df)
    c = df["close"].to_numpy(dtype=float)
    o = df["open"].to_numpy(dtype=float)
    h = df["high"].to_numpy(dtype=float)
    p = df["pct_change"].to_numpy(dtype=float)
    v = df["volume"].to_numpy(dtype=float)
    r10 = (c[j + 10] / c[j] - 1) * 100 if j + 10 < n else None
    vr = float(v[j]) / float(v[j - 5:j].mean()) if j >= 5 and v[j - 5:j].mean() > 0 else None
    open_prem = (o[j] / c[j - 1] - 1) * 100 if j >= 1 and c[j - 1] > 0 else None
    th = 19.5 if code.startswith(("688", "689", "300", "301")) else 9.8
    sealed = (p >= th) & (c >= h * 0.995)
    streak, st = 0, 0
    for k in range(j):  # 截至昨日
        st = st + 1 if sealed[k] else 0
        streak = st
    wash = float(min(p[max(0, j - 4):j + 1]))  # 近5日最大单日跌幅（含当日）
    rows.append({**e,
                 "f7_vr": round(vr, 2) if vr is not None else None,
                 "f8_open_prem": round(open_prem, 2) if open_prem is not None else None,
                 "f9_pct": round(float(p[j]), 2) if np.isfinite(p[j]) else None,
                 "f10_streak": int(streak),
                 "f11_wash": round(wash, 2),
                 "r_t10": round(r10, 3) if r10 is not None else None})
df_e = pd.DataFrame(rows)
print(f"含特征 {len(df_e)} 笔（T+10 可回填 {df_e['r_t10'].notna().sum()} 笔）\n", flush=True)
json.dump(rows, open("/tmp/factor_screen2_rows.json", "w", encoding="utf-8"), ensure_ascii=False)

arch: dict[str, Any] = {"entries_with_features": len(df_e), "buckets": {}}


def report(key, title, buckets, labels):
    for outcome in ("r_sell", "r_t10"):
        sub = df_e.dropna(subset=[key, outcome])
        print(f"-- {title} → {outcome} --")
        stats = []
        for b, lab in zip(buckets, labels):
            g = sub[sub[key].between(*b)] if isinstance(b, tuple) else sub[sub[key] == b]
            if len(g) == 0:
                continue
            r = g[outcome]
            row = {"label": lab, "n": int(len(g)),
                   "win": round(float((r > 0).mean() * 100), 1),
                   "avg": round(float(r.mean()), 2),
                   "sum_wan": round(float(sum(x / 100 for x in r)), 2)}
            stats.append(row)
            print(f"  {lab:<14}{row['n']:>4}笔  胜率{row['win']:>5.1f}%  均{row['avg']:>+6.2f}%  合计{row['sum_wan']:>+7.2f}万")
        sp = sub[key].rank().corr(sub[outcome].rank()) if len(sub) > 10 else None
        if sp is not None:
            print(f"  Spearman({key},{outcome}) = {sp:+.3f}")
        arch["buckets"].setdefault(key, {})[outcome] = {"stats": stats,
                                                        "spearman": round(float(sp), 3) if sp is not None else None}
        print()


print("===== F7 入场日量比 =====")
report("f7_vr", "F7 量比", [(0, 1.5), (1.5, 2), (2, 3), (3, 5), (5, 999)],
       ["<1.5", "1.5~2", "2~3", "3~5", ">5"])
print("===== F8 开盘溢价 =====")
report("f8_open_prem", "F8 开盘溢价%", [(-99, -2), (-2, 0), (0, 2), (2, 5), (5, 99)],
       ["<-2%", "-2~0%", "0~2%", "2~5%", ">5%"])
print("===== F9 入场日涨幅 =====")
report("f9_pct", "F9 入场日涨幅%", [(-99, 3), (3, 5), (5, 7), (7, 9.8), (9.8, 99)],
       ["<3%", "3~5%", "5~7%", "7~9.8%", "≈封板"])
print("===== F10 个股连板高度（截至昨日） =====")
report("f10_streak", "F10 连板高度", [(-1, 0), (1, 1), (2, 2), (3, 99)],
       ["0(非板)", "1板", "2板", "≥3板"])
print("===== F11 洗盘深度（近5日最大单日跌幅，负值=跌幅） =====")
report("f11_wash", "F11 洗盘深度%", [(-99, -7), (-7, -5), (-5, -3), (-3, 0.01)],
       ["≥7%深洗", "5~7%", "3~5%", "<3%温和"])

print("===== F8 × F9 交互：开盘溢价 ≤2% vs >2% × 当日是否封板 =====")
inter = {}
for outcome in ("r_sell", "r_t10"):
    sub = df_e.dropna(subset=["f8_open_prem", "f9_pct", outcome])
    for prem_lab, prem_g in (("开盘≤2%", sub[sub["f8_open_prem"] <= 2]),
                             ("开盘>2%", sub[sub["f8_open_prem"] > 2])):
        for seal_lab, g in (("未封板", prem_g[prem_g["f9_pct"] < 9.8]),
                            ("≈封板", prem_g[prem_g["f9_pct"] >= 9.8])):
            if len(g) == 0:
                continue
            r = g[outcome]
            key = f"{prem_lab}|{seal_lab}"
            row = {"n": int(len(g)), "win": round(float((r > 0).mean() * 100), 1),
                   "avg": round(float(r.mean()), 2),
                   "sum_wan": round(float(sum(x / 100 for x in r)), 2)}
            inter.setdefault(key, {})[outcome] = row
            print(f"  {key:<16} → {outcome}: {row['n']:>4}笔 胜率{row['win']:>5.1f}% 均{row['avg']:>+6.2f}% 合{row['sum_wan']:>+7.2f}万")
arch["buckets"]["openprem_x_sealed"] = inter

try:
    json.dump(arch, open(ARCHIVE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n归档: {ARCHIVE}")
except Exception as exc:  # noqa: BLE001
    print(f"\n归档失败: {exc}")
