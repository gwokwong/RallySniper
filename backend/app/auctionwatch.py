"""竞价承接扫描（9:26 集合竞价后、开盘前）：标的池个股 + 持仓 + ETF 池的量价分层。

量化依据（2026-11-28 回测，STRATEGY_BOUNDARY.md）：
- 个股 158 万样本：放量承接（量比≥2）日内 +3.1~4.0%（平开/低开/高开1~3 全成立），
  缩量（量比<1）日内 -0.2~-1.0%（缩量高开最毒）；高开>3=兑现区（即使放量 -0.22%）。
- ETF 池矩阵同向：放量高开日内 +1.4~2.2%（胜率 78%）、缩量高开 -0.3~-1.4%；
  但股票型 ETF 为 T+1，日内收益不可实现（黄金等 T+0 品种除外）——用途=当日预期管理。
- 用途定位：**持仓者当日处置指引 + 关注名单**，非竞价买入信号（竞价买入=当日不可卖）。
数据：腾讯行情快照（个股，9:26 量比字段已含竞价成交）与东财 ETF 快照。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from . import datasource as ds, notifications, notifier, scanstore
from .config import load_config

_last: dict[str, Any] | None = None


def _layer(gap: float, vr: float | None) -> tuple[str, str]:
    """量价分层：返回 (标签, 处置提示)。口径=回测矩阵。"""
    vr_s = f"量比{vr:.1f}" if vr is not None else "量比--"
    if gap >= 3:
        return ("兑现区·高开>3", f"{vr_s}——大幅高开日内均-0.2~-0.8%，注意兑现节奏")
    if gap >= 1:
        if vr is not None and vr >= 1.5:
            return ("放量承接·高开", f"{vr_s}——放量高开日内均+3.1%（个股口径），持有为主")
        if vr is not None and vr < 1:
            return ("缩量高开·警惕", f"{vr_s}——缩量高开日内均-0.8~-1%（最毒组合），减仓预期")
        return ("高开·量未确认", f"{vr_s}——量能中性，观察开盘后承接")
    if gap <= -2:
        if vr is not None and vr >= 1.5:
            return ("低开放量·承接", f"{vr_s}——低开放量日内均+1.9%，恐慌承接中")
        return ("低开·缩量", f"{vr_s}——低开缩量日内+0.4%，弱势阴跌观察")
    if vr is not None and vr >= 1.5:
        return ("平开/微幅·放量", f"{vr_s}——放量承接最强组合（日内均+3.2~4.0%），持有为主")
    if vr is not None and vr < 1:
        return ("缩量·平淡", f"{vr_s}——缩量日内-0.2~-0.4%，随波逐流")
    return ("平稳", f"{vr_s}——量价中性")


def auc_scan(push: bool = True) -> dict[str, Any]:
    """竞价承接扫描（9:26）：持仓个股 + 标的池 + ETF 池，三层量价分层。"""
    cfg = load_config()
    eff = ds.effective_trading_date()
    today_raw = str(eff["date"])
    today = today_raw if "-" in today_raw else f"{today_raw[:4]}-{today_raw[4:6]}-{today_raw[6:8]}"
    codes: dict[str, str] = {}
    for c in list(dict.fromkeys([*cfg.watchlist, *cfg.holdings])):
        codes[c.zfill(6)] = "持仓" if c in cfg.holdings else "标的池"
    stocks: list[dict[str, Any]] = []
    if codes:
        try:
            quotes = ds.spot_quote_map(list(codes.keys()))
        except Exception:  # noqa: BLE001
            quotes = {}
        for c, tag in codes.items():
            q = quotes.get(c)
            if not q or not q.get("pre_close") or not q.get("price"):
                continue
            gap = (float(q["price"]) / float(q["pre_close"]) - 1) * 100
            vr = q.get("volume_ratio")
            lab, note = _layer(gap, float(vr) if vr else None)
            stocks.append({"code": c, "name": q.get("name", c), "tag": tag,
                           "price": float(q["price"]), "gap": round(gap, 2),
                           "vr": vr, "layer": lab, "note": note})
    etfs: list[dict[str, Any]] = []
    try:
        spot = {x["code"]: x for x in ds.etf_spot(30)}
    except Exception:  # noqa: BLE001
        spot = {}
    for it in cfg.etf.pool:
        q = spot.get(it.code.zfill(6))
        if not q or not q.get("price") or not q.get("pre_close"):
            continue
        gap = (float(q["price"]) / float(q["pre_close"]) - 1) * 100
        vr = q.get("vr")
        lab, note = _layer(gap, float(vr) if vr else None)
        t0 = "T+0" if it.code.zfill(6).startswith(("518", "511", "513")) else "T+1"
        etfs.append({"code": it.code.zfill(6), "name": it.name or q.get("name", it.code),
                     "price": float(q["price"]), "gap": round(gap, 2), "vr": vr,
                     "layer": lab, "note": note, "rule": t0})
    result = {"success": True, "date": today,
              "time": datetime.now().strftime("%H:%M:%S"), "stocks": stocks, "etfs": etfs}
    global _last
    _last = result
    if push:
        title = f"【竞价承接·{result['time'][:5]}】个股 {len(stocks)} / ETF {len(etfs)}（开盘前处置指引）"
        body = "个股（持仓者当日处置口径）："
        for s in stocks:
            body += (f"\n- {'★' if s['tag'] == '持仓' else ''}{s['name']}({s['code']}) "
                     f"竞价{s['gap']:+.1f}%｜{s['layer']}｜{s['note']}")
        body += "\nETF（T+1 标的日内收益不可实现，本层为当日预期管理；T+0 品种可操作）："
        for s in etfs:
            body += f"\n- {s['name']}({s['code']}·{s['rule']}) 竞价{s['gap']:+.1f}%｜{s['layer']}｜{s['note']}"
        body += "\n\n口径=竞价承接量价矩阵（158万样本，见 STRATEGY_BOUNDARY）；状态指引非买卖指令。"
        try:
            notifications.add(title, body, [], kind="auction")
            if cfg.notification:
                notifier.broadcast(cfg.notification, title, body, [])
        except Exception as exc:  # noqa: BLE001
            print(f"[auctionwatch] 推送失败: {exc}")
    try:
        scanstore.save("auction", result)
    except Exception:  # noqa: BLE001
        pass
    return result


def latest() -> dict[str, Any]:
    if _last:
        return _last
    d = scanstore.load("auction") or {}
    return {"success": bool(d), **d}
