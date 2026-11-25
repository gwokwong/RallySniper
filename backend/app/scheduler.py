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

from . import datasource as ds, ignitionwatch, market_scan, notifications, notifier, patternwatch, replay, scanstore, wzqwatch
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
        lines.append("\n提醒：候选≠买入信号；候选爆发时快车道45秒内提醒，"
                     "确认以点火确认+尾盘推送为准。")
        title = f"【盘前作战地图】{r['prev_date']} 情绪背景 + {r['n_candidates']} 只反包候选"
        body = "\n".join(lines)
        notifications.add(title, body, [], kind="pattern")
        cfg2 = load_config()
        if cfg2.notification:
            notifier.broadcast(cfg2.notification, title, body, [])
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
