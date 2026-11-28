"""胜率因子快速筛选：在 398 笔入场（3.0现行配置）上检验 6 个全网常见因子。
因子全部用 K线缓存计算（不依赖盘中接口）：
F1 同板块当日涨停家数（板块效应/梯队）
F2 市场最高连板高度（情绪高度）
F3 昨日市场炸板率（情绪分歧）
F4 信号日前20日涨幅（前期涨幅/位置）
F5 信号日收盘在20日区间的位置（低位/高位）
F6 信号日换手率（>15%出货论）
输出：每因子分桶的 胜率/平均/合计，及 Spearman 相关。
"""
import sys, json
sys.path.insert(0, "/Users/xxwei/myspace/gupiao/backend")
import numpy as np
import pandas as pd
from collections import defaultdict
from app import replay as rp
from app import datasource as ds
from app.config import effective_config

dates = rp.trading_dates()
date_idx = {d: i for i, d in enumerate(dates)}
run_dates = [d for d in dates if d >= "2026-07-01"]
bars = rp._load_bars()
try:
    ind_map = ds.industry_map_full()
except Exception:
    ind_map = {}

# ===== 预计算每交易日市场状态 =====
# sealed: 当日收盘封板; touched: 盘中触及涨停价; streak: 连续封板天数
day_sealed = defaultdict(list)      # date -> [(code, industry)]
day_touched_n = defaultdict(int)    # date -> 触板家数
day_sealed_n = defaultdict(int)
day_max_streak = defaultdict(int)   # date -> 市场最高连板
for code, df in bars.items():
    c = df["close"].to_numpy(dtype=float)
    h = df["high"].to_numpy(dtype=float)
    p = df["pct_change"].to_numpy(dtype=float)
    n = len(c)
    th = 19.5 if code.startswith(("688", "689", "300", "301")) else 9.8
    sealed = (p >= th) & (c >= h * 0.995)
    pc = np.empty(n); pc[0] = np.nan; pc[1:] = c[:-1]
    touched = (~np.isnan(pc)) & (h / pc - 1 >= (th - 0.3))
    dt = [str(x)[:10] for x in df["date"]]
    ind = ind_map.get(code, "")
    # streak（截至每日的连续封板数）
    st = 0
    streaks = np.zeros(n, dtype=int)
    for j in range(n):
        st = st + 1 if sealed[j] else 0
        streaks[j] = st
    for j in range(n):
        d = dt[j]
        if d < "2026-06-01":
            continue
        if sealed[j]:
            day_sealed[d].append((code, ind))
            day_sealed_n[d] += 1
            if streaks[j] > day_max_streak[d]:
                day_max_streak[d] = int(streaks[j])
        if touched[j]:
            day_touched_n[d] += 1

def bomb_rate(d):   # 炸板率 = 触板未封 / 触板
    t = day_touched_n.get(d, 0)
    s = day_sealed_n.get(d, 0)
    return (t - s) / t * 100 if t > 0 else None

def board_count(d, ind):   # 同板块当日涨停家数
    return sum(1 for _, i in day_sealed.get(d, []) if i == ind)

def max_streak_market(d):
    return day_max_streak.get(d, 0)

# ===== 取 3.0 入场集 =====
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
                        "r": s["r_sell"], "price": s["price"]})
print(f"入场 {len(entries)} 笔", flush=True)

# ===== 计算因子值 =====
rows = []
for e in entries:
    code, d = e["code"], e["date"]
    df = bars.get(code)
    if df is None:
        continue
    j = rp._row_of(code, d)
    if j is None or j < 21:
        continue
    c = df["close"].to_numpy(dtype=float)
    to = None
    try:
        to = rp._turnover_at(code, j)
    except Exception:
        pass
    di = date_idx.get(d)
    prev_d = dates[di - 1] if di and di >= 1 else None
    c20 = c[j - 19:j + 1]
    rng = float(c20.max()) - float(c20.min())
    ind = ind_map.get(code, "")
    rows.append({
        **e,
        "f1_board": board_count(d, ind) if ind else None,
        "f2_height": max_streak_market(d),
        "f3_bomb": bomb_rate(prev_d) if prev_d else None,
        "f4_prev20": round((c[j] / c[j - 20] - 1) * 100, 2),
        "f5_pos20": round((c[j] - float(c20.min())) / rng, 2) if rng > 0 else None,
        "f6_turnover": to,
    })
df_e = pd.DataFrame(rows)
print(f"含因子值 {len(df_e)} 笔\n", flush=True)
json.dump(rows, open("/tmp/factor_screen_rows.json", "w", encoding="utf-8"), ensure_ascii=False)

def report(col, buckets, labels):
    sub = df_e.dropna(subset=[col, "r"])
    out = []
    for b, lab in zip(buckets, labels):
        g = sub[sub[col].between(*b)] if isinstance(b, tuple) else sub[sub[col] == b]
        if len(g) == 0:
            continue
        r = g["r"]
        out.append((lab, len(g), r.mean(), sum(x / 100 for x in r), (r > 0).mean() * 100))
    sp = sub[col].rank().corr(sub["r"].rank()) if len(sub) > 10 else None
    print(f"-- {col} (Spearman={sp:+.3f}) --")
    for lab, n, m, tot, w in out:
        print(f"  {lab:<14}{n:>4}笔  胜率{w:>5.1f}%  均{m:>+6.2f}%  合计{tot:>+7.2f}万")
    print()

print("===== F1 同板块当日涨停家数（板块效应）=====")
report("f1_board", [(1, 1), (2, 3), (4, 6), (7, 10), (11, 999)], ["仅自己1只", "2~3只", "4~6只", "7~10只", ">10只"])
print("===== F2 市场最高连板高度 =====")
report("f2_height", [(1, 2), (3, 4), (5, 6), (7, 20)], ["1~2板", "3~4板", "5~6板", "≥7板"])
print("===== F3 昨日市场炸板率 =====")
report("f3_bomb", [(0, 15), (15, 25), (25, 35), (35, 100)], ["<15%", "15~25%", "25~35%", ">35%"])
print("===== F4 前20日涨幅 =====")
report("f4_prev20", [(-999, 0), (0, 20), (20, 40), (40, 999)], ["负/平", "0~20%", "20~40%", ">40%"])
print("===== F5 20日区间位置 =====")
report("f5_pos20", [(0, 0.33), (0.33, 0.66), (0.66, 1.01)], ["低1/3", "中1/3", "高1/3"])
print("===== F6 信号日换手率 =====")
report("f6_turnover", [(0, 5), (5, 10), (10, 15), (15, 999)], ["<5%", "5~10%", "10~15%", ">15%"])
