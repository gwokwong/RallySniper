"""信号验证闭环：实盘触发信号落盘 + T+N 结果自动回填 + 胜率统计。

三部分职责：
1. record_batch：各发射点（全市场扫描/标的池/卖出风控/指标预警/点火精选）把
   trigger 级信号按数据交易日落盘到 data/signals/{YYYYMMDD}.json。只记「可操作」
   信号：stage=trigger，或卖出风控那种无 stage 但 direction=down 的预警行；
   观察中(watch)与昨日涨停简报不记。同日同票同战法同方向去重（只更新推送时间）。
2. resolve_pending：收盘后（等 15:10 K线缓存刷新完）用本地缓存离线回填
   T+1/5/10 收盘对收盘收益与窗口内最大盈亏（MFE/MAE），t10 数据齐 → resolved。
   代码不在缓存里重试 5 次后标 no_data。幂等，可重复跑。
3. summary / list_signals：供 API 与前端「信号验证」页展示分策略胜率。

口径约定（与 backtests/backtest_vr_turnover.py 一致）：
- T+N 按个股自身 bar 序列（第 N 根 K 线收盘），停牌缺口不计；
- 收益基准 = 信号日收盘（盘中实时价并存为 entry_price 供对照）；
- 胜负按方向翻转：up 信号 r>0 算赢，down 信号（卖出预警）r<0 算赢。
"""
from __future__ import annotations

import json
import statistics
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from .config import load_config
from .scanstore import _atomic_write

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data" / "signals"
HORIZONS = (1, 5, 10)          # 回填窗口（个股自身第 N 根K线）
_MAX_RETRY = 5                 # 代码不在K线缓存中的最大重试次数
_lock = threading.RLock()


def _default_tdate() -> str:
    """默认数据交易日：取最近交易日，取不到退化为今天。"""
    try:
        from . import datasource as ds
        return str(ds.effective_trading_date()["date"])
    except Exception:  # noqa: BLE001
        return datetime.now().strftime("%Y-%m-%d")


def _file(tdate: str) -> Path:
    return DATA / f"{tdate}.json"


def _load_day(tdate: str) -> dict[str, Any]:
    f = _file(tdate)
    if not f.exists():
        return {"tdate": tdate, "signals": {}}
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("signals"), dict):
            return raw
    except Exception:  # noqa: BLE001
        pass
    return {"tdate": tdate, "signals": {}}


def _recordable(row: dict[str, Any]) -> bool:
    """只记可操作信号：trigger 级，或卖出风控无 stage 的 down 预警。"""
    if str(row.get("code") or "").strip() in ("", "--"):
        return False  # 板块异动等非个股行
    stage = row.get("stage")
    if stage == "trigger":
        return True
    return stage is None and row.get("direction") == "down"


def record_batch(rows: list[dict[str, Any]] | None, source: str,
                 tdate: str | None = None) -> int:
    """记录一批 trigger 级信号，返回新记录条数（同日重复触发返回 0，只更新 pushed_at）。"""
    if not rows:
        return 0
    try:
        if not load_config().scan.signal_track_enabled:
            return 0
    except Exception:  # noqa: BLE001
        pass
    day = tdate or _default_tdate()
    now = datetime.now().isoformat(timespec="seconds")
    added = 0
    with _lock:
        payload = _load_day(day)
        signals = payload["signals"]
        for row in rows:
            try:
                if not isinstance(row, dict) or not _recordable(row):
                    continue
                code = str(row["code"]).zfill(6)
                strategy = str(row.get("strategy") or "").strip()
                direction = "down" if row.get("direction") == "down" else "up"
                if not strategy:
                    continue
                sig_id = f"{day}:{code}:{strategy}:{direction}"
                rec = signals.get(sig_id)
                if rec:
                    pushed = rec.setdefault("pushed_at", [])
                    if now not in pushed:
                        pushed.append(now)
                    # 价格/涨跌幅取最后一次推送的实时快照
                    if row.get("price") not in (None, ""):
                        rec["entry_price"] = row.get("price")
                    if row.get("change_pct") is not None:
                        rec["change_pct"] = row.get("change_pct")
                    continue
                signals[sig_id] = {
                    "id": sig_id,
                    "tdate": day,
                    "code": code,
                    "name": str(row.get("name") or ""),
                    "strategy": strategy,
                    "direction": direction,
                    "stage_label": str(row.get("stage_label") or ""),
                    "entry_price": row.get("price"),
                    "change_pct": row.get("change_pct"),
                    "reasons": [str(r) for r in (row.get("reasons") or [])][:6],
                    "readings": row.get("readings") or None,
                    "source": source,
                    "recorded_at": now,
                    "pushed_at": [now],
                    "outcome": {},
                }
                added += 1
            except Exception:  # noqa: BLE001
                continue
        if added or signals:
            payload["saved_at"] = now
            DATA.mkdir(parents=True, exist_ok=True)
            _atomic_write(_file(day), payload)
    return added


def _resolve_one(sig: dict[str, Any], bars: dict[str, pd.DataFrame]) -> str:
    """回填单条信号，返回 'resolved' / 'pending' / 'no_data'。只改 outcome 字段。"""
    out = sig.get("outcome") or {}
    code = sig["code"]
    df = bars.get(code)
    if df is None or df.empty:
        out["status"] = "no_data"
        out["attempts"] = int(out.get("attempts") or 0) + 1
        if out["attempts"] < _MAX_RETRY:
            out["status"] = "pending"
        sig["outcome"] = out
        return out["status"]
    dates = [str(x)[:10] for x in df["date"]]
    try:
        idx = dates.index(sig["tdate"])
    except ValueError:
        # 信号日该股无K线（停牌/新股），后续仍可能补上
        out["attempts"] = int(out.get("attempts") or 0) + 1
        if out["attempts"] >= _MAX_RETRY:
            out["status"] = "no_data"
        else:
            out["status"] = "pending"
        sig["outcome"] = out
        return out["status"]

    base = float(df["close"].iloc[idx])
    if base <= 0:
        out["status"] = "no_data"
        sig["outcome"] = out
        return out["status"]
    win_sign = -1.0 if sig.get("direction") == "down" else 1.0
    n = len(df)
    filled_all = True
    for k in HORIZONS:
        j = idx + k
        if j >= n:
            filled_all = False
            break  # 窗口尚未走完，K线缓存每日刷新后自然补齐
        r = (float(df["close"].iloc[j]) / base - 1.0) * 100.0
        hi = float(df["high"].iloc[idx + 1:j + 1].max())
        lo = float(df["low"].iloc[idx + 1:j + 1].min())
        out[f"r_t{k}"] = round(r, 2)
        out[f"win_t{k}"] = bool(r * win_sign > 0)
        out[f"mfe_t{k}"] = round((hi / base - 1.0) * 100.0, 2)
        out[f"mae_t{k}"] = round((lo / base - 1.0) * 100.0, 2)
    out["status"] = "resolved" if filled_all else "pending"
    out.pop("attempts", None)
    sig["outcome"] = out
    return out["status"]


def resolve_pending(bars: dict[str, pd.DataFrame] | None = None,
                    keep_days: int | None = None) -> dict[str, Any]:
    """回填全部未完成信号的结果（幂等，可手动触发）。bars 缺省用 replay K线缓存。"""
    if bars is None:
        from . import replay as rp
        bars = rp._load_bars()
    counts = {"resolved_new": 0, "pending": 0, "no_data": 0, "pruned_files": 0}
    now = datetime.now().isoformat(timespec="seconds")
    with _lock:
        for f in sorted(DATA.glob("*.json")):
            try:
                payload = json.loads(f.read_text(encoding="utf-8"))
                sigs = payload.get("signals")
                if not isinstance(sigs, dict):
                    continue
                changed = False
                for sig in sigs.values():
                    out = sig.get("outcome") or {}
                    if out.get("status") in ("resolved", "no_data"):
                        if out.get("status") == "no_data":
                            counts["no_data"] += 1
                        continue
                    st = _resolve_one(sig, bars)
                    changed = True
                    if st == "resolved":
                        counts["resolved_new"] += 1
                    elif st == "no_data":
                        counts["no_data"] += 1
                    else:
                        counts["pending"] += 1
                if changed:
                    payload["saved_at"] = now
                    _atomic_write(f, payload)
            except Exception:  # noqa: BLE001
                continue
        if keep_days:
            counts["pruned_files"] = prune(keep_days)
    return counts


def prune(keep_days: int = 120) -> int:
    """删除超过保留天数的信号文件，返回删除数。"""
    cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    removed = 0
    with _lock:
        for f in DATA.glob("*.json"):
            stem = f.stem
            if len(stem) == 10 and stem < cutoff:
                try:
                    f.unlink(missing_ok=True)
                    removed += 1
                except OSError:
                    pass
    return removed


def _agg(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    """对已 resolved 的记录按 r_{key} 聚合：n/胜率/均值/中位/累计(万，按1万/笔)。

    胜率用回填时存的 win_t{k} 标志（方向感知：down 信号 r<0 算赢），
    避免 up/down 混合统计时把正确的卖出预警算成亏损。
    """
    rs, wins = [], 0
    for s in rows:
        out = s.get("outcome") or {}
        if out.get("status") != "resolved" or f"r_{key}" not in out:
            continue
        rs.append(out[f"r_{key}"])
        wins += 1 if out.get(f"win_{key}") else 0
    if not rs:
        return {"n": 0}
    return {
        "n": len(rs),
        "win_rate": round(100.0 * wins / len(rs), 1),
        "avg": round(sum(rs) / len(rs), 2),
        "med": round(statistics.median(rs), 2),
        "sum_wan": round(sum(r / 100.0 for r in rs), 2),
    }


def summary() -> dict[str, Any]:
    """汇总统计：整体计数 + 按 horizon 胜率 + 分策略胜率。"""
    recs = list_signals(status="all", limit=100000)
    resolved = [s for s in recs if s.get("outcome", {}).get("status") == "resolved"]
    horizons = {}
    for k in HORIZONS:
        horizons[f"t{k}"] = _agg(recs, f"t{k}")
    up = [s for s in recs if s.get("direction") == "up"]
    down = [s for s in recs if s.get("direction") == "down"]
    by_dir = {"up": {f"t{k}": _agg(up, f"t{k}") for k in HORIZONS},
              "down": {f"t{k}": _agg(down, f"t{k}") for k in HORIZONS}}
    # 分策略： strategy+direction 分组
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for s in recs:
        groups.setdefault((s.get("strategy", ""), s.get("direction", "up")), []).append(s)
    by_strategy = []
    for (strategy, direction), rows in sorted(groups.items(), key=lambda x: -len(x[1])):
        by_strategy.append({
            "strategy": strategy,
            "direction": direction,
            "total": len(rows),
            "resolved": sum(1 for s in rows if s.get("outcome", {}).get("status") == "resolved"),
            "t1": _agg(rows, "t1"), "t5": _agg(rows, "t5"), "t10": _agg(rows, "t10"),
        })
    return {
        "total": len(recs),
        "resolved": len(resolved),
        "pending": sum(1 for s in recs if s.get("outcome", {}).get("status") == "pending"),
        "no_data": sum(1 for s in recs if s.get("outcome", {}).get("status") == "no_data"),
        "horizons": horizons,
        "by_direction": by_dir,
        "by_strategy": by_strategy,
    }


def list_signals(status: str = "all", limit: int = 100,
                 strategy: str | None = None, source: str | None = None) -> list[dict[str, Any]]:
    """按状态/战法/来源过滤列出信号（新日期在前）。status: all/pending/resolved/no_data。"""
    rows: list[dict[str, Any]] = []
    with _lock:
        for f in sorted(DATA.glob("*.json"), reverse=True):
            try:
                payload = json.loads(f.read_text(encoding="utf-8"))
                sigs = payload.get("signals") or {}
            except Exception:  # noqa: BLE001
                continue
            for sig in sigs.values():
                st = (sig.get("outcome") or {}).get("status", "pending")
                if status != "all" and st != status:
                    continue
                if strategy and sig.get("strategy") != strategy:
                    continue
                if source and sig.get("source") != source:
                    continue
                rows.append(sig)
    rows.sort(key=lambda s: (s.get("tdate", ""), s.get("recorded_at", "")), reverse=True)
    return rows[: max(0, limit)]
