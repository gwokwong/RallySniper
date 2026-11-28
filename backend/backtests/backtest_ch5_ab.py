"""第5卖出通道（首板回踩MA10极限）A/B 回测：2026-07-01 ~ 数据末日。
- A = 第5通道关（通道1-4）；B = 第5通道开（通道1-5）
- 入场信号完全相同（卖出规则不影响入场），仅卖出配对不同
- 另做第5通道独立预警统计：预警日后 5/10/20 交易日价格走势（卖出胜率）
"""
import sys, json, time
sys.path.insert(0, "/Users/xxwei/myspace/gupiao/backend")
import numpy as np
import pandas as pd
from bisect import bisect_left
from datetime import datetime
from app import replay as rp
from app import datasource as ds
from app import sellwatch as sw
from app.config import effective_config

START = "2026-07-01"
OUT = "/tmp/ch5_ab_result.json"

cfg_base = effective_config()
print("实盘卖出配置:", {k: getattr(cfg_base.sell, k) for k in
      ("touch_ma10_exit_enabled", "touch_ma10_count", "touch_ma10_window",
       "ma10_consec_enabled", "ma10_consec_days")}, flush=True)

cfg_off = cfg_base.model_copy(deep=True)
cfg_off.sell = cfg_base.sell.model_copy(update={"touch_ma10_exit_enabled": False})
rp.effective_config = lambda: cfg_off

# 缓存股票名表，避免 100 次日期循环重复请求
_names_cache = None
def _cached_names():
    global _names_cache
    if _names_cache is None:
        try:
            _names_cache = ds.stock_code_names()
        except Exception:
            _names_cache = {}
    return _names_cache
ds.stock_code_names = _cached_names

orig_completed = sw._completed_history
dates = rp.trading_dates()
run_dates = [d for d in dates if d >= START]
print(f"交易日 {dates[0]} ~ {dates[-1]}，回测 {run_dates[0]} ~ {run_dates[-1]} 共 {len(run_dates)} 天", flush=True)

t0 = time.time()
entries = []   # 每个: code,name,date,price,strength,buy_row, sell_off_*
for i, d in enumerate(run_dates):
    try:
        res = rp.replay(d)
    except Exception as e:
        print(f"  {d} replay失败: {e}", flush=True)
        continue
    if not res.get("success") or res.get("blocked_reason"):
        continue
    for s in res["signals"]:
        entries.append({"code": s["code"], "name": s["name"], "date": d,
                        "price": s["price"], "strength": s["strength"],
                        "buy_row": s["buy_row"],
                        "sell_off": s["sell_date"], "r_off": s["r_sell"],
                        "hold_off": s["hold_trd"]})
    if (i + 1) % 10 == 0:
        print(f"  进度 {i+1}/{len(run_dates)}，已入场 {len(entries)}，{time.time()-t0:.0f}s", flush=True)

print(f"回放完成：{len(run_dates)}天，总入场 {len(entries)}，耗时 {time.time()-t0:.0f}s", flush=True)

# ===== B：第5通道开，仅重跑卖出配对 =====
bars = rp._load_bars()
sell_rules_on = cfg_base.sell.model_dump()
orig_completed = sw._completed_history
n_on = 0
for s in entries:
    code, buy_i = s["code"], s["buy_row"]
    df = bars.get(code)
    if df is None:
        s.update(sell_on=s["sell_off"], r_on=s["r_off"], hold_on=s["hold_off"], on_strats=[])
        continue
    n = len(df)
    sell_date = sell_price = None
    on_strats = []
    for j in range(buy_i + 1, n):
        window = df.iloc[:j].copy()
        sw._completed_history = lambda c, w=window: w
        row = df.iloc[j]
        quote = {"price": float(row["close"]), "open": float(row["open"]),
                 "change_pct": float(row["pct_change"]), "low": float(row["low"]),
                 "volume": float(row["volume"])}
        try:
            found = sw._evaluate(code, s["name"], quote, sell_rules_on, "confirm", cost=s["price"])
        except Exception:
            found = []
        if found:
            sell_date = dates[j] if j < len(dates) else None
            sell_date = df["date"].iloc[j] if hasattr(df["date"].iloc[j], "__str__") else None
            sell_date = str(df["date"].iloc[j])[:10]
            sell_price = round(float(row["close"]), 2)
            on_strats = sorted({x["strategy"] for x in found})
            break
    sw._completed_history = orig_completed
    if sell_date is None:
        sell_date = str(df["date"].iloc[-1])[:10]
        sell_price = round(float(df["close"].iloc[-1]), 2)
    else:
        n_on += 1
    bi = bisect_left(dates, s["date"]); ei = bisect_left(dates, sell_date)
    s.update(sell_on=sell_date, r_on=round((sell_price / s["price"] - 1) * 100, 2),
             hold_on=max(0, ei - bi), on_strats=on_strats)
sw._completed_history = orig_completed
print(f"B 配对完成：{len(entries)} 笔，其中触发卖出 {n_on}", flush=True)

def agg(rows, rk, hk):
    rets = [r[rk] for r in rows]
    win = sum(1 for x in rets if x > 0) / len(rets) * 100 if rets else None
    return {"n": len(rows), "win%": round(win, 1) if win is not None else None,
            "avg%": round(float(np.mean(rets)), 2) if rets else None,
            "med%": round(float(np.median(rets)), 2) if rets else None,
            "total万": round(sum(x / 100 for x in rets), 2) if rets else 0,
            "hold_avg": round(float(np.mean([r[hk] for r in rows])), 1) if rows else None}

# 第5通道驱动的提前退出：on_strats 含第5通道 且 卖出日比 A 提前
ch5_driven = [s for s in entries if "首板回踩MA10极限" in s.get("on_strats", [])
              and s["sell_on"] != s["sell_off"]]

result = {
    "period": f"{run_dates[0]}~{run_dates[-1]}",
    "n_days": len(run_dates), "n_entries": len(entries),
    "A_off": agg(entries, "r_off", "hold_off"),
    "B_on": agg(entries, "r_on", "hold_on"),
    "ch5_driven_n": len(ch5_driven),
    "ch5_driven": [{k: s[k] for k in ("code", "name", "date", "price", "sell_off", "r_off",
                                       "sell_on", "r_on", "hold_off", "hold_on")} for s in ch5_driven],
    "ch5_driven_diff": {
        "saved_worse": sum(1 for s in ch5_driven if s["r_on"] > s["r_off"]),
        "lost_better": sum(1 for s in ch5_driven if s["r_on"] < s["r_off"]),
        "avg_gain_pct": round(float(np.mean([s["r_on"] - s["r_off"] for s in ch5_driven])), 2) if ch5_driven else None,
        "total_diff万": round(sum((s["r_on"] - s["r_off"]) / 100 for s in ch5_driven), 2) if ch5_driven else 0,
    },
    "entries_detail": [{k: s.get(k) for k in ("code", "name", "date", "price", "strength",
                                               "sell_off", "r_off", "hold_off", "sell_on", "r_on",
                                               "hold_on", "on_strats")} for s in entries],
}
json.dump(result, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("A(关):", result["A_off"], flush=True)
print("B(开):", result["B_on"], flush=True)
print("第5通道驱动提前退出:", result["ch5_driven_n"], "笔 →", result["ch5_driven_diff"], flush=True)
print(f"结果已写 {OUT}", flush=True)
