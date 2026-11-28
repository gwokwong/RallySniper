"""盘中形态监控：全市场「洗盘反包·进行中」与「N日新高突破」实时提醒。

- 反包·进行中：近 wash_lookback 日有洗盘大跌（≥wash_drop_pct），今日价格已收复
  洗盘日收盘且涨幅≥5% 或盘中触板——冠军战法的实时前置版（不等封板确认）。
- N日新高突破：现价 > 前 pattern_break_high_days 日最高×1.002 且量比≥1.5
  （形态突破；注意：历史回测中突破形态的隔夜持有期望为负，本提醒仅作盘中观察/决策参考）。

防刷屏：每票每日每形态一次、每日总量上限 pattern_max_alerts、同轮多票合并一封邮件。
数据：前一日基准来自 kline_cache（replay._load_bars 瘦身索引），盘中用实时行情快照判定。
"""
from __future__ import annotations

import threading
from collections import defaultdict
from bisect import bisect_left
from datetime import datetime
from typing import Any

import numpy as np

from . import datasource as ds, notifications, notifier, replay
from .config import load_config


class PatternState:
    def __init__(self) -> None:
        self.date: str | None = None
        self.prepared: bool = False            # 当日候选/基准是否已就绪
        self.rev_candidates: set[str] = set()  # 反包候选（昨日收盘判定）
        self.high_base: dict[str, float] = {}  # 突破基准：代码→前N日最高
        self.wzt_base: dict[str, dict] = {}    # 涨停缩量突破：代码→{low, ph, ldate}
        self.names: dict[str, str] = {}        # 代码→名称
        self.industry_map: dict[str, str] = {}  # 代码→行业
        self.board_alerted: set[str] = set()   # 已提醒的板块异动（ind:方向）
        self.alerted: set[str] = set()         # 已提醒 code:pattern
        self.day_alerts: int = 0
        self.last_poll: str | None = None
        self.last_new: list[dict[str, Any]] = []
        self._lock = threading.RLock()

    def reset_for(self, date: str) -> None:
        self.date = date
        self.prepared = False
        self.rev_candidates = set()
        self.high_base = {}
        self.wzt_base = {}
        self.names = {}
        self.industry_map = {}
        self.board_alerted = set()
        self.alerted = set()
        self.day_alerts = 0
        self.last_poll = None
        self.last_new = []


state = PatternState()


def _prev_day_row(code: str, prev_day: str) -> int | None:
    dl = replay._dlists.get(code)
    if not dl:
        return None
    i = bisect_left(dl, prev_day)
    if i < len(dl) and dl[i] == prev_day:
        return i
    return None


def _prepare_day(prev_day: str, names: dict[str, str] | None = None,
                 industry_map: dict[str, str] | None = None) -> None:
    """开盘前一次性预计算：反包候选 + 突破基准（全部基于前一日及更早，无未来函数）。"""
    cfg = load_config()
    rules = cfg.strategy_rules
    lookback = int(rules.wash_lookback)
    drop = float(rules.wash_drop_pct)
    bars = replay._load_bars()
    rev: set[str] = set()
    high_base: dict[str, float] = {}
    wzt_base: dict[str, dict] = {}
    mfilter = cfg.market_filter
    for code, df in bars.items():
        if not code.startswith(("60", "00", "68", "30")):
            continue
        if mfilter.main_board is False and code.startswith(("60", "00")):
            continue
        if mfilter.kcb is False and code.startswith(("688", "689")):
            continue
        if mfilter.cyb is False and code.startswith(("300", "301")):
            continue
        k = _prev_day_row(code, prev_day)
        if k is None:
            continue
        pc = df["pct_change"].iloc[max(0, k - lookback + 1): k + 1]
        # 反包候选：近 N 日有洗盘大跌，且昨日未封板（封板属连板场景，非反包前置）
        if bool((pc <= -drop).any()):
            th = 19.5 if code.startswith(("688", "689", "300", "301")) else 9.8
            if not (float(df["pct_change"].iloc[k]) >= th - 0.4
                    and float(df["close"].iloc[k]) >= float(df["high"].iloc[k]) * 0.995):
                rev.add(code)
        # 突破基准：前 N 日最高（不含当日）
        lo = max(0, k - int(cfg.scan.pattern_break_high_days) + 1)
        high_base[code] = float(df["high"].iloc[lo:k + 1].max())
        # 涨停缩量突破基准（用户战法）：近10个交易日内最近一次收盘封板 L，
        # 涨停后从次日起连续≥3日量能逐日递减（持续缩量），回调段收盘均不破 L 最低价
        th = 19.5 if code.startswith(("688", "689", "300", "301")) else 9.8
        L = None
        for j in range(k, max(0, k - 9) - 1, -1):          # 近10个交易日（含昨日）找最近封板日
            if float(df["pct_change"].iloc[j]) >= th and float(df["close"].iloc[j]) >= float(df["high"].iloc[j]) * 0.995:
                L = j
                break
        if L is not None:
            pull = k - L                                    # 涨停日后的回调天数
            if pull >= 3:
                vols = df["volume"].iloc[L:k + 1].astype(float).tolist()
                shrink3 = vols[1] > vols[2] > vols[3]       # 涨停次日起连续3日缩量
                low_l = float(df["low"].iloc[L])
                holds = bool((df["close"].iloc[L + 1: k + 1] >= low_l).all())
                if shrink3 and holds:
                    ph = float(df["high"].iloc[L:k + 1].max())   # 区间最高（涨停日至今）
                    wzt_base[code] = {"low": low_l, "ph": round(ph, 3),
                                      "ldate": str(df["date"].iloc[L])[:10]}
    with state._lock:
        state.rev_candidates = rev
        state.high_base = high_base
        state.wzt_base = wzt_base
        state.names = dict(names or {})
        state.industry_map = dict(industry_map or {})
        state.prepared = True


def poll_fast() -> dict[str, Any]:
    """候选股快车道：只盯反包候选池（几十只），45秒级轮询。

    与全市场 poll() 共用 state.alerted 去重（每票每日一次）。只查候选股行情
    （量小、频率高），全市场扫描/板块异动仍走慢车道 poll()。
    """
    cfg = load_config()
    if not cfg.scan.pattern_watch_enabled or int(cfg.scan.pattern_fast_seconds or 0) <= 0:
        return {"checked": False, "reason": "快车道未启用"}
    if ds.market_status()["status"] not in ("trading",):
        return {"checked": False, "reason": "非盘中"}
    eff = ds.effective_trading_date()
    today = eff["date"]
    now_hm = datetime.now().strftime("%H:%M")
    with state._lock:
        if state.date != today:
            state.reset_for(today)
        if not state.prepared:
            return {"checked": False, "reason": "候选池未就绪（等待慢车道首次预计算）"}
        rev = set(state.rev_candidates)
        alerted = state.alerted
        day_alerts = state.day_alerts
        cap = int(cfg.scan.pattern_max_alerts)
    # 鬼手抄底·5日内信号股：纳入盘中关注（二次启动≥3%即提醒）
    dip_codes: set[str] = set()
    try:
        from . import tdxwatch
        lx = tdxwatch.latest_signals(5)
        for c, v in (lx.get("signals") or {}).items():
            if any(x.get("signal") == "鬼手抄底" for x in v):
                dip_codes.add(c)
    except Exception:  # noqa: BLE001
        pass
    # 烂板接力候选（T-1烂板换手板）——接力确认=今日封板（规则7：上午板才算，下午板不打）
    lan_codes: dict[str, dict] = {}
    try:
        from . import lanban as _lb
        for c in _lb.candidates().get("candidates", []):
            lan_codes[c["code"]] = c
    except Exception:  # noqa: BLE001
        pass
    # 昨日涨停股（换手连板形成盯守）：今日开盘无板+放量+上午封板 → 买入窗口+明日烂板接力候选
    hsb_codes: set[str] = set()
    try:
        dts = replay.trading_dates()
        today_s = str(today)
        if "-" not in today_s:
            today_s = f"{today_s[:4]}-{today_s[4:6]}-{today_s[6:8]}"
        prev_list = [x for x in dts if x < today_s]
        if prev_list:
            hsb_codes = set(replay.lu_codes_on(prev_list[-1]))
    except Exception:  # noqa: BLE001
        pass
    watch = rev | dip_codes | set(lan_codes.keys()) | hsb_codes
    if not watch:
        return {"checked": True, "new": []}
    quotes = ds.spot_quote_map(sorted(watch))
    if not quotes:
        return {"checked": True, "new": []}
    new: list[dict[str, Any]] = []
    for code, q in quotes.items():
        if day_alerts >= cap:
            continue
        # 烂板接力·秒板确认（规则3/7：放量换手板才有合力；上午板才算；一字不打）
        if code in lan_codes and f"{code}:lanban" not in alerted:
            q_l = lan_codes[code]
            limit_px = float(q_l.get("relay_px") or q_l["limit_px"])
            price_f = float(q.get("price") or 0)
            opened_lim = float(q.get("open") or 0) >= limit_px * 0.995
            vr_today = float(q.get("volume_ratio") or 0)
            now_h = int(now_hm.split(":")[0]) if now_hm else 12
            if price_f >= limit_px * 0.995 and not opened_lim:
                if now_h >= 13:
                    pass  # 下午板不打（规则7）
                elif q_l["streak"] >= 4 and float(q.get("open") or 0) < float(q_l["t1_close"]):
                    pass  # 高位低开必死（规则6）
                elif vr_today < 1.2:
                    pass  # 无量上板（规则4：量化封板多为消息刺激，无量不碰）
                else:
                    alerted.add(f"{code}:lanban")
                    day_alerts += 1
                    tag = f"{q_l['streak']}进{q_l['streak'] + 1}"
                    new.append({"code": code, "name": nm, "pattern": f"烂板接力·{tag}封板",
                                "price": round(price_f, 2), "pct": round(float(q.get("change_pct") or 0), 2),
                                "vr": round(vr_today, 2),
                                "detail": (f"昨日换手烂板（放量{q_l['vol_ratio_1d']}倍），今日放量封板（量比{vr_today:.1f}）"
                                           f"｜接力确认价{limit_px}｜回测封住5日均+8.65%（二进三）/ +4.12%（三进四可控）")})
                continue
        # 昨日涨停股·换手连板形成（今日开盘无板+放量+上午封板）→ 买入窗口+明日烂板接力候选
        if (code in hsb_codes and code not in rev and code not in dip_codes
                and code not in lan_codes and f"{code}:hsb" not in alerted):
            th_h = 19.5 if code.startswith(("688", "689", "300", "301")) else 9.8
            pre_h = float(q.get("pre_close") or 0)
            limit_h = round(pre_h * (1 + th_h / 100), 2)
            price_h = float(q.get("price") or 0)
            op_h = float(q.get("open") or 0)
            vr_h = float(q.get("volume_ratio") or 0)
            now_min = int(now_hm[:2]) * 60 + int(now_hm[3:5]) if now_hm else 0
            if (pre_h and price_h >= limit_h * 0.995 and op_h < limit_h * 0.995
                    and vr_h >= 1.2 and now_min <= 11 * 60 + 30):
                alerted.add(f"{code}:hsb")
                day_alerts += 1
                new.append({"code": code, "name": str(q.get("name") or code), "pattern": "换手连板·上午封板",
                            "price": round(price_h, 2), "pct": round(float(q.get("change_pct") or 0), 2),
                            "vr": round(vr_h, 2),
                            "detail": ("昨日涨停，今日开盘无板+放量换手封板（非一字）——换手板买入窗口，"
                                       "明日自动进入烂板接力候选；封不住/炸板即放弃")})
                continue
        # 鬼手抄底票·二次启动（pct≥3%）：候选池外的鬼手表也盯
        if code not in rev and code in dip_codes and f"{code}:tdxdip" not in alerted:
            pct_d = q.get("change_pct")
            price_d = q.get("price")
            if pct_d is not None and float(pct_d) >= 3.0 and price_d:
                alerted.add(f"{code}:tdxdip")
                day_alerts += 1
                nm_d = str(q.get("name") or code)
                new.append({"code": code, "name": nm_d, "pattern": "鬼手抄底·二次启动",
                            "price": round(float(price_d), 2), "pct": round(float(pct_d), 2),
                            "vr": None,
                            "detail": "5日内有鬼手抄底信号（回测71.8%/+5.82%），今日再度放量上攻≥3%"})
                continue
        if code not in rev or f"{code}:rev" in alerted:
            continue
        price = q.get("price")
        pct = q.get("change_pct")
        pre = q.get("pre_close")
        high = q.get("high")
        if price is None or pct is None or not pre:
            continue
        nm_chk = str(q.get("name") or "")
        if "ST" in nm_chk.upper() or "退" in nm_chk:
            continue  # ST 烂板 5% 涨跌幅无追打价值（与慢车道口径一致）
        th = 19.5 if code.startswith(("688", "689", "300", "301")) else 9.8
        near_board = pct >= min(5.0, th - 0.5) or (high and high >= pre * (1 + th / 100) * 0.999)
        if not near_board:
            continue
        df = replay._bars.get(code)
        wash_close = None
        if df is not None:
            drop = float(cfg.strategy_rules.wash_drop_pct)
            lb = int(cfg.strategy_rules.wash_lookback)
            di = _prev_day_row(code, today)
            start = max(0, (di if di is not None else len(df)) - lb)
            seg = df.iloc[start:(di if di is not None else len(df))]
            if not seg.empty:
                wash_idx = seg["pct_change"].idxmin()
                wash_close = float(df["close"].iloc[wash_idx])
        if wash_close is None or price < wash_close:
            continue
        alerted.add(f"{code}:rev")
        day_alerts += 1
        new.append({"code": code, "name": str(q.get("name") or code),
                    "pattern": "洗盘反包·进行中", "price": round(price, 2),
                    "pct": round(pct, 2),
                    "vr": round(float(q["volume_ratio"]), 2) if q.get("volume_ratio") else None,
                    "detail": (f"今日涨幅{pct:+.1f}%、守在洗盘日收盘{wash_close:.2f}上方"
                               f"（生命线={wash_close:.2f}，跌破即形态失败）")})
    with state._lock:
        state.day_alerts = max(day_alerts, state.day_alerts)
        if new:
            cur = list(state.last_new or [])
            state.last_new = (cur + new)[-50:]
    if new:
        title = f"【盘中快车道】{now_hm} {len(new)} 只异动（反包/接力/连板）"
        body = ("触发时间：" + now_hm + "\n" +
                "\n".join(f"- {x['name']}({x['code']})【{x['pattern']}】现价{x['price']} {x['pct']:+.1f}%：{x['detail']}"
                          for x in new[:15]) +
                "\n提醒：进行中≠买入确认；确认信号以点火确认+尾盘推送为准，生命线跌破即放弃。")
        rows = [{"strategy": x["pattern"], "stage": "watch", "stage_label": x["pattern"],
                 "code": x["code"], "name": x["name"], "price": x.get("price"),
                 "change_pct": x.get("pct") or 0, "reasons": [x.get("detail", "")]} for x in new]
        notifications.add(title, body, rows, kind="pattern")
        if cfg.notification:
            notifier.broadcast(cfg.notification, title, body, rows)
    return {"checked": True, "new": new}


def war_map(top_n: int = 15) -> dict[str, Any]:
    """盘前作战地图：今日反包候选名单 + 生命线 + 昨日情绪背景（9:25 发送）。"""
    cfg = load_config()
    dates = replay.trading_dates()
    eff = ds.effective_trading_date()
    today = eff["date"]
    pos = [k for k, d in enumerate(dates) if d < today]
    if not pos:
        return {"success": False, "error": "无前置交易日"}
    prev_day = dates[pos[-1]]
    try:
        _prepare_day(prev_day, names=ds.stock_code_names(), industry_map=ds.industry_map_full())
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"候选池预计算失败: {str(exc)[:80]}"}
    with state._lock:
        rev = sorted(state.rev_candidates)
        state.date = today
    names = ds.stock_code_names()
    imap = ds.industry_map_full()
    lb = int(cfg.strategy_rules.wash_lookback)
    rows: list[dict[str, Any]] = []
    for code in rev:
        nm = str(names.get(code, ""))
        if "ST" in nm.upper() or "退" in nm or code.startswith(("4", "8", "92")):
            continue
        df = replay._bars.get(code)
        k = _prev_day_row(code, prev_day)
        if df is None or k is None:
            continue
        seg = df.iloc[max(0, k - lb):k]
        if seg.empty:
            continue
        wash_idx = seg["pct_change"].idxmin()
        wash_close = round(float(df["close"].iloc[wash_idx]), 2)
        prev_close = round(float(df["close"].iloc[k]), 2)
        if prev_close < wash_close:
            continue  # 已跌破生命线=形态失败，不进名单
        rows.append({"code": code, "name": nm or code,
                     "industry": (imap.get(code) or "--"),
                     "prev_close": prev_close, "life_line": wash_close,
                     "wash_date": str(df["date"].iloc[wash_idx])[:10]})
    # 排序：洗盘越近越活跃优先
    rows.sort(key=lambda r: r["wash_date"], reverse=True)
    sent = replay.market_sentiment().get(prev_day, {})
    return {"success": True, "prev_date": prev_day, "n_candidates": len(rows),
            "candidates": rows[:top_n], "total_rows": len(rows),
            "sentiment": {"bomb_rate": sent.get("bomb_rate"),
                          "max_streak": sent.get("max_streak"),
                          "bomb_max": float(cfg.market_regime.ignition_bomb_rate_max)},
            "market_label": eff.get("market_label")}


def poll() -> dict[str, Any]:
    """盘中执行一次形态监控（调度器每 N 分钟调用）。返回本轮新增触发。"""
    cfg = load_config()
    if not cfg.scan.pattern_watch_enabled:
        return {"checked": False, "reason": "未启用"}
    status = ds.market_status()["status"]
    if status not in ("trading",):
        return {"checked": False, "reason": f"非盘中({status})"}

    eff = ds.effective_trading_date()
    today = eff["date"]
    now_hm = datetime.now().strftime("%H:%M")
    with state._lock:
        if state.date != today:
            state.reset_for(today)
        state.last_poll = now_hm

    if not state.prepared:
        dates = replay.trading_dates()
        pos = [k for k, d in enumerate(dates) if d < today]
        if not pos:
            return {"checked": False, "reason": "无前置交易日"}
        _prepare_day(dates[pos[-1]])
    if not state.industry_map:
        # 板块成分中位口径的行业映射（业绩报表所处行业，与push2限流隔离）
        try:
            state.industry_map = ds.industry_map_full()
        except Exception:  # noqa: BLE001
            pass

    quotes = ds.spot_quote_map()
    if not quotes:
        with state._lock:
            state.alerted.clear()
        return {"checked": True, "reason": "行情快照不可用，跳过本轮"}

    mfilter = cfg.market_filter
    vr_min = float(cfg.scan.pattern_break_vr)
    cap = int(cfg.scan.pattern_max_alerts)
    new: list[dict[str, Any]] = []
    with state._lock:
        alerted = state.alerted
        day_alerts = state.day_alerts
        industry_map = dict(state.industry_map)

    # 板块异动检测：优先东财行业板块指数涨跌幅（与行情软件板块口径一致）；
    # 板块行情不可用时回退「成分股涨跌中位」口径。每板块每日每方向最多一次。
    # 拉涨/砸盘阈值独立：砸盘默认更灵敏（2.3 vs 3.0）。
    # 大跌日可能同轮触发十几二十个板块——每轮合并为一封邮件，不按板块逐封刷额度。
    up_th = float(cfg.scan.pattern_board_anomaly_pct)
    down_th = float(cfg.scan.pattern_board_down_pct)
    board_round: list[dict[str, Any]] = []   # 本轮新触发的板块（拉涨/砸盘混合）

    def _board_fire(ind: str, bp: float, members: int | None) -> None:
        direction = "拉涨" if bp >= up_th else "砸盘"
        if f"{ind}:{direction}" in state.board_alerted:
            return
        state.board_alerted.add(f"{ind}:{direction}")
        label = (f"行业板块涨跌 {bp:+.1f}%" if members is None
                 else f"行业聚合涨跌中位 {bp:+.1f}%（成分 {members} 只）")
        new.append({"code": "--", "name": f"板块·{ind}", "pattern": f"板块异动·{direction}",
                    "price": None, "pct": round(bp, 2), "vr": None, "detail": label})
        if state.day_alerts >= cap:
            return
        state.day_alerts += 1
        board_round.append({"ind": ind, "direction": direction, "pct": round(bp, 2),
                            "label": label})

    try:
        bdf = ds.industry_boards()
    except Exception:  # noqa: BLE001
        bdf = None
    if bdf is not None and not bdf.empty and "涨跌幅" in bdf.columns:
        for _, row in bdf.iterrows():
            ind = str(row.get("板块名称", "")).strip()
            try:
                bp = float(row.get("涨跌幅"))
            except (TypeError, ValueError):
                continue
            if not ind:
                continue
            if bp >= up_th:
                _board_fire(ind, bp, None)
            elif bp <= -down_th:
                _board_fire(ind, bp, None)
    else:
        # 回退：成分股中位口径（industry_map 为空时自动跳过）
        ind_pcts: dict[str, list[float]] = defaultdict(list)
        for code, q in quotes.items():
            ind = industry_map.get(code)
            pct = q.get("change_pct")
            if ind and pct is not None:
                ind_pcts[ind].append(float(pct))
        members: dict[str, int] = {}
        for c, ic in industry_map.items():
            members[ic] = members.get(ic, 0) + 1
        board_now = {ind: float(np.median(vals)) for ind, vals in ind_pcts.items() if len(vals) >= 3}
        for ind, bp in sorted(board_now.items(), key=lambda kv: -kv[1]):
            if bp >= up_th:
                _board_fire(ind, bp, members.get(ind))
            elif bp <= -down_th:
                _board_fire(ind, bp, members.get(ind))

    # 本轮板块异动合并为一封邮件（大跌日防刷屏+省额度；每板块每日每方向仍只记一次）
    if board_round:
        ups = sorted([b for b in board_round if b["direction"] == "拉涨"], key=lambda x: -x["pct"])
        downs = sorted([b for b in board_round if b["direction"] == "砸盘"], key=lambda x: x["pct"])
        def _board_rows(items, direction):
            return [{"strategy": "板块异动", "stage": "trigger" if direction == "拉涨" else "watch",
                     "stage_label": direction, "code": "--", "name": b["ind"],
                     "price": "", "change_pct": b["pct"], "reasons": [b["label"]]}
                    for b in items]
        if downs:
            title = f"【板块异动·砸盘】{now_hm} {len(downs)} 个板块集体跳水（阈值 -{down_th:g}%）"
            body = ("触发时间：" + now_hm + "\n" +
                    "\n".join(f"- {b['ind']} {b['pct']:+.1f}%（{b['label']}）" for b in downs) +
                    "\n纪律：上述板块候选暂停追入，持仓注意风险；跳空低开日先看市场高度与炸板率。")
            rows = _board_rows(downs, "砸盘")
            notifications.add(title, body, rows, kind="pattern")
            if cfg.notification:
                notifier.broadcast(cfg.notification, title, body, rows)
        if ups:
            title = f"【板块异动·拉涨】{now_hm} {len(ups)} 个板块异动（阈值 +{up_th:g}%）"
            body = ("触发时间：" + now_hm + "\n" +
                    "\n".join(f"- {b['ind']} {b['pct']:+.1f}%（{b['label']}）" for b in ups) +
                    "\n该板块反包候选将优先进入主推精选；追高注意兑现节奏。")
            rows = _board_rows(ups, "拉涨")
            notifications.add(title, body, rows, kind="pattern")
            if cfg.notification:
                notifier.broadcast(cfg.notification, title, body, rows)

    for code, q in quotes.items():
        if day_alerts >= cap:
            break
        nm = str(q.get("name") or code)
        if "ST" in nm.upper() or "退" in nm:
            continue
        if not code.startswith(("60", "00", "68", "30")):
            continue
        if mfilter.main_board is False and code.startswith(("60", "00")):
            continue
        if mfilter.kcb is False and code.startswith(("688", "689")):
            continue
        if mfilter.cyb is False and code.startswith(("300", "301")):
            continue
        price = q.get("price")
        pct = q.get("change_pct")
        vr = q.get("volume_ratio")
        pre = q.get("pre_close")
        if price is None or pct is None or not pre:
            continue
        if mfilter.max_price and price > mfilter.max_price:
            continue
        if mfilter.min_price and price < mfilter.min_price:
            continue
        th = 19.5 if code.startswith(("688", "689", "300", "301")) else 9.8
        high = q.get("high") or 0

        # 形态1：洗盘反包·进行中（候选 + 涨幅≥5%或触板 + 价格守在洗盘日收盘上方）
        if code in state.rev_candidates and f"{code}:rev" not in alerted:
            near_board = pct >= min(5.0, th - 0.5) or (high and high >= pre * (1 + th / 100) * 0.999)
            if near_board:
                # 守住洗盘日收盘：近 lookback 日最近一个洗盘日的收盘
                df = replay._bars.get(code)
                wash_close = None
                if df is not None:
                    drop = float(cfg.strategy_rules.wash_drop_pct)
                    lb = int(cfg.strategy_rules.wash_lookback)
                    di = _prev_day_row(code, today)
                    start = max(0, (di if di is not None else len(df)) - lb)
                    seg = df.iloc[start:(di if di is not None else len(df))]
                    if not seg.empty:
                        wash_idx = seg["pct_change"].idxmin()
                        wash_close = float(df["close"].iloc[wash_idx])
                if wash_close is not None and price >= wash_close:
                    alerted.add(f"{code}:rev")
                    day_alerts += 1
                    new.append({"code": code, "name": nm, "pattern": "洗盘反包·进行中",
                                "price": round(price, 2), "pct": round(pct, 2),
                                "vr": round(vr, 2) if vr is not None else None,
                                "detail": f"今日涨幅{pct:+.1f}%、守在洗盘日收盘{wash_close:.2f}上方"})

        # 形态2：N日新高突破（首次越线 + 量比≥阈值）
        if f"{code}:brk" not in alerted:
            base = state.high_base.get(code)
            if base and base > 0 and price > base * 1.002 and vr is not None and vr >= vr_min:
                alerted.add(f"{code}:brk")
                day_alerts += 1
                new.append({"code": code, "name": nm, "pattern": "新高突破·仅供观察",
                            "price": round(price, 2), "pct": round(pct, 2),
                            "vr": round(vr, 2),
                            "detail": (f"突破前{cfg.scan.pattern_break_high_days}日最高{base:.2f}，量比{vr:.1f}"
                                       "（⚠️ 该形态回测期望为负，勿作买点）")})

        # 形态3：涨停缩量突破（用户战法：近10日有涨停→缩量≥3日不破涨停日最低→放量突破区间最高）
        if f"{code}:wzt" not in alerted:
            wb = state.wzt_base.get(code)
            if wb and price > wb["ph"] and vr is not None and vr >= vr_min:
                low_today = q.get("low")
                if low_today is None or low_today >= wb["low"] * 0.995:   # 当日也未破涨停日最低
                    alerted.add(f"{code}:wzt")
                    day_alerts += 1
                    new.append({"code": code, "name": nm, "pattern": "涨停缩量突破·仅供观察",
                                "price": round(price, 2), "pct": round(pct, 2),
                                "vr": round(vr, 2),
                                "detail": (f"突破{wb['ldate'][5:]}涨停以来区间最高{wb['ph']:.2f}"
                                           f"（缩量≥3日不破{wb['low']:.2f}），量比{vr:.1f}"
                                           "（⚠️ 该形态回测期望为负，勿作买点）")})

    with state._lock:
        state.last_new = new
        # 板块异动直接累加在 state 上，个股计数在局部：取并集避免互相覆盖
        state.day_alerts = max(day_alerts, state.day_alerts)

    # 个股形态才发汇总提醒；板块异动已发专属提醒，不重复占用邮件额度
    stock_new = [x for x in new if not str(x.get("name", "")).startswith("板块·")]
    if stock_new:
        title = f"【形态突破】{now_hm} 新增 {len(stock_new)} 条（今日累计 {state.day_alerts}）"
        lines = "\n".join(
            f"- {x['name']}({x['code']})【{x['pattern']}】现价{x['price']} {x['pct']:+.1f}%"
            f"（量比{x['vr'] if x['vr'] is not None else '--'}）：{x['detail']}"
            for x in stock_new[:20])
        body = (f"触发时间：{now_hm}\n{lines}\n\n"
                "提醒：盘中形态信息，仅供观察与决策参考，不构成买卖信号。\n"
                "「新高突破」「涨停缩量突破」经回测期望为负，仅作盘中观察，请勿作为买点依据；\n"
                "买入决策以「洗盘反包+点火闸门」尾盘扫描推送为准。")
        rows = [{"strategy": x.get("pattern", ""), "stage": "watch",
                 "stage_label": x.get("pattern", ""), "code": x.get("code"),
                 "name": x.get("name"), "price": x.get("price"),
                 "change_pct": x.get("pct") or 0, "reasons": [x.get("detail", "")]}
                for x in stock_new]
        notifications.add(title, body, rows, kind="pattern")
        if cfg.notification:
            notifier.broadcast(cfg.notification, title, body, rows)

    return {"checked": True, "new": new, "day_alerts": state.day_alerts}


def ignition_picks(top_n: int = 4) -> dict[str, Any]:
    """点火日精选候选：反包候选池按「盈利优先→营收增速→行业分散」取 Top N。

    回测证明点火日内按任何单一维度精选都不优于等权——本名单是资金有限时的
    结构化分散参考（非 alpha 承诺），配合实时「反包·进行中」提醒与生命线止损使用。
    """
    cfg = load_config()
    eff = ds.effective_trading_date()
    today = eff["date"]
    with state._lock:
        need_prep = state.date != today or not state.prepared
    if need_prep:
        dates = replay.trading_dates()
        pos = [k for k, d in enumerate(dates) if d < today]
        if not pos:
            return {"picks": [], "reason": "无前置交易日"}
        try:
            names = ds.stock_code_names()
        except Exception:  # noqa: BLE001
            names = {}
        industry_map = {}
        try:
            ydf = ds.cache.get_or_fetch(
                "yjbb_picks_ind", 43200,
                lambda: __import__("akshare").stock_yjbb_em(date="20260930")) or None
            if ydf is not None:
                for _, rr in ydf.iterrows():
                    industry_map[str(rr.get("股票代码", "")).strip().zfill(6)] = str(rr.get("所处行业", ""))
        except Exception:  # noqa: BLE001
            pass
        _prepare_day(dates[pos[-1]], names, industry_map)
    codes = sorted(state.rev_candidates)
    if not codes:
        return {"picks": [], "reason": "今日反包候选为空"}

    mm = int(today[4:6])
    report = "20260930" if mm >= 10 else "20260630"

    def _fund_map():
        def fetch():
            import akshare as _ak
            df = _ak.stock_yjbb_em(date=report)
            out = {}
            for _, r in df.iterrows():
                code = str(r.get("股票代码", "")).strip().zfill(6)
                try:
                    rev = float(r.get("营业总收入-同比增长"))
                except (TypeError, ValueError):
                    rev = None
                try:
                    np_ = float(r.get("净利润-净利润"))
                except (TypeError, ValueError):
                    np_ = None
                out[code] = {"rev_yoy": rev, "profit": np_, "industry": str(r.get("所处行业", ""))}
            return out or None
        return ds.cache.get_or_fetch(f"yjbb_picks_{report}", 43200, fetch) or {}

    fmap = _fund_map()
    cap_yi = float(cfg.fundamental_filter.pick_max_float_cap)
    per_ind = max(1, int(cfg.fundamental_filter.pick_per_industry))
    quotes = ds.spot_quote_map(codes)

    cands = []
    for code in codes:
        q = quotes.get(code) or {}
        f = fmap.get(code, {})
        rev = f.get("rev_yoy")
        prof = f.get("profit")
        fc = q.get("float_cap")   # 流通市值（元）
        cap_ok = fc is None or fc <= cap_yi * 1e8
        if not cap_ok:
            continue
        cands.append({"code": code, "name": str(q.get("name") or state.names.get(code, code)),
                      "price": q.get("price"), "pct": q.get("change_pct"),
                      "float_cap": round(fc / 1e8, 1) if fc is not None else None,
                      "rev_yoy": rev, "profit_pos": (prof is not None and prof > 0) or None,
                      "industry": f.get("industry", "")})
    picks, seen_cnt = [], {}
    for s in sorted(cands, key=lambda x: (not x["profit_pos"], -(x["rev_yoy"] if x["rev_yoy"] is not None else -999),
                                          -(x["pct"] if x["pct"] is not None else -999))):
        ind = s["industry"] or s["code"]
        if seen_cnt.get(ind, 0) >= per_ind:
            continue
        seen_cnt[ind] = seen_cnt.get(ind, 0) + 1
        picks.append(s)
        if len(picks) >= top_n:
            break
    return {"picks": picks, "total_candidates": len(cands),
            "filtered_by_cap": len(cands) < len(codes)}


def status() -> dict[str, Any]:
    cfg = load_config()
    with state._lock:
        return {
            "enabled": bool(cfg.scan.pattern_watch_enabled),
            "minutes": int(cfg.scan.pattern_poll_minutes),
            "high_days": int(cfg.scan.pattern_break_high_days),
            "vr_min": float(cfg.scan.pattern_break_vr),
            "cap": int(cfg.scan.pattern_max_alerts),
            "date": state.date,
            "prepared": state.prepared,
            "rev_candidates": len(state.rev_candidates),
            "day_alerts": state.day_alerts,
            "last_poll": state.last_poll,
            "last_new": state.last_new,
        }
