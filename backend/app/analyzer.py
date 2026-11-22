"""盯盘异动分析引擎。

基于实时行情做结构化筛选，不产生投资建议：
- 个股异动：涨跌幅 / 量比 / 换手率 / 涨速 / 振幅
- 板块异常：行业与概念板块涨跌幅、上涨下跌家数、领涨股
- 龙虎榜：上榜个股净买入排行
- 盘口异动：逐笔大单/快速拉抬等（若接口可用）
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from . import datasource as ds
from .config import AlertRules


# 列名兼容映射：不同 AKShare 版本列名略有差异
COLUMN_ALIASES = {
    "code": ["代码", "股票代码", "symbol"],
    "name": ["名称", "股票名称", "简称"],
    "price": ["最新价", "现价", "收盘价"],
    "change_pct": ["涨跌幅", "涨跌幅(%)"],
    "change_amount": ["涨跌额"],
    "volume": ["成交量"],
    "amount": ["成交额"],
    "amplitude": ["振幅"],
    "high": ["最高"],
    "low": ["最低"],
    "open": ["今开"],
    "prev_close": ["昨收"],
    "volume_ratio": ["量比"],
    "turnover": ["换手率"],
    "speed": ["涨速"],
    "change_5min": ["5分钟涨跌"],
    "change_60day": ["60日涨跌幅"],
    "ytd": ["年初至今涨跌幅"],
    "pe": ["市盈率-动态", "市盈率"],
    "pb": ["市净率"],
    "market_cap": ["总市值"],
    "float_cap": ["流通市值"],
    "board_name": ["板块名称", "名称"],
    "board_code": ["板块代码", "代码"],
    "up_count": ["上涨家数"],
    "down_count": ["下跌家数"],
    "leader": ["领涨股票"],
    "leader_pct": ["领涨股票-涨跌幅"],
    "leader_code": ["领涨股票-代码"],
    "rank": ["排名"],
    "net_buy": ["净买入额", "龙虎榜净买入额", "净买入"],
}


def _col(df: pd.DataFrame, key: str) -> str | None:
    """在 DataFrame 中找到列的实际名称"""
    for alias in COLUMN_ALIASES.get(key, [key]):
        if alias in df.columns:
            return alias
    return None


def _num(value: Any) -> float:
    """数值解析，兼容 '1.48亿' / '7588.54万' / '-571.77万' 等中文单位字符串"""
    try:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return 0.0
        if isinstance(value, str):
            s = value.strip().replace(",", "")
            if not s or s in ("-", "--"):
                return 0.0
            multiplier = 1.0
            if s.endswith("亿"):
                multiplier, s = 1e8, s[:-1]
            elif s.endswith("万"):
                multiplier, s = 1e4, s[:-1]
            elif s.endswith("%"):
                s = s[:-1]
            return float(s) * multiplier
        return float(value)
    except (ValueError, TypeError):
        return 0.0


def _to_float(df: pd.DataFrame, key: str) -> pd.Series:
    col = _col(df, key)
    if col is None:
        return pd.Series([0.0] * len(df), index=df.index)
    return pd.to_numeric(df[col], errors="coerce").fillna(0)


# ----------------------------- 个股异动 -----------------------------

def stock_anomalies(rules: AlertRules, watchlist: list[str] | None = None) -> list[dict[str, Any]]:
    """扫描个股异动，返回按异动强度排序的列表。"""
    df = ds.stock_spot()
    if df is None or df.empty:
        return []

    # 筛选范围
    codes_col = _col(df, "code")
    if codes_col is None:
        return []
    if watchlist and rules.watchlist_only:
        df = df[df[codes_col].astype(str).isin(watchlist)].copy()
    if df.empty:
        return []

    name_col = _col(df, "name")
    change = _to_float(df, "change_pct")
    vol_ratio = _to_float(df, "volume_ratio")
    turnover = _to_float(df, "turnover")
    speed = _to_float(df, "speed")
    amplitude = _to_float(df, "amplitude")
    amount = _to_float(df, "amount")

    threshold = rules.price_change_pct
    results: list[dict[str, Any]] = []

    for idx, row in df.iterrows():
        chg = _num(change.loc[idx])
        vr = _num(vol_ratio.loc[idx])
        tr = _num(turnover.loc[idx])
        sp = _num(speed.loc[idx])
        amp = _num(amplitude.loc[idx])
        reasons: list[str] = []
        score = 0.0

        if abs(chg) >= threshold:
            direction = "大涨" if chg > 0 else "大跌"
            reasons.append(f"{direction}{abs(chg):.2f}%")
            score += min(abs(chg) / threshold, 5) * 30
        if vr >= rules.volume_ratio:
            reasons.append(f"放量{vr:.2f}倍")
            score += min(vr / rules.volume_ratio, 5) * 15
        if tr >= 15:
            reasons.append(f"高换手{tr:.1f}%")
            score += min(tr / 15, 3) * 10
        if abs(sp) >= 0.5:
            reasons.append(f"涨速{sp:+.2f}%")
            score += min(abs(sp), 3) * 8
        if amp >= 8:
            reasons.append(f"振幅{amp:.1f}%")
            score += min(amp / 8, 3) * 5

        if not reasons:
            continue

        results.append({
            "code": str(row.get(codes_col, "")),
            "name": str(row.get(name_col, "")) if name_col else "",
            "price": _num(row.get(_col(df, "price"), 0)),
            "change_pct": round(chg, 2),
            "volume_ratio": round(vr, 2),
            "turnover": round(tr, 2),
            "speed": round(sp, 2),
            "amplitude": round(amp, 2),
            "amount": round(_num(amount.loc[idx]), 2),
            "reasons": reasons,
            "signal": " / ".join(reasons),
            "score": round(score, 1),
            "direction": "up" if chg > 0 else ("down" if chg < 0 else "flat"),
            "type": "个股异动",
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results


def fund_flow_anomalies(rules: AlertRules, watchlist: list[str] | None = None) -> list[dict[str, Any]]:
    """主力资金流异动。"""
    df = ds.individual_fund_flow()
    if df is None or df.empty:
        return []

    # 找代码列与净流入列
    code_col = _col(df, "code")
    name_col = _col(df, "name")
    if code_col is None:
        return []

    # 同花顺接口代码列可能为整数（509 -> 应补零为 000509）
    if not pd.api.types.is_string_dtype(df[code_col]):
        df = df.copy()
        df[code_col] = df[code_col].apply(lambda v: str(int(v)).zfill(6) if pd.notna(v) else "")

    # 资金流列名形态：
    # 东财: 主力净流入-净额 / 主力净流入-净占比
    # 同花顺: 净额 / 资金流入净额 / 净流入
    net_col = None
    pct_col = None
    for c in df.columns:
        s = str(c)
        if "主力净流入" in s and "净额" in s:
            net_col = c
        if "主力净流入" in s and "净占比" in s:
            pct_col = c
    if net_col is None:
        for preferred in ("净额", "资金流入净额", "净流入", "主力净额"):
            if preferred in df.columns:
                net_col = preferred
                break
    if net_col is None:
        for c in df.columns:
            if "净额" in str(c) or "净流入" in str(c):
                net_col = c
                break
    if net_col is None:
        return []

    df = df.copy()
    # 同花顺净额是带单位的字符串，需解析
    sample = df[net_col].dropna().head(1)
    if len(sample) and isinstance(sample.iloc[0], str):
        df["_net"] = df[net_col].apply(_num)
    else:
        df["_net"] = pd.to_numeric(df[net_col], errors="coerce").fillna(0)
    df = df[df["_net"].abs() >= rules.fund_flow_amount]
    if watchlist and rules.watchlist_only:
        df = df[df[code_col].astype(str).isin(watchlist)]

    out: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        net = _num(row["_net"])
        out.append({
            "code": str(row.get(code_col, "")),
            "name": str(row.get(name_col, "")) if name_col else "",
            "net_flow": round(net, 2),
            "net_pct": round(_num(row.get(pct_col, 0)), 2) if pct_col else None,
            "signal": f"主力{'净流入' if net > 0 else '净流出'}{abs(net) / 1e8:.2f}亿",
            "score": round(min(abs(net) / rules.fund_flow_amount, 10) * 20, 1),
            "direction": "in" if net > 0 else "out",
            "type": "资金异动",
        })
    out.sort(key=lambda x: abs(x["net_flow"]), reverse=True)
    return out[:50]


# ----------------------------- 板块异常 -----------------------------

def _board_records(df: pd.DataFrame | None, board_type: str, rules: AlertRules) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    name_col = _col(df, "board_name")
    code_col = _col(df, "board_code")
    if name_col is None:
        return []

    change = _to_float(df, "change_pct")
    turnover = _to_float(df, "turnover")
    up = _to_float(df, "up_count")
    down = _to_float(df, "down_count")

    out: list[dict[str, Any]] = []
    for idx, row in df.iterrows():
        chg = _num(change.loc[idx])
        if abs(chg) < rules.sector_change_pct:
            continue
        leader_col = _col(df, "leader")
        leader_pct_col = _col(df, "leader_pct")
        out.append({
            "board_name": str(row.get(name_col, "")),
            "board_code": str(row.get(code_col, "")) if code_col else "",
            "board_type": board_type,
            "change_pct": round(chg, 2),
            "turnover": round(_num(turnover.loc[idx]), 2),
            "up_count": int(_num(up.loc[idx])),
            "down_count": int(_num(down.loc[idx])),
            "leader": str(row.get(leader_col, "")) if leader_col else "",
            "leader_pct": round(_num(row.get(leader_pct_col, 0)), 2) if leader_pct_col else 0,
            "signal": f"{'板块大涨' if chg > 0 else '板块大跌'}{abs(chg):.2f}%",
            "score": round(min(abs(chg) / rules.sector_change_pct, 6) * 25, 1),
            "direction": "up" if chg > 0 else "down",
            "type": "板块异常",
        })
    return out


def sector_anomalies(rules: AlertRules) -> list[dict[str, Any]]:
    industry = _board_records(ds.industry_boards(), "行业", rules)
    concept = _board_records(ds.concept_boards(), "概念", rules)
    # 板块异动详情（若有），仅作为附加信息不重复计数
    result = industry + concept
    result.sort(key=lambda x: x["score"], reverse=True)
    return result


def sector_board_overview(rules: AlertRules, board_type: str = "行业") -> list[dict[str, Any]]:
    """板块全景（含未触发阈值的板块）用于列表展示。"""
    df = ds.industry_boards() if board_type == "行业" else ds.concept_boards()
    if df is None or df.empty:
        return []
    name_col = _col(df, "board_name")
    if name_col is None:
        return []
    change = _to_float(df, "change_pct")
    out: list[dict[str, Any]] = []
    for idx, row in df.iterrows():
        chg = _num(change.loc[idx])
        leader_col = _col(df, "leader")
        out.append({
            "board_name": str(row.get(name_col, "")),
            "board_code": str(row.get(_col(df, "board_code"), "")) if _col(df, "board_code") else "",
            "board_type": board_type,
            "change_pct": round(chg, 2),
            "turnover": round(_num(_to_float(df, "turnover").loc[idx]), 2),
            "up_count": int(_num(_to_float(df, "up_count").loc[idx])),
            "down_count": int(_num(_to_float(df, "down_count").loc[idx])),
            "leader": str(row.get(leader_col, "")) if leader_col else "",
            "abnormal": abs(chg) >= rules.sector_change_pct,
        })
    out.sort(key=lambda x: x["change_pct"], reverse=True)
    return out


# ----------------------------- 龙虎榜 -----------------------------

def dragon_tiger(rules: AlertRules, start_date: str | None = None,
                 end_date: str | None = None) -> list[dict[str, Any]]:
    """龙虎榜上榜个股，按净买入额过滤。"""
    df = ds.dragon_tiger_list(start_date, end_date)
    if df is None or df.empty:
        return []

    code_col = _col(df, "code")
    name_col = _col(df, "name")
    if code_col is None:
        return []

    # 净买入列识别（AKShare 实测列名：龙虎榜净买额）
    net_col = None
    for preferred in ("龙虎榜净买额", "净买入额", "净买额", "净买入", "净额"):
        if preferred in df.columns:
            net_col = preferred
            break
    if net_col is None:
        for c in df.columns:
            if "净买" in str(c):
                net_col = c
                break

    out: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        net = _num(row.get(net_col, 0)) if net_col else 0.0
        # 无净额数据时不过滤；有则按阈值
        if net_col and abs(net) < rules.lhb_net_buy:
            continue
        signal = ""
        if net_col:
            signal = f"净买入{net / 1e8:.2f}亿" if net > 0 else f"净卖出{abs(net) / 1e8:.2f}亿"
        out.append({
            "code": str(row.get(code_col, "")),
            "name": str(row.get(name_col, "")) if name_col else "",
            "net_buy": round(net, 2),
            "reason": str(row.get("上榜原因", "")) if "上榜原因" in df.columns else "",
            "signal": signal,
            "score": round(min(abs(net) / max(rules.lhb_net_buy, 1), 10) * 10, 1),
            "direction": "in" if net > 0 else "out",
            "type": "龙虎榜",
            "raw": {str(k): (str(v) if pd.notna(v) else None) for k, v in row.items()},
        })
    out.sort(key=lambda x: abs(x["net_buy"]), reverse=True)
    return out[:80]


# ----------------------------- 盘口异动 -----------------------------

def intraday_changes(limit: int = 50) -> list[dict[str, Any]]:
    df = ds.stock_changes()
    return ds.df_to_records(df, limit=limit)


def limit_up_stocks(trade_date: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    df = ds.limit_up_pool(trade_date)
    return ds.df_to_records(df, limit=limit)


# ----------------------------- 汇总 -----------------------------

def all_signals(rules: AlertRules, watchlist: list[str] | None = None) -> list[dict[str, Any]]:
    """汇总全部信号，供消息推送使用。"""
    result: list[dict[str, Any]] = []
    result.extend(stock_anomalies(rules, watchlist))
    result.extend(fund_flow_anomalies(rules, watchlist))
    result.extend(sector_anomalies(rules))
    result.extend(dragon_tiger(rules))
    result.sort(key=lambda x: x.get("score", 0), reverse=True)
    return result
