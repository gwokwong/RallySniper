"""主升浪战法识别引擎。

针对标的池逐只股票，基于日K历史数据识别 5 种主升浪启动形态：
  1. 量价突破（经典）    - 均线多头 + 放量突破前高
  2. 三重共振（稳健）    - 均线 + 成交量 + MACD 三者同步
  3. 四特征共振（严格）  - 涨停 + 倍量 + 跳空缺口 + 多连阳
  4. 龙出水             - 长期横盘后放量突破箱体
  5. 三线底部启动（提前）- 60日线走平上拐 + 5穿20 + 三线多头
  6. 涨停回调（低吸）    - 涨停/连板后持续缩量回调，且守住最近涨停日最低价

设计原则：
- 只识别"最近 1~3 个交易日"的启动点，历史触发不重复提醒
- 每个战法返回 hit + reasons（可读理由）+ strength（强度评分）
- 全部基于公开行情数据的技术形态计算，不构成投资建议
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from . import datasource as ds


# ----------------------------- 指标计算 -----------------------------

def _limit_up_threshold(code: str = "", name: str = "") -> float:
    """按板块返回涨停涨幅门槛：主板 9.8%，科创/创业 19.5%，北交所 29.5%，ST 4.8%。

    原来统一用 9.8% 会把科创/创业板的 10% 普通大涨误判为涨停，
    也会让 ST 股（5% 涨停）永远识别不到。
    """
    c = str(code or "").zfill(6)
    if "ST" in str(name or "").upper():
        return 4.8
    if c.startswith(("688", "689", "300", "301")):
        return 19.5
    if c.startswith(("43", "83", "87", "92", "8")):
        return 29.5
    return 9.8


def _indicators(df: pd.DataFrame, code: str = "", name: str = "") -> pd.DataFrame:
    """在统一列的历史K线上补充战法所需指标。"""
    out = df.copy()
    close = out["close"]
    vol = out["volume"]

    for n in (5, 10, 20, 60):
        out[f"ma{n}"] = close.rolling(n, min_periods=n).mean()
    for n in (5, 10, 20):
        out[f"vma{n}"] = vol.rolling(n, min_periods=n).mean()

    # MACD (12,26,9)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    out["dif"] = ema12 - ema26
    out["dea"] = out["dif"].ewm(span=9, adjust=False).mean()
    out["macd"] = (out["dif"] - out["dea"]) * 2

    # 阳线 / 涨停 / 缺口（上涨用）
    out["is_yang"] = out["close"] > out["open"]
    out["limit_up"] = out["pct_change"] >= _limit_up_threshold(code, name)
    out["gap_up"] = out["open"] > out["close"].shift(1)
    # K线形态（下跌用）：上下影线 / 实体 / 十字星
    out["is_yin"] = out["close"] < out["open"]
    body = (out["close"] - out["open"]).abs()
    out["body"] = body
    out["upper_shadow"] = out["high"] - out[["open", "close"]].max(axis=1)
    out["lower_shadow"] = out[["open", "close"]].min(axis=1) - out["low"]
    out["price_range"] = (out["high"] - out["low"]).replace(0, float("nan"))
    out["upper_shadow_ratio"] = out["upper_shadow"] / out["price_range"]
    out["doji"] = body <= out["price_range"] * 0.1  # 十字星
    out["gap_down"] = out["open"] < out["close"].shift(1)
    return out


def _recent_window(hit_series: pd.Series, window: int) -> tuple[bool, int]:
    """判断最近 window 根K线内是否触发，返回 (是否触发, 距今几根)。"""
    tail = hit_series.tail(window).fillna(False).astype(bool).tolist()
    if not any(tail):
        return False, -1
    # 从最近往前找第一个 True
    for i in range(len(tail) - 1, -1, -1):
        if tail[i]:
            return True, (len(tail) - 1 - i)
    return False, -1


def _name_of(code: str, spot_names: dict[str, str]) -> str:
    key = str(code).strip().zfill(6)
    return spot_names.get(key, str(code))


def _spot_name_map() -> dict[str, str]:
    """获取代码->名称映射，优先交易所静态表，避免实时行情限流导致名称丢失。"""
    try:
        return ds.stock_code_names()
    except Exception:  # noqa: BLE001
        return {}


# ----------------------------- 五种战法 -----------------------------

def _strategy_volume_breakout(d: pd.DataFrame, r: dict[str, Any]) -> dict[str, Any]:
    """战法1：量价突破（经典）—— 均线多头 + 放量突破前高。"""
    if len(d) < 61:
        return {"hit": False, "reasons": []}
    cur = d.iloc[-1]
    prev = d.iloc[-2]
    ratio = r.get("volume_ratio", 1.5)
    # 均线多头排列且向上
    ma_ok = (cur["ma5"] > cur["ma10"] > cur["ma20"] > cur["ma60"]
             and cur["ma20"] > prev["ma20"])
    # 放量
    vol_ok = cur["volume"] >= cur["vma5"] * ratio
    # 突破近20日高点（不含今日）
    prev_high = d["high"].iloc[-21:-1].max()
    breakout = cur["close"] >= prev_high
    hit = bool(ma_ok and vol_ok and breakout and cur["close"] > cur["open"])
    reasons: list[str] = []
    if ma_ok:
        reasons.append("均线多头排列(5>10>20>60且向上)")
    if vol_ok:
        reasons.append(f"放量{cur['volume'] / cur['vma5']:.1f}倍(≥{ratio})")
    if breakout:
        reasons.append(f"突破20日高点{prev_high:.2f}")
    return {"hit": hit, "reasons": reasons, "strength": round((3 if hit else 0) + min(cur["pct_change"], 10) / 5, 1)}


def _strategy_triple_resonance(d: pd.DataFrame, r: dict[str, Any]) -> dict[str, Any]:
    """战法2：三重共振 —— 均线金叉 + 量能金叉 + MACD金叉，3日内同步。"""
    if len(d) < 61:
        return {"hit": False, "reasons": []}
    # 均线：MA5上穿MA10（或已多头且MA20走平向上）
    ma_cross = (d["ma5"] > d["ma10"]) & (d["ma5"].shift(1) <= d["ma10"].shift(1))
    ma_bull = (d["ma5"] > d["ma10"]) & (d["ma20"] >= d["ma20"].shift(1))
    ma_sig = ma_cross | ma_bull
    # 量能：VMA5上穿VMA10 或 已放量
    vol_cross = (d["vma5"] > d["vma10"]) & (d["vma5"].shift(1) <= d["vma10"].shift(1))
    vol_bull = (d["vma5"] > d["vma10"]) & (d["volume"] >= d["vma5"] * r.get("volume_ratio", 1.5))
    vol_sig = vol_cross | vol_bull
    # MACD：DIF上穿DEA 或 零轴上方红柱放大
    macd_cross = (d["dif"] > d["dea"]) & (d["dif"].shift(1) <= d["dea"].shift(1))
    macd_bull = (d["dif"] > d["dea"]) & (d["macd"] > d["macd"].shift(1)) & (d["dif"] > -0.3)
    macd_sig = macd_cross | macd_bull

    win = r.get("resonance_window", 3)
    ma_hit, ma_ago = _recent_window(ma_sig, win)
    vol_hit, vol_ago = _recent_window(vol_sig, win)
    macd_hit, macd_ago = _recent_window(macd_sig, win)
    hit = bool(ma_hit and vol_hit and macd_hit)
    reasons: list[str] = []
    if ma_hit:
        reasons.append(f"均线多头/金叉({ma_ago}日前)")
    if vol_hit:
        reasons.append(f"量能放大/金叉({vol_ago}日前)")
    if macd_hit:
        reasons.append(f"MACD金叉/红柱放大({macd_ago}日前)")
    # 三者同步度：都在最近1根最强
    sync = 1 if (ma_ago <= 1 and vol_ago <= 1 and macd_ago <= 1) else 0
    return {"hit": hit, "reasons": reasons, "strength": round(4 + sync * 2 - max(ma_ago, vol_ago, macd_ago) * 0.3, 1)}


def _strategy_four_features(d: pd.DataFrame, r: dict[str, Any]) -> dict[str, Any]:
    """战法3：四特征共振 —— 涨停 + 倍量 + 跳空缺口3日不补 + 多连阳。"""
    if len(d) < 30:
        return {"hit": False, "reasons": []}
    recent = d.tail(r.get("feature_window", 20))
    # 1) 一月内有涨停
    has_limit = bool(recent["limit_up"].any())
    # 2) 持续温和倍量：近20日内出现 >=2倍量
    has_double_vol = bool((recent["volume"] >= recent["vma5"] * 2).any())
    # 3) 向上跳空缺口3日不补：缺口日开盘 > 前日收盘，且后3日最低价 > 缺口日开盘
    gap_ok = False
    gap_idx = [i for i in range(max(1, len(d) - 20), len(d) - 3) if d.iloc[i]["gap_up"]]
    for i in gap_idx:
        gap_open = d.iloc[i]["open"]
        after = d.iloc[i + 1:i + 4]["low"]
        if len(after) >= 3 and (after > gap_open).all():
            gap_ok = True
            break
    # 4) 逆势多连阳：最近出现连续3根以上阳线
    yang = d["is_yang"].tail(10).tolist()
    max_streak = cur_streak = 0
    for v in yang:
        cur_streak = cur_streak + 1 if v else 0
        max_streak = max(max_streak, cur_streak)
    multi_yang = max_streak >= 3
    hit = bool(has_limit and has_double_vol and gap_ok and multi_yang)
    reasons: list[str] = []
    if has_limit:
        reasons.append("近一月内有涨停")
    if has_double_vol:
        reasons.append("出现温和倍量")
    if gap_ok:
        reasons.append("跳空缺口3日未回补")
    if multi_yang:
        reasons.append(f"连续{max_streak}根阳线")
    return {"hit": hit, "reasons": reasons, "strength": round((5 if hit else 0) + max_streak * 0.2, 1)}


def _strategy_dragon_out(d: pd.DataFrame, r: dict[str, Any]) -> dict[str, Any]:
    """战法4：龙出水 —— 长期横盘后放量突破箱体上沿。"""
    if len(d) < 65:
        return {"hit": False, "reasons": []}
    box = r.get("box_days", 40)
    window = d.iloc[-box - 1:-1]  # 不含今日的箱体区间
    box_high = window["high"].max()
    box_low = window["low"].min()
    box_range = (box_high - box_low) / box_low if box_low > 0 else 9
    # 横盘判定：箱体振幅小于阈值
    sideway = box_range <= r.get("box_max_range", 0.35)
    # 均线粘合：MA5/10/20/60 离散度
    mas = d.iloc[-2][["ma5", "ma10", "ma20", "ma60"]].astype(float)
    ma_spread = (mas.max() - mas.min()) / mas.mean() if mas.mean() > 0 else 9
    mashed = ma_spread <= r.get("ma_spread", 0.10)
    # 放量突破箱体上沿
    cur = d.iloc[-1]
    vol_ok = cur["volume"] >= cur["vma20"] * r.get("box_break_vol", 2.0)
    breakout = cur["close"] > box_high
    hit = bool(sideway and breakout and vol_ok)
    reasons: list[str] = []
    if sideway:
        reasons.append(f"横盘{box}日(振幅{box_range * 100:.0f}%)")
    if mashed:
        reasons.append(f"均线粘合({ma_spread * 100:.1f}%)")
    if breakout:
        reasons.append(f"放量突破箱体{box_high:.2f}")
    if vol_ok:
        reasons.append(f"量能{cur['volume'] / cur['vma20']:.1f}倍")
    return {"hit": hit, "reasons": reasons, "strength": round((5 if hit else 0) + min(cur["pct_change"], 10) / 4, 1)}


def _strategy_three_line(d: pd.DataFrame, r: dict[str, Any]) -> dict[str, Any]:
    """战法5：三线底部启动 —— 60日线走平上拐 + 5穿20金叉 + 三线多头。"""
    if len(d) < 61:
        return {"hit": False, "reasons": []}
    cur = d.iloc[-1]
    prev = d.iloc[-2]
    # 60日线由跌转平/上拐：今日MA60 >= 昨日，且5日前 < 今日（之前下行）
    ma60_turn = (cur["ma60"] >= prev["ma60"]) and (d.iloc[-6]["ma60"] <= cur["ma60"])
    # 5穿20金叉（近 window 日内）
    cross = (d["ma5"] > d["ma20"]) & (d["ma5"].shift(1) <= d["ma20"].shift(1))
    cross_hit, cross_ago = _recent_window(cross, r.get("cross_window", 5))
    # 三线多头
    bull = cur["ma5"] > cur["ma20"] > cur["ma60"]
    # 放量站稳60日线
    vol_ok = cur["volume"] >= cur["vma5"] * 1.2
    above60 = cur["close"] > cur["ma60"]
    hit = bool(ma60_turn and cross_hit and bull and above60 and vol_ok)
    reasons: list[str] = []
    if ma60_turn:
        reasons.append("60日线走平上拐")
    if cross_hit:
        reasons.append(f"5穿20金叉({cross_ago}日前)")
    if bull:
        reasons.append("三线多头(5>20>60)")
    if above60:
        reasons.append("站稳60日线")
    if vol_ok:
        reasons.append("温和放量")
    return {"hit": hit, "reasons": reasons, "strength": round((4 if hit else 0) + (5 - cross_ago) * 0.2 if cross_ago >= 0 else 4, 1)}


def _is_limit_up_close(row: pd.Series) -> bool:
    """收盘封板涨停：涨幅达标且收盘价贴近当日最高价。

    比单看 pct_change 更严格，排除盘中触及涨停但收盘回落的长上影线。
    """
    try:
        return bool(row["limit_up"]) and float(row["close"]) >= float(row["high"]) * 0.995
    except Exception:  # noqa: BLE001
        return False


def _strategy_limitup_pullback(d: pd.DataFrame, r: dict[str, Any]) -> dict[str, Any]:
    """战法6：涨停回调·放量启动 —— 涨停（含连板）后缩量洗盘，守住生命线，再放量才提醒。

    两阶段输出（用 stage 区分，便于分别提醒与冷却）：
    - watch：缩量到位、守住生命线，但尚未放量 → 观察提示；
    - trigger：近 1~2 日放量且未破位 → 正式启动提醒。

    硬条件：近 lookback 日内有收盘封板涨停（首板或连板均可）、曾回调、
    回调期间收盘从未跌破最近涨停日最低价、近 shrink_days 缩量。
    """
    lookback = int(r.get("limitup_lookback", 20))
    max_days = int(r.get("pullback_max_days", 8))
    shrink_days = int(r.get("pullback_shrink_days", 3))
    shrink_ratio = float(r.get("pullback_shrink_ratio", 0.7))
    vol_ratio = float(r.get("pullback_vol_ratio", 1.5))
    if len(d) < max(40, lookback + 2, shrink_days + 2):
        return {"hit": False, "reasons": []}

    # 1) 定位最近一次收盘封板涨停（首板或连板均可）
    tail = d.tail(lookback)
    board_pos = next(
        (i for i in range(len(tail) - 1, -1, -1) if _is_limit_up_close(tail.iloc[i])),
        None,
    )
    if board_pos is None:
        return {"hit": False, "reasons": []}
    board_idx = len(d) - lookback + board_pos
    board = d.iloc[board_idx]

    # 连板不淘汰：向前数连续封板的根数，仅用于理由展示与强度微调。
    # 用户明确要求「候选过滤首板那个可以保留连板」，故删除原先“前 lookback 日
    # 出现过涨停即淘汰”的硬过滤。
    streak = 1
    while board_idx - streak >= 0 and _is_limit_up_close(d.iloc[board_idx - streak]):
        streak += 1

    days_since = len(d) - 1 - board_idx
    if days_since < 1 or days_since > max_days:
        return {"hit": False, "reasons": []}

    cur = d.iloc[-1]
    board_low = float(board["low"])
    board_close = float(board["close"])
    board_vol = max(float(board["volume"]), 1.0)

    # 2) 生命线硬条件：涨停后每个交易日收盘都未跌破涨停日最低价，且确实回调过
    after = d.iloc[board_idx + 1:]
    if len(after) == 0 or bool((after["close"] < board_low).any()):
        return {"hit": False, "reasons": []}
    if not bool((after["close"] < board_close).any()):
        return {"hit": False, "reasons": []}

    # 3) 缩量到位：近 shrink_days 日均量明显低于涨停日
    if days_since < shrink_days:
        return {"hit": False, "reasons": []}
    vols = d["volume"].iloc[-shrink_days:].astype(float).tolist()
    avg_vol = sum(vols) / len(vols)
    if avg_vol > board_vol * shrink_ratio:
        return {"hit": False, "reasons": []}

    board_label = f"{streak}连板" if streak > 1 else "首板"
    reasons = [
        f"{days_since}日前{board_label}涨停({float(board['pct_change']):.1f}%)",
        f"缩量至涨停日的{avg_vol / board_vol * 100:.0f}%",
        f"守住涨停日最低价{board_low:.2f}",
    ]

    ma5 = float(cur["ma5"]) if pd.notna(cur.get("ma5")) else 0.0
    ma10 = float(cur["ma10"]) if pd.notna(cur.get("ma10")) else 0.0
    prev_close = float(d.iloc[-2]["close"])
    stabilize = float(cur["close"]) >= prev_close or (ma5 > 0 and float(cur["close"]) > ma5)

    # 4) 放量检测：今日或昨日出现量比达标且守住生命线的交易日
    vol_days: list[tuple[int, float, float]] = []
    for offset in (1, 2):
        if days_since < offset:
            continue
        idx = len(d) - offset
        bar = d.iloc[idx]
        base = d["volume"].iloc[max(0, idx - 5):idx].astype(float)
        base_avg = float(base.mean()) if len(base) >= 3 else avg_vol
        vr = float(bar["volume"]) / max(base_avg, 1.0)
        if vr >= vol_ratio and float(bar["close"]) >= board_low:
            vol_days.append((offset, vr, float(bar["pct_change"])))

    # 5) 未破位确认：现价在生命线之上，且没有跌破 10 日线
    not_break = float(cur["close"]) >= board_low and (ma10 <= 0 or float(cur["close"]) >= ma10)

    if vol_days and not_break:
        ago, vr, chg = vol_days[0]
        reasons.append(f"{'今日' if ago == 1 else '昨日'}放量启动，量比{vr:.1f}(涨幅{chg:+.1f}%)")
        reasons.append(
            "未破位：守住涨停日最低价且站上10日线" if ma10 > 0
            else "未破位：守住涨停日最低价"
        )
        if float(cur["close"]) >= board_close:
            reasons.append("已收复涨停日收盘价")
        strength = 6.0
        strength += min(1.5, max(0.0, (vr - vol_ratio) * 1.0))
        strength += 0.5 if stabilize else 0.0
        strength += max(0.0, 1.0 - abs(days_since - 4) / 8.0)
        return {"hit": True, "stage": "trigger", "reasons": reasons, "strength": round(strength, 1)}

    if stabilize or avg_vol <= board_vol * shrink_ratio * 0.9:
        reasons.append("缩量到位，等待放量确认")
        strength = 3.0 + min(1.0, max(0.0, (1 - avg_vol / board_vol) * 1.5))
        strength += 0.5 if stabilize else 0.0
        return {"hit": True, "stage": "watch", "reasons": reasons, "strength": round(strength, 1)}

    return {"hit": False, "reasons": []}


def _volume_ratio_at(d: pd.DataFrame, idx: int, window: int = 5) -> float:
    """某根 K 线相对其前 window 日均量的量比（不含当日）。"""
    base = d["volume"].iloc[max(0, idx - window):idx].astype(float)
    if len(base) < 3:
        return 1.0
    try:
        return float(d["volume"].iloc[idx]) / max(float(base.mean()), 1.0)
    except Exception:  # noqa: BLE001
        return 1.0


def _strategy_strong_continue(d: pd.DataFrame, r: dict[str, Any]) -> dict[str, Any]:
    """战法7：强势续攻 —— 涨停后缩量整理或不回头续板，守住生命线，放量再启动即提醒。

    与「涨停回调」的区别：用户明确「回调即缩量」，不要求价格跌破涨停收盘价。
    两种启动形态（满足其一，且只在启动转折当日提醒一次，天然去重）：
    - 缩量转折：涨停后 1~max_days 日内出现「昨日缩量、今日放量」；
    - 放量加速：涨停后 1~2 日直接放量续板并创收盘新高（连续板不回头）。

    生命线：涨停后每个交易日收盘都未跌破涨停日最低价；现价站上 10 日线。
    """
    lookback = int(r.get("limitup_lookback", 20))
    max_days = int(r.get("strong_max_days", 10))
    vol_ratio = float(r.get("strong_vol_ratio", 1.4))
    shrink_ratio = float(r.get("strong_shrink_ratio", 1.0))
    accel_ratio = float(r.get("strong_accel_ratio", 2.0))
    if len(d) < 45:
        return {"hit": False, "reasons": []}

    tail = d.tail(lookback)
    board_pos = next(
        (i for i in range(len(tail) - 1, -1, -1) if _is_limit_up_close(tail.iloc[i])),
        None,
    )
    if board_pos is None:
        return {"hit": False, "reasons": []}
    board_idx = len(d) - lookback + board_pos
    days_since = len(d) - 1 - board_idx
    if days_since < 1 or days_since > max_days:
        return {"hit": False, "reasons": []}

    board = d.iloc[board_idx]
    board_low = float(board["low"])
    board_close = float(board["close"])
    streak = 1
    while board_idx - streak >= 0 and _is_limit_up_close(d.iloc[board_idx - streak]):
        streak += 1

    after = d.iloc[board_idx + 1:]
    if len(after) == 0 or bool((after["close"] < board_low).any()):
        return {"hit": False, "reasons": []}

    idx = len(d) - 1
    cur = d.iloc[idx]
    close = float(cur["close"])
    if close < board_low:
        return {"hit": False, "reasons": []}
    ma10 = float(cur["ma10"]) if pd.notna(cur.get("ma10")) else 0.0
    if ma10 > 0 and close < ma10:
        return {"hit": False, "reasons": []}

    vr = _volume_ratio_at(d, idx)
    prev_vr = _volume_ratio_at(d, idx - 1) if idx - 1 >= 0 else 1.0
    board_label = f"{streak}连板" if streak > 1 else "首板"
    reasons: list[str] = []
    mode = ""
    # 情形 A：缩量整理后放量转折（今日放量、昨日缩量）
    if idx > board_idx and vr >= vol_ratio and prev_vr <= shrink_ratio:
        mode = "缩量转折"
        reasons = [
            f"{days_since}日前{board_label}涨停({float(board['pct_change']):.1f}%)",
            f"昨日缩量(量比{prev_vr:.2f})，今日放量(量比{vr:.1f})",
            f"守住涨停日最低价{board_low:.2f}",
        ]
    # 情形 B：涨停后 1~2 日直接放量续板并创收盘新高（不回头的连续板）
    elif 1 <= days_since <= 2 and vr >= accel_ratio:
        close_max = float(d["close"].iloc[max(0, idx - 20):idx].max())
        if close > close_max:
            mode = "放量加速"
            reasons = [
                f"{days_since}日前{board_label}涨停，今日放量续攻(量比{vr:.1f})",
                "收盘创近20日新高，未回头",
                f"守住涨停日最低价{board_low:.2f}",
            ]
    if not mode:
        return {"hit": False, "reasons": []}

    reasons.append("未破位：守住涨停日最低价且站上10日线" if ma10 > 0
                   else "未破位：守住涨停日最低价")
    if close >= board_close:
        reasons.append("已站上涨停日收盘价")
    strength = 6.5 if mode == "缩量转折" else 6.0
    strength += min(1.5, max(0.0, (vr - vol_ratio) * 0.8))
    return {"hit": True, "stage": "trigger", "reasons": reasons, "strength": round(strength, 1)}


def recent_a_kill(d: pd.DataFrame, r: dict[str, Any]) -> bool:
    """买入过滤：近 N 个交易日内（含今日）出现过「A杀」→ 不碰。

    A杀定义（冲高-跳水长上影）：当日盘中冲高 ≥ a_kill_high_pct%（默认5%），
    且收盘较盘中高点回落 ≥ a_kill_fall_pct%（默认6%）——两者都相对昨收。
    两条件同时成立意味着收盘涨幅 ≤ -1%，即冲高后大幅跳水。
    检查窗口 = a_kill_days（默认3）个交易日内（含今日）出现过 A杀日。
    """
    days = max(0, int(r.get("a_kill_days", 3) or 0))
    hp_th = float(r.get("a_kill_high_pct", 5.0) or 5.0)
    fp_th = float(r.get("a_kill_fall_pct", 6.0) or 6.0)
    if days <= 0 or len(d) < 2:
        return False
    tail = min(len(d), days + 3)  # 多取2行保证 shift(1) 的昨收可用
    sub = d.iloc[-tail:]
    pc = sub["close"].shift(1)
    hp = (sub["high"] / pc - 1) * 100        # 盘中冲高幅度
    fall = (sub["high"] - sub["close"]) / pc * 100  # 收盘距高点回落幅度
    kill = (hp >= hp_th) & (fall >= fp_th)
    return bool(kill.iloc[-(days + 1):].fillna(False).any())


def _strategy_wash_reversal(d: pd.DataFrame, r: dict[str, Any]) -> dict[str, Any]:
    """战法8：洗盘反包 —— 大跌/跌停洗盘后首板反包，抓连板股启动初期。

    背景：连板股常在启动前有跌停洗盘（如万向德农 8/21 跌停 → 8/25 首板反包），
    「强势续攻」要求涨停次日才提醒、且连板中途不重复，会漏掉这种反包首板。
    形态：近 lookback 日内出现过大跌（跌幅≥wash_drop_pct），今日涨停且收盘价
    已收复洗盘日收盘价（反包），并且昨日不是涨停（避开连板中途刷屏）。
    """
    lookback = int(r.get("wash_lookback", 8))
    drop_limit = float(r.get("wash_drop_pct", 7.0))
    if len(d) < 45:
        return {"hit": False, "reasons": []}
    idx = len(d) - 1
    cur = d.iloc[idx]
    if not _is_limit_up_close(cur):
        return {"hit": False, "reasons": []}
    close = float(cur["close"])
    vr = _volume_ratio_at(d, idx)
    # 近 lookback 日内（不含今日）出现大跌洗盘
    start = max(0, idx - lookback)
    seg = d.iloc[start:idx]
    if seg.empty:
        return {"hit": False, "reasons": []}
    min_pct = float(seg["pct_change"].min())
    if min_pct > -drop_limit:
        return {"hit": False, "reasons": []}
    wash_loc = seg["pct_change"].idxmin()
    wash_close = float(d.loc[wash_loc, "close"])
    wash_date = str(d.loc[wash_loc, "date"])[:10]
    # 反包：今日收盘价收复洗盘日收盘价
    if close < wash_close:
        return {"hit": False, "reasons": []}
    # 连板去重：昨日也是涨停则不是"首板反包"，跳过（避免连板中途刷屏）
    if idx >= 1 and _is_limit_up_close(d.iloc[idx - 1]):
        return {"hit": False, "reasons": []}
    ma10 = float(cur["ma10"]) if pd.notna(cur.get("ma10")) else 0.0
    reasons = [
        f"{wash_date[5:]}洗盘大跌{min_pct:.1f}%（收{wash_close:.2f}）",
        f"今日涨停反包，收{close:.2f}收复洗盘跌幅，量比{vr:.2f}",
    ]
    if ma10 > 0 and close >= ma10:
        reasons.append("收盘站上10日线，洗盘后重新转强")
    strength = 7.0 + min(1.0, max(0.0, (vr - 1.0) * 0.5))
    return {"hit": True, "stage": "trigger", "reasons": reasons, "strength": round(strength, 1)}


# ============================= 下跌 / 离场战法 =============================

def _strategy_dual_death_cross(d: pd.DataFrame, r: dict[str, Any]) -> dict[str, Any]:
    """跌法1：双线死叉 —— 5日均线在20日线下方（死叉状态），高位形成更可靠。"""
    if len(d) < 25:
        return {"hit": False, "reasons": []}
    cross = (d["ma5"] < d["ma20"]) & (d["ma5"].shift(1) >= d["ma20"].shift(1))
    win = r.get("death_cross_window", 5)
    hit_cross, ago = _recent_window(cross, win)
    cur = d.iloc[-1]
    # 当前处于死叉状态（5日线在20日线下方）即视为进入下跌模式
    in_death = cur["ma5"] < cur["ma20"]
    # 相对高位：近60日价格分位
    high_pos = d["close"].tail(60).rank(pct=True).iloc[-1] if len(d) >= 20 else 1.0
    reasons: list[str] = []
    if in_death:
        reasons.append("5日线位于20日线下方(死叉状态)")
    if hit_cross:
        reasons.append(f"死叉形成于{ago}日前")
    if high_pos >= 0.6:
        reasons.append(f"相对高位(近60日{high_pos * 100:.0f}%分位)")
    # 命中：处于死叉状态，且(刚死叉 或 相对高位)
    hit = bool(in_death and (hit_cross or high_pos >= r.get("death_high_pct", 0.5)))
    fresh_bonus = (win - min(ago, win)) * 0.3 if hit_cross else 0
    return {"hit": hit, "reasons": reasons,
            "strength": round((4 if hit else 0) + fresh_bonus + high_pos * 2, 1)}


def _strategy_triple_death_cross(d: pd.DataFrame, r: dict[str, Any]) -> dict[str, Any]:
    """跌法2：三死叉见顶 —— 均线死叉 + 均量死叉 + MACD死叉 同步。"""
    if len(d) < 30:
        return {"hit": False, "reasons": []}
    ma_cross = (d["ma5"] < d["ma10"]) & (d["ma5"].shift(1) >= d["ma10"].shift(1))
    vol_cross = (d["vma5"] < d["vma10"]) & (d["vma5"].shift(1) >= d["vma10"].shift(1))
    macd_cross = (d["dif"] < d["dea"]) & (d["dif"].shift(1) >= d["dea"].shift(1))
    win = r.get("triple_death_window", 5)
    ma_hit, ma_ago = _recent_window(ma_cross, win)
    vol_hit, vol_ago = _recent_window(vol_cross, win)
    macd_hit, macd_ago = _recent_window(macd_cross, win)
    reasons: list[str] = []
    if ma_hit:
        reasons.append(f"均线死叉({ma_ago}日前)")
    if vol_hit:
        reasons.append(f"均量死叉({vol_ago}日前)")
    if macd_hit:
        reasons.append(f"MACD死叉({macd_ago}日前)")
    # 严格三死叉：三者均在窗口内同步
    strict = ma_hit and vol_hit and macd_hit
    hit = bool(strict)
    fresh = (win - max(ma_ago, vol_ago, macd_ago)) * 0.3 if strict else 0
    return {"hit": hit, "reasons": reasons,
            "strength": round((6 if hit else 0) + fresh, 1)}


def _strategy_lifeline_break(d: pd.DataFrame, r: dict[str, Any]) -> dict[str, Any]:
    """跌法3：生命线破位 —— 收盘有效跌破20/60日线且站不回。"""
    if len(d) < 61:
        return {"hit": False, "reasons": []}
    cur = d.iloc[-1]
    prev = d.iloc[-2]
    reasons: list[str] = []
    # 20日线生命线：今日收盘跌破 且 昨日收盘在上方（首次破位）
    broken20 = (cur["close"] < cur["ma20"]) and (prev["close"] >= prev["ma20"])
    # 或已连续2日站不回
    fail_back = (cur["close"] < cur["ma20"]) and (prev["close"] < prev["ma20"])
    broken60 = (cur["close"] < cur["ma60"]) and (prev["close"] >= prev["ma60"])
    if broken20:
        reasons.append("跌破20日生命线")
    elif fail_back:
        reasons.append("连续2日站不回20日线")
    if broken60:
        reasons.append("跌破60日趋势线")
    # 放量下跌增强信号（加分项，非必要条件——缩量破位同样提示风险）
    vol_down = cur["volume"] >= cur["vma5"] * r.get("break_vol", 1.5) and cur["close"] < cur["open"]
    if vol_down:
        reasons.append(f"放量下跌{cur['volume'] / cur['vma5']:.1f}倍")
    broken = broken20 or fail_back or broken60
    hit = bool(broken)
    strength = (5 if hit else 0) + (2 if broken60 else 0) + (1 if vol_down else 0)
    return {"hit": hit, "reasons": reasons, "strength": round(strength, 1)}


def _strategy_macd_death_divergence(d: pd.DataFrame, r: dict[str, Any]) -> dict[str, Any]:
    """跌法4：MACD高位死叉 / 顶背离。"""
    if len(d) < 35:
        return {"hit": False, "reasons": []}
    cur = d.iloc[-1]
    recent = d.tail(r.get("divergence_window", 30))
    # 死叉：DIF在DEA下方（死叉状态）；刚发生则记录天数
    death = (d["dif"] < d["dea"]) & (d["dif"].shift(1) >= d["dea"].shift(1))
    death_hit, death_ago = _recent_window(death, r.get("death_cross_window", 5))
    in_death = cur["dif"] < cur["dea"]
    # 零轴上方死叉 = 高位死叉（更可靠）；零轴下方死叉强度打折
    high_death = in_death and cur["dif"] > 0
    low_death = in_death and cur["dif"] <= 0
    # 顶背离：价格创近30日新高，但 MACD 红柱低于上一次新高时
    price_new_high = cur["close"] >= recent["close"].iloc[:-1].max()
    macd_fade = cur["macd"] < recent["macd"].max() * 0.6
    divergence = bool(price_new_high and macd_fade and cur["macd"] < d["macd"].iloc[-2])
    reasons: list[str] = []
    if high_death:
        reasons.append("MACD零轴上方死叉(高位)")
        if death_hit:
            reasons.append(f"死叉形成于{death_ago}日前")
    if low_death:
        reasons.append("MACD零轴下方死叉(弱势延续)")
    if divergence:
        reasons.append("股价新高但MACD红柱萎缩(顶背离)")
    # 高位死叉或顶背离即命中；零轴下方死叉（弱势延续）也提示但不算强信号
    hit = bool(high_death or divergence)
    return {"hit": hit, "reasons": reasons, "strength": round((5 if high_death else 0) + (1 if divergence else 0), 1)}


def _strategy_volume_price_divergence(d: pd.DataFrame, r: dict[str, Any]) -> dict[str, Any]:
    """跌法5：量价背离 / 缩量新高。"""
    if len(d) < 25:
        return {"hit": False, "reasons": []}
    cur = d.iloc[-1]
    win = r.get("divergence_window", 30)
    recent = d.tail(win)
    # 股价创近30日新高
    new_high = cur["close"] >= recent["close"].iloc[:-1].max()
    # 但成交量未同步放大：量比<1 或低于近期高量
    vol_shrink = cur["volume"] < cur["vma5"] * 0.9
    vol_below_peak = cur["volume"] < recent["volume"].max() * 0.6
    reasons: list[str] = []
    if new_high:
        reasons.append("股价创近期新高")
    if vol_shrink:
        reasons.append(f"缩量(量比{cur['volume'] / cur['vma5']:.2f})")
    if vol_below_peak:
        reasons.append("量能远低于近期峰值")
    hit = bool(new_high and (vol_shrink or vol_below_peak))
    return {"hit": hit, "reasons": reasons, "strength": round((4 if hit else 0) + (1 if vol_below_peak else 0), 1)}


# (战法名, 函数, 简述, 方向)  方向: up=上涨启动 / down=下跌离场
STRATEGIES = [
    ("量价突破", _strategy_volume_breakout, "经典：均线多头 + 放量突破前高", "up"),
    ("三重共振", _strategy_triple_resonance, "稳健：均线 + 量能 + MACD 三者同步", "up"),
    ("四特征共振", _strategy_four_features, "严格：涨停 + 倍量 + 缺口 + 连阳", "up"),
    ("龙出水", _strategy_dragon_out, "长期横盘后放量突破箱体", "up"),
    ("三线底部启动", _strategy_three_line, "提前：60日线拐头 + 5穿20 + 三线多头", "up"),
    ("涨停回调", _strategy_limitup_pullback, "涨停（含连板）后缩量洗盘守住生命线，放量再启动才提醒", "up"),
    ("强势续攻", _strategy_strong_continue, "涨停后缩量整理或不回头续板，守住生命线即放量提醒", "up"),
    ("洗盘反包", _strategy_wash_reversal, "大跌/跌停洗盘后首板反包，抓连板股启动初期", "up"),
    ("双线死叉", _strategy_dual_death_cross, "5日下穿20日死叉，高位离场", "down"),
    ("三死叉见顶", _strategy_triple_death_cross, "均线+均量+MACD三死叉共振", "down"),
    ("生命线破位", _strategy_lifeline_break, "放量跌破20/60日线且站不回", "down"),
    ("MACD高位死叉", _strategy_macd_death_divergence, "零轴上方死叉或顶背离", "down"),
    ("量价背离", _strategy_volume_price_divergence, "股价新高但量能萎缩", "down"),
]


def analyze_stock(code: str, strategy_rules: dict[str, Any],
                  spot_names: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """对单只股票执行全部战法，返回命中的战法信号列表。"""
    hist = ds.stock_history(code)
    if hist is None or len(hist) < 30:
        return []
    name = _name_of(code, spot_names if spot_names is not None else _spot_name_map())
    d = _indicators(hist, code, name)
    out: list[dict[str, Any]] = []
    cur = d.iloc[-1]
    for sname, fn, _desc, direction in STRATEGIES:
        try:
            res = fn(d, strategy_rules)
        except Exception:  # noqa: BLE001
            continue
        if not res.get("hit"):
            continue
        stage = res.get("stage", "")
        stage_label = {"trigger": "放量启动", "watch": "观察中"}.get(stage, "")
        out.append({
            "code": code,
            "name": name,
            "strategy": sname,
            "direction": direction,
            "stage": stage,
            "stage_label": stage_label,
            "date": str(cur["date"]),
            "price": round(float(cur["close"]), 2),
            "change_pct": round(float(cur["pct_change"]), 2),
            "reasons": res["reasons"],
            "signal": f"{sname}{('·' + stage_label) if stage_label else ''}：{' + '.join(res['reasons'])}",
            "strength": res.get("strength", 0),
            "type": "上涨信号" if direction == "up" else "下跌信号",
        })
    out.sort(key=lambda x: x["strength"], reverse=True)
    return out


def scan_watchlist(watchlist: list[str], strategy_rules: dict[str, Any] | None = None,
                   enabled: dict[str, bool] | None = None) -> list[dict[str, Any]]:
    """扫描标的池，返回所有战法命中信号。

    enabled: 战法开关字典，如 {"量价突破": True, ...}，None 表示全部启用。
    """
    rules = strategy_rules or {}
    names = _spot_name_map()
    result: list[dict[str, Any]] = []
    for code in watchlist:
        sigs = analyze_stock(code, rules, names)
        for s in sigs:
            if enabled is not None and not enabled.get(s["strategy"], False):
                continue
            result.append(s)
    result.sort(key=lambda x: x["strength"], reverse=True)
    return result


def strategy_catalog() -> list[dict[str, str]]:
    """战法说明清单（供前端展示），含上涨与下跌两类。"""
    return [
        {"key": sname, "name": sname, "desc": desc, "direction": direction,
         "detail": _STRATEGY_DETAILS.get(sname, desc)}
        for sname, _fn, desc, direction in STRATEGIES
    ]


_STRATEGY_DETAILS = {
    "量价突破": "5/10/20/60日均线多头排列且向上，当日成交量≥5日均量1.5倍，收盘突破近20日高点。信号直接，可能追高。",
    "三重共振": "3个交易日内，均线多头/金叉、量能放大/金叉、MACD金叉/红柱放大三者同步。误报少，略滞后。",
    "四特征共振": "近一月涨停 + 温和倍量 + 向上跳空缺口3日不补 + 连续3根以上阳线，四条齐备。信号少而精。",
    "龙出水": "箱体横盘≥40日(振幅≤35%)、均线粘合，当日放量≥20日均量2倍并突破箱体上沿。爆发力强。",
    "三线底部启动": "60日线由跌转平上拐，5日线上穿20日金叉，形成5>20>60多头并站稳60日线。抓底部反转。",
    "涨停回调": "分两步提醒：① 观察中——首板涨停后1~8日缩量洗盘（近3日均量≤涨停日70%），且始终守住涨停日最低价；② 放量启动——今日或昨日量比≥1.5且未跌破涨停日最低价、收盘站上10日线，才发正式买点提醒。跌破涨停日最低价则停止跟踪。阈值均可在配置页调整。",
    "强势续攻": "抓涨停后不深调的主升浪：涨停（含连板）后价格不必跌破涨停收盘价（回调即缩量），守住生命线。两种启动形态只在转折当日提醒一次：① 缩量转折——昨日量比≤1.0、今日量比≥1.2（抓缩量后首波启动，如会稽山8/24）；② 放量加速——涨停后1~2日量比≥2.0且收盘创近20日新高（连续板不回头）。需未跌破涨停日最低价并站上10日线。",
    "洗盘反包": "抓连板股启动初期的反包首板：近8日内出现过大跌/跌停洗盘（跌幅≥7%），今日涨停且收盘价收复洗盘日收盘价（反包），昨日非涨停（避开连板中途刷屏）。如万向德农8/21跌停洗盘、8/25首板反包。",
    "双线死叉": "5日均线从上往下击穿20日均线形成死叉，且股价处于近60日相对高位。短线资金出逃、多头行情结束，止盈离场信号。震荡市均线缠绕时会有假信号。",
    "三死叉见顶": "均线死叉(5穿10) + 均量线死叉(5穿10) + MACD死叉(DIF穿DEA) 三者3日内同步出现，高位共振见顶准确率较高，信号略滞后。",
    "生命线破位": "收盘有效跌破20日生命线或60日趋势线，且连续站不回；配合放量下跌增强信号。中期趋势走坏，无条件离场，防止深套。",
    "MACD高位死叉": "DIF在零轴上方下穿DEA形成高位死叉，或股价创新高但MACD红柱明显萎缩形成顶背离。上涨动能衰竭警示。",
    "量价背离": "股价创近30日新高，但成交量萎缩（低于5日均量或远低于近期峰值）。上涨缺乏增量资金推动，头部先行信号。",
}
