"""后台盯盘调度器（主升浪捕手）。

常驻进程内定时扫描标的池，命中主升浪战法后经邮件推送。
按「股票代码 + 战法」做冷却去重，避免同一信号反复提醒。
保留最近告警历史供前端查看。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from . import analyzer, datasource as ds, mainwave
from .config import AppConfig, load_config
from .notifier import broadcast


class MonitorState:
    """内存中的盯盘状态"""

    def __init__(self, max_history: int = 200) -> None:
        self.running = False
        self.last_scan: str | None = None
        self.last_scan_count = 0
        self.history: deque[dict[str, Any]] = deque(maxlen=max_history)
        self._last_sent: dict[str, float] = {}  # 去重 key -> 时间戳
        self._lock = threading.RLock()

    def should_send(self, key: str, cooldown: int) -> bool:
        with self._lock:
            now = time.time()
            last = self._last_sent.get(key, 0)
            if now - last < cooldown:
                return False
            self._last_sent[key] = now
            return True

    def add_history(self, item: dict[str, Any]) -> None:
        with self._lock:
            self.history.appendleft(item)

    def get_history(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.history)[:limit]

    def clear_history(self) -> None:
        with self._lock:
            self.history.clear()
            self._last_sent.clear()


state = MonitorState()
_scheduler: BackgroundScheduler | None = None
_scheduler_lock = threading.Lock()


def scan_once(cfg: AppConfig | None = None, push: bool = True) -> dict[str, Any]:
    """执行一次主升浪扫描。返回本次扫描摘要。"""
    cfg = cfg or load_config()
    started = datetime.now()
    try:
        # 默认冷却用 scan 配置；兼容旧配置缺失时回退 24h
        cooldown = getattr(cfg.scan, "cooldown_seconds", 86400)
        # 合并上涨 + 下跌战法开关
        enabled = {**cfg.strategies.as_dict(), **cfg.bear_strategies.as_dict()}
        signals = mainwave.scan_watchlist(
            cfg.watchlist,
            cfg.strategy_rules.model_dump(),
            enabled,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "success": False,
            "error": str(exc),
            "time": started.isoformat(timespec="seconds"),
        }

    pushed = 0
    fresh: list[dict[str, Any]] = []
    if signals:
        # 冷却去重：同一股票 + 同一战法 + 同一阶段。
        # 观察中与放量启动分别提醒，避免观察提示占用冷却导致正式买点被吞。
        fresh = [
            s for s in signals
            if state.should_send(
                f"{s.get('code')}:{s.get('strategy')}:{s.get('stage', '')}", cooldown
            )
        ]

    if push and fresh and cfg.notification:
        trigger_n = sum(1 for s in fresh if s.get("stage") == "trigger")
        watch_n = sum(1 for s in fresh if s.get("stage") == "watch")
        down_n = sum(1 for s in fresh if s.get("direction") == "down")
        title = (
            f"【主升浪提醒】{started.strftime('%H:%M')} "
            f"放量启动 {trigger_n} / 观察中 {watch_n} / 危险 {down_n}"
        )
        content = (
            f"扫描时间：{started.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"标的池：{len(cfg.watchlist)} 只\n"
            f"放量启动 {trigger_n} 条：回调后重新放量且未破位，重点关注\n"
            f"观察中 {watch_n} 条：缩量到位、守住涨停日最低价，等待放量确认\n"
            f"危险信号 {down_n} 条：趋势走坏，注意离场"
        )
        results = broadcast(cfg.notification, title, content, fresh)
        for r in results:
            state.add_history({
                "time": started.isoformat(timespec="seconds"),
                "channel": r["channel_name"],
                "success": r["success"],
                "message": r["message"],
                "signals": fresh[:20],
            })
            if r["success"]:
                pushed += 1
    elif fresh:
        # 未推送（如手动扫描 preview）也记录，便于 UI 查看
        state.add_history({
            "time": started.isoformat(timespec="seconds"),
            "channel": "扫描记录",
            "success": True,
            "message": f"命中 {len(fresh)} 个战法信号（未推送）",
            "signals": fresh[:20],
        })

    summary = {
        "success": True,
        "time": started.isoformat(timespec="seconds"),
        "signal_count": len(signals),
        "fresh_count": len(fresh),
        "pushed_channels": pushed,
        "top_signals": signals[:10],
        "market": ds.market_status(started),
    }
    state.last_scan = summary["time"]
    state.last_scan_count = len(signals)
    return summary


def _interval_seconds(cfg: AppConfig) -> int:
    market = ds.market_status()
    status = market["status"]
    if status == "trading":
        return cfg.scan.trading_interval
    if status == "lunch":
        return cfg.scan.lunch_interval
    return cfg.scan.closed_interval


def _job() -> None:
    cfg = load_config()
    if not cfg.scan.enabled:
        return
    if cfg.scan.only_trading_days and datetime.now().weekday() >= 5:
        return
    scan_once(cfg, push=True)
    with _scheduler_lock:
        if _scheduler is not None:
            _scheduler.reschedule_job(
                "scan_job", trigger=IntervalTrigger(seconds=_interval_seconds(cfg))
            )


def start_monitor() -> dict[str, Any]:
    """启动后台盯盘"""
    global _scheduler
    with _scheduler_lock:
        if _scheduler is not None and _scheduler.running:
            return {"success": True, "message": "盯盘服务已在运行", "interval": _interval_seconds(load_config())}
        cfg = load_config()
        _scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
        _scheduler.add_job(_job, IntervalTrigger(seconds=_interval_seconds(cfg)), id="scan_job",
                           max_instances=1, coalesce=True)
        _scheduler.start()
        state.running = True
        return {"success": True, "message": "盯盘服务已启动", "interval": _interval_seconds(cfg)}


def stop_monitor() -> dict[str, Any]:
    """停止后台盯盘"""
    global _scheduler
    with _scheduler_lock:
        if _scheduler is not None and _scheduler.running:
            _scheduler.shutdown(wait=False)
            _scheduler = None
        state.running = False
        return {"success": True, "message": "盯盘服务已停止"}


def status() -> dict[str, Any]:
    cfg = load_config()
    return {
        "running": bool(_scheduler and _scheduler.running),
        "enabled": cfg.scan.enabled,
        "last_scan": state.last_scan,
        "last_scan_count": state.last_scan_count,
        "interval": _interval_seconds(cfg),
        "market": ds.market_status(),
        "history_count": len(state.history),
        "email_configured": any(
            c.enabled and c.smtp_host and c.mail_to for c in cfg.notification
        ),
    }
