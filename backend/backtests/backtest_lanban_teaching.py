"""烂板接力教学回测：验证「大烂板→次日高开爆量确认→当天离场/同题材套利」打法的可量化部分。

对照现有实现：wzqwatch 巨量炸板观察池（触板未封+收盘离板≥1.5%+量比≥2.5）= 教学第1条的
「大烂板」，但缺换手率条件；竞价提醒有高开分层但「超预期高开+爆量」「同题材300套利」未量化。
本脚本全部用 K线缓存离线计算（量比用 vol/前5日均量 shifted 口径近似行情源量比）：

  S1 确认信号 2×2：T日高开(<2% / ≥2%) × 爆量(量比<2 / ≥2) → 套利收益(开买收卖)/隔夜收益/当天最大浮亏/封板率
  S2 换手率分桶（T-1）：教学"近60%浮筹充分交换"是否有效 → <30 / 30~45 / 45~60 / >60
  S3 高开幅度精扫：低开 / 0~2 / 2~5 / 5~板（一字板不可竞价成交，单独计数的已剔除）
  S4 同题材300套利：主板主标的确认日（高开≥2%∧爆量≥2），同行业300标的当日开→收收益 vs
     当日全300基线，并与「烂板但主标的未确认日」对照——验证溢出效应是否存在
样本：2025-08-01 起（K线缓存 2025-07-17 起 + 预热）；T+1/T+2 数据不足的事件剔除。
"""
import sys, json
sys.path.insert(0, "/Users/xxwei/myspace/gupiao/backend")
from collections import defaultdict
from typing import Any

import numpy as np
import pandas as pd

from app import replay as rp
from app import datasource as ds

ARCHIVE = "/Users/xxwei/myspace/gupiao/backend/backtests/lanban_teaching_result.json"
START = "2025-08-01"

dates = rp.trading_dates()
last_date = dates[-1]
bars = rp._load_bars()
try:
    names = ds.stock_code_names()
except Exception:
    names = {}
try:
    imap = ds.industry_map_full()
except Exception:
    imap = {}

# ===== 逐股向量化预计算 + 事件检测 =====
events = []
for code, df in bars.items():
    if code.startswith(("4", "8", "92")) or len(df) < 30:
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
    try:
        to = rp._turnover_cache.get(code)
        if to is None:
            f = rp.CACHE_DIR / f"{code}.csv"
            to = pd.read_csv(f, usecols=["turnover"])["turnover"].astype(float).tolist()
            rp._turnover_cache[code] = to
    except Exception:
        to = None
    dt = [str(x)[:10] for x in df["date"]]
    th = 19.5 if code.startswith(("688", "689", "300", "301")) else 9.8
    pc = np.empty(len(c)); pc[0] = np.nan; pc[1:] = c[:-1]
    touched = (~np.isnan(pc)) & (h / pc - 1 >= (th - 0.3) / 100)
    sealed = (p >= th - 0.4) & (c >= h * 0.995)
    vma5 = pd.Series(v).rolling(5).mean().shift(1).to_numpy()
    vr5 = np.where(vma5 > 0, v / vma5, np.nan)
    run = 0
    streaks = np.zeros(len(c), dtype=int)
    for j in range(len(c)):
        run = run + 1 if sealed[j] else 0
        streaks[j] = run
    wzq = touched & ~sealed & (c <= h * 0.985) & (vr5 >= 2.5)
    idxs = np.where(wzq[:-2])[0]  # T+1 必须存在
    for i in idxs:
        i = int(i)
        d1 = dt[i]
        if d1 < START:
            continue
        j = i + 1
        open_prem = (o[j] / c[i] - 1) * 100
        yizi = bool(open_prem >= th - 0.2 and l[j] >= o[j] * 0.998)
        r_day = (c[j] / o[j] - 1) * 100
        has_next = j + 1 < len(c)
        events.append({
            "code": code, "name": nm or code, "industry": imap.get(code, ""),
            "t1_date": d1, "t_date": dt[j],
            "board": "创业" if code.startswith(("300", "301")) else ("科创" if code.startswith(("688", "689")) else "主板"),
            "streak_prev": int(streaks[i - 1]) if i >= 1 else 0,
            "turnover": round(float(to[i]), 2) if to is not None and i < len(to) and np.isfinite(to[i]) else None,
            "open_prem": round(float(open_prem), 2),
            "vr_t": round(float(vr5[j]), 2) if np.isfinite(vr5[j]) else None,
            "yizi": yizi,
            "r_day": round(r_day, 3), "adverse": round((l[j] / o[j] - 1) * 100, 3),
            "r_on": round((o[j + 1] / o[j] - 1) * 100, 3) if has_next else None,
            "r_nc": round((c[j + 1] / o[j] - 1) * 100, 3) if has_next else None,
            "sealed_t": bool(sealed[j]),
        })
print(f"大烂板事件 {len(events)} 件（{START} ~ {last_date}）", flush=True)
ev = pd.DataFrame([e for e in events if not e["yizi"]])
n_yizi = sum(1 for e in events if e["yizi"])
print(f"剔除一字板 {n_yizi} 件（无法竞价买入），可成交 {len(ev)} 件\n", flush=True)


def agg(g, col):
    rs = g[col].dropna() if len(g) else g[col]
    if len(g) == 0 or len(rs) == 0:
        return {"n": 0}
    return {"n": int(len(rs)), "win": round(float((rs > 0).mean() * 100), 1),
            "avg": round(float(rs.mean()), 2), "med": round(float(rs.median()), 2),
            "sum_wan": round(float(rs.sum() / 100), 2)}


arch: dict[str, Any] = {"events_total": len(events), "yizi_excluded": n_yizi, "sections": {}}

# ===== S1 确认信号 2×2 =====
print("===== S1 确认信号 2×2：T日高开 × T日爆量（套利=开买收卖）=====")
sub = ev.dropna(subset=["vr_t"])
cells = {}
for hi_lab, hi_g in (("高开<2%", sub[sub["open_prem"] < 2]), ("高开≥2%", sub[sub["open_prem"] >= 2])):
    for vr_lab, g in (("爆量<2", hi_g[hi_g["vr_t"] < 2]), ("爆量≥2", hi_g[hi_g["vr_t"] >= 2])):
        key = f"{hi_lab}|{vr_lab}"
        a = agg(g, "r_day")
        on = agg(g.dropna(subset=["r_on"]), "r_on")
        adv = round(float(g["adverse"].mean()), 2) if len(g) else None
        srate = round(float(g["sealed_t"].mean() * 100), 1) if len(g) else None
        cells[key] = {"day": a, "overnight": on, "adverse_avg": adv, "sealed_rate": srate}
        adv_s = f"浮亏均值{adv:+.2f}%" if adv is not None else ""
        print(f"  {key:<16} 当天: {a['n']:>4}件 胜率{a.get('win', 0):>5.1f}% 均{a.get('avg', 0):>+6.2f}%"
              f" 合{a.get('sum_wan', 0):>+6.2f}万 ｜ 隔夜均{on.get('avg', 0):>+6.2f}%"
              f" ｜ 封板率{srate}% ｜ {adv_s}")
arch["sections"]["confirm_2x2"] = cells
print()

# ===== S2 换手率分桶 =====
print("===== S2 T-1 换手率分桶（教学：近60%浮筹充分交换）=====")
sub2 = ev.dropna(subset=["turnover"])
to_buckets = [(-1, 30), (30, 45), (45, 60), (60, 999)]
to_labels = ["<30%", "30~45%", "45~60%", ">60%"]
to_stats = []
for b, lab in zip(to_buckets, to_labels):
    g = sub2[sub2["turnover"].between(*b)]
    a = agg(g, "r_day")
    on = agg(g.dropna(subset=["r_on"]), "r_on")
    to_stats.append({"label": lab, "day": a, "overnight": on})
    print(f"  换手{lab:<8} {a['n']:>4}件 当天胜率{a.get('win', 0):>5.1f}% 均{a.get('avg', 0):>+6.2f}%"
          f" ｜ 隔夜均{on.get('avg', 0):>+6.2f}%")
print("  -- 仅高开≥2% 子集（教学实际入场条件）--")
sub2h = sub2[sub2["open_prem"] >= 2]
for b, lab in zip(to_buckets, to_labels):
    g = sub2h[sub2h["turnover"].between(*b)]
    a = agg(g, "r_day")
    print(f"  换手{lab:<8} {a['n']:>4}件 当天胜率{a.get('win', 0):>5.1f}% 均{a.get('avg', 0):>+6.2f}%")
arch["sections"]["turnover_buckets"] = to_stats
print()

# ===== S3 高开幅度精扫 =====
print("===== S3 高开幅度精扫（套利口径）=====")
pre_buckets = [(-99, 0), (0, 2), (2, 5), (5, 19.5)]
pre_labels = ["低开", "0~2%", "2~5%", "5~板"]
pre_stats = []
for b, lab in zip(pre_buckets, pre_labels):
    g = ev[ev["open_prem"].between(*b)]
    a = agg(g, "r_day")
    on = agg(g.dropna(subset=["r_on"]), "r_on")
    srate = round(float(g["sealed_t"].mean() * 100), 1) if len(g) else None
    pre_stats.append({"label": lab, "day": a, "overnight": on, "sealed_rate": srate})
    print(f"  {lab:<10} {a['n']:>4}件 胜率{a.get('win', 0):>5.1f}% 均{a.get('avg', 0):>+6.2f}%"
          f" 合{a.get('sum_wan', 0):>+6.2f}万 ｜ 隔夜均{on.get('avg', 0):>+6.2f}% ｜ 封板率{srate}%")
arch["sections"]["open_prem_scan"] = pre_stats
print()

# ===== S3b 连板核心 × 高开（lanban 系统只做 streak≥1 的连板核心）=====
print("===== S3b 连板核心维度（streak_prev = 截至 T-2 的连板高度，烂板前）=====")
seg_stats = {}
for seg, lab in ((5, "高开5~板"), (0, "低开")):
    g_seg = ev[ev["open_prem"].between(*(5, 19.5) if seg == 5 else (-99, 2))]
    for st_lab, g in (("连板核心(streak_prev≥1)", g_seg[g_seg["streak_prev"] >= 1]),
                      ("首板烂板(streak_prev=0)", g_seg[g_seg["streak_prev"] == 0])):
        a = agg(g, "r_day")
        on = agg(g.dropna(subset=["r_on"]), "r_on")
        srate = round(float(g["sealed_t"].mean() * 100), 1) if len(g) else None
        seg_stats[f"{lab}|{st_lab}"] = {"day": a, "overnight": on, "sealed_rate": srate}
        print(f"  {lab}·{st_lab:<18} {a['n']:>4}件 胜率{a.get('win', 0):>5.1f}% 均{a.get('avg', 0):>+6.2f}%"
              f" ｜ 隔夜均{on.get('avg', 0):>+6.2f}% ｜ 封板率{srate}%")
arch["sections"]["streak_cross"] = seg_stats
print()

# ===== S4 同题材300套利 =====
print("===== S4 同题材300套利：主标的确认日的溢出效应 =====")
cyb_codes = [c for c in bars if c.startswith(("300", "301"))]
cyb_dmap: dict[str, dict[str, int]] = {}
cyb_oc: dict[str, tuple[np.ndarray, np.ndarray]] = {}
cyb_day_ret: dict[str, list[float]] = defaultdict(list)  # 当日全300基线
for code in cyb_codes:
    df = bars[code]
    dtl = [str(x)[:10] for x in df["date"]]
    cyb_dmap[code] = {d: j for j, d in enumerate(dtl)}
    o2 = df["open"].to_numpy(dtype=float)
    c2 = df["close"].to_numpy(dtype=float)
    cyb_oc[code] = (o2, c2)
    for j in range(1, len(o2)):
        if o2[j] > 0:
            cyb_day_ret[dtl[j]].append((c2[j] / o2[j] - 1) * 100)
ind_to_cyb = defaultdict(list)
for c2 in cyb_codes:
    ind = imap.get(c2)
    if ind and ind != "--":
        ind_to_cyb[ind].append(c2)

main_ev = ev[ev["board"] == "主板"]
confirmed = main_ev[main_ev["open_prem"].ge(2) & main_ev["vr_t"].ge(2)]
ctrl = main_ev[~(main_ev["open_prem"].ge(2) & main_ev["vr_t"].ge(2))]


def mate_stats(sub_ev: pd.DataFrame, label: str) -> dict[str, Any]:
    """同行业300标的当日 open->close（每个主标的确认事件各计一次，独立机会）。"""
    rets, base = [], []
    for e in sub_ev.itertuples():
        if not e.industry or e.industry == "--":
            continue
        for c2 in ind_to_cyb.get(e.industry, []):
            if c2 == e.code:
                continue
            j2 = cyb_dmap[c2].get(e.t_date)
            if j2 is None:
                continue
            o2, cl2 = cyb_oc[c2]
            if o2[j2] > 0:
                rets.append((cl2[j2] / o2[j2] - 1) * 100)
        base.extend(cyb_day_ret.get(e.t_date, []))
    if not rets:
        print(f"  {label}: 无样本")
        return {"label": label, "n": 0}
    arr = np.array(rets)
    barr = np.array(base) if base else np.array([0.0])
    row = {"label": label, "n": int(len(arr)),
           "win": round(float((arr > 0).mean() * 100), 1),
           "avg": round(float(arr.mean()), 2), "med": round(float(np.median(arr)), 2),
           "base_win": round(float((barr > 0).mean() * 100), 1),
           "base_avg": round(float(barr.mean()), 2)}
    print(f"  {label}: {row['n']}件次 胜率{row['win']}% 均{row['avg']:+.2f}% 中位{row['med']:+.2f}%"
          f" ｜ 当日全300基线 胜率{row['base_win']}% 均{row['base_avg']:+.2f}%")
    return row


s4 = {"confirmed": mate_stats(confirmed, "主标的确认日(高开≥2%∧爆量≥2) 的同行业300"),
      "control": mate_stats(ctrl, "主标的未确认日 的同行业300")}
arch["sections"]["cyb_arbitrage"] = s4

try:
    json.dump(arch, open(ARCHIVE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n归档: {ARCHIVE}")
except Exception as exc:  # noqa: BLE001
    print(f"\n归档失败: {exc}")
