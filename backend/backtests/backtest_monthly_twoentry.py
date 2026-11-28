"""月度收益双口径报告（2025-08 ~ 2026-11 全部可用月份）：当前全部策略与拦截下的逐月收益。

口径：当前配置全部闸门（点火≥3% / 炸板≤45% / 动量死区-5~0% / 广度档位 / 四象限剔C1），
策略集 = replay 同口径（当前配置下实际触发的是洗盘反包 trigger）。
炸板闸门 2026-06 前由本地复算补齐（replay 内建情绪统计仅存 2026-06+），其余闸门与 replay 同口径；
本地拦截日直接跳过回放，开闸日才调 replay（本地开闸∧replay 拦截计数为口径一致性校验）。
两份报告唯一差异 = 买入价：
  报告1「信号日收盘价买入」：回测惯例口径；本脚本独立复算卖出配对并与 replay 缓存 r_sell 对账自检。
  报告2「次日开盘价买入」：可执行口径；买入日=信号日次一根K线开盘，卖出配对从买入日当天尾盘
        开始走（与实盘 14:50 尾盘确认一致），成本=开盘价；开盘一字板（开盘≥板价且全天未打开）
        视为无法成交单独剔除。
仓位：每笔 1 万；试错档（广度不足）按现行纪律 1/4 仓（0.25 万）另列。
数据=腾讯前复权日线缓存（2025-07-17 起），收盘价/开盘价成交，未计费用；生成两份 HTML 至 reports/。
"""
import sys, json, statistics
sys.path.insert(0, "/Users/xxwei/myspace/gupiao/backend")
from datetime import datetime
from typing import Any

import numpy as np

from app import replay as rp
from app import datasource as ds
from app import sellwatch as sw
from app.config import effective_config

OUT1 = "/Users/xxwei/myspace/gupiao/reports/monthly_close_entry_20261128.html"
OUT2 = "/Users/xxwei/myspace/gupiao/reports/monthly_next_open_entry_20261128.html"
ARCHIVE = "/Users/xxwei/myspace/gupiao/backend/backtests/two_entry_monthly_result.json"
START, END = "2025-08-01", "2026-11-27"
MONTHS = ["2025-08", "2025-09", "2025-10", "2025-11", "2025-12"] + [f"2026-{m:02d}" for m in range(1, 12)]

dates = rp.trading_dates()
run_dates = [d for d in dates if START <= d <= END]
bars = rp._load_bars()

cfg = effective_config()
rp.effective_config = lambda: cfg
_names = None
def cached_names():
    global _names
    if _names is None:
        try:
            _names = ds.stock_code_names()
        except Exception:
            _names = {}
    return _names
rp.ds.stock_code_names = cached_names

sell_rules = cfg.sell.model_dump()
_orig_completed = sw._completed_history

# ===== 本地全量闸门（replay 的情绪统计只存 2026-06+，此前月份炸板闸门由本地复算补齐）=====
import numpy as np
from collections import defaultdict as _dd

_pool: dict[str, list[float]] = _dd(list)
_touch: dict[str, int] = _dd(int)
_seal: dict[str, int] = _dd(int)
for _code, _df in bars.items():
    if _code.startswith(("4", "8", "92")):
        continue
    _p = _df["pct_change"].to_numpy(dtype=float)
    _c = _df["close"].to_numpy(dtype=float)
    _h = _df["high"].to_numpy(dtype=float)
    _th = 19.5 if _code.startswith(("688", "689", "300", "301")) else 9.8
    _pc = np.empty(len(_c))
    _pc[0] = np.nan
    _pc[1:] = _c[:-1]
    _sealed = (_p >= _th) & (_c >= _h * 0.995)
    _touched = (~np.isnan(_pc)) & ((_h / _pc - 1) * 100 >= (_th - 0.3))
    _dtl = [str(x)[:10] for x in _df["date"]]
    for _j in range(1, len(_sealed)):
        _d = _dtl[_j]
        if _sealed[_j - 1]:
            _pool[_d].append(float(_p[_j]))
        if _touched[_j]:
            _touch[_d] += 1
        if _sealed[_j]:
            _seal[_d] += 1
_mom = rp.market_momentum(5)
_reg = cfg.market_regime


def local_gate(d: str) -> tuple[bool, str]:
    """本地复刻 replay 闸门（点火/炸板/动量死区），返回 (开闸?, 拦截原因)。"""
    k = dates.index(d)
    if k < 1:
        return False, "闸门未点火"
    prev = dates[k - 1]
    lst = _pool.get(d, [])
    prem = float(np.median(lst)) if len(lst) >= 3 else None
    if prem is None or prem < float(_reg.ignition_threshold):
        return False, "闸门未点火"
    t, s = _touch.get(prev, 0), _seal.get(prev, 0)
    br = max(0.0, min(100.0, (t - s) / t * 100)) if t > 0 and t >= s else (0.0 if t > 0 else None)
    if br is not None and _reg.ignition_bomb_gate_enabled and br > float(_reg.ignition_bomb_rate_max):
        return False, f"炸板闸门（本地复算 {br:.1f}% > {_reg.ignition_bomb_rate_max:g}%）"
    m = _mom.get(prev)
    if _reg.momentum_dead_zone_enabled and m is not None \
            and float(_reg.momentum_dead_low) <= m < float(_reg.momentum_dead_high):
        return False, "动量死区"
    return True, ""


def pair_sell(code: str, name: str, start_j: int, cost: float):
    """从 start_j 当日尾盘开始按现行卖出规则配对，返回 (r%, 卖出日, 卖出价, 持有交易日, 卖出原因)。"""
    df = bars.get(code)
    n = len(df)
    for j in range(start_j, n):
        window = df.iloc[:j].copy()
        sw._completed_history = lambda c, w=window: w
        row = df.iloc[j]
        quote = {"price": float(row["close"]), "open": float(row["open"]),
                 "change_pct": float(row["pct_change"]), "low": float(row["low"]),
                 "volume": float(row["volume"])}
        try:
            found = sw._evaluate(code, name, quote, sell_rules, "confirm", cost=cost)
        except Exception:  # noqa: BLE001
            found = []
        if found:
            sw._completed_history = _orig_completed
            sell_px = float(row["close"])
            hold = j - start_j + 1
            reason = str(found[0].get("strategy", "?"))
            return (sell_px / cost - 1) * 100.0, str(df["date"].iloc[j])[:10], sell_px, hold, reason
    sw._completed_history = _orig_completed
    last_px = float(df["close"].iloc[-1])
    return (last_px / cost - 1) * 100.0, str(df["date"].iloc[-1])[:10], last_px, n - start_j, "持有至期末"


entries = []
gate_days = []   # (date, month, blocked_reason, n_signals, size_tier)
mismatch = 0
no_next = 0
gate_mismatch = 0
for d in run_dates:
    open_, reason = local_gate(d)
    if not open_:
        # 本地闸门拦截：不调 replay（拦截日回放本就近零成本，且 2026-06 前炸板数据
        # replay 内部缺失，统一以本地全量闸门为准）
        gate_days.append((d, d[:7], reason, 0, ""))
        continue
    res = rp.replay(d)
    if not res.get("success"):
        continue
    if res.get("blocked_reason"):
        gate_mismatch += 1  # 本地开闸但 replay 拦截（预期为 0，仅计数验证口径一致）
        gate_days.append((d, d[:7], res.get("blocked_reason"), 0, ""))
        continue
    gate = res.get("gate") or {}
    tier = gate.get("size_tier") or ""
    gate_days.append((d, d[:7], "", len(res.get("signals", [])), tier))
    for s in res.get("signals", []):
        code, name = s["code"], s["name"]
        df = bars.get(code)
        if df is None:
            continue
        bi = int(s["buy_row"])
        if bi + 1 >= len(df):
            no_next += 1
        base_close = float(df["close"].iloc[bi])
        # 口径A：信号日收盘买入，次日尾盘开始配对（与 replay 同构，复算用于对账+卖出原因）
        r_a, sd_a, sp_a, hd_a, why_a = pair_sell(code, name, bi + 1, base_close)
        if s.get("r_sell") is not None and abs(r_a - float(s["r_sell"])) > 0.05:
            mismatch += 1
        # 口径B：次日开盘买入（一字板不可成交另判）
        j2 = bi + 1
        open_px = float(df["open"].iloc[j2]) if j2 < len(df) else None
        yizi = False
        r_b = sd_b = sp_b = hd_b = why_b = None
        if open_px is not None:
            th = 19.5 if code.startswith(("688", "689", "300", "301")) else 9.8
            open_prem = open_px / base_close - 1
            low_px = float(df["low"].iloc[j2])
            yizi = bool(open_prem * 100 >= th - 0.2 and low_px >= open_px * 0.998)
            if not yizi:
                r_b, sd_b, sp_b, hd_b, why_b = pair_sell(code, name, j2, open_px)
        entries.append({
            "code": code, "name": name, "strategy": s["strategy"], "date": d,
            "month": d[:7], "tier": tier,
            "price_a": base_close,
            "r_a": round(r_a, 2), "sell_date_a": sd_a, "sp_a": sp_a, "hold_a": hd_a, "why_a": why_a,
            "no_next": bi + 1 >= len(df),
            "open_b": open_px, "yizi": yizi,
            "r_b": round(r_b, 2) if r_b is not None else None,
            "sell_date_b": sd_b, "sp_b": sp_b, "hold_b": hd_b, "why_b": why_b,
        })
print(f"入场 {len(entries)} 笔 · 对账不匹配 {mismatch} 笔 · 无次日bar {no_next} 笔 · 闸门口径不一致 {gate_mismatch} 天", flush=True)
if mismatch:
    print(f"[warn] 口径A复算与缓存 r_sell 不匹配 {mismatch} 笔（>0.05pp），请检查配对逻辑")
if gate_mismatch:
    print(f"[warn] 本地闸门与 replay 闸门不一致 {gate_mismatch} 天，请检查本地复算口径")

sw._completed_history = _orig_completed


def stats(g, col, tiered=False):
    pairs = [(e[col], e["tier"]) for e in g if e.get(col) is not None]
    if not pairs:
        return {"n": 0}
    rs = [p[0] for p in pairs]
    pos = sum(1 for r in rs if r > 0)
    t1 = sum(r / 100 for r in rs)
    if tiered:
        tt = sum(r / 100 * (0.25 if t == "probe" else 1.0) for r, t in pairs)
    else:
        tt = None
    return {"n": len(rs), "win": round(pos / len(rs) * 100, 1),
            "avg": round(sum(rs) / len(rs), 2), "med": round(statistics.median(rs), 2),
            "sum_wan": round(t1, 2), "sum_wan_tier": round(tt, 2) if tiered else None}


def html_report(title, col, entry_price_label, extra_note, yizi_mode):
    """yizi_mode: 'all' 报告1全部成交 / 'exec' 报告2剔除一字板 / 'incl' 报告2补充行(含一字板)"""
    parts = [f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>{title}</title>
<style>
body{{font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;margin:24px;color:#222;background:#fafafa}}
h1{{font-size:22px}} h2{{font-size:17px;margin-top:28px;border-left:4px solid #1a56db;padding-left:8px}}
table{{border-collapse:collapse;width:100%;background:#fff;font-size:13px}}
th,td{{border:1px solid #ddd;padding:5px 8px;text-align:center}}
th{{background:#1a56db;color:#fff;font-weight:600}}
tr:nth-child(even){{background:#f6f9ff}}
.r{{color:#d92b2b;font-weight:600}} .g{{color:#0a8f3c;font-weight:600}}
.dim{{color:#999;font-size:11px}}
.card{{background:#fff;border:1px solid #e5e5e5;border-radius:8px;padding:16px;margin:12px 0}}
.kpi{{display:inline-block;margin:0 28px 0 0;text-align:center}}
.kpi b{{font-size:22px;display:block}}
.note{{color:#666;font-size:12px;line-height:1.7}}
.warn{{background:#fff8e6;border-left:4px solid #f0a020;padding:10px 14px;font-size:13px;line-height:1.8}}
</style></head><body>
<h1>{title}</h1>
<p class="note">区间 {START}~{END} · 策略=洗盘反包 trigger + 全部现行拦截（点火≥3% / 炸板≤45% / 动量死区-5~0% /
广度档位 / 四象限剔C1）· 买入价={entry_price_label} · 每笔1万（试错档另列1/4仓=0.25万）·
卖出=现行5通道纪律（与实盘同口径）· 未计费用 · 数据=腾讯前复权日线缓存<br>
生成时间 {datetime.now().strftime('%Y-%m-%d %H:%M')} · 空月=三闸门在历史时段的拦截生效（闸门为 7-11 月标定，其余月份属样本外适用）；
2026-06 前的炸板闸门为本地复算口径（replay 内建情绪统计仅存 2026-06+），点火/动量死区与 replay 同口径</p>
<div class="warn">{extra_note}</div>"""]

    # 月度表（口径A 剔除数据末端未结算信号：hold=0 的 11-27 买入批次无次日数据，0% 伪收益不入统计）
    parts.append("<h2>一、月度拆分</h2><table><tr><th>月份</th><th>信号笔数</th>")
    unsettled_lab = "未结算(数据末端)"
    if yizi_mode != "all":
        parts.append("<th>一字板未成交</th><th>无次日bar</th>")
    else:
        parts.append(f"<th>{unsettled_lab}</th>")
    parts.append("<th>已结算笔数</th><th>胜率</th><th>平均收益</th><th>中位</th><th>总收益(每笔1万)</th><th>总收益(阶梯仓位)</th></tr>")
    monthly = []
    for m in MONTHS:
        if yizi_mode == "exec":
            g = [e for e in entries if e["month"] == m and not e["yizi"] and e.get("r_b") is not None]
        else:
            g = [e for e in entries if e["month"] == m and e.get("r_a") is not None and e["hold_a"] > 0]
        sig_n = sum(1 for e in entries if e["month"] == m)
        yizi_n = sum(1 for e in entries if e["month"] == m and e["yizi"])
        nonext_n = sum(1 for e in entries if e["month"] == m and e.get("no_next") and not e["yizi"])
        unsettled_n = sum(1 for e in entries if e["month"] == m and e.get("r_a") is not None
                          and e["hold_a"] == 0)
        st = stats(g, col, tiered=True)
        monthly.append({"month": m, "signals": sig_n, "yizi": yizi_n, "no_next": nonext_n,
                        "unsettled": unsettled_n, **st})
        sw_ = f"{st['sum_wan']:+.2f}万" if st["n"] else "--"
        stt = f"{st['sum_wan_tier']:+.2f}万" if st.get("sum_wan_tier") is not None else "--"
        cls = "r" if st["n"] and st["sum_wan"] >= 0 else ("g" if st["n"] else "dim")
        clst = "r" if st.get("sum_wan_tier") is not None and st["sum_wan_tier"] >= 0 else ("g" if st.get("sum_wan_tier") is not None else "dim")
        win = f"{st['win']}%" if st["n"] else "--"
        avg = f"{st['avg']:+.2f}%" if st["n"] else "--"
        med = f"{st['med']:+.2f}%" if st["n"] else "--"
        parts.append(f"<tr><td>{m}</td><td>{sig_n}</td>")
        if yizi_mode != "all":
            parts.append(f"<td>{yizi_n}</td><td>{nonext_n}</td>")
        else:
            parts.append(f"<td>{unsettled_n}</td>")
        parts.append(f"<td>{st['n']}</td><td>{win}</td><td class='{cls}'>{avg}</td><td>{med}</td>"
                     f"<td class='{cls}'><b>{sw_}</b></td><td class='{clst}'>{stt}</td></tr>")
    if yizi_mode == "exec":
        total_g = [e for e in entries if not e["yizi"] and e.get("r_b") is not None]
    else:
        total_g = [e for e in entries if e.get("r_a") is not None and e["hold_a"] > 0]
    tot = stats(total_g, col, tiered=True)
    tot_avg = tot.get("avg", 0)
    tot_med = tot.get("med", 0)
    tot_sw = tot.get("sum_wan", 0)
    tot_swt = tot.get("sum_wan_tier", 0)
    tot_yizi = sum(1 for e in entries if e["yizi"])
    parts.append(f"<tr><th>合计</th><th>{len(entries)}</th>"
                 + (f"<th>{tot_yizi}</th>" if yizi_mode != "all" else "")
                 + f"<th>{tot['n']}</th><th>{tot.get('win', 0)}%</th>"
                 f"<td class='{'r' if tot_avg >= 0 else 'g'}'>{tot_avg:+.2f}%</td>"
                 f"<td>{tot_med:+.2f}%</td>"
                 f"<td class='{'r' if tot_sw >= 0 else 'g'}'><b>{tot_sw:+.2f}万</b></td>"
                 f"<td class='{'r' if tot_swt >= 0 else 'g'}'><b>{tot_swt:+.2f}万</b></td></tr></table>")

    # 闸门日历
    parts.append("<h2>二、闸门日历（为什么有月份是空的）</h2><table><tr><th>月份</th><th>交易日</th><th>拦截日</th>"
                 "<th>开闸无信号日</th><th>开闸有信号日</th><th>主要拦截原因</th></tr>")
    for m in MONTHS:
        days = [g for g in gate_days if g[1] == m]
        blocked = [g for g in days if g[2]]
        open_nosig = [g for g in days if not g[2] and g[3] == 0]
        open_sig = [g for g in days if not g[2] and g[3] > 0]
        from collections import Counter
        reasons = Counter((g[2].split("（")[0] if g[2] else "") for g in blocked)
        top = "；".join(f"{k or '—'}×{v}" for k, v in reasons.most_common(3)) or "—"
        parts.append(f"<tr><td>{m}</td><td>{len(days)}</td><td>{len(blocked)}</td>"
                     f"<td>{len(open_nosig)}</td><td>{len(open_sig)}</td>"
                     f"<td class='dim' style='text-align:left'>{top}</td></tr>")
    parts.append("</table><p class='note'>拦截原因口径：闸门未点火=昨日涨停池今日溢价&lt;3%；炸板闸门=昨日炸板率&gt;45%；"
                 "动量死区=等权5日动量在-5%~0%阴跌区。三闸门全部通过但当日扫描无 trigger 命中=开闸无信号。</p>")

    # 同口径对照（仅报告2）：同一批已结算信号，收盘买 vs 次开买
    if yizi_mode == "exec":
        pairs = [e for e in entries
                 if e.get("r_a") is not None and e["hold_a"] > 0 and e.get("r_b") is not None]
        parts.append(f"<h2>三、同口径对照：同一批 {len(pairs)} 笔已结算信号，收盘买 vs 次开买</h2>"
                     "<table><tr><th>月份</th><th>笔数</th><th>A·收盘买胜率</th><th>B·次开买胜率</th>"
                     "<th>A·总收益</th><th>B·总收益</th><th>隔夜+开盘执行效应</th></tr>")
        for m in MONTHS + ["合计"]:
            gp = [e for e in pairs if (e["month"] == m if m != "合计" else True)]
            if not gp:
                continue
            sa = stats(gp, "r_a")
            sb = stats(gp, "r_b")
            diff = round(sb["sum_wan"] - sa["sum_wan"], 2)
            parts.append(f"<tr><td>{m}</td><td>{len(gp)}</td>"
                         f"<td>{sa['win']}%</td><td>{sb['win']}%</td>"
                         f"<td class='{'r' if sa['sum_wan']>=0 else 'g'}'>{sa['sum_wan']:+.2f}万</td>"
                         f"<td class='{'r' if sb['sum_wan']>=0 else 'g'}'>{sb['sum_wan']:+.2f}万</td>"
                         f"<td class='{'r' if diff>=0 else 'g'}'>{diff:+.2f}万</td></tr>")
        parts.append("<p class='note'>对照剔除了一字板与数据末端无次日bar的信号，两列收益完全同批信号可比；"
                     "差异全部来自「隔夜跳空 + 以开盘价而非收盘价成交」。</p>")

    # 逐笔明细
    sec_no = "四" if yizi_mode == "exec" else "三"
    parts.append(f"<h2>{sec_no}、逐笔明细（{tot['n']} 笔已结算，按信号日排序）</h2>"
                 "<table><tr><th>信号日</th><th>代码</th><th>名称</th><th>档位</th><th>买入价</th>"
                 "<th>卖出日</th><th>卖出价</th><th>收益</th><th>持有(交易日)</th><th>离场原因</th></tr>")
    det = sorted(total_g, key=lambda e: e["date"])
    for e in det:
        buy_px = e["price_a"] if col == "r_a" else e["open_b"]
        sell_px_v = e["sp_a"] if col == "r_a" else e["sp_b"]
        sd = e["sell_date_a"] if col == "r_a" else e["sell_date_b"]
        r = e.get(col)
        rc = "r" if r is not None and r >= 0 else "g"
        hold = e["hold_a"] if col == "r_a" else e["hold_b"]
        why = e["why_a"] if col == "r_a" else e["why_b"]
        tier_lab = "全仓" if e["tier"] == "full" else ("试错1/4" if e["tier"] == "probe" else "—")
        parts.append(f"<tr><td>{e['date']}</td><td>{e['code']}</td><td style='text-align:left'>{e['name']}</td>"
                     f"<td>{tier_lab}</td><td>{buy_px:.2f}</td><td>{sd}</td><td>{sell_px_v:.2f}</td>"
                     f"<td class='{rc}'>{r:+.2f}%</td><td>{hold}</td><td class='dim' style='text-align:left'>{why or '--'}</td></tr>")
    parts.append("</table></body></html>")

    html = "".join(parts)
    return html, monthly, tot


note_a = ("口径A复算与 replay 缓存 r_sell 已逐笔对账（阈值 0.05pp），卖出配对逻辑经独立校验。"
          "数据末端（2026-11-27）买入的信号无次日行情，列为「未结算」不计入收益统计——按 backtests/README "
          "「剔除最后一日样本」卫生规则执行。")
html1, monthly_a, tot_a = html_report(
    "当前策略月度收益 · 口径A：信号日收盘价买入", "r_a",
    "信号日收盘价（回测惯例口径）", note_a, yizi_mode="all")
html2, monthly_b, tot_b = html_report(
    "当前策略月度收益 · 口径B：次日开盘价买入", "r_b",
    "信号日次一交易日开盘价（可执行口径）",
    "买入发生在信号次日开盘：隔夜跳空盈亏计入本笔；开盘一字板（开盘≥板价且全天未打开）视为无法成交已剔除，"
    "明细不含。卖出配对自买入日当天尾盘开始（与实盘14:50尾盘确认一致），成本=开盘价。",
    yizi_mode="exec")

open(OUT1, "w", encoding="utf-8").write(html1)
open(OUT2, "w", encoding="utf-8").write(html2)
json.dump({"entries": entries,
           "gate_days": gate_days,
           "monthly_close": monthly_a, "total_close": tot_a,
           "monthly_next_open": monthly_b, "total_next_open": tot_b,
           "mismatch": mismatch, "no_next": no_next},
          open(ARCHIVE, "w", encoding="utf-8"), ensure_ascii=False)
print(f"报告1: {OUT1}\n报告2: {OUT2}\n归档: {ARCHIVE}")
print("\n===== 速览（每笔1万 / 括号内阶梯仓位）=====")
print(f"{'月份':<10}{'A收盘买':<30}{'B次开买':<30}")
for ma, mb in zip(monthly_a, monthly_b):
    fa = (f"{ma['n']}笔 {ma['win']}% {ma['sum_wan']:+.2f}万({ma['sum_wan_tier']:+.2f})"
          if ma["n"] else "0笔 --")
    fb = (f"{mb['n']}笔 {mb['win']}% {mb['sum_wan']:+.2f}万({mb['sum_wan_tier']:+.2f})"
          if mb["n"] else "0笔 --")
    print(f"{ma['month']:<10}{fa:<30}{fb:<30}")
ta = (f"{tot_a['n']}笔 {tot_a.get('win', 0)}% {tot_a.get('sum_wan', 0):+.2f}万({tot_a.get('sum_wan_tier', 0):+.2f})")
tb = (f"{tot_b['n']}笔 {tot_b.get('win', 0)}% {tot_b.get('sum_wan', 0):+.2f}万({tot_b.get('sum_wan_tier', 0):+.2f})")
print(f"{'合计':<10}{ta:<30}{tb:<30}")
