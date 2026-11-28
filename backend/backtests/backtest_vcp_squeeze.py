"""波动收缩因子回测：在 replay 入场集（trigger，2026-07-01 起）上检验
VCP 收缩比与布林 squeeze 对战法收益是否有增量。

特征全部用 mw._indicators（与实盘同一代码路径）在 K线缓存上计算，
不含当前 bar（防前视）：
  V1 vcp_ratio   近10日振幅(不含当日)/前20日振幅(截至11日前)，<1 收敛
  V2 bb_squeeze_prior  入场前一 bar 布林带宽 ≤ max(6%, 前60日带宽25分位×1.1) 且 <12%
  V3 squeeze_release   昨日squeeze + 今日带宽放大>5% + 收盘突破上轨（挤压释放突破）
结果口径沿用 backtests/README：r_sell = replay 卖出配对收益；另加
T+5/T+10 前向收益（个股自身第 N 根K线收盘，剔除最后一日无前向数据的入场）。
输出：分桶 胜率/平均/合计万 + Spearman，JSON 存 /tmp 与归档副本。
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

OUT_JSON = "/tmp/vcp_squeeze_result.json"
ARCHIVE = "/Users/xxwei/myspace/gupiao/backend/backtests/vcp_squeeze_result.json"
START = "2026-07-01"

dates = rp.trading_dates()
run_dates = [d for d in dates if d >= START]
bars = rp._load_bars()

# 与 factor_screen 相同的配置/名称打桩（吃 replay 磁盘缓存）
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

entries = []
for d in run_dates:
    res = rp.replay(d)
    if not res.get("success"):
        continue
    for s in res.get("signals", []):
        entries.append({"code": s["code"], "name": s["name"], "date": s["date"],
                        "r_sell": s.get("r_sell"), "price": s.get("price"),
                        "strategy": s.get("strategy")})
print(f"replay 入场 {len(entries)} 笔（{run_dates[0]} ~ {run_dates[-1]}）", flush=True)

# 逐股一次算好指标列（rolling 列与截断无关，逐行取值即逐日截断口径）
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
dropped_fwd = 0
for e in entries:
    code, d = e["code"], e["date"]
    df = ind_frames.get(code)
    if df is None:
        continue
    j = rp._row_of(code, d)
    if j is None or j < 40:  # vcp 需前30根 + 保险余量
        continue
    n = len(df)
    c = df["close"].to_numpy(dtype=float)
    r5 = (c[j + 5] / c[j] - 1) * 100 if j + 5 < n else None
    r10 = (c[j + 10] / c[j] - 1) * 100 if j + 10 < n else None
    if r5 is None or r10 is None:
        dropped_fwd += 1  # 卫生规则：剔除无前向数据的入场（最后几日）
        continue
    cur, prev = df.iloc[j], df.iloc[j - 1]
    bw, bw_prev = float(cur["bb_bw"]), float(prev["bb_bw"])
    sq_prior = bool(cur["bb_squeeze_prior"])
    release = bool(sq_prior and np.isfinite(bw) and np.isfinite(bw_prev)
                   and bw > bw_prev * 1.05 and c[j] > float(cur["bb_up"]))
    rows.append({**e, "vcp": float(cur["vcp_ratio"]) if np.isfinite(cur["vcp_ratio"]) else None,
                 "sq_prior": sq_prior, "release": release,
                 "pct_b": (c[j] - float(cur["bb_dn"])) / (float(cur["bb_up"]) - float(cur["bb_dn"]))
                          if float(cur["bb_up"]) > float(cur["bb_dn"]) else None,
                 "r_t5": round(r5, 3), "r_t10": round(r10, 3)})
df_e = pd.DataFrame(rows)
print(f"含特征值 {len(df_e)} 笔（剔除无前向数据 {dropped_fwd} 笔）\n", flush=True)
json.dump(rows, open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False)


BUCKETS = {
    "vcp": ("V1 VCP收缩比", [(-9, 0.65), (0.65, 1.0), (1.0, 1.3), (1.3, 99)],
            ["强收敛<0.65", "收敛0.65~1", "轻扩张1~1.3", "扩张≥1.3"]),
    "sq_prior": ("V2 昨日布林squeeze", [True, False], ["squeeze", "非squeeze"]),
    "release": ("V3 挤压释放突破", [True, False], ["释放突破", "非释放"]),
    "shrunk": ("A/B 收缩组合(任一)", [True, False], ["收缩组", "其余"]),
}
arch: dict[str, Any] = {
    "start": START, "end": run_dates[-1], "entries_total": len(entries),
    "entries_with_features": len(df_e), "dropped_no_forward": dropped_fwd,
    "buckets": {},
}


def report(key, outcome):
    title, buckets, labels = BUCKETS[key]
    col = key if key != "shrunk" else "shrunk"
    sub = df_e.dropna(subset=[col] if col in df_e.columns else []).dropna(subset=[outcome])
    print(f"-- {title} → {outcome} --")
    stats = []
    for b, lab in zip(buckets, labels):
        g = sub[sub[col].between(*b)] if isinstance(b, tuple) else sub[sub[col] == b]
        if len(g) == 0:
            continue
        r = g[outcome]
        row = {"label": lab, "n": int(len(g)),
               "win_rate": round(float((r > 0).mean() * 100), 1),
               "avg": round(float(r.mean()), 2),
               "sum_wan": round(float(sum(x / 100 for x in r)), 2)}
        stats.append(row)
        print(f"  {lab:<16}{row['n']:>4}笔  胜率{row['win_rate']:>5.1f}%  "
              f"均{row['avg']:>+6.2f}%  合计{row['sum_wan']:>+7.2f}万")
    sp = sub[col].rank().corr(sub[outcome].rank()) if len(sub) > 10 else None
    if sp is not None:
        print(f"  Spearman({col},{outcome}) = {sp:+.3f}")
    arch["buckets"].setdefault(key, {})[outcome] = {"stats": stats,
                                                    "spearman": round(float(sp), 3) if sp is not None else None}
    print()


# ===== A/B 组合列 =====
def combined(row) -> bool:
    return (row["vcp"] is not None and row["vcp"] <= 0.65) or row["sq_prior"]

df_e["shrunk"] = df_e.apply(combined, axis=1)

# ===== 主报告 =====
for outcome in ("r_sell", "r_t5", "r_t10"):
    print(f"===== 收缩因子 vs {outcome} =====")
    for key in ("vcp", "sq_prior", "release", "shrunk"):
        report(key, outcome)

print("===== 分战法 × 收缩组合（r_sell）=====")
by_strategy = {}
for st, g in df_e.groupby("strategy"):
    a = g[g["shrunk"]]["r_sell"]
    b = g[~g["shrunk"]]["r_sell"]
    line = f"  {st}: "
    if len(a):
        line += f"收缩 {len(a)}笔 胜率{(a > 0).mean() * 100:.1f}% 均{a.mean():+.2f}%"
    else:
        line += "收缩 0笔"
    if len(b):
        line += f" ｜ 其余 {len(b)}笔 胜率{(b > 0).mean() * 100:.1f}% 均{b.mean():+.2f}%"
    print(line)
    by_strategy[st] = {
        "shrunk": {"n": int(len(a)), "win_rate": round(float((a > 0).mean() * 100), 1),
                   "avg": round(float(a.mean()), 2)} if len(a) else {"n": 0},
        "rest": {"n": int(len(b)), "win_rate": round(float((b > 0).mean() * 100), 1),
                 "avg": round(float(b.mean()), 2)} if len(b) else {"n": 0},
    }
arch["by_strategy"] = by_strategy

try:
    json.dump(arch, open(ARCHIVE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n归档: {ARCHIVE}")
except Exception as exc:  # noqa: BLE001
    print(f"\n归档失败: {exc}")
