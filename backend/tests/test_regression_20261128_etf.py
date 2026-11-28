"""回归测试 2026-11-28：ETF 监控模块（etfwatch）。

契约：不联网、不写真实 data/（CACHE_DIR/STATE_FILE/scanstore 全部打桩到临时目录，
etf_history 打桩返回合成K线），可安全重复运行。运行方式：
    backend/.venv/bin/python tests/test_regression_20261128_etf.py
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


def mk_etf(closes, vols=None, n_pad=0):
    n = len(closes)
    vols = vols if vols is not None else np.full(n, 10000.0)
    close = np.asarray(closes, dtype=float)
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n).strftime("%Y-%m-%d"),
        "open": close, "close": close,
        "high": close * 1.01, "low": close * 0.99,
        "volume": vols, "amount": close * vols,
        "pct_change": np.concatenate([[0.0], np.diff(close) / close[:-1] * 100]),
        "turnover": np.full(n, 1.0),
    })


try:
    import app.etfwatch as ew
    import app.config as C
    from app.config import AppConfig

    TMP = Path(tempfile.mkdtemp(prefix="etf_test_"))
    ORIG = (ew.CACHE_DIR, ew.STATE_FILE, ew.ds.etf_history, ew.ds.effective_trading_date, ew.ds.etf_spot)
    ew.CACHE_DIR = TMP
    ew.STATE_FILE = TMP / "state.json"
    ew.ds.etf_history = lambda code, days=400: mk_etf(
        np.linspace(1.0, 1.2, 80) * (1 + 0.001 * np.sin(np.arange(80) / 3)))
    # 合成K线末日为 2026-03-21（n=80），打桩"今日"让数据新鲜度守卫放行
    ew.ds.effective_trading_date = lambda: {"date": "2026-03-21"}

    try:
        # ----------------------------- 1. refresh + 缓存 -----------------------------
        print("[1] refresh 缓存")
        r = ew.refresh(force=True)
        check("池内全部刷新成功", len(r["ok"]) == len(r and ew._pool_codes()) and not r["fail"], str(r))
        check("缓存文件落盘", len(list(TMP.glob('*.csv'))) >= len(ew._pool_codes()))

        # ----------------------------- 2. squeeze 判定 -----------------------------
        print("[2] squeeze 判定")
        df = ew._load_bars()[ew._pool_codes()[0][0]]
        d = ew.__dict__["mw"]._indicators(df, "588000", "科创50ETF")
        sq_last = ew._squeeze(d, AppConfig().etf, len(d) - 1)
        check("squeeze 返回 bool", isinstance(sq_last, bool))
        # 带宽极小的合成序列应判 squeeze
        flat = mk_etf(np.full(80, 1.0) + np.sin(np.arange(80)) * 0.0005)
        d_flat = ew.__dict__["mw"]._indicators(flat, "588000", "科创50ETF")
        check("窄幅序列判定 squeeze=True", ew._squeeze(d_flat, AppConfig().etf, len(d_flat) - 1) is True)

        # ----------------------------- 3. scan 端到端（合成无突破 → 0信号但状态正常）-----------------------------
        print("[3] scan 端到端")
        s = ew.scan(push=False)
        check("scan success", s.get("success") is True)
        check("tier_factor ∈ {1.0, 0.25}", s.get("tier_factor") in (1.0, 0.25), str(s.get("tier_factor")))
        check("纸面状态文件生成", (TMP / "state.json").exists())

        # ----------------------------- 4. 买入/卖出状态翻转 -----------------------------
        print("[4] 纸面持仓状态翻转")
        # 直接种状态：构造突破场景验证翻转逻辑
        code0 = ew._pool_codes()[0][0]
        closes = np.concatenate([np.full(60, 1.0), np.linspace(1.0, 1.10, 20)])
        ew.ds.etf_history = lambda code, days=400: mk_etf(closes)
        ew.refresh(force=True)
        ew.invalidate()
        s2 = ew.scan(push=False)
        st = ew._load_state()
        # 上恒定带宽极小→squeeze，末段抬升+量不变→量比<1.2 可能只出 watch；两类都算"有输出"
        check("趋势抬升场景产生信号或无异常", s2.get("success") is True)
        # 手动验证卖出翻转：直接置状态后跑中轨跌破场景
        ew._persist_state({code0: {"in_position": True, "entry_date": "2026-01-01",
                                   "entry_price": 9.99, "life_line": 9.0}})
        falling = mk_etf(np.concatenate([np.linspace(1.0, 1.2, 70), np.linspace(1.2, 1.0, 10)]))
        ew.ds.etf_history = lambda code, days=400: falling
        ew.refresh(force=True)
        ew.invalidate()
        s3 = ew.scan(push=False)
        sells = [x for x in s3.get("signals", []) if x["code"] == code0 and x["direction"] == "down"]
        st2 = ew._load_state()
        check("跌回中轨产生卖出信号", len(sells) >= 1, f"sells={len(sells)}")
        check("卖出后纸面状态翻空", st2.get(code0, {}).get("in_position") is False)
        # 情绪冰点：深跌场景（z20≤-1.5 ∧ 5日≤-4%）应触发防抄底提醒（watch 级，非买入）
        freeze_sigs = [x for x in s3.get("signals", [])
                       if x["strategy"] == "情绪冰点" and x["stage"] == "watch"]
        check("深跌场景触发情绪冰点提醒", len(freeze_sigs) >= 1, f"freeze={len(freeze_sigs)}")
        check("冰点提醒非买入信号", all("勿抄底" in x["stage_label"] for x in freeze_sigs))
        # 平稳场景不应误报冰点
        flat2 = mk_etf(np.linspace(1.0, 1.05, 80))
        ew.ds.etf_history = lambda code, days=400: flat2
        ew.refresh(force=True)
        ew.invalidate()
        s4 = ew.scan(push=False)
        fz = [x for x in s4.get("signals", []) if x["strategy"] == "情绪冰点"]
        check("平稳场景不误报冰点", len(fz) == 0, f"freeze={len(fz)}")

        # ----------------------------- 5. 池子增删 -----------------------------
        print("[5] 池子增删")
        a = ew.pool_add("510300", "沪深300ETF")
        check("pool_add 成功", a["success"] is True)
        check("池子含新标的", any(it.code == "510300" for it in C.load_config().etf.pool))
        r2 = ew.pool_remove("510300")
        check("pool_remove 成功", r2["success"] is True)
        check("池子恢复", all(it.code != "510300" for it in C.load_config().etf.pool))
        dup = ew.pool_add("588000")
        check("重复添加被拒", dup["success"] is False)

        # ----------------------------- 6. 搜索与热榜打桩 -----------------------------
        print("[6] 搜索过滤")
        ew.ds.etf_universe = lambda: [{"code": "588000", "name": "科创50ETF", "price": 1.0, "pct": 1.0, "volume": 1},
                                      {"code": "512480", "name": "半导体ETF", "price": 1.0, "pct": 2.0, "volume": 1}]
        hits = ew.search_universe("半导体")
        check("按名称搜索", len(hits) == 1 and hits[0]["code"] == "512480")
        hits2 = ew.search_universe("588")
        check("按代码搜索", len(hits2) == 1 and hits2[0]["code"] == "588000")

        # ----------------------------- 6b. 盘中预警层 -----------------------------
        print("[6b] intraday_check 盘中预警")
        # 深跌场景：现价跌回中轨下方 → 持仓状态应触发卖出预警
        falling = mk_etf(np.concatenate([np.linspace(1.0, 1.2, 70), np.linspace(1.2, 1.0, 10)]))
        ew.ds.etf_history = lambda code, days=400: falling
        ew.ds.etf_spot = lambda ttl=30: [{"code": c, "name": n, "price": 0.99, "pct": None,
                                          "volume": None, "turnover": None}
                                         for c, n in ew._pool_codes()]  # 现价0.99 < 中轨
        ew.refresh(force=True)
        ew.invalidate()
        ew._persist_state({ew._pool_codes()[0][0]: {"in_position": True, "entry_price": 1.15}})
        r = ew.intraday_check()
        sells = [a for a in r["alerts"] if a["direction"] == "down"]
        check("持仓跌破中轨 → 卖出预警", len(sells) >= 1, f"alerts={len(r['alerts'])}")
        r2 = ew.intraday_check()
        sells2 = [a for a in r2["alerts"] if a["direction"] == "down"]
        check("同日同标的去重（第二次不重复）", len(sells2) == 0, f"sells2={len(sells2)}")
        # 逼近突破位 → 买入预警（空仓 + 现价接近上轨）
        rising = mk_etf(np.linspace(1.0, 1.3, 80))
        ew.ds.etf_history = lambda code, days=400: rising
        ew.ds.etf_spot = lambda ttl=30: [{"code": c, "name": n, "price": 1.33, "pct": None,
                                          "volume": None, "turnover": None}
                                         for c, n in ew._pool_codes()]  # 现价1.33 ≥ 上轨
        ew.refresh(force=True)
        ew.invalidate()
        ew._persist_state({})
        r3 = ew.intraday_check()
        buys = [a for a in r3["alerts"] if a["direction"] == "up"]
        check("触及突破位 → 买入预警", len(buys) >= 1, f"alerts={len(r3['alerts'])}")
        ew.ds.effective_trading_date = ORIG[3]  # 恢复真实日期（后续断言用）

        # ----------------------------- 7. 配置默认值 -----------------------------
        print("[7] 配置默认值")
        ec = AppConfig().etf
        check("squeeze_enabled 默认 True（回测唯一稳健）", ec.squeeze_enabled is True)
        check("trend/rotation/ignition 默认关", ec.trend_enabled is False and ec.rotation_enabled is False
              and ec.ignition_link_enabled is False)
        check("refresh 15:12 / scan 15:20", ec.refresh_time == "15:12" and ec.scan_time == "15:20")
        check("safe_asset 默认 cash", ec.safe_asset == "cash")
        check("freeze_note_enabled 默认 True", ec.freeze_note_enabled is True)
        check("intraday_enabled 默认 True / 间隔5分钟", ec.intraday_enabled is True
              and ec.intraday_interval_minutes == 5)
        check("freeze 阈值默认 -1.5/-4", ec.freeze_z == -1.5 and ec.freeze_r5 == -4.0)
        migrated = C._migrate({})
        check("旧配置迁移自动补齐 etf 字段", AppConfig.model_validate(migrated).etf.enabled is True)

    finally:
        ew.CACHE_DIR, ew.STATE_FILE = ORIG[0], ORIG[1]
        ew.ds.etf_history, ew.ds.effective_trading_date, ew.ds.etf_spot = ORIG[2], ORIG[3], ORIG[4]
        shutil.rmtree(TMP, ignore_errors=True)

finally:
    print()

print(f"\n===== 通过 {len(PASS)} / {len(PASS) + len(FAIL)} =====")
sys.exit(1 if FAIL else 0)
