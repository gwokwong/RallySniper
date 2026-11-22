"""站内通知存储与接口。

全市场扫描命中信号后写入站内通知，前端铃铛与通知页读取。
数据持久化到 backend/data/notifications.json，重启不丢失。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
NOTIFY_FILE = DATA_DIR / "notifications.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)

_MAX = 200
_lock = threading.RLock()
_items: list[dict[str, Any]] | None = None


def _load() -> list[dict[str, Any]]:
    global _items
    with _lock:
        if _items is not None:
            return _items
        try:
            if NOTIFY_FILE.exists():
                data = json.loads(NOTIFY_FILE.read_text(encoding="utf-8"))
                _items = data if isinstance(data, list) else []
            else:
                _items = []
        except Exception:  # noqa: BLE001
            _items = []
        return _items


def _persist() -> None:
    try:
        NOTIFY_FILE.write_text(
            json.dumps(_items or [], ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[notifications] 持久化失败: {exc}")


def add(title: str, body: str, signals: list[dict[str, Any]] | None = None,
        kind: str = "market") -> dict[str, Any]:
    """新增一条站内通知。"""
    with _lock:
        items = _load()
        item = {
            "id": f"{int(datetime.now().timestamp() * 1000)}",
            "time": datetime.now().isoformat(timespec="seconds"),
            "title": title,
            "body": body,
            "kind": kind,
            "read": False,
            "signals": (signals or [])[:30],
        }
        items.insert(0, item)
        del items[_MAX:]
        _persist()
        return item


def add_market_signals(push_time: str, signals: list[dict[str, Any]],
                       scan_date_label: str = "") -> dict[str, Any] | None:
    """把一次全市场扫描的命中信号写成一条站内通知。无信号则不写。

    scan_date_label: 行情数据对应的交易日（休市时为最近交易日），写入标题避免误读。
    """
    if not signals:
        return None
    trigger = sum(1 for s in signals if s.get("stage") == "trigger")
    watch = sum(1 for s in signals if s.get("stage") == "watch")
    strong_n = sum(1 for s in signals if s.get("strategy") == "强势续攻")
    pullback_n = sum(1 for s in signals if s.get("strategy") == "涨停回调")
    date_part = f"{scan_date_label} " if scan_date_label else ""
    parts = [f"放量启动 {trigger} / 观察中 {watch}"]
    if strong_n or pullback_n:
        parts.append(f"强势续攻 {strong_n} / 涨停回调 {pullback_n}")
    title = f"[{push_time}] {date_part}全市场主升浪：" + "，".join(parts)
    names = "、".join(
        f"{s.get('name', s.get('code'))}({s.get('code')})"
        for s in signals[:6] if s.get("stage") == "trigger"
    ) or "、".join(f"{s.get('name', s.get('code'))}" for s in signals[:6])
    body = f"命中 {len(signals)} 只，重点关注：{names}"
    return add(title, body, signals, kind="market")


def list_all(limit: int = 100) -> list[dict[str, Any]]:
    with _lock:
        return _load()[:limit]


def unread_count() -> int:
    with _lock:
        return sum(1 for x in _load() if not x.get("read"))


def mark_read(notify_id: str | None = None) -> dict[str, Any]:
    with _lock:
        items = _load()
        changed = False
        for x in items:
            if notify_id is None or x.get("id") == notify_id:
                if not x.get("read"):
                    x["read"] = True
                    changed = True
        if changed:
            _persist()
        return {"success": True, "marked": sum(1 for x in items if x.get("read")), "id": notify_id}


def clear_all() -> dict[str, Any]:
    global _items
    with _lock:
        _items = []
        _persist()
        return {"success": True}


def status() -> dict[str, Any]:
    with _lock:
        items = _load()
        return {"total": len(items), "unread": sum(1 for x in items if not x.get("read"))}
