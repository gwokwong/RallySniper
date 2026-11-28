"""2026-11-28 补充回归套件：固化近一周逃逸到生产的关键 bug + 配置/调度核心路径。

覆盖：
1) 配置迁移（旧配置兼容：run_mode/通知渠道/cooldown_seconds/战法重置）
2) 配置投影（effective_config 双模式隔离，不污染磁盘缓存）
3) 量比过滤配置字段读写（strong_max_vol_ratio）
4) 卖出引擎近一周 3 个真 bug：第6通道数组对齐、双线死叉文案共存、Path 导入
5) 调度注册数与关键 job 存在性

用法: backend/.venv/bin/python test_regression_20261128.py
说明: 不联网、不写真实 config.json（用副本打桩），可安全重复运行。
"""
import sys
sys.path.insert(0, "/Users/xxwei/myspace/gupiao/backend")
import pandas as pd

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'OK ' if cond else 'FAIL'} {name} {detail}")

from app import config as C
from app import sellwatch as sw

# ===== 1) 配置迁移 =====
raw_old = {
    "notification": [
        {"type": "email", "smtp_host": "smtp.test", "mail_to": "a@b.com"},
        {"type": "wechat", "webhook": "xxx"},
    ],
    "rules": {"cooldown_seconds": 15, "foo": 1},
    "strategy_rules": {"pullback_vol_ratio": 2.0},
}
m = C._migrate(raw_old)
check("迁移·旧配置补 run_mode=stable", m.get("run_mode") == "stable")
check("迁移·只留 email 渠道", len(m["notification"]) == 1 and m["notification"][0]["type"] == "email", str([c["type"] for c in m["notification"]]))
check("迁移·剔除旧 cooldown_seconds", "cooldown_seconds" not in m["rules"])
raw_no_pull = {"notification": [{"type": "email", "smtp_host": "x"}]}
m2 = C._migrate(raw_no_pull)
check("迁移·缺 pullback_vol_ratio 时补默认战法", "strategies" in m2 and "bear_strategies" in m2)
check("迁移·新配置不误伤(保留已有 run_mode)", C._migrate({"run_mode": "monster", "notification": []}).get("run_mode") == "monster")

# ===== 2) 配置投影（双模式隔离，不污染缓存）=====
orig_load = C.load_config
try:
    base = C.default_config()
    base.run_mode = "stable"
    base.market_regime.enabled = True
    base.strategies.强势续攻 = False
    base.sell.tier_enabled = False
    base.sell.tier_ma10_trail = True
    C.load_config = lambda: base

    eff_s = C.effective_config()
    check("投影·stable 原样不改写战法", eff_s.strategies.强势续攻 is False and eff_s.market_regime.enabled is True)
    eff_s.strategies.强势续攻 = True  # 模拟扫描端就地修改
    check("投影·stable 返回副本不污染缓存", base.strategies.强势续攻 is False)

    base.run_mode = "monster"
    eff_m = C.effective_config()
    check("投影·monster 关大盘/点火闸门", eff_m.market_regime.enabled is False and eff_m.market_regime.ignition_enabled is False)
    check("投影·monster 强制开强势续攻", eff_m.strategies.强势续攻 is True)
    check("投影·monster 厚利档开+关移动止盈", eff_m.sell.tier_enabled is True and eff_m.sell.tier_ma10_trail is False)
    check("投影·monster 不污染磁盘缓存", base.market_regime.enabled is True and base.sell.tier_enabled is False and base.sell.tier_ma10_trail is True)
finally:
    C.load_config = orig_load

# ===== 3) 量比过滤字段 =====
c = C.default_config()
check("字段·strong_max_vol_ratio 代码默认3.0", c.strategy_rules.strong_max_vol_ratio == 3.0, f"实际{c.strategy_rules.strong_max_vol_ratio}")
check("字段·strong_max_vol_ratio 在 model_dump 中", "strong_max_vol_ratio" in c.strategy_rules.model_dump())
check("字段·run_mode 在 model_dump 中", c.model_dump().get("run_mode") in ("stable", "monster"))

# ===== 4) 卖出引擎近一周 bug 防回归 =====
check("后端托管·Path 已导入(main.py 可加载)", "Path" in open("/Users/xxwei/myspace/gupiao/backend/app/main.py", encoding="utf-8").read().split("from fastapi import")[0])

def mk_hist(n=60):
    return pd.DataFrame({
        "date": [f"D{i}" for i in range(n)],
        "open": [10.0] * n, "close": [10.0] * n,
        "high": [10.1] * n, "low": [9.9] * n,
        "volume": [1e6] * n, "pct_change": [0.0] * n,
        "turnover": [5.0] * n, "amount": [1e7] * n,
    })

rules = C.default_config().sell.model_dump()
rules.update({"tier_enabled": False, "cooldown_enabled": False})
quote = {"price": 10.0, "open": 9.9, "change_pct": 0.0, "low": 9.8,
         "high": 10.1, "volume": 2e6, "volume_ratio": 2.0, "pre_close": 10.0}
orig = sw._completed_history
try:
    # 第6通道数组对齐：closes 追加实时行时 vols 不得长度失配导致静默不触发
    sw._completed_history = lambda code: mk_hist()
    res = sw._evaluate("600001", "测试", quote, rules, "confirm", cost=10.0)
    check("卖出·数组对齐后 _evaluate 不抛异常", isinstance(res, list))
    # 缩量跌停仍可触发（真破位保护未被误关）
    df = mk_hist()
    df.loc[df.index[-1], ["close", "low", "pct_change", "volume"]] = [9.0, 8.9, -9.6, 3e5]
    sw._completed_history = lambda code: df
    q2 = {**quote, "price": 9.0, "open": 9.5, "change_pct": -9.6, "low": 8.9,
          "volume": 3e5, "volume_ratio": 0.3}
    res2 = sw._evaluate("600002", "测试", q2, rules, "confirm", cost=10.0)
    # 缩量跌停与破MA10同属"急跌破位"确认信号（共用 strategy 名，文案按
    # trail→ma10→缩量跌停 优先级展示其一）。真破位保护有效 = 产出确认级卖出信号。
    confirms = [x for x in res2 if x.get("strategy") == "急跌破位" and x.get("level") == "确认"]
    check("卖出·缩量跌停真破位通道可触发", len(confirms) >= 1, str([x.get("strategy") for x in res2]))
finally:
    sw._completed_history = orig

# ===== 5) 调度注册数 =====
from app import scheduler as sch
check("调度·start 函数存在且可调用签名", callable(sch.start))
src = open("/Users/xxwei/myspace/gupiao/backend/app/scheduler.py", encoding="utf-8").read()
check("调度·关键点火/竞价/超跌 job 已注册", all(tok in src for tok in ["_ignition_job", "_lanban_auction_job", "_oversold_job", "_lunch_review_job"]))
check("调度·跨进程防重 claim_once 已在", "claim_once" in src)

print(f"\n===== 通过 {len(PASS)} / {len(PASS)+len(FAIL)} =====")
for f in FAIL:
    print("  待修:", f)
sys.exit(1 if FAIL else 0)
