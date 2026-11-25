"""情绪点火盘中监控：昨日涨停股当前中位溢价的状态机，点火即提醒（稳妥版）。

设计原则（稳妥）：
- 不做一次性阈值穿越报警——开盘冲高常见、随即回落（点火一日游），必须
  「连续 K 次检查都 ≥ 阈值」才确认点火（默认每 5 分钟查一次 × 3 次 = 持续 15 分钟）；
- 确认后当日只提醒一次；若盘中中位溢价跌破 0，追加一条「点火退潮」提醒
  （对应 11/10 陷阱：点火次日即退潮，追高被收割）；
- 数据降级 / 池子过小（<10 只）/ 休市 → 跳过本次检查且中断连续计数，绝不误报；
- 状态按交易日落盘 backend/data/ignition_state.json，重启不重置、不重复提醒。

只做提醒，不下单、不自动改闸门；可选「点火即扫描」由调度器在确认转沿时执行。
"""
from __future__ import annotations

import json
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from . import datasource as ds, notifications, notifier, patternwatch
from .config import effective_config, load_config

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STATE_FILE = DATA_DIR / "ignition_state.json"


class IgnitionState:
    def __init__(self) -> None:
        self.date: str | None = None            # 状态所属交易日(YYYYMMDD)
        self.readings: deque[tuple[str, float]] = deque(maxlen=20)   # (HH:MM, median)
        self.confirmed: bool = False            # 当日已确认点火
        self.confirmed_time: str | None = None
        self.confirmed_median: float | None = None
        self.collapsed: bool = False            # 当日已提醒退潮
        self.collapsed_time: str | None = None
        self.streak: int = 0                    # 连续达标次数（数据失败即清零）
        self.today_zt: int | None = None        # 确认时当日实时涨停家数（广度档位）
        self.tier: str | None = None            # 档位：全仓档/试错档
        self.last_check: str | None = None
        self.last_median: float | None = None
        self.last_n: int | None = None
        self._lock = threading.RLock()

    def reset_for(self, date: str) -> None:
        self.date = date
        self.readings.clear()
        self.confirmed = False
        self.confirmed_time = None
        self.confirmed_median = None
        self.collapsed = False
        self.collapsed_time = None
        self.streak = 0
        self.today_zt = None
        self.tier = None
        self.last_check = None
        self.last_median = None
        self.last_n = None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "date": self.date,
                "confirmed": self.confirmed,
                "confirmed_time": self.confirmed_time,
                "confirmed_median": self.confirmed_median,
                "collapsed": self.collapsed,
                "collapsed_time": self.collapsed_time,
                "streak": self.streak,
                "today_zt": self.today_zt,
                "tier": self.tier,
                "last_check": self.last_check,
                "last_median": self.last_median,
                "last_n": self.last_n,
                "readings": list(self.readings)[-12:],
            }


state = IgnitionState()


def _load_persisted() -> None:
    """恢复当日状态（重启不重置、不重复提醒）。"""
    try:
        if not STATE_FILE.exists():
            return
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if raw.get("date") != datetime.now().strftime("%Y%m%d"):
            return
        state.date = raw.get("date")
        state.readings = deque(raw.get("readings", []), maxlen=20)
        state.confirmed = raw.get("confirmed", False)
        state.confirmed_time = raw.get("confirmed_time")
        state.confirmed_median = raw.get("confirmed_median")
        state.collapsed = raw.get("collapsed", False)
        state.collapsed_time = raw.get("collapsed_time")
        state.streak = raw.get("streak", 0)
    except Exception:  # noqa: BLE001
        pass


def _persist() -> None:
    try:
        STATE_FILE.write_text(json.dumps(state.snapshot(), ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


_load_persisted()


def _notify(title: str, body: str) -> int:
    notifications.add(title, body, [], kind="ignition")
    cfg = load_config()
    pushed = 0
    if cfg.notification:
        results = notifier.broadcast(cfg.notification, title, body, [])
        pushed = sum(1 for r in results if r["success"])
    return pushed


def check() -> dict[str, Any]:
    """执行一次点火监控检查（调度器每 N 分钟调用）。返回本次检查摘要。"""
    cfg = effective_config()
    reg = cfg.market_regime
    threshold = float(reg.ignition_threshold)
    need = max(1, int(reg.ignition_sustain_checks))
    min_pool = max(1, int(getattr(reg, "ignition_min_pool", 10)))

    status = ds.market_status()["status"]
    if status not in ("trading", "lunch", "after_hours"):
        return {"checked": False, "reason": f"非交易时段({status})"}

    eff = ds.effective_trading_date()
    today = eff["date"]
    now_hm = datetime.now().strftime("%H:%M")
    with state._lock:
        if state.date != today:
            state.reset_for(today)
        state.last_check = now_hm

        ign = ds.ignition_premium(today, ttl=30)
        if ign.get("degraded") or ign.get("median") is None:
            state.streak = 0
            state.last_median = None
            state.last_n = ign.get("n")
            _persist()
            return {"checked": True, "reason": "数据降级跳过（连续计数清零）", **state.snapshot()}
        median = float(ign["median"])
        n = int(ign.get("n") or 0)
        state.last_median = median
        state.last_n = n
        if n < min_pool:
            state.streak = 0
            _persist()
            return {"checked": True, "reason": f"涨停池过小({n}<{min_pool})跳过", **state.snapshot()}

        state.readings.append((now_hm, median))
        fired = False

        if not state.confirmed:
            state.streak = state.streak + 1 if median >= threshold else 0
            if state.streak >= need:
                state.confirmed = True
                state.confirmed_time = now_hm
                state.confirmed_median = median
                # 广度档位判定：今日实时涨停家数（全仓日/试错日是最关键的决策信息）
                tier_th = float(cfg.market_regime.ignition_tier_breadth)
                today_zt = None
                try:
                    detail = ds.limit_up_pool_detail(today, ttl=120)
                    today_zt = len(detail) if detail else 0
                except Exception:  # noqa: BLE001
                    today_zt = None
                state.today_zt = today_zt
                if today_zt is None:
                    tier = "档位未知"
                    tier_note = "实时涨停家数获取失败，按试错档谨慎处理"
                elif today_zt >= tier_th:
                    tier = "全仓档"
                    tier_note = f"普涨高潮（≥{tier_th:g}家），此类信号胜率回测 67%~82%"
                else:
                    tier = "试错档"
                    tier_note = f"广度不足（<{tier_th:g}家），此类信号胜率回测 10%~23%，建议 1/4 仓或观望"
                state.tier = tier
                fired = True
                mins = need * int(reg.ignition_watch_minutes)
                title = f"【情绪点火确认·{tier}】中位溢价 {median:+.2f}% ≥ {threshold:g}%"
                picks_note = ""
                try:
                    pk = patternwatch.ignition_picks(4)
                    picks = pk.get("picks", [])
                    if picks:
                        lines = []
                        for s in picks:
                            ry = s.get("rev_yoy")
                            pp = s.get("profit_pos")
                            fund = f"营收{'--' if ry is None else format(ry, '+.0f') + '%'}｜{'盈利' if pp else ('亏损' if pp is False else '未知')}"
                            px = f"现价{s['price']}" if s.get('price') else '未开盘'
                            lines.append(f"- ★ {s['name']}({s['code']}) {px}｜{fund}｜{s['industry']}")
                        picks_note = "\n".join(lines)
                except Exception as _exc:  # noqa: BLE001
                    picks_note = f"（精选名单生成失败：{str(_exc)[:60]}）"
                bomb_note = ""
                try:
                    from . import replay as _rp
                    _br = (_rp.market_sentiment().get(ign.get("prev_date", ""), {}) or {}).get("bomb_rate")
                    _br_max = float(reg.ignition_bomb_rate_max)
                    if _br is not None and _br > _br_max:
                        bomb_note = (f"\n⚠️ 昨日市场炸板率 {_br:.1f}% > {_br_max:g}%（资金分歧极大）："
                                     f"今日开仓闸门仍会拦截，本提醒仅供观察、不开新仓。")
                except Exception:  # noqa: BLE001
                    pass
                body = (
                    f"确认时间：{now_hm}（连续 {need} 次检查达标，约 {mins} 分钟）\n"
                    f"样本：{ign.get('prev_date', '')} 涨停池 {n} 只，当前中位溢价 {median:+.2f}%\n"
                    f"广度：今日实时涨停 {today_zt if today_zt is not None else '?'} 家 → {tier_note}\n"
                    f"纪律提醒：点火日可按战法开仓（洗盘反包为主），14:00/14:40 推送信号；\n"
                    f"若后续跌破 0 会再发「点火退潮」提醒，届时停止追高。"
                    + bomb_note +
                    f"\n\n【今日精选候选·结构化分散参考】（盈利优先+行业分散，非买入承诺；"
                    f"生命线以触发后推送为准）\n{picks_note}"
                )
                _notify(title, body)
        elif not state.collapsed and median < 0:
            state.collapsed = True
            state.collapsed_time = now_hm
            fired = True
            title = f"【点火退潮】中位溢价 {median:+.2f}% 跌破 0"
            body = (
                f"退潮时间：{now_hm}（{state.confirmed_time or ''} 确认点火后回落）\n"
                f"样本：{n} 只，当前中位溢价 {median:+.2f}%\n"
                f"纪律提醒：点火一日游形态，今日追高信号大概率被收割，停止开新仓；\n"
                f"已持仓者按卖出三通道纪律执行。"
            )
            _notify(title, body)

        _persist()
        return {"checked": True, "fired": fired, **state.snapshot()}


def status() -> dict[str, Any]:
    cfg = effective_config()
    return {
        "enabled": bool(cfg.market_regime.ignition_watch_enabled),
        "minutes": int(cfg.market_regime.ignition_watch_minutes),
        "sustain_checks": int(cfg.market_regime.ignition_sustain_checks),
        "auto_scan": bool(cfg.market_regime.ignition_auto_scan),
        "state": state.snapshot(),
    }


def reset_day() -> dict[str, Any]:
    """手动清空当日状态（调试用）。"""
    with state._lock:
        state.reset_for(state.date or datetime.now().strftime("%Y%m%d"))
        _persist()
    return state.snapshot()
