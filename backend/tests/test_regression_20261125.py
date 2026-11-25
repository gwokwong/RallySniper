"""2026-11-25 全因子回归套件（固化版，替代零散 heredoc）：
1) 第5通道（事件计数）6项
2) A杀多日连跌 7项
3) 换手率×量比四象限 10分类+5过滤
4) 置信档位 6项
5) market_sentiment 健全性（北交所剔除后仍有值、炸板率区间合理）
用法: backend/.venv/bin/python test_regression_20261125.py
"""
import sys
sys.path.insert(0, "/Users/xxwei/myspace/gupiao/backend")
import numpy as np
import pandas as pd

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name} {detail}")

# ===== 1) 第5通道 =====
from app import sellwatch as sw
from app import replay as rp
from app import mainwave as mw
from app import market_scan as ms

rules = {
    "ma10_consec_enabled": True, "ma10_consec_days": 2,
    "touch_ma10_exit_enabled": True, "touch_ma10_count": 4, "touch_ma10_window": 30,
    "ma20_buffer": 0.01, "ma20_slope": -0.005, "volume_surge_ratio": 5.0,
    "fast_break_enabled": False, "fast_break_pct": 0.02,
    "rapid_drop_enabled": True, "rapid_drop_pct": 0.08,
    "ma10_break_enabled": True, "ma10_break_drop_pct": 0.05,
    "shrink_limit_ratio": 0.5, "shrink_limit_drop": 0.095,
    "cooldown_reset_pct": 0.03, "tier_enabled": False,
}
def mk(closes, pcts):
    n = len(closes)
    return pd.DataFrame({"date": [f"D{i}" for i in range(n)],
                         "open": [c * 0.995 for c in closes], "close": list(closes),
                         "high": [c * 1.005 for c in closes], "low": [c * 0.99 for c in closes],
                         "volume": [1e6] * n, "pct_change": list(pcts), "turnover": [5.0] * n})
def run_eval(closes, rr):
    df = mk(closes, [0.0] + [(closes[i] / closes[i - 1] - 1) * 100 for i in range(1, len(closes))])
    cur = closes[-1]
    quote = {"price": cur, "open": cur * 0.99, "change_pct": float(df["pct_change"].iloc[-1]),
             "low": cur * 0.98, "high": cur * 1.01, "volume": 2e6, "volume_ratio": 2.0,
             "pre_close": closes[-2]}
    orig = sw._completed_history
    sw._completed_history = lambda code: df
    try:
        return [x["strategy"] for x in sw._evaluate("600001", "测试", quote, rr, "confirm", cost=10.0)]
    finally:
        sw._completed_history = orig

c_hit = [10.0] * 15 + [11.0] + [10.2, 11.0] * 4 + [10.1]
check("第5通道·4次独立回踩触发", "首板回踩MA10极限" in run_eval(c_hit, rules))
check("第5通道·MA10上方不触发", "首板回踩MA10极限" not in run_eval([10.0] * 30, rules))
c_few = [10.0] * 15 + [11.0] + [10.2, 11.0] * 2 + [10.4]
check("第5通道·仅2次不触发", "首板回踩MA10极限" not in run_eval(c_few, rules))
check("急跌通道正常", "急跌破位" in run_eval([10.0] * 25 + [9.0], rules))

# ===== 2) A杀（多日连跌口径）=====
mf = {"a_kill_days": 3, "a_kill_down_days": 5, "a_kill_cum_drop": 15.0}
def akill(closes):
    n = len(closes)
    d = pd.DataFrame({"close": list(closes), "high": [c * 1.005 for c in closes],
                      "open": [c * 0.995 for c in closes], "low": [c * 0.99 for c in closes],
                      "volume": [1e6] * n, "pct_change": [0.0] * n, "date": [f"D{i}" for i in range(n)],
                      "turnover": [5.0] * n})
    return mw.recent_a_kill(d, mf)

up = [round(10 * 1.04 ** i, 3) for i in range(6)]
down = [round(up[-1] * 0.965 ** i, 3) for i in range(1, 6)]
ck = up + down
check("A杀·杀到位当天拦截", akill(ck))
check("A杀·触底后3天仍拦截", akill(ck + [ck[-1] * 1.005] * 3))
check("A杀·触底后4天放行", not akill(ck + [ck[-1] * 1.005] * 4))
check("A杀·温和连跌不拦", not akill(up + [round(up[-1] * 0.988 ** i, 3) for i in range(1, 6)]))
check("A杀·3天急杀(天数不足)不拦", not akill(up + [round(up[-1] * 0.94 ** i, 3) for i in range(1, 4)] + [round(up[-1] * 0.94 ** 3 * 1.001, 3)]))
check("A杀·连跌8天进行中拦截", akill(up + [round(up[-1] * 0.965 ** i, 3) for i in range(1, 8)]))

# ===== 3) 四象限 =====
cases = [(6.0, 2.5, "C1"), (15.0, 6.0, "C3"), (12.0, 0.8, "C4"), (8.0, 4.0, "C2"),
         (4.0, 2.5, ""), (6.0, 3.5, "C2"), (None, 2.5, "")]
c_ok = sum(mw.vp_combo_class(t, v) == w for t, v, w in cases)
check(f"四象限·分类 {c_ok}/{len(cases)}", c_ok == len(cases))
mf_on = {"vp_combo_filter_enabled": True, "vp_combo_exclude_c4": False}
mf_c4 = {**mf_on, "vp_combo_exclude_c4": True}
mf_off = {"vp_combo_filter_enabled": False}
check("四象限·剔C1", mw.vp_combo_should_skip(6.0, 2.5, mf_on) and not mw.vp_combo_should_skip(12.0, 0.8, mf_on))
check("四象限·C4可选剔", mw.vp_combo_should_skip(12.0, 0.8, mf_c4) and not mw.vp_combo_should_skip(8.0, 4.0, mf_on))
check("四象限·总开关", not mw.vp_combo_should_skip(6.0, 2.5, mf_off))

# ===== 4) 置信档位 =====
cases = [
    ({"industry": "元件"}, {"元件": 5}, 8, "full", "high"),
    ({"industry": "汽车零部件"}, {"汽车零部": 4}, 7, "full", "high"),
    ({"industry": "元件"}, {"元件": 2}, 8, "full", "normal"),
    ({"industry": "元件"}, {"元件": 5}, 4, "full", "normal"),
    ({"industry": "元件"}, {"元件": 10}, 9, "probe", "probe"),
    ({"industry": None}, {"元件": 10}, 9, "full", "normal"),
]
c_ok = 0
for sig, cnt, h, tier, want in cases:
    s = [dict(sig, code="x")]
    ms.attach_confidence(s, cnt, h, tier)
    c_ok += (s[0]["confidence"] == want)
check(f"置信档位 {c_ok}/{len(cases)}", c_ok == len(cases))

# ===== 5) market_sentiment 健全性 =====
from app import replay as rp
st = rp.market_sentiment()
check("情绪统计·覆盖>100交易日", len(st) > 100, f"{len(st)}天")
bad = [d for d, v in st.items() if v.get("bomb_rate") is not None and not (0 <= v["bomb_rate"] <= 100)]
check("情绪统计·炸板率区间合理", not bad, f"异常{len(bad)}天")
codes_in = None
check("情绪统计·最高板字段存在", all("max_streak" in v for v in list(st.values())[:50]))

print(f"\n===== 通过 {len(PASS)} / {len(PASS)+len(FAIL)} =====")
for f in FAIL:
    print("  待修:", f)
sys.exit(1 if FAIL else 0)
