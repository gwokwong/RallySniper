"""扫描结果落盘与跨进程防重锁。

两部分职责：
1. 「上一次结果」持久化：全市场机会 / 昨日涨停跟进每次成功扫描/推送后原子写入
   data/last_scan.json / data/last_yesterday.json（含 saved_at 与 source 来源标注）。
   重启后内存态为空，API 自动回退到落盘快照——页面保留上一次结果，标明
   时间和方式（手动扫描 / 手动推送 / 系统定时）。
2. 跨进程一次性执行令牌：多进程误并存（如 systemd + 手动 nohup 双 uvicorn）时
   两个调度器会在同一时刻各自触发推送造成重复邮件；claim_once 用 O_EXCL 独占
   创建锁文件，只有拿到令牌的进程真正发送。锁文件带时间戳，超时视为残留自动接管。
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data"
LOCK_DIR = DATA / "locks"
_lock = threading.Lock()


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def save(kind: str, payload: dict[str, Any]) -> None:
    """保存「上一次结果」快照（自动附加 saved_at）。kind: scan / yesterday。"""
    payload = {**payload, "saved_at": datetime.now().isoformat(timespec="seconds")}
    with _lock:
        _atomic_write(DATA / f"last_{kind}.json", payload)


def load(kind: str) -> dict[str, Any] | None:
    f = DATA / f"last_{kind}.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def claim_once(key: str, stale_seconds: int = 900) -> bool:
    """跨进程一次性令牌：拿到返回 True（负责执行），否则 False（已有进程在发/已发过）。

    key 形如 yesterday_2026-11-25_10:30，按 数据交易日+任务+时点 唯一。
    锁文件 mtime 超过 stale_seconds 视为上次执行崩溃残留，自动接管重发。
    """
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    f = LOCK_DIR / f"{key}.lock"
    now = time.time()
    with _lock:
        try:
            if f.exists():
                if now - f.stat().st_mtime > stale_seconds:
                    f.unlink(missing_ok=True)  # 残留锁：接管
                else:
                    return False
            with f.open("x") as fh:
                fh.write(datetime.now().isoformat(timespec="seconds"))
            return True
        except OSError:
            return False
