"""全市场扫描定时任务：交易日下午 14:00 / 14:40 双时点推送。

命中「涨停回调·放量启动 / 观察中」信号后，同时写入站内通知并发送邮件。
按用户要求：两个时点各自推送，即使同一股票信号未变化也不去重。
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import datasource as ds, ignitionwatch, market_scan, notifications, notifier, patternwatch, replay, scanstore, tdxwatch, wzqwatch
from . import sellwatch, yesterday
import json

from .config import load_config

_scheduler: BackgroundScheduler | None = None
_lock = threading.RLock()
YESTERDAY_TIMES = [("10", "30"), ("14", "40")]


def _push_times_cfg() -> list[tuple[str, str]]:
    """全市场推送时点（可配置，默认 14:00/14:40 尾盘）。

    scan.market_push_times 为 HH:MM 列表，支持全天任意交易时点；
    非法项忽略，全部非法时回退默认值。
    """
    try:
        raw = load_config().scan.market_push_times or []
    except Exception:  # noqa: BLE001
        raw = []
    times: list[tuple[str, str]] = []
    for t in raw:
        try:
            h, mnt = str(t).strip().split(":")
            times.append((f"{int(h):02d}", f"{int(mnt):02d}"))
        except (ValueError, AttributeError):
            continue
    return times or [("14", "00"), ("14", "40")]


def _system_email(title: str, body: str) -> None:
    """系统级通知（自检/心跳/异常）：站内 + 邮件。"""
    notifications.add(title, body, [], kind="system")
    cfg = load_config()
    if cfg.notification:
        notifier.broadcast(cfg.notification, title, body, [])


def run_daily_check() -> dict[str, Any]:
    """每日系统自检：逐项检查并发邮件（全部正常/异常标题区分）。"""
    cfg = load_config()
    checks: list[tuple[str, bool, str]] = []
    try:
        h = ds.health()
        degraded = bool(h.get("degraded"))
        checks.append(("数据源", not degraded,
                       f"akshare {h.get('akshare_version', '')}{'，降级中' if degraded else '，接口正常'}"))
    except Exception as exc:  # noqa: BLE001
        checks.append(("数据源", False, str(exc)[:80]))
    try:
        cs = replay.cache_status()
        checks.append(("K线缓存(回放)", cs["codes"] > 100,
                       f"{cs['codes']} 只，覆盖至 {cs['end_date']}"))
    except Exception as exc:  # noqa: BLE001
        checks.append(("K线缓存(回放)", False, str(exc)[:80]))
    # 定时调度状态走本地 HTTP 自查：无论自检从服务进程还是独立进程触发，看到的都是服务真实状态
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:8000/api/market/scan-status", timeout=8) as resp:
            sch = json.loads(resp.read().decode()).get("data", {})
        n_jobs = len(sch.get("jobs", []))
        checks.append(("定时调度", bool(sch.get("running")), f"{'运行中' if sch.get('running') else '未运行'}，{n_jobs} 个任务"))
    except Exception as exc:  # noqa: BLE001
        checks.append(("定时调度", False, f"查询失败：{str(exc)[:60]}"))
    ch_ok = any(c.enabled and c.smtp_host and c.mail_to for c in cfg.notification)
    checks.append(("邮件渠道", ch_ok, "已配置" if ch_ok else "未配置"))
    mf = cfg.market_filter
    boards = "/".join(n for n, on in (("主板", mf.main_board), ("科创", mf.kcb), ("创业", mf.cyb)) if on) or "无"
    checks.append(("筛选口径", True, f"{boards}，价格 {mf.min_price:g}-{mf.max_price:g} 元"))
    checks.append(("持仓", True, f"{len(cfg.holdings)} 只（0 只时卖出风控无对象）"))
    ok_all = all(ok for _, ok, _ in checks)
    title = "【系统自检】全部正常" if ok_all else "【系统自检·异常】"
    body = "\n".join(f"- {n}: {'正常' if ok else '异常'}（{detail}）" for n, ok, detail in checks)
    _system_email(title, body)
    return {"success": True, "ok_all": ok_all,
            "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in checks]}


def _daily_check_job() -> None:
    try:
        run_daily_check()
    except Exception as exc:  # noqa: BLE001
        print(f"[scheduler] 每日自检失败: {exc}")


def _pattern_job() -> None:
    """盘中形态监控：反包进行中/N日新高突破，触发即提醒。"""
    try:
        r = patternwatch.poll()
        if r.get("new"):
            print(f"[scheduler] 形态监控: 新增{len(r['new'])}条")
    except Exception as exc:  # noqa: BLE001
        print(f"[scheduler] 形态监控失败: {exc}")


def _pattern_fast_job() -> None:
    """反包候选快车道（默认45秒级）：只盯候选池，爆发即刻提醒。"""
    try:
        if ds.market_status()["status"] not in ("trading",):
            return
        r = patternwatch.poll_fast()
        if r.get("new"):
            print(f"[scheduler] 快车道: 新增{len(r['new'])}条")
    except Exception as exc:  # noqa: BLE001
        print(f"[scheduler] 快车道失败: {exc}")


def _lunch_review_job() -> None:
    """午盘复盘（11:35）：上午板块红绿榜 + 点火读数 + 候选晨盘异动 + 持仓快照 + 午后展望。"""
    try:
        if ds.market_status()["status"] not in ("trading", "lunch"):
            return
        cfg2 = load_config()
        eff = ds.effective_trading_date()
        now_hm = datetime.now().strftime("%H:%M")
        lines = [f"数据交易日：{eff.get('label') or eff.get('date')}｜复盘时点 {now_hm}（上午收盘）"]
        # ① 点火读数
        try:
            ign = ds.ignition_premium(eff["date"])
            med = ign.get("median")
            th = float(cfg2.market_regime.ignition_threshold)
            lines.append(f"① 点火读数：{ign.get('prev_date', '')} 涨停池 {ign.get('pool_n', '?')} 只，"
                         f"今日中位溢价 {med if med is None else format(med, '+.2f')}%"
                         f"（阈值 {th:g}）→ {'点火进行中' if ign.get('median') is not None and med >= th else '未点火'}")
        except Exception as _e:  # noqa: BLE001
            lines.append(f"① 点火读数暂不可用（{str(_e)[:40]}）")
        # ② 板块红绿榜
        try:
            bdf = ds.industry_boards()
            if bdf is not None and not bdf.empty and "涨跌幅" in bdf.columns:
                bb = bdf[["板块名称", "涨跌幅"]].copy()
                bb["涨跌幅"] = bb["涨跌幅"].astype(float)
                top = bb.sort_values("涨跌幅", ascending=False).head(3)
                bot = bb.sort_values("涨跌幅").head(3)
                lines.append("② 板块红榜：" + "、".join(f"{r['板块名称']} {r['涨跌幅']:+.1f}%" for _, r in top.iterrows()))
                lines.append("   板块绿榜：" + "、".join(f"{r['板块名称']} {r['涨跌幅']:+.1f}%" for _, r in bot.iterrows()))
        except Exception:
            lines.append("② 板块行情暂不可用")
        # ③ 晨盘候选异动
        try:
            items = [x for x in (patternwatch.state.last_new or [])]
            if items:
                lines.append(f"③ 上午候选/形态异动 {len(items)} 条：" +
                             "、".join(f"{x.get('name')}({x.get('pattern')})" for x in items[:5]) +
                             ("…" if len(items) > 5 else ""))
            else:
                lines.append("③ 上午候选/形态异动：无")
        except Exception:
            pass
        # ④ 持仓快照（含表格行）
        rows = []
        holdings = [h for h in cfg2.holdings if len(h) == 6 and h.isdigit()]
        if holdings:
            try:
                quotes = ds.spot_quote_map(holdings)
                for h in holdings:
                    q = quotes.get(h) or {}
                    if not q.get("price"):
                        continue
                    nm = str(q.get("name") or h)
                    pct = float(q.get("change_pct") or 0)
                    lines.append(f"④ 持仓 {nm}({h}) 现价{q.get('price')} {pct:+.2f}%")
                    rows.append({"strategy": "持仓", "stage": "watch", "stage_label": "持仓",
                                 "code": h, "name": nm, "price": q.get("price"),
                                 "change_pct": pct, "direction": "down" if pct < 0 else "up",
                                 "reasons": ["午盘快照"]})
            except Exception:
                lines.append("④ 持仓行情暂不可用")
        # ⑤ 午后展望
        try:
            ign = ds.ignition_premium(eff["date"])
            med = ign.get("median")
            th = float(cfg2.market_regime.ignition_threshold)
            if med is not None and med >= th:
                lines.append("⑤ 午后展望：上午已达标点火——14:00/14:40 推送照常，注意炸板率与板块分化。")
            else:
                lines.append("⑤ 午后展望：上午未达标——若午后中位溢价升破阈值，点火监控约1分钟内会提醒。")
        except Exception:
            pass
        title = f"【午盘复盘】{now_hm} 上午小结（候选/持仓/板块/点火）"
        ghost_rows = []
        body = "\n".join(lines)
        notifications.add(title, body, rows, kind="pattern")
        if cfg2.notification:
            notifier.broadcast(cfg2.notification, title, body, rows)
        print(f"[scheduler] 午盘复盘已发送（持仓行 {len(rows)}）")
    except Exception as exc:  # noqa: BLE001
        print(f"[scheduler] 午盘复盘失败: {exc}")


def _tdx_job() -> None:
    """通达信指标日K预警（收盘后）：等K线刷新完成后扫描当日信号，合并一封邮件。"""
    try:
        if ds.market_status()["status"] == "closed":
            return
        import time as _time
        for _ in range(180):  # 等 15:10 的缓存刷新完成（最多15分钟）
            prog = getattr(replay, "_refresh_progress", {}) or {}
            if prog.get("finished"):
                break
            _time.sleep(5)
        r = tdxwatch.scan_today()
        items = r.get("items", [])
        if not items:
            print("[scheduler] 通达信指标: 今日无信号")
            return
        cfg2 = load_config()
        holdings = set(h for h in cfg2.holdings if len(h) == 6 and h.isdigit())
        rows, lines = [], []
        dips = [x for x in items if x["signal"] == "鬼手抄底"]
        jians = [x for x in items if x["signal"] == "鬼手减仓"]
        others = [x for x in items if x["signal"] not in ("鬼手抄底", "鬼手减仓")]
        if dips:
            lines.append(f"【⭐鬼手抄底·买进信号】{len(dips)} 只（回测7-11月485次：次日开盘买胜率71.8%/均+5.82%）")
            for x in dips[:15]:
                star = "★持仓触发 " if x["code"] in holdings else ""
                lines.append(f"- {star}{x['name']}({x['code']}) 现价{x['price']} {x['pct']:+.1f}%")
                rows.append({"strategy": "鬼手抄底", "stage": "trigger", "stage_label": "⭐买进信号(回测72%胜率)",
                             "code": x["code"], "name": x["name"], "price": x["price"],
                             "change_pct": x["pct"], "reasons": ["深跌放量单日大涨8%+，仍处长期均线下方——超跌反弹结构"]})
        if jians:
            hi = [x for x in jians if x["code"] in holdings]
            lines.append(f"【⚠️鬼手减仓·卖出预警】{len(jians)} 只触发（信号后5日平均 -2.50%）"
                         + (f"，其中持仓触发 {len(hi)} 只：{'、'.join(x['name'] for x in hi[:8])}" if hi else ""))
            for x in jians[:10]:
                star = "★持仓触发 " if x["code"] in holdings else ""
                lines.append(f"- {star}{x['name']}({x['code']}) 现价{x['price']} {x['pct']:+.1f}%")
                rows.append({"strategy": "鬼手减仓", "stage": "watch", "stage_label": "⚠️减仓预警",
                             "code": x["code"], "name": x["name"], "price": x["price"],
                             "change_pct": x["pct"], "direction": "down",
                             "reasons": ["RSI6高位跌破82——信号后5日平均-2.50%，持仓注意减仓"]})
        if others:
            lines.append(f"【仅供参考⚠️回测无优势】鬼手买进A/B、三线超卖首现等 {len(others)} 条（不构成建议）")
            for x in others[:8]:
                lines.append(f"- {x['name']}({x['code']}) {x['signal']} 现价{x['price']}")
        title = f"【盘尾指标预警】鬼手抄底 {len(dips)} 只 / 减仓预警 {len(jians)} 只"
        body = "\n".join(lines)
        notifications.add(title, body, rows, kind="pattern")
        if cfg2.notification:
            notifier.broadcast(cfg2.notification, title, body, rows)
        print(f"[scheduler] 通达信指标预警: 抄底{len(dips)} 减仓{len(jians)} 其他{len(others)}")
    except Exception as exc:  # noqa: BLE001
        print(f"[scheduler] 通达信指标预警失败: {exc}")


def _early_review_job() -> None:
    """早盘快评（9:31）：集合竞价结束后立即发送——竞价版点火读数 + 候选高开排行 + 激进版组合。

    定位：激进前置参考。正式确认信号仍以点火确认+14:00/14:40尾盘推送为准。
    """
    try:
        if ds.market_status()["status"] == "closed":
            return
        cfg2 = load_config()
        eff = ds.effective_trading_date()
        today = str(eff["date"])
        if "-" not in today:
            today = f"{today[:4]}-{today[4:6]}-{today[6:8]}"
        dts = replay.trading_dates()
        prev_list = [x for x in dts if x < today]
        prev = prev_list[-1] if prev_list else None
        if prev is None:
            return
        snap = ds.spot_quote_map()
        if not snap:
            print("[scheduler] 早盘快评: 行情快照不可用")
            return
        now_hm = datetime.now().strftime("%H:%M")
        lines = [f"集合竞价快评 {now_hm}（数据=开盘价口径）"]
        # ① 竞价版点火读数：昨日涨停池今日开盘涨幅中位数
        try:
            pool = ds.limit_up_pool_detail(prev.replace("-", ""), ttl=300)
            gaps = []
            for p in pool or []:
                q = snap.get(p.get("code", ""))
                if q and q.get("change_pct") is not None:
                    gaps.append(float(q["change_pct"]))
            if len(gaps) >= 3:
                med = float(np.median(gaps)) if False else float(sorted(gaps)[len(gaps) // 2])
                hi = sum(1 for g in gaps if g >= 3)
                lo = sum(1 for g in gaps if g <= -5)
                lines.append(f"① 竞价点火读数：昨日涨停 {len(gaps)} 只开盘溢价中位 {med:+.2f}%"
                             f"（高开≥3% {hi} 只 / 低开≤-5% {lo} 只）→ "
                             f"{'开盘偏强，点火概率高' if med >= 1.5 else ('中性' if med >= -1 else '开盘偏弱，警惕退潮')}")
            else:
                lines.append("① 竞价点火读数：样本不足")
        except Exception as _e:  # noqa: BLE001
            lines.append(f"① 竞价点火读数暂不可用（{str(_e)[:40]}）")
        # ② 候选高开排行（反包候选 + 鬼手抄底5日内）
        r = patternwatch.war_map(top_n=0)
        ghost_rows = []
        try:
            from . import tdxwatch as _tw
            lx = _tw.latest_signals(5)
            nm_map = ds.stock_code_names()
            for c, v in (lx.get("signals") or {}).items():
                hit = next((x for x in v if x.get("signal") == "鬼手抄底"), None)
                if hit:
                    ghost_rows.append({"code": c, "name": nm_map.get(c, c), "days_ago": hit.get("days_ago", 0)})
        except Exception:  # noqa: BLE001
            pass
        cand_rows = []
        for c in r.get("candidates", []):
            q = snap.get(c["code"]) or {}
            if q.get("change_pct") is None:
                continue
            cand_rows.append({**c, "gap": float(q["change_pct"]), "now_price": q.get("price")})
        cand_by_code = {x["code"]: x for x in cand_rows}
        for g in ghost_rows:
            base = cand_by_code.get(g["code"])
            q = snap.get(g["code"]) or {}
            if q.get("change_pct") is None:
                continue
            if base is None:
                base = {"code": g["code"], "name": g["name"], "industry": "--",
                        "life_line": 0, "wash_date": "", "prev_close": q.get("price")}
            cand_rows.append({**base, "gap": float(q["change_pct"]),
                              "now_price": q.get("price"), "ghost": True,
                              "days_ago": g["days_ago"]})
        # 分层：承接区(高开2~6%) / 追高危险(≥7%) / 平开观察(0~2%) / 弱势(<0)
        zone_hot, zone_good, zone_flat, zone_weak = [], [], [], []
        for x in cand_rows:
            g = x["gap"]
            if g >= 7: zone_hot.append(x)
            elif g >= 2: zone_good.append(x)
            elif g >= 0: zone_flat.append(x)
            else: zone_weak.append(x)
        zone_good.sort(key=lambda x: -x["gap"]); zone_hot.sort(key=lambda x: -x["gap"])
        zone_flat.sort(key=lambda x: -x["gap"]); zone_weak.sort(key=lambda x: x["gap"])
        lines.append(f"② 候选高开排行：承接区(高开2~6%) {len(zone_good)} 只｜追高危险(≥7%) {len(zone_hot)} 只｜"
                     f"平开 {len(zone_flat)} 只｜弱势低开 {len(zone_weak)} 只")
        # ③ 激进版组合（承接区内选，行业分散，快照现价折算手数）
        cap_amt = float(getattr(cfg2, "available_capital", 0) or 0)
        alloc = None
        if cap_amt > 0 and zone_good:
            try:
                from . import market_scan as _ms
                pseudo = [{"code": x["code"], "name": x["name"], "price": x["now_price"] or x["gap"],
                           "stage": "trigger", "confidence": "normal",
                           "confidence_label": "激进参考", "profit_pos": None,
                           "rev_yoy": None, "strength": x["gap"],
                           "industry": x.get("industry", "--")} for x in zone_good[:12]]
                alloc = _ms.recommend_allocation(
                    pseudo, cap_amt,
                    max_count=int(getattr(cfg2, "alloc_max_count", 0) or 0),
                    commission_rate=float(getattr(cfg2, "commission_rate", 0.00025) or 0.00025),
                    commission_min=float(getattr(cfg2, "commission_min", 5.0) or 0))
            except Exception:  # noqa: BLE001
                alloc = None
        rows = []
        for label, items in (("承接区", zone_good), ("追高危险", zone_hot),
                             ("平开观察", zone_flat), ("弱势低开", zone_weak)):
            for x in items[:8]:
                rows.append({"strategy": "反包候选", "stage": "watch",
                             "stage_label": label, "code": x["code"], "name": x["name"],
                             "price": x.get("now_price") or "", "change_pct": x["gap"],
                             "reasons": [f"生命线{x.get('life_line', 0) or '--'}",
                                         f"{'鬼手抄底·%d日前' % x['days_ago'] if x.get('ghost') else '洗盘日' + str(x.get('wash_date', ''))[5:]}"]})
        if alloc and alloc.get("alloc"):
            lines.append(f"③ 激进版组合（承接区候选，现价折算手数；⚠️信号未确认，尾盘推送为准）：")
            for a in alloc["alloc"]:
                conf = a.get("confidence_label") or ""
                lines.append(f"- ★ {a['name']}({a['code']}) 开盘价{a['price']}"
                             f"｜{a['lots']}手={a['shares']}股≈{a['amount']:g}元"
                             + (f"｜{conf}" if conf else ""))
            lines.append(f"合计投入约 {alloc['invested']:g} 元，留存 {alloc['residual']:g} 元，"
                         f"双边佣金约 {alloc['fee_est']:g} 元。生命线跌破即放弃。")
        elif cap_amt > 0:
            lines.append(f"③ 可用 {cap_amt:g} 元：承接区内无可组合候选（高开过度或弱势），激进组合今日不给出。")
        title = f"【早盘快评·集合竞价】{now_hm} 开盘点火读数 + 候选高开排行"
        body = "\n".join(lines)
        notifications.add(title, body, rows, kind="pattern")
        if cfg2.notification:
            notifier.broadcast(cfg2.notification, title, body, rows)
        print(f"[scheduler] 早盘快评已发送: 承接区{len(zone_good)} 追高{len(zone_hot)} 弱势{len(zone_weak)}")
    except Exception as exc:  # noqa: BLE001
        print(f"[scheduler] 早盘快评失败: {exc}")


def _war_map_job() -> None:
    """盘前作战地图：候选名单+生命线+昨日情绪背景（休市日跳过）。"""
    try:
        if ds.market_status()["status"] == "closed":
            return
        r = patternwatch.war_map()
        if not r.get("success"):
            print(f"[scheduler] 作战地图失败: {r.get('error')}")
            return
        s = r.get("sentiment") or {}
        ghost_rows = []
        lines = [f"昨日（{r['prev_date']}）市场情绪：炸板率 {s.get('bomb_rate', '--')}%"
                 f"（闸门阈值 {s.get('bomb_max'):g}%）、市场最高板 {s.get('max_streak', '--')} 板"]
        br = s.get("bomb_rate")
        if br is not None and br > float(s.get("bomb_max") or 45):
            lines.append("⚠️ 昨日炸板率过高：今日即使点火，开仓闸门也会拦截——今日以观察为主，降低预期。")
        else:
            lines.append("昨日分歧可控：若盘中点火确认，按纪律执行（信号以推送为准）。")
        lines.append(f"\n【反包候选 {r['n_candidates']} 只·展示前{len(r['candidates'])}只】"
                     f"（生命线=洗盘日收盘，跌破即形态失败）")
        for c in r.get("candidates", []):
            lines.append(f"- {c['name']}({c['code']}) {c['industry']}｜昨收{c['prev_close']}"
                         f"｜生命线{c['life_line']}（洗盘日{c['wash_date'][5:]}）")
        # 鬼手抄底·5日内信号股（反弹延续关注；快车道45秒会盯二次启动）
        try:
            from . import tdxwatch as _tw
            lx = _tw.latest_signals(5)
            nm_map = ds.stock_code_names()
            dip = []
            for c, v in (lx.get("signals") or {}).items():
                hit = next((x for x in v if x.get("signal") == "鬼手抄底"), None)
                if hit:
                    dip.append({"code": c, "name": nm_map.get(c, c), "days_ago": hit.get("days_ago", 0)})
            dip.sort(key=lambda x: x["days_ago"])
            if dip:
                lines.append(f"\n【鬼手抄底·5日内信号 {len(dip)} 只】（反弹延续关注；二次放量上攻则升级，快车道会提醒）")
                for x in dip[:10]:
                    lines.append(f"- {x['name']}({x['code']}) {x['days_ago']}日前信号")
                    ghost_rows.append({"strategy": "鬼手抄底", "stage": "watch",
                                       "stage_label": "抄底信号", "code": x["code"],
                                       "name": x["name"], "price": "", "change_pct": 0,
                                       "reasons": [f"{x['days_ago']}日前触发，5日反弹窗口有效"]})
        except Exception as _e:  # noqa: BLE001
            lines.append(f"\n（鬼手指标列表暂不可用：{str(_e)[:40]}）")
        lines.append("\n提醒：候选≠买入信号；候选爆发时快车道45秒内提醒，"
                     "确认以点火确认+尾盘推送为准。")
        title = f"【盘前作战地图】{r['prev_date']} 情绪背景 + {r['n_candidates']} 只反包候选"
        body = "\n".join(lines)
        rows = [{"strategy": "反包候选", "stage": "watch", "stage_label": "候选",
                 "code": c["code"], "name": c["name"], "price": c["prev_close"],
                 "change_pct": 0,
                 "reasons": [f"生命线{c['life_line']}（洗盘日{c['wash_date'][5:]}）", f"行业{c['industry']}"]}
                for c in r.get("candidates", [])]
        rows.extend(ghost_rows)
        notifications.add(title, body, rows, kind="pattern")
        cfg2 = load_config()
        if cfg2.notification:
            notifier.broadcast(cfg2.notification, title, body, rows)
        print(f"[scheduler] 作战地图已发送: {r['n_candidates']} 只候选")
    except Exception as exc:  # noqa: BLE001
        print(f"[scheduler] 作战地图失败: {exc}")

def _wzq_job() -> None:
    """弱转强观察池：收盘后筛巨量炸板名单并推送（休市日跳过）。"""
    try:
        if ds.market_status()["status"] not in ("trading", "after_hours"):
            return
        r = wzqwatch.run_watch(push=True)
        print(f"[scheduler] 弱转强观察池: {r.get('total', '?')} 只")
    except Exception as exc:  # noqa: BLE001
        print(f"[scheduler] 弱转强观察池失败: {exc}")


def _cache_refresh_job() -> None:
    """收盘后刷新全市场K线缓存（休市日跳过）。"""
    try:
        if ds.market_status()["status"] == "closed":
            return
        r = replay.refresh()
        print(f"[scheduler] K线缓存刷新: {r.get('message')}")
        # 预热市场情绪统计（炸板率/最高板）：次日 14:00 推送的炸板闸门首查
        # 不用现场加载全市场K线（进程重启后冷启动可达分钟级）。refresh 为异步
        # 线程，有界轮询等它跑完再预热（最多 15 分钟，超时则放弃由首查自算）。
        import time as _time
        for _ in range(180):
            prog = getattr(replay, "_refresh_progress", {}) or {}
            if prog.get("finished"):
                break
            _time.sleep(5)
        st = replay.market_sentiment()
        print(f"[scheduler] 情绪统计预热: {len(st)} 个交易日")
        # 预热通达信指标（鬼手抄底5日窗口）：次日快车道/作战地图首查不冷启动
        lx = tdxwatch.latest_signals(5)
        print(f"[scheduler] 通达信指标预热: {len(lx.get('signals', {}))} 只")
    except Exception as exc:  # noqa: BLE001
        print(f"[scheduler] K线缓存刷新失败: {exc}")


def run_market_push(push_time: str | None = None, source: str = "系统定时") -> dict[str, Any]:
    """执行一次全市场扫描 + 站内通知 + 邮件推送（成功后落盘为「上一次全市场机会」）。"""
    result = market_scan.scan_market(push_time=push_time)
    if not result.get("success"):
        return result
    market_scan.persist_last_scan(result, source)

    signals = result.get("signals", [])
    label = push_time or datetime.now().strftime("%H:%M")
    scan_label = result.get("scan_date_label") or ""
    closed_note = "（休市，取最近交易日收盘数据）" if result.get("is_closed") else ""
    # 1) 站内通知（无信号不写）
    notice = notifications.add_market_signals(label, signals, scan_label)

    # 2) 邮件推送
    pushed = 0
    cfg = load_config()
    if signals and cfg.notification:
        trigger_n = result.get("trigger_count", 0)
        watch_n = result.get("watch_count", 0)
        strong_n = sum(1 for s in signals if s.get("strategy") == "强势续攻")
        pullback_n = sum(1 for s in signals if s.get("strategy") == "涨停回调")
        title = f"【全市场主升浪】{scan_label} {label} 放量启动 {trigger_n} / 观察中 {watch_n}"
        content = (
            f"推送时点：{datetime.now().strftime('%Y-%m-%d ' + label)}\n"
            f"数据交易日：{scan_label}{closed_note}\n"
            f"候选股票：{result.get('candidates', 0)} 只（截止该交易日的近20日涨停池）\n"
            f"放量启动 {trigger_n} 条 / 观察中 {watch_n} 条\n"
            f"其中 强势续攻 {strong_n} 条（缩量整理或不回头续板后放量）、"
            f"涨停回调 {pullback_n} 条（缩量洗盘守住生命线）\n"
            f"表格含量比、换手率、主力净流入，红涨绿跌。"
        )
        ign = result.get("ignition") or {}
        if ign.get("today_zt") is not None:
            tier_label = {"full": "全仓档（广度普涨，信号胜率回测82.7%）",
                          "probe": "试错档（建议1/4仓位，广度不足胜率回测19.8%）"}.get(
                ign.get("size_tier"), "档位未知")
            content += (
                f"\n【广度档位】今日涨停 {ign['today_zt']} 家（阈值 {ign.get('tier_th', 100)}）→ {tier_label}；"
                f"试错档请按纪律控制仓位。"
            )
            # 置信档位汇总（2026-11-25 因子研究：板块效应×情绪高度 → 高置信档回测86.6%）
            if signals:
                n_high = sum(1 for s in signals if s.get("confidence") == "high")
                n_norm = sum(1 for s in signals if s.get("confidence") == "normal")
                n_probe = sum(1 for s in signals if s.get("confidence") == "probe")
                content += (
                    f"\n【置信档位】高置信 {n_high} 只（同板块涨停≥4∧市场高度≥7，回测86.6%，建议3x）、"
                    f"普通 {n_norm} 只（建议1x）"
                    + (f"、试错 {n_probe} 只（建议0.5x）" if n_probe else "")
                    + "；档位仅为历史回测分层，非收益承诺。"
                )
            # 资金分配建议（用户自管仓位，系统不预留）：按 available_capital 给具体组合
            cap_amt = float(getattr(cfg, "available_capital", 0) or 0)
            if cap_amt > 0:
                try:
                    al = market_scan.recommend_allocation(
                        signals, cap_amt,
                        max_count=int(getattr(cfg, "alloc_max_count", 0) or 0),
                        commission_rate=float(getattr(cfg, "commission_rate", 0.00025) or 0.00025),
                        commission_min=float(getattr(cfg, "commission_min", 5.0) or 0))
                    if al.get("alloc"):
                        content += (f"\n【资金分配建议】可用 {al['capital']:g} 元 → 建议 {len(al['alloc'])} 只"
                                    f"（目标每只约 {al['capital'] / max(1, al['n_target']):g} 元，行业分散）")
                        for a in al["alloc"]:
                            conf = a.get("confidence_label") or ""
                            content += (f"\n- ★ {a['name']}({a['code']}) 现价{a['price']}"
                                        f"｜{a['lots']}手={a['shares']}股≈{a['amount']:g}元"
                                        + (f"｜{conf}" if conf else ""))
                        content += (f"\n合计投入约 {al['invested']:g} 元，留存现金约 {al['residual']:g} 元，"
                                    f"双边佣金约 {al['fee_est']:g} 元（按你的佣金参数估算）。"
                                    + (f"（{al['note']}）" if al.get("note") else "") +
                                    "\n仅为按最小交易单位的参考开局，仓位由你自行控制。")
                    else:
                        content += (f"\n【资金分配建议】可用 {cap_amt:g} 元：今日信号价格超出可买范围"
                                    f"（单只最低需 100股×现价），无法按 {al['n_target'] if isinstance(al, dict) else 1} 只组合建仓。")
                except Exception:  # noqa: BLE001
                    pass
        # 主推精选（结构化分散参考，非alpha承诺）：盈利优先→行业分散→取前3只。
        # 数据背景：点火日内按任何单一维度精选（低价/营收增速/强度）均不优于等权，
        # 此段只为资金有限时提供 2-4 只的分散结构；档位（全仓/试错）才是已验证的分层。
        trig_all = [s for s in signals if s.get("stage") == "trigger"]
        if trig_all:
            picks, seen_ind = [], set()
            # 高置信优先入精选（回测86.6%），其余按盈利优先+行业分散
            for s in sorted(trig_all, key=lambda x: (
                    x.get("confidence") != "high",
                    not x.get("profit_pos"), -(x.get("rev_yoy") if x.get("rev_yoy") is not None else -999),
                    -x.get("strength", 0))):
                ind = s.get("industry") or s.get("code", "")
                if ind in seen_ind:
                    continue
                seen_ind.add(ind)
                picks.append(s)
                if len(picks) >= 3:
                    break
            if picks:
                content += "\n【主推精选·结构化分散参考】（高置信优先+盈利+行业分散；非买入承诺，生命线=涨停日最低价）"
                for s in picks:
                    ry = s.get("rev_yoy")
                    fund = f"营收{'--' if ry is None else format(ry, '+.0f') + '%'}"
                    pp = "盈利" if s.get("profit_pos") else ("未知" if s.get("profit_pos") is None else "亏损")
                    conf = s.get("confidence_label") or ""
                    content += (f"\n- ★ {s.get('name', '')}({s.get('code', '')}) 现价{s.get('price', '')}"
                                f"｜{fund}｜{pp}｜{s.get('industry', '--')}"
                                + (f"｜{conf}" if conf else ""))

        # 逐票命中原因（战法+指标级理由+基本面），让邮件正文自带"为什么买"
        trig = [s for s in signals if s.get("stage") == "trigger"][:10]
        if trig:
            content += "\n【买入理由明细】"
            for s in trig:
                fund = ""
                ry = s.get("rev_yoy")
                pp = s.get("profit_pos")
                if ry is not None:
                    fund += f"｜营收同比{ry:+.0f}%"
                if pp is not None:
                    fund += f"｜{'盈利' if pp else '亏损'}"
                if s.get("industry"):
                    fund += f"｜{s['industry']}"
                conf = s.get("confidence_label") or ""
                content += (f"\n- {s.get('name', '')}({s.get('code', '')})【{s.get('strategy', '')}·"
                            f"{s.get('stage_label', '放量启动')}】现价{s.get('price', '')}{fund}"
                            + (f"｜{conf}" if conf else "")
                            + f"：{'；'.join(s.get('reasons', []))}")
        if ds.cache.is_degraded("fund_flow_map"):
            content += "\n（资金流数据源降级中，净流入可能显示为 --）"
        results = notifier.broadcast(cfg.notification, title, content, signals)
        pushed = sum(1 for r in results if r["success"])

    return {
        **result,
        "notice_id": notice["id"] if notice else None,
        "pushed_channels": pushed,
        "push_time": label,
    }


def _mail_rows(signals: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    """把不同模块的记录转成邮件表格通用结构。"""
    rows: list[dict[str, Any]] = []
    for s in signals:
        if kind == "yesterday":
            rows.append(s)
            continue
        rows.append({
            "code": s.get("code", ""),
            "name": s.get("name", ""),
            "price": s.get("price", "--"),
            "change_pct": s.get("change_pct", 0),
            "volume_ratio": s.get("volume_ratio"),
            "turnover": s.get("turnover"),
            "net_inflow": s.get("net_inflow"),
            "direction": "down",
            "stage_label": f"{s.get('level', '')}·{s.get('strategy', '')}",
            "reasons": s.get("reasons", []),
        })
    return rows


def run_yesterday_push(push_time: str, source: str = "系统定时") -> dict[str, Any]:
    """昨日涨停跟进简报：扫描 + 站内通知 + 邮件（成功后落盘为「上一次昨日涨停」）。"""
    result = yesterday.scan_yesterday(source=source)
    if not result.get("success"):
        return result
    title, body, mail_rows = yesterday.brief_lines()
    notices = notifications.add(title, f"[{push_time}] {body}", mail_rows, kind="yesterday")
    cfg = load_config()
    pushed = 0
    if cfg.notification:
        content = f"推送时点：{push_time}\n{body}"
        results = notifier.broadcast(cfg.notification, f"[{push_time}] {title}", content, mail_rows)
        pushed = sum(1 for r in results if r["success"])
    return {**result, "notice_id": notices["id"], "pushed_channels": pushed, "push_time": push_time}


def run_sell_alert(mode: str) -> dict[str, Any]:
    """持仓卖出提醒：盘中预警 / 尾盘确认。

    用户要求卖出信号「至少通知 2 次」：尾盘确认命中后按 sell.notify_times
    （默认 2）连续发送多提醒（第1次完整理由 + 后续纪律复核），盘中预警为提前提示另算。
    """
    result = sellwatch.scan(mode)
    signals = result.get("signals", [])
    if not signals:
        return result
    cfg = load_config()
    times = max(1, int(cfg.sell.notify_times)) if mode == "confirm" else 1
    names = "、".join(f"{s['name']}({s['code']})" for s in signals[:10])
    pushed = 0
    titles: list[str] = []
    for i in range(times):
        if mode == "confirm" and times > 1:
            label = "尾盘确认" if i == 0 else f"尾盘确认·第{i + 1}次提醒"
        else:
            label = "盘中预警" if mode == "intraday" else "尾盘确认"
        title = f"【持仓卖出{label}】{len(signals)} 条风险信号"
        if i == 0:
            body = "\n".join(
                f"{s['name']}({s['code']}) {s['strategy']}：{' + '.join(s['reasons'])}"
                for s in signals[:10]
            )
        else:
            body = (f"再次提醒：{names} 已触发卖出确认，请按纪律执行，避免因遗漏造成损失。\n"
                    f"（第 {i + 1}/{times} 次提醒）")
        notifications.add(title, body, _mail_rows(signals, "sell"), kind="sell")
        titles.append(title)
        if cfg.notification:
            results = notifier.broadcast(cfg.notification, title, body, _mail_rows(signals, "sell"))
            pushed += sum(1 for r in results if r["success"])
    return {**result, "pushed_channels": pushed, "title": titles[0],
            "notify_times": times, "notify_titles": titles}


def _job(push_time: str, heartbeat: bool = False) -> None:
    """heartbeat=True（末次推送时点）时：无信号也发系统正常邮件，扫描抛错发异常邮件。"""
    try:
        # 休市日不触发（cron 每日都会到点）：周五信号周五已推送，周末/节假日
        # 若无闸门会拿最近交易日数据重复推送（2026-11-21 审计确认的缺口）
        if ds.market_status()["status"] == "closed":
            return
        # 跨进程防重：双进程误并存（如 systemd + 手动 nohup）时两个调度器会在
        # 同一时点各自推送造成重复邮件，只有抢到令牌的进程真正发送
        tdate = ds.effective_trading_date()["date"]
        if not scanstore.claim_once(f"market_{tdate}_{push_time}"):
            print(f"[scheduler] {push_time} 全市场推送已有进程执行（防重跳过）")
            return
        result = run_market_push(push_time)
        if heartbeat and not result.get("signals"):
            if result.get("success"):
                ign = result.get("ignition") or {}
                body = (f"数据交易日：{result.get('scan_date_label') or '--'}\n"
                        f"候选：{result.get('candidates', 0)} 只；命中：0 只\n"
                        f"闸门：{ign.get('median', '--')}"
                        f"（阈值 {ign.get('threshold', '--')}，"
                        f"{'已点火' if ign.get('ignited') else '未点火'}）\n"
                        f"{('提示：' + result.get('note')) if result.get('note') else ''}\n"
                        f"调度任务、卖出风控运行正常。")
                _system_email(f"【系统自检】{push_time} 推送正常 · 今日无命中信号", body)
            else:
                _system_email(f"【系统自检·异常】{push_time} 扫描失败",
                              f"错误：{result.get('error')}")
    except Exception as exc:  # noqa: BLE001
        if heartbeat:
            _system_email(f"【系统自检·异常】{push_time} 推送任务抛错", str(exc)[:300])
        print(f"[scheduler] {push_time} 全市场推送失败: {exc}")


def _yesterday_job(push_time: str) -> None:
    try:
        if ds.market_status()["status"] not in ("trading", "lunch"):
            return
        # 跨进程防重（同 _job）：10:30 双进程并存时的重复邮件由此拦截
        tdate = ds.effective_trading_date()["date"]
        if not scanstore.claim_once(f"yesterday_{tdate}_{push_time}"):
            print(f"[scheduler] {push_time} 昨日涨停简报已有进程执行（防重跳过）")
            return
        run_yesterday_push(push_time)
    except Exception as exc:  # noqa: BLE001
        print(f"[scheduler] {push_time} 昨日涨停简报失败: {exc}")


def _sell_job(mode: str) -> None:
    try:
        # confirm 也加闸门：休市日行情源返回上一交易日快照，sent_keys 按自然日
        # 重置会把上一交易日的确认信号在周末/节假日重复发（审计确认的缺口）
        if ds.market_status()["status"] not in ("trading", "after_hours"):
            return
        run_sell_alert(mode)
    except Exception as exc:  # noqa: BLE001
        print(f"[scheduler] 持仓卖出{mode}提醒失败: {exc}")


def _ignition_job() -> None:
    """点火盘中监控：连续确认即提醒；可选点火即扫描（默认关）。"""
    try:
        if ds.market_status()["status"] not in ("trading", "after_hours"):
            return
        r = ignitionwatch.check()
        if r.get("fired") and r.get("confirmed_time"):
            cfg = load_config()
            if cfg.market_regime.ignition_auto_scan:
                run_market_push(f"点火确认{r['confirmed_time']}")
    except Exception as exc:  # noqa: BLE001
        print(f"[scheduler] 点火监控失败: {exc}")


def start() -> dict[str, Any]:
    """启动全部定时任务（幂等）：全市场、昨日涨停、持仓卖出。"""
    global _scheduler
    with _lock:
        if _scheduler is not None and _scheduler.running:
            return {"success": True, "message": "定时推送已在运行", "times": _times()}
        _scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
        push_times = _push_times_cfg()
        for idx, (hour, minute) in enumerate(push_times):
            t = f"{hour}:{minute}"
            heartbeat = idx == len(push_times) - 1  # 末次推送时点无信号也发自检邮件
            _scheduler.add_job(
                _job, trigger=CronTrigger(hour=int(hour), minute=int(minute), timezone="Asia/Shanghai"),
                args=[t, heartbeat], id=f"market_push_{hour}{minute}", max_instances=1, coalesce=True,
            )
        for hour, minute in YESTERDAY_TIMES:
            t = f"{hour}:{minute}"
            _scheduler.add_job(
                _yesterday_job,
                trigger=CronTrigger(hour=int(hour), minute=int(minute), timezone="Asia/Shanghai"),
                args=[t], id=f"yesterday_push_{hour}{minute}", max_instances=1, coalesce=True,
            )
        cfg = load_config()
        interval = max(1, int(cfg.sell.intraday_interval_minutes))
        _scheduler.add_job(
            _sell_job,
            trigger=CronTrigger(hour="9-14", minute=f"*/{interval}", timezone="Asia/Shanghai"),
            args=["intraday"], id="sell_intraday", max_instances=1, coalesce=True,
        )
        ch, cm = cfg.sell.confirm_time.split(":")
        _scheduler.add_job(
            _sell_job,
            trigger=CronTrigger(hour=int(ch), minute=int(cm), timezone="Asia/Shanghai"),
            args=["confirm"], id="sell_confirm", max_instances=1, coalesce=True,
        )
        _scheduler.add_job(
            _ignition_job,
            trigger=CronTrigger(hour="9-14", minute=f"*/{max(1, int(cfg.market_regime.ignition_watch_minutes))}",
                                timezone="Asia/Shanghai"),
            id="ignition_watch", max_instances=1, coalesce=True,
        )
        kr_h, kr_m = cfg.scan.kline_refresh_time.split(":")
        _scheduler.add_job(
            _cache_refresh_job,
            trigger=CronTrigger(hour=int(kr_h), minute=int(kr_m), timezone="Asia/Shanghai"),
            id="kline_refresh", max_instances=1, coalesce=True,
        )
        dc_h, dc_m = cfg.scan.daily_check_time.split(":")
        _scheduler.add_job(
            _daily_check_job,
            trigger=CronTrigger(hour=int(dc_h), minute=int(dc_m), timezone="Asia/Shanghai"),
            id="daily_check", max_instances=1, coalesce=True,
        )
        wz_h, wz_m = cfg.scan.wzq_watch_time.split(":")
        _scheduler.add_job(
            _wzq_job,
            trigger=CronTrigger(hour=int(wz_h), minute=int(wz_m), timezone="Asia/Shanghai"),
            id="wzq_watch", max_instances=1, coalesce=True,
        )
        _scheduler.add_job(
            _pattern_job,
            trigger=CronTrigger(hour="9-14", minute=f"*/{max(1, int(cfg.scan.pattern_poll_minutes))}",
                                timezone="Asia/Shanghai"),
            id="pattern_watch", max_instances=1, coalesce=True,
        )
        if int(cfg.scan.pattern_fast_seconds or 0) > 0:
            _scheduler.add_job(
                _pattern_fast_job,
                trigger=IntervalTrigger(seconds=max(15, int(cfg.scan.pattern_fast_seconds)),
                                        timezone="Asia/Shanghai"),
                id="pattern_fast", max_instances=1, coalesce=True,
            )
        wm_h, wm_m = cfg.scan.war_map_time.split(":")
        _scheduler.add_job(
            _war_map_job,
            trigger=CronTrigger(hour=int(wm_h), minute=int(wm_m), timezone="Asia/Shanghai"),
            id="war_map", max_instances=1, coalesce=True,
        )
        lr_h, lr_m = cfg.scan.lunch_review_time.split(":")
        _scheduler.add_job(
            _lunch_review_job,
            trigger=CronTrigger(hour=int(lr_h), minute=int(lr_m), timezone="Asia/Shanghai"),
            id="lunch_review", max_instances=1, coalesce=True,
        )
        if cfg.scan.tdx_enabled:
            tx_h, tx_m = cfg.scan.tdx_time.split(":")
            _scheduler.add_job(
                _tdx_job,
                trigger=CronTrigger(hour=int(tx_h), minute=int(tx_m), timezone="Asia/Shanghai"),
                id="tdx_watch", max_instances=1, coalesce=True,
            )
        er_h, er_m = cfg.scan.early_review_time.split(":")
        _scheduler.add_job(
            _early_review_job,
            trigger=CronTrigger(hour=int(er_h), minute=int(er_m), timezone="Asia/Shanghai"),
            id="early_review", max_instances=1, coalesce=True,
        )
        _scheduler.start()
        return {"success": True, "message": "定时推送已启动", "times": _times()}


def refresh() -> dict[str, Any]:
    """配置变更后重排任务（推送时点 / 持仓卖出间隔与确认时点）。"""
    with _lock:
        if _scheduler is None or not _scheduler.running:
            return {"success": True, "message": "调度器未运行，配置将在下次启动时生效"}
        cfg = load_config()
        # 全市场推送时点重排
        for job in list(_scheduler.get_jobs()):
            if job.id.startswith("market_push_"):
                _scheduler.remove_job(job.id)
        push_times = _push_times_cfg()
        for idx, (hour, minute) in enumerate(push_times):
            t = f"{hour}:{minute}"
            heartbeat = idx == len(push_times) - 1  # 末次推送时点无信号也发自检邮件
            _scheduler.add_job(
                _job, trigger=CronTrigger(hour=int(hour), minute=int(minute), timezone="Asia/Shanghai"),
                args=[t, heartbeat], id=f"market_push_{hour}{minute}", max_instances=1, coalesce=True,
            )
        for job_id in ("sell_intraday", "sell_confirm"):
            if _scheduler.get_job(job_id):
                _scheduler.remove_job(job_id)
        interval = max(1, int(cfg.sell.intraday_interval_minutes))
        _scheduler.add_job(
            _sell_job,
            trigger=CronTrigger(hour="9-14", minute=f"*/{interval}", timezone="Asia/Shanghai"),
            args=["intraday"], id="sell_intraday", max_instances=1, coalesce=True,
        )
        ch, cm = cfg.sell.confirm_time.split(":")
        _scheduler.add_job(
            _sell_job,
            trigger=CronTrigger(hour=int(ch), minute=int(cm), timezone="Asia/Shanghai"),
            args=["confirm"], id="sell_confirm", max_instances=1, coalesce=True,
        )
        if _scheduler.get_job("ignition_watch"):
            _scheduler.remove_job("ignition_watch")
        _scheduler.add_job(
            _ignition_job,
            trigger=CronTrigger(hour="9-14", minute=f"*/{max(1, int(cfg.market_regime.ignition_watch_minutes))}",
                                timezone="Asia/Shanghai"),
            id="ignition_watch", max_instances=1, coalesce=True,
        )
        if _scheduler.get_job("kline_refresh"):
            _scheduler.remove_job("kline_refresh")
        kr_h, kr_m = cfg.scan.kline_refresh_time.split(":")
        _scheduler.add_job(
            _cache_refresh_job,
            trigger=CronTrigger(hour=int(kr_h), minute=int(kr_m), timezone="Asia/Shanghai"),
            id="kline_refresh", max_instances=1, coalesce=True,
        )
        if _scheduler.get_job("daily_check"):
            _scheduler.remove_job("daily_check")
        dc_h, dc_m = cfg.scan.daily_check_time.split(":")
        _scheduler.add_job(
            _daily_check_job,
            trigger=CronTrigger(hour=int(dc_h), minute=int(dc_m), timezone="Asia/Shanghai"),
            id="daily_check", max_instances=1, coalesce=True,
        )
        if _scheduler.get_job("wzq_watch"):
            _scheduler.remove_job("wzq_watch")
        wz_h, wz_m = cfg.scan.wzq_watch_time.split(":")
        _scheduler.add_job(
            _wzq_job,
            trigger=CronTrigger(hour=int(wz_h), minute=int(wz_m), timezone="Asia/Shanghai"),
            id="wzq_watch", max_instances=1, coalesce=True,
        )
        if _scheduler.get_job("pattern_watch"):
            _scheduler.remove_job("pattern_watch")
        _scheduler.add_job(
            _pattern_job,
            trigger=CronTrigger(hour="9-14", minute=f"*/{max(1, int(cfg.scan.pattern_poll_minutes))}",
                                timezone="Asia/Shanghai"),
            id="pattern_watch", max_instances=1, coalesce=True,
        )
        if _scheduler.get_job("pattern_fast"):
            _scheduler.remove_job("pattern_fast")
        if int(cfg.scan.pattern_fast_seconds or 0) > 0:
            _scheduler.add_job(
                _pattern_fast_job,
                trigger=IntervalTrigger(seconds=max(15, int(cfg.scan.pattern_fast_seconds)),
                                        timezone="Asia/Shanghai"),
                id="pattern_fast", max_instances=1, coalesce=True,
            )
        if _scheduler.get_job("war_map"):
            _scheduler.remove_job("war_map")
        wm_h, wm_m = cfg.scan.war_map_time.split(":")
        _scheduler.add_job(
            _war_map_job,
            trigger=CronTrigger(hour=int(wm_h), minute=int(wm_m), timezone="Asia/Shanghai"),
            id="war_map", max_instances=1, coalesce=True,
        )
        if _scheduler.get_job("lunch_review"):
            _scheduler.remove_job("lunch_review")
        lr_h, lr_m = cfg.scan.lunch_review_time.split(":")
        _scheduler.add_job(
            _lunch_review_job,
            trigger=CronTrigger(hour=int(lr_h), minute=int(lr_m), timezone="Asia/Shanghai"),
            id="lunch_review", max_instances=1, coalesce=True,
        )
        if _scheduler.get_job("tdx_watch"):
            _scheduler.remove_job("tdx_watch")
        if cfg.scan.tdx_enabled:
            tx_h, tx_m = cfg.scan.tdx_time.split(":")
            _scheduler.add_job(
                _tdx_job,
                trigger=CronTrigger(hour=int(tx_h), minute=int(tx_m), timezone="Asia/Shanghai"),
                id="tdx_watch", max_instances=1, coalesce=True,
            )
        if _scheduler.get_job("early_review"):
            _scheduler.remove_job("early_review")
        er_h, er_m = cfg.scan.early_review_time.split(":")
        _scheduler.add_job(
            _early_review_job,
            trigger=CronTrigger(hour=int(er_h), minute=int(er_m), timezone="Asia/Shanghai"),
            id="early_review", max_instances=1, coalesce=True,
        )
        return {"success": True, "message": "定时任务已按新配置重排",
                "times": _times()}


def stop() -> dict[str, Any]:
    global _scheduler
    with _lock:
        if _scheduler is not None and _scheduler.running:
            _scheduler.shutdown(wait=False)
            _scheduler = None
        return {"success": True, "message": "全市场定时推送已停止"}


def _times() -> list[str]:
    return [f"{h}:{m}" for h, m in _push_times_cfg()]


def _yesterday_times() -> list[str]:
    return [f"{h}:{m}" for h, m in YESTERDAY_TIMES]


def status() -> dict[str, Any]:
    with _lock:
        running = bool(_scheduler and _scheduler.running)
        jobs = []
        if _scheduler is not None:
            for job in _scheduler.get_jobs():
                jobs.append({
                    "id": job.id,
                    "next_run": job.next_run_time.isoformat(timespec="seconds") if job.next_run_time else None,
                })
        return {
            "running": running,
            "times": _times(),
            "yesterday_times": _yesterday_times(),
            "jobs": jobs,
            "scan": market_scan.state.snapshot(),
            "yesterday": yesterday.latest_snapshot(),
            "sell": sellwatch.status(),
            "ignition": ignitionwatch.status(),
            "wzq": wzqwatch.status(),
            "pattern": patternwatch.status(),
        }
