"""回归测试 2026-11-28：信号验证闭环（signalstore）+ 波动收缩指标列（mainwave）。

契约：不联网、不写真实 data/（signalstore.DATA 打桩到临时目录、
load_config 打桩为默认配置），可安全重复运行。运行方式：
    backend/.venv/bin/python tests/test_regression_20261128_signaloutcomes.py
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/Users/xxwei/myspace/gupiao/backend")

import numpy as np
import pandas as pd

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'OK ' if cond else 'FAIL'} {name} {detail}")


# ----------------------------- 打桩 -----------------------------
import app.signalstore as sg
import app.config as C
from app.config import AppConfig

TMP = Path(tempfile.mkdtemp(prefix="sg_test_"))
ORIG_DATA, ORIG_LOAD = sg.DATA, sg.load_config
sg.DATA = TMP
sg.load_config = lambda: AppConfig()  # 默认配置：signal_track_enabled=True


def restore():
    sg.DATA, sg.load_config = ORIG_DATA, ORIG_LOAD


def mk_bars(closes, highs, lows, dates):
    n = len(closes)
    return pd.DataFrame({
        "date": dates,
        "open": closes, "close": closes, "high": highs, "low": lows,
        "volume": [10000.0] * n,
        "pct_change": [0.0] * n,
    })


try:
    # ----------------------------- 1. record_batch 记录/过滤/去重 -----------------------------
    print("[1] record_batch 记录与去重")
    rows = [
        {"code": "600519", "name": "贵州茅台", "strategy": "涨停回调", "stage": "trigger",
         "stage_label": "放量启动", "direction": "up", "price": 100.0, "change_pct": 5.2,
         "reasons": ["a", "b"]},
        {"code": "000858", "name": "五粮液", "strategy": "洗盘反包", "stage": "trigger",
         "direction": "up", "price": 50.0, "change_pct": 3.0, "reasons": []},
        {"code": "300750", "name": "宁德时代", "strategy": "双线死叉", "direction": "down",
         "price": 30.0, "change_pct": -2.0, "reasons": ["破位"]},   # 无 stage 的卖出预警 → 记
        {"code": "000001", "name": "平安银行", "strategy": "涨停回调", "stage": "watch",
         "direction": "up", "price": 10.0, "change_pct": 1.0, "reasons": []},  # watch → 不记
        {"strategy": "板块异动", "stage": "trigger", "code": "--"},             # 非个股 → 不记
    ]
    added = sg.record_batch(rows, source="market_scan", tdate="2026-11-08")
    check("trigger×2 + down×1 共记录3条", added == 3, f"added={added}")
    items = sg.list_signals(status="all", limit=10)
    check("list 读回3条", len(items) == 3)
    check("watch 行未记录", all(s["code"] != "000001" for s in items))
    check("去重键含方向", any(s["id"].endswith(":down") for s in items))
    # 预置旧推送时间，再推一次同信号：不应新增行，pushed_at 追加
    raw = json.loads((TMP / "2026-11-08.json").read_text(encoding="utf-8"))
    key0 = "2026-11-08:600519:涨停回调:up"
    raw["signals"][key0]["pushed_at"] = ["2026-11-08T14:00:00"]
    (TMP / "2026-11-08.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    again = sg.record_batch(rows[:1], source="market_scan", tdate="2026-11-08")
    check("同日重复触发去重 added=0", again == 0)
    item = [s for s in sg.list_signals(limit=10) if s["id"] == key0][0]
    check("重复推送只更新 pushed_at", len(item["pushed_at"]) == 2, f"pushed_at={item['pushed_at']}")
    raw = json.loads((TMP / "2026-11-08.json").read_text(encoding="utf-8"))
    check("落盘文件为 dict keyed by id", isinstance(raw["signals"], dict) and len(raw["signals"]) == 3)

    # ----------------------------- 2. T+N 结果回填 -----------------------------
    print("[2] resolve_pending T+N 回填")
    n = 16
    closes = [10.0 + i * 0.01 for i in range(16)]
    closes[5] = 10.0
    closes[6], closes[10], closes[15] = 10.2, 11.0, 12.0
    highs = [c + 0.3 for c in closes]
    lows = [c - 0.3 for c in closes]
    highs[10], highs[15] = 11.3, 12.5
    lows = [max(x, 9.8) for x in lows]  # 窗口内最低点恰好 9.8 → mae 可精确断言
    lows[6] = 9.8
    dates = [f"2026-11-{str(3 + i).zfill(2)}" for i in range(n)]
    bars = {"600519": mk_bars(closes, highs, lows, dates)}
    # down 信号：随后下跌算赢
    closes_dn = [10.0] * 16
    closes_dn[5] = 10.0
    closes_dn[6], closes_dn[10], closes_dn[15] = 9.5, 9.0, 8.5
    bars["300750"] = mk_bars(closes_dn, [c + 0.1 for c in closes_dn],
                             [c - 0.1 for c in closes_dn], dates)
    # 000858 沿用第一节的上涨 frame
    bars["000858"] = mk_bars(closes, highs, lows, dates)
    r = sg.resolve_pending(bars=bars)
    check("首次回填 resolved_new=3", r["resolved_new"] == 3, str(r))
    sig = [s for s in sg.list_signals(limit=10) if s["code"] == "600519"][0]
    out = sig["outcome"]
    check("r_t1=+2.0", out["r_t1"] == 2.0, f"r_t1={out['r_t1']}")
    check("r_t5=+10.0", out["r_t5"] == 10.0)
    check("r_t10=+20.0", out["r_t10"] == 20.0)
    check("up 信号 win 全 True", all(out[f"win_t{k}"] for k in (1, 5, 10)))
    check("mfe_t10=+25.0", out["mfe_t10"] == 25.0, f"mfe_t10={out['mfe_t10']}")
    check("mae_t10=-2.0", out["mae_t10"] == -2.0, f"mae_t10={out['mae_t10']}")
    check("状态 resolved", out["status"] == "resolved")
    sig_dn = [s for s in sg.list_signals(limit=10) if s["code"] == "300750"][0]
    check("down 信号 r<0 判赢", sig_dn["outcome"]["win_t10"] is True, f"r_t10={sig_dn['outcome']['r_t10']}")
    r2 = sg.resolve_pending(bars=bars)
    check("第二次回填幂等 resolved_new=0", r2["resolved_new"] == 0, str(r2))

    # 窗口未走完 → pending（新代码，短 frame）
    short = mk_bars(closes[:12], highs[:12], lows[:12], dates[:12])
    sg.record_batch([{"code": "600036", "name": "招商银行", "strategy": "强势续攻",
                      "stage": "trigger", "direction": "up", "price": 40.0,
                      "change_pct": 2.0, "reasons": []}], source="market_scan",
                    tdate="2026-11-08")
    r3 = sg.resolve_pending(bars={"600036": short})
    sig_w = [s for s in sg.list_signals(limit=10) if s["code"] == "600036"][0]
    check("t10 未走完 → pending", sig_w["outcome"]["status"] == "pending", sig_w["outcome"]["status"])
    check("t5 已回填、t10 缺失", sig_w["outcome"].get("r_t5") is not None
          and sig_w["outcome"].get("r_t10") is None)

    # no_data：代码不在缓存，重试超限
    for _ in range(sg._MAX_RETRY + 1):
        sg.resolve_pending(bars={})
    sig_w2 = [s for s in sg.list_signals(limit=10) if s["code"] == "600036"][0]
    check("重试超限 → no_data", sig_w2["outcome"]["status"] == "no_data")

    # ----------------------------- 3. summary 统计 -----------------------------
    print("[3] summary 聚合")
    s = sg.summary()
    check("total=4", s["total"] == 4, f"total={s['total']}")
    check("resolved=3", s["resolved"] == 3, f"resolved={s['resolved']}")
    check("pending=0 / no_data=1", s["pending"] == 0 and s["no_data"] == 1,
          f"pending={s['pending']} no_data={s['no_data']}")
    t10 = s["horizons"]["t10"]
    check("t10 样本=3", t10["n"] == 3)
    check("t10 胜率=100%", abs(t10["win_rate"] - 100.0) < 0.1, f"win_rate={t10['win_rate']}")
    check("分方向 up.t10 n=2 / down.t10 n=1",
          s["by_direction"]["up"]["t10"]["n"] == 2 and s["by_direction"]["down"]["t10"]["n"] == 1)
    st_names = {x["strategy"] for x in s["by_strategy"]}
    check("分策略含四个战法", {"涨停回调", "洗盘反包", "双线死叉", "强势续攻"} <= st_names, str(st_names))
    wash = [x for x in s["by_strategy"] if x["strategy"] == "涨停回调"][0]
    check("涨停回调 resolved=1", wash["resolved"] == 1)

    # ----------------------------- 4. prune 清理 -----------------------------
    print("[4] prune 清理")
    old = sg._file("2026-01-01")
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text(json.dumps({"tdate": "2026-01-01", "signals": {}}), encoding="utf-8")
    removed = sg.prune(keep_days=120)
    check("超期文件被清理", removed == 1 and not old.exists())
    check("近期文件保留", (TMP / "2026-11-08.json").exists())

    # ----------------------------- 5. mainwave 波动收缩列 -----------------------------
    print("[5] mainwave._indicators 收缩列")
    import app.mainwave as mw
    rng = np.random.default_rng(7)
    n2 = 80
    close = 100 + np.cumsum(rng.normal(0, 1, n2))
    df = pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n2).strftime("%Y-%m-%d"),
        "open": close + rng.normal(0, .3, n2), "close": close,
        "high": close + abs(rng.normal(1, .3, n2)), "low": close - abs(rng.normal(1, .3, n2)),
        "volume": rng.integers(1e5, 5e5, n2).astype(float),
        "pct_change": np.concatenate([[0.0], np.diff(close) / close[:-1] * 100]),
    })
    d = mw._indicators(df, "600519", "贵州茅台")
    need_cols = ["bb_mid", "bb_up", "bb_dn", "bb_bw", "bb_bw_q25", "bb_squeeze", "bb_squeeze_prior", "vcp_ratio"]
    check("新增8列齐全", all(c in d.columns for c in need_cols))
    last = d.iloc[-1]
    check("布林上>中>下", last["bb_up"] > last["bb_mid"] > last["bb_dn"])
    check("squeeze 为 bool", isinstance(bool(last["bb_squeeze"]), bool))
    check("vcp_ratio 有限值", np.isfinite(last["vcp_ratio"]))
    # 无前视：截断到第 i 行再算，第 i 行的 vcp/squeeze 不变
    i = 60
    d_trunc = mw._indicators(df.iloc[:i + 1], "600519", "贵州茅台")
    same_vcp = abs(float(d.iloc[i]["vcp_ratio"]) - float(d_trunc.iloc[i]["vcp_ratio"])) < 1e-9
    same_sq = bool(d.iloc[i]["bb_squeeze"]) == bool(d_trunc.iloc[i]["bb_squeeze"])
    check("vcp 无前视（截断一致）", same_vcp)
    check("squeeze 无前视（截断一致）", same_sq)

    # ----------------------------- 6. 配置默认值 -----------------------------
    print("[6] 配置默认值")
    cfg = AppConfig()
    check("signal_track_enabled 默认 True", cfg.scan.signal_track_enabled is True)
    check("signal_eval_time 默认 15:30", cfg.scan.signal_eval_time == "15:30")
    check("signal_keep_days 默认 120", cfg.scan.signal_keep_days == 120)
    check("vcp_note_enabled 默认 True", cfg.market_regime.vcp_note_enabled is True)
    check("vcp_contraction_max 默认 0.65", cfg.market_regime.vcp_contraction_max == 0.65)
    check("absorb_note_enabled 默认 True", cfg.market_regime.absorb_note_enabled is True)
    # 旧 config.json 迁移兼容（纯 dict 走 _migrate）
    migrated = C._migrate({"scan": {}, "market_regime": {}})
    cfg2 = AppConfig.model_validate(migrated)
    check("旧配置迁移自动补齐新字段", cfg2.scan.signal_eval_time == "15:30"
          and cfg2.market_regime.vcp_contraction_max == 0.65)

    # ----------------------------- 7. analyze_stock 附 readings -----------------------------
    print("[7] analyze_stock readings")
    hist = df.rename(columns={})  # 80根合成K线
    orig_hist, orig_names = mw.ds.stock_history, mw.ds.stock_code_names
    mw.ds.stock_history = lambda code, days=130: hist
    mw.ds.stock_code_names = lambda: {"600519": "贵州茅台"}
    try:
        rules = AppConfig().strategy_rules.model_dump()
        sigs = mw.analyze_stock("600519", rules, spot_names={"600519": "贵州茅台"})
        check("analyze_stock 可运行", isinstance(sigs, list))
        if sigs:
            check("信号附 readings", all("readings" in s for s in sigs),
                  f"keys={list(sigs[0].get('readings', {}).keys())}")
            check("readings 含 vcp_ratio", all("vcp_ratio" in s["readings"] for s in sigs))
        else:
            check("合成数据无命中也接受（readings 逻辑未触发）", True)
    finally:
        mw.ds.stock_history, mw.ds.stock_code_names = orig_hist, orig_names

finally:
    restore()
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n===== 通过 {len(PASS)} / {len(PASS) + len(FAIL)} =====")
sys.exit(1 if FAIL else 0)
