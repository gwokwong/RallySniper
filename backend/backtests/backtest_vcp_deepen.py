"""VCP 收缩比深化回测：在 factor_screen 同口径入场集（2026-07-01 起）上回答四个问题。

V1 阈值网格：0.45~0.90 步进 0.05，每个阈值的「收缩组 vs 其余」r_sell 对照，找稳健区间；
V2 分月稳健性：7/8/9/10/11 月逐月看 VCP≤0.65 vs >0.65 是否方向一致（防单一时段过拟合，
   参照营收增速因子"7月曾反向"的教训）；
V3 与广度档位交互：全仓档(size_tier=full) / 试错档(probe) 内部分别看 VCP 增量；
V4 尾部剔除 A/B：剔除 vcp≥1.3 的入场对 总收益/胜率/场均 的影响（r_sell 全样本 + r_t10 深样本）。
输出：中文表格 + JSON 归档。口径同 backtests/README（收盘价成交、未计费用、无前视）。
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

ARCHIVE = "/Users/xxwei/myspace/gupiao/backend/backtests/vcp_deepen_result.json"
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

# ===== 入场集（同 factor_screen：replay 全部 trigger + r_sell）+ 广度档位 =====
# 卫生规则（backtests/README「剔除最后一日样本」）：hold_trd=0 = 数据末端买入无次日行情、
# 被标记为 0% 伪收益的未结算信号，一律剔除。
entries = []
dropped_unsettled = 0
for d in run_dates:
    res = rp.replay(d)
    if not res.get("success"):
        continue
    tier = (res.get("gate") or {}).get("size_tier", "")
    for s in res.get("signals", []):
        if int(s.get("hold_trd") or 0) <= 0:
            dropped_unsettled += 1
            continue
        entries.append({"code": s["code"], "name": s["name"], "date": s["date"],
                        "strategy": s["strategy"], "r_sell": s.get("r_sell"),
                        "sold": s.get("sold"), "tier": tier})
print(f"剔除数据末端未结算 {dropped_unsettled} 笔", flush=True)
print(f"入场 {len(entries)} 笔（{run_dates[0]} ~ {run_dates[-1]}）", flush=True)

# ===== 特征：vcp_ratio（与实盘同一 _indicators 代码路径）+ r_t10 =====
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
    if j is None or j < 40:
        continue
    c = df["close"].to_numpy(dtype=float)
    n = len(df)
    r10 = (c[j + 10] / c[j] - 1) * 100 if j + 10 < n else None
    v = df["vcp_ratio"].iloc[j]
    rows.append({**e, "vcp": float(v) if np.isfinite(v) else None,
                 "r_t10": round(r10, 3) if r10 is not None else None})
df_e = pd.DataFrame(rows)
df_e["month"] = df_e["date"].str[:7]
print(f"含特征 {len(df_e)} 笔（T+10 可回填 {df_e['r_t10'].notna().sum()} 笔）\n", flush=True)


def bucket_stats(g: pd.DataFrame, col: str) -> dict[str, Any]:
    if len(g) == 0:
        return {"n": 0}
    r = g[col]
    return {"n": int(len(g)),
            "win": round(float((r > 0).mean() * 100), 1),
            "avg": round(float(r.mean()), 2),
            "sum_wan": round(float(sum(x / 100 for x in r)), 2)}


arch: dict[str, Any] = {"entries_with_features": len(df_e), "sections": {}}

# ===== V1 阈值网格 =====
print("===== V1 VCP 阈值网格（r_sell，全样本）=====")
grid = []
sub_all = df_e.dropna(subset=["vcp", "r_sell"])
for cut in np.arange(0.45, 0.91, 0.05):
    cut = round(float(cut), 2)
    a = sub_all[sub_all["vcp"] <= cut]
    b = sub_all[sub_all["vcp"] > cut]
    sa, sb = bucket_stats(a, "r_sell"), bucket_stats(b, "r_sell")
    grid.append({"cutoff": cut, "shrink": sa, "rest": sb})
    fa = f"{sa['n']:>3}笔 胜率{sa.get('win', 0):>5.1f}% 均{sa.get('avg', 0):>+6.2f}% 合{sa.get('sum_wan', 0):>+6.2f}万" if sa["n"] else "  0笔"
    fb = f"{sb['n']:>3}笔 胜率{sb.get('win', 0):>5.1f}% 均{sb.get('avg', 0):>+6.2f}% 合{sb.get('sum_wan', 0):>+6.2f}万" if sb["n"] else "  0笔"
    print(f"  ≤{cut:<4}: {fa}  ｜ 其余: {fb}")
arch["sections"]["threshold_grid"] = grid
print()

# ===== V2 分月稳健性 =====
print("===== V2 分月稳健性（vcp≤0.65 vs >0.65，r_sell）=====")
monthly = []
for m, g in sub_all.groupby("month"):
    a = g[g["vcp"] <= 0.65]
    b = g[g["vcp"] > 0.65]
    sa, sb = bucket_stats(a, "r_sell"), bucket_stats(b, "r_sell")
    monthly.append({"month": m, "shrink": sa, "rest": sb})
    fa = f"{sa['n']:>3}笔 {sa.get('win', 0):>5.1f}% 均{sa.get('avg', 0):>+6.2f}%" if sa["n"] else "  0笔"
    fb = f"{sb['n']:>3}笔 {sb.get('win', 0):>5.1f}% 均{sb.get('avg', 0):>+6.2f}%" if sb["n"] else "  0笔"
    print(f"  {m}: 收缩 {fa}  ｜ 其余 {fb}")
arch["sections"]["monthly"] = monthly
print()

# ===== V3 广度档位交互 =====
print("===== V3 广度档位 × VCP（r_sell）=====")
tier_stats = []
for tier, label in (("full", "全仓档"), ("probe", "试错档")):
    g = sub_all[sub_all["tier"] == tier]
    if len(g) == 0:
        print(f"  {label}: 0笔")
        continue
    a = g[g["vcp"] <= 0.65]
    b = g[g["vcp"] > 0.65]
    sa, sb = bucket_stats(a, "r_sell"), bucket_stats(b, "r_sell")
    tier_stats.append({"tier": tier, "shrink": sa, "rest": sb})
    fa = f"{sa['n']:>3}笔 {sa.get('win', 0):>5.1f}% 均{sa.get('avg', 0):>+6.2f}%" if sa["n"] else "  0笔"
    fb = f"{sb['n']:>3}笔 {sb.get('win', 0):>5.1f}% 均{sb.get('avg', 0):>+6.2f}%" if sb["n"] else "  0笔"
    print(f"  {label}: 收缩 {fa}  ｜ 其余 {fb}")
arch["sections"]["tier_interaction"] = tier_stats
print()

# ===== V4 尾部剔除 A/B =====
print("===== V4 尾部剔除 A/B（vcp≥1.3 拦截效果）=====")
ab = {}
for col, sub in (("r_sell", df_e.dropna(subset=["r_sell"])),
                 ("r_t10", df_e.dropna(subset=["r_t10"]))):
    keep = sub.dropna(subset=["vcp"])
    base = bucket_stats(keep, col)
    tail = bucket_stats(keep[keep["vcp"] >= 1.3], col)
    after = bucket_stats(keep[keep["vcp"] < 1.3], col)
    ab[col] = {"baseline": base, "tail": tail, "after_filter": after}
    print(f"  [{col}] 基线 {base['n']}笔 {base['win']}% 均{base['avg']:+.2f}% 合{base['sum_wan']:+.2f}万")
    print(f"        尾部≥1.3 {tail['n']}笔 {tail['win']}% 均{tail['avg']:+.2f}% 合{tail['sum_wan']:+.2f}万")
    print(f"        剔除后 {after['n']}笔 {after['win']}% 均{after['avg']:+.2f}% 合{after['sum_wan']:+.2f}万")
arch["sections"]["tail_filter_ab"] = ab
print()

# ===== V5 分战法 × VCP（r_sell）=====
print("===== V5 分战法 × VCP≤0.65（r_sell）=====")
by_strategy = {}
for st, g in sub_all.groupby("strategy"):
    a = g[g["vcp"] <= 0.65]
    b = g[g["vcp"] > 0.65]
    by_strategy[st] = {"shrink": bucket_stats(a, "r_sell"), "rest": bucket_stats(b, "r_sell")}
    sa, sb = by_strategy[st]["shrink"], by_strategy[st]["rest"]
    line = f"  {st}: "
    line += f"收缩 {sa['n']}笔 {sa.get('win', 0)}% 均{sa.get('avg', 0):+.2f}%" if sa["n"] else "收缩 0笔"
    if sb["n"]:
        line += f" ｜ 其余 {sb['n']}笔 {sb.get('win', 0)}% 均{sb.get('avg', 0):+.2f}%"
    print(line)
arch["sections"]["by_strategy"] = by_strategy

try:
    json.dump(arch, open(ARCHIVE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n归档: {ARCHIVE}")
except Exception as exc:  # noqa: BLE001
    print(f"\n归档失败: {exc}")
