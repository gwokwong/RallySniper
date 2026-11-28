"""换手率×量比四象限回测（2026-07-01~11-24，419笔入场）。
组合定义（用户口径，换手率=turnover%，量比=volume/前5日均量）：
  C1 主力介入: 换手>5  且 量比 2~3        → 进场
  C2 行情加速: 换手 5~10 且 量比 3~5      → 持有
  C3 吸筹/离场: 换手>10 且 量比>5        → 低位(pos20≤0.3)吸筹 / 高位(pos20≥0.7)离场
  C4 无量诱多: 换手>10 且 量比<1         → 撤离
输出：
  1) 入场日象限分布 vs 每笔最终收益（B口径 r_on）
  2) 持仓期间各象限出现日的后5日前向收益（负=应当卖出）
"""
import sys, json
sys.path.insert(0, "/Users/xxwei/myspace/gupiao/backend")
import numpy as np
import pandas as pd
from pathlib import Path
from app import replay as rp

CACHE = Path("/Users/xxwei/myspace/gupiao/backend/data/kline_cache")
d = json.load(open('/tmp/ch5_ab_result.json', encoding='utf-8'))
ents = d['entries_detail']

def bucket(to, vr, pos):
    if to is None or vr is None:
        return None
    if to > 10 and vr > 5:
        if pos is not None and pos <= 0.3:
            return "C3低·吸筹"
        if pos is not None and pos >= 0.7:
            return "C3高·离场"
        return "C3中位"
    if to > 10 and vr < 1:
        return "C4·无量诱多"
    if to > 5 and 2 <= vr < 3:
        return "C1·主力介入"
    if 5 <= to <= 10 and 3 <= vr <= 5:
        return "C2·行情加速"
    return "其他"

def load_code(code):
    f = CACHE / f"{code}.csv"
    if not f.exists():
        return None
    try:
        df = pd.read_csv(f, usecols=["date", "close", "volume", "turnover"],
                         dtype={"close": "float32", "volume": "float64", "turnover": "float32"})
        return df
    except Exception:
        return None

# ===== 1) 入场日象限分布 =====
entry_stat = {}
for s in ents:
    code = s['code']
    df = load_code(code)
    if df is None: continue
    bi = rp._row_of(code, s['date']) if False else None
    # CSV 行号与缓存行号一致（同源文件），按日期定位
    idx = df.index[df["date"].astype(str).str[:10] == s['date']]
    if len(idx) == 0 or idx[0] < 6: continue
    j = int(idx[0])
    to = float(df["turnover"].iloc[j])
    vr = float(df["volume"].iloc[j]) / max(1e-9, float(df["volume"].iloc[j-5:j].mean()))
    win = df["close"].iloc[max(0, j-19):j+1].astype(float)
    rng = float(win.max()) - float(win.min())
    pos = (float(df["close"].iloc[j]) - float(win.min())) / rng if rng > 0 else 0.5
    b = bucket(to, vr, pos)
    if b is None: continue
    g = entry_stat.setdefault(b, {'n': 0, 'r': []})
    g['n'] += 1; g['r'].append(s['r_on'])

print("===== 1) 入场日象限 → 该笔最终收益 =====")
print(f"{'象限':<14}{'笔数':>5}{'胜率':>8}{'平均':>8}{'合计':>9}")
for k in sorted(entry_stat, key=lambda x: -entry_stat[x]['n']):
    g = entry_stat[k]
    w = sum(1 for x in g['r'] if x > 0)/len(g['r'])*100
    print(f"{k:<14}{g['n']:>5}{w:>7.1f}%{np.mean(g['r']):>+7.2f}%{sum(x/100 for x in g['r']):>+8.2f}万")

# ===== 2) 持仓期间象限出现日 → 后5日前向收益 =====
fwd_stat = {}
for s in ents:
    code = s['code']
    df = load_code(code)
    if df is None: continue
    idx = df.index[df["date"].astype(str).str[:10] == s['date']]
    if len(idx) == 0: continue
    bi = int(idx[0])
    # 卖出日（B口径）行号
    sidx = df.index[df["date"].astype(str).str[:10] == s['sell_on']]
    ei = int(sidx[0]) if len(sidx) else min(bi + 10, len(df) - 1)
    closes = df["close"].astype(float).to_numpy()
    vols = df["volume"].to_numpy()
    tos = df["turnover"].astype(float).to_numpy()
    for j in range(bi + 1, min(ei, len(df) - 6) + 1):
        if j < 6: continue
        vr = float(vols[j]) / max(1e-9, float(vols[j-5:j].mean()))
        win = closes[max(0, j-19):j+1]
        rng = float(win.max()) - float(win.min())
        pos = (closes[j] - float(win.min())) / rng if rng > 0 else 0.5
        b = bucket(float(tos[j]), vr, pos)
        if b is None: continue
        f5 = (closes[j+5] / closes[j] - 1) * 100
        g = fwd_stat.setdefault(b, {'n': 0, 'r': []})
        g['n'] += 1; g['r'].append(f5)

print("\n===== 2) 持仓期间出现象限日 → 后5日前向收益（负=卖对） =====")
print(f"{'象限':<14}{'天数':>5}{'后5日中位':>9}{'后5日均值':>9}{'跌概率':>8}")
for k in sorted(fwd_stat, key=lambda x: -fwd_stat[x]['n']):
    g = fwd_stat[k]
    print(f"{k:<14}{g['n']:>5}{np.median(g['r']):>+8.2f}%{np.mean(g['r']):>+8.2f}%"
          f"{sum(1 for x in g['r'] if x < 0)/len(g['r'])*100:>7.1f}%")
json.dump({"entry": entry_stat, "fwd": fwd_stat},
          open('/tmp/vr_turnover_result.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
