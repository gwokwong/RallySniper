"""AKShare 数据源封装。

统一负责：
- 懒加载 akshare（接口发现 + 调用）
- 数据缓存（避免频繁请求被封）
- DataFrame -> JSON 安全转换
- 交易时段判断

所有行情数据来自 AKShare（东方财富等公开数据源），
本模块不内置任何写死的行情数值。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

import pandas as pd

# akshare 延迟导入：未安装时也能启动服务并给出友好提示
_ak: Any | None = None
_import_error: str | None = None


def get_ak() -> Any:
    global _ak, _import_error
    if _ak is not None:
        return _ak
    if _import_error is not None:
        raise RuntimeError(f"AKShare 不可用: {_import_error}")
    try:
        import akshare as ak  # type: ignore

        _ak = ak
        return _ak
    except Exception as exc:  # noqa: BLE001
        _import_error = str(exc)
        raise RuntimeError(f"AKShare 导入失败: {exc}") from exc


def available_function(name: str) -> Callable | None:
    """从 akshare 中按名称查找函数（精确匹配优先）"""
    ak = get_ak()
    fn = getattr(ak, name, None)
    return fn if callable(fn) else None


def find_function(*candidates: str) -> tuple[Callable, str] | None:
    """按候选名顺序查找可用接口。"""
    ak = get_ak()
    for name in candidates:
        fn = getattr(ak, name, None)
        if callable(fn):
            return fn, name
    return None


# ----------------------------- 缓存 -----------------------------

class TTLCache:
    """线程安全 TTL 缓存 + 失败熔断。

    东方财富 push2 行情接口限流时，短时间内反复重试会延长封禁时间，
    因此对失败结果做短时缓存（circuit breaker），并对外暴露状态。
    """

    def __init__(self) -> None:
        self._store: dict[str, tuple[float, Any]] = {}
        self._failures: dict[str, tuple[float, str]] = {}
        self._failure_ttl = 120  # 失败后 2 分钟内不再重试同一接口
        self._locks: dict[str, bool] = {}

    def get_or_fetch(self, key: str, ttl: int, fetcher: Callable[[], Any]) -> Any:
        now = time.time()
        cached = self._store.get(key)
        if cached and now - cached[0] < ttl:
            return cached[1]

        # 熔断中：返回上一次成功值（哪怕过期），否则返回 None
        failure = self._failures.get(key)
        if failure and now - failure[0] < self._failure_ttl:
            return cached[1] if cached else None

        value = fetcher()
        if value is None:
            self._failures[key] = (now, "fetch returned None")
        else:
            self._failures.pop(key, None)
            self._store[key] = (now, value)
        return value

    def peek(self, key: str) -> Any | None:
        """读取缓存值（不过期校验），用于本地持久缓存预热。"""
        cached = self._store.get(key)
        return cached[1] if cached else None

    def put(self, key: str, value: Any) -> None:
        """直接写入缓存并清除该键的熔断状态。"""
        self._store[key] = (time.time(), value)
        self._failures.pop(key, None)

    def is_degraded(self, key: str) -> bool:
        failure = self._failures.get(key)
        return bool(failure and time.time() - failure[0] < self._failure_ttl)

    def clear(self, key: str | None = None) -> None:
        if key is None:
            self._store.clear()
            self._failures.clear()
        else:
            self._store.pop(key, None)
            self._failures.pop(key, None)


cache = TTLCache()

# 股票代码->名称的本地持久缓存；网络失败时仍能显示名称并支持搜索
CODE_NAMES_FILE = Path(__file__).resolve().parent.parent / "data" / "code_names.json"


def _load_code_names_file() -> dict[str, str]:
    try:
        raw = json.loads(CODE_NAMES_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and raw:
            return {str(k).strip().zfill(6): str(v).strip() for k, v in raw.items()}
    except Exception:  # noqa: BLE001
        pass
    return {}


def _save_code_names_file(mapping: dict[str, str]) -> None:
    try:
        CODE_NAMES_FILE.parent.mkdir(parents=True, exist_ok=True)
        CODE_NAMES_FILE.write_text(
            json.dumps(mapping, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001
        pass


# ----------------------------- 工具函数 -----------------------------

def df_to_records(df: pd.DataFrame | None, limit: int | None = None) -> list[dict[str, Any]]:
    """DataFrame 转 JSON 安全的记录列表"""
    if df is None or df.empty:
        return []
    out = df.copy()
    if limit:
        out = out.head(limit)
    # NaN/Inf -> None
    out = out.astype(object).where(pd.notna(out), None)
    records = out.to_dict(orient="records")
    for row in records:
        for k, v in list(row.items()):
            if hasattr(v, "item"):
                try:
                    row[k] = v.item()
                except Exception:  # noqa: BLE001
                    row[k] = str(v)
            elif isinstance(v, (pd.Timestamp, datetime, date)):
                row[k] = v.isoformat()
    return records


def _safe_call(fn: Callable, *args: Any, **kwargs: Any) -> pd.DataFrame | None:
    """调用接口，失败时返回 None（由上层决定降级策略）"""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        print(f"[datasource] 调用 {getattr(fn, '__name__', fn)} 失败: {exc}")
        return None


# ----------------------------- 行情接口 -----------------------------

def stock_spot(ttl: int = 30) -> pd.DataFrame | None:
    """沪深京 A 股实时行情。

    主源：东方财富 stock_zh_a_spot_em（字段全、快）
    备源：新浪 stock_zh_a_spot（东财限流时降级，列名为中文但字段较少）
    """
    def fetch() -> pd.DataFrame | None:
        em = find_function("stock_zh_a_spot_em")
        if em:
            df = _safe_call(em[0])
            if df is not None and not df.empty:
                return df

        # 新浪备用源（新版 AKShare 接口名为 stock_zh_a_spot_sina）
        # 新浪需翻约70页、耗时十几秒，单独长缓存，避免每30秒重复拉取
        sina = find_function("stock_zh_a_spot_sina", "stock_zh_a_spot")
        if sina:
            return cache.get_or_fetch("stock_spot_sina", 180,
                                      lambda: _safe_call(sina[0]))
        return None

    return cache.get_or_fetch("stock_spot", ttl, fetch)


def spot_quote_map(codes: list[str] | set[str] | None = None,
                   ttl: int = 30) -> dict[str, dict[str, Any]]:
    """实时行情按代码建索引，供昨日涨停跟进/持仓风控批量取数。

    字段：price/change_pct/open/high/low/pre_close/volume/amount/volume_ratio/turnover。
    新浪降级源没有量比字段时留 None，由上层按成交量自行估算。

    空代码列表直接返回，避免为了空结果去拉全市场行情（曾导致持有列表为空时
    /api/holdings 卡住、标的池页 Promise.all 一起挂起）。
    """
    if not codes:
        return {}
    df = stock_spot(ttl)
    if df is None or df.empty:
        return {}
    cc = next((c for c in ("代码", "code", "symbol") if c in df.columns), None)
    if not cc:
        return {}
    wanted = set(codes) if codes else None

    def val(row: Any, *names: str) -> float | None:
        for n in names:
            if n in df.columns:
                try:
                    v = pd.to_numeric(row[n], errors="coerce")
                    return None if pd.isna(v) else float(v)
                except (TypeError, ValueError):
                    return None
        return None

    out: dict[str, dict[str, Any]] = {}
    for _, row in df.iterrows():
        raw = str(row[cc]).strip()
        digits = "".join(ch for ch in raw if ch.isdigit())
        code = digits[-6:] if len(digits) >= 6 else ""
        if not code or not code.isdigit() or (wanted is not None and code not in wanted):
            continue
        out[code] = {
            "name": str(row.get("名称", code)).strip(),
            "price": val(row, "最新价"),
            "change_pct": val(row, "涨跌幅"),
            "open": val(row, "今开"),
            "high": val(row, "最高"),
            "low": val(row, "最低"),
            "pre_close": val(row, "昨收"),
            "volume": val(row, "成交量"),
            "amount": val(row, "成交额"),
            "volume_ratio": val(row, "量比"),
            "turnover": val(row, "换手率"),
            "float_cap": val(row, "流通市值"),
            "total_cap": val(row, "总市值"),
            "pe_dynamic": val(row, "市盈率-动态"),
        }
    # 新浪源没有量比/换手率：用日K近5日均量补算（小批量标的，缓存后开销可控）
    eff = effective_trading_date()
    for code, q in out.items():
        if q["volume_ratio"] is not None and q["turnover"] is not None:
            continue
        hist = stock_history(code, days=40)
        if hist is None or hist.empty:
            continue
        dates = [str(x)[:10].replace("-", "") for x in hist["date"]]
        # 仅盘中且该日尚未收盘时剔除最后一根；休市日整根都是收盘数据，不能剔除
        if not eff["is_closed"] and dates and dates[-1] == eff["date"] and len(hist) > 1:
            hist = hist.iloc[:-1]
        if "volume" in hist.columns:
            vols = hist["volume"].astype(float)
            avg = float(vols.tail(5).mean()) if len(vols) >= 5 else 0.0
            if q["volume_ratio"] is None and avg > 0 and q["volume"]:
                q["volume_ratio"] = round(float(q["volume"]) / avg, 2)
        if q["turnover"] is None and "turnover" in hist.columns and len(hist) > 0:
            tv = hist["turnover"].iloc[-1]
            if pd.notna(tv):
                q["turnover"] = round(float(tv), 2)
    return out


def stock_code_names(ttl: int = 86400) -> dict[str, str]:
    """A 股代码 -> 名称映射。

    按稳定性排序：本地持久缓存 > 上交所/深交所静态表 > 交易所全量表 >
    实时行情。任何一条链路成功都会写入 data/code_names.json，
    因此东财限流或单个交易所接口超时都不会让名称退化成代码。
    """
    if not cache.peek("stock_code_names"):
        persisted = _load_code_names_file()
        if persisted:
            cache.put("stock_code_names", persisted)

    def merge(df: pd.DataFrame | None, cc: str, nc: str,
              target: dict[str, str]) -> None:
        if df is None or df.empty or cc not in df.columns or nc not in df.columns:
            return
        for _, row in df[[cc, nc]].iterrows():
            code = str(row[cc]).strip().zfill(6)
            name = str(row[nc]).strip()
            if len(code) == 6 and name and name != "nan":
                target[code] = name

    def fetch() -> dict[str, str] | None:
        result: dict[str, str] = {}

        # 1) 上交所 + 深交所静态表（不走东财 push2，最稳定）
        sh = find_function("stock_info_sh_name_code")
        if sh:
            for symbol in ("主板A股", "科创板"):
                df = _safe_call(sh[0], symbol=symbol)
                merge(df, "证券代码", "证券简称", result)
        sz = find_function("stock_info_sz_name_code")
        if sz:
            merge(_safe_call(sz[0], symbol="A股列表"), "A股代码", "A股简称", result)
        if len(result) >= 3000:
            _save_code_names_file(result)
            return result

        # 2) 交易所全量表（含北交所，偶尔会因 bse.cn 超时失败，忽略即可）
        found = find_function("stock_info_a_code_name")
        if found:
            merge(_safe_call(found[0]), "code", "name", result)
        if result:
            _save_code_names_file(result)
            return result

        # 3) 实时行情补充（东财未限流时可用）
        spot = stock_spot(ttl=30)
        if spot is not None and not spot.empty:
            cc = next((c for c in ("代码", "股票代码", "code", "symbol") if c in spot.columns), None)
            nc = next((c for c in ("名称", "股票名称", "简称", "name") if c in spot.columns), None)
            if cc and nc:
                merge(spot, cc, nc, result)
        if result:
            _save_code_names_file(result)
            return result

        # 4) 最后一次读取本地文件
        return _load_code_names_file() or None

    return cache.get_or_fetch("stock_code_names", ttl, fetch) or {}


def individual_fund_flow(ttl: int = 120) -> pd.DataFrame | None:
    """个股资金流排名（今日）。

    优先东财 rank 接口，被限流时降级到同花顺 stock_fund_flow_individual_ths。
    两条链路的数据结构不同，由 analyzer 层做列兼容。
    """
    def fetch() -> pd.DataFrame | None:
        # 1) 东方财富主力净流入排名
        found = find_function(
            "stock_fund_flow_individual_rank_em",   # 新（1.20.x）
            "stock_individual_fund_flow_rank",      # 旧
            "stock_fund_flow_main_em",
        )
        if found:
            fn = found[0]
            # 根据接口签名选择合法参数，避免无意义的 TypeError 日志
            import inspect
            try:
                params = inspect.signature(fn).parameters
            except Exception:  # noqa: BLE001
                params = {}
            candidates = []
            if "indicator" in params:
                candidates.append({"indicator": "今日"})
            if "symbol" in params:
                candidates.append({"symbol": "全部股票"})
            candidates.append({})
            for kwargs in candidates:
                df = _safe_call(fn, **kwargs)
                if df is not None and not df.empty:
                    return df

        # 2) 同花顺备用（走不同服务器，通常不受东财限流影响）
        ths = find_function("stock_fund_flow_individual_ths")
        if ths:
            df = _safe_call(ths[0], symbol="即时")
            if df is not None and not df.empty:
                return df
        return None

    return cache.get_or_fetch("fund_flow", ttl, fetch)


def sector_fund_flow(ttl: int = 120) -> pd.DataFrame | None:
    """板块资金流排名（今日/行业），带同花顺备用源。"""
    def fetch() -> pd.DataFrame | None:
        found = find_function(
            "stock_fund_flow_sector_rank_em",   # 新（1.20.x）
            "stock_sector_fund_flow_rank",      # 旧
        )
        if found:
            fn = found[0]
            import inspect
            try:
                params = inspect.signature(fn).parameters
            except Exception:  # noqa: BLE001
                params = {}
            candidates = []
            if "indicator" in params and "sector_type" in params:
                candidates.append({"indicator": "今日", "sector_type": "行业资金流"})
            elif "indicator" in params:
                candidates.append({"indicator": "今日"})
            candidates.append({})
            for kwargs in candidates:
                df = _safe_call(fn, **kwargs)
                if df is not None and not df.empty:
                    return df

        # 同花顺行业/概念资金流（单位为亿）
        for name in ("stock_fund_flow_industry_ths", "stock_fund_flow_concept_ths"):
            ths = find_function(name)
            if ths:
                df = _safe_call(ths[0], symbol="即时")
                if df is not None and not df.empty:
                    return df
        return None

    return cache.get_or_fetch("sector_fund_flow", ttl, fetch)


def industry_boards(ttl: int = 60) -> pd.DataFrame | None:
    """行业板块行情。"""
    def fetch() -> pd.DataFrame | None:
        found = find_function("stock_board_industry_name_em", "stock_board_industry_name_ths")
        if not found:
            return None
        return _safe_call(found[0])

    return cache.get_or_fetch("industry_boards", ttl, fetch)


def concept_boards(ttl: int = 60) -> pd.DataFrame | None:
    """概念板块行情。"""
    def fetch() -> pd.DataFrame | None:
        found = find_function("stock_board_concept_name_em", "stock_board_concept_name_ths")
        if not found:
            return None
        return _safe_call(found[0])

    return cache.get_or_fetch("concept_boards", ttl, fetch)


def board_change_detail(ttl: int = 60) -> pd.DataFrame | None:
    """板块异动详情。"""
    def fetch() -> pd.DataFrame | None:
        found = find_function("stock_board_change_em")
        if not found:
            return None
        for args in ((),):
            df = _safe_call(found[0], *args)
            if df is not None and not df.empty:
                return df
        return None

    return cache.get_or_fetch("board_change", ttl, fetch)


def stock_changes(ttl: int = 30) -> pd.DataFrame | None:
    """盘口异动（分时）。"""
    def fetch() -> pd.DataFrame | None:
        found = find_function("stock_changes_em")
        if not found:
            return None
        return _safe_call(found[0])

    return cache.get_or_fetch("stock_changes", ttl, fetch)


def limit_up_pool(trade_date: str | None = None, ttl: int = 120) -> pd.DataFrame | None:
    """涨停股池。trade_date 格式 YYYYMMDD。"""
    d = trade_date or effective_trading_date()["date"]

    def fetch() -> pd.DataFrame | None:
        found = find_function("stock_zt_pool_em")
        if not found:
            return None
        return _safe_call(found[0], date=d)

    return cache.get_or_fetch(f"limit_up_{d}", ttl, fetch)


def dragon_tiger_list(start_date: str | None = None, end_date: str | None = None,
                      ttl: int = 300) -> pd.DataFrame | None:
    """龙虎榜数据。日期格式 YYYYMMDD。

    多接口兼容：优先 stock_lhb_detail_em，其次 stock_lhb_stock_detail_em 等。
    """
    # 统一口径：默认截止到行情实际对应的交易日（休市时为最近交易日）
    end = end_date or effective_trading_date()["date"]
    start = start_date or (datetime.strptime(end, "%Y%m%d") - timedelta(days=7)).strftime("%Y%m%d")
    key = f"lhb_{start}_{end}"

    def fetch() -> pd.DataFrame | None:
        ak = get_ak()
        # 明细接口签名: start_date, end_date
        for name in ("stock_lhb_detail_em", "stock_lhb_stock_detail_em"):
            fn = getattr(ak, name, None)
            if callable(fn):
                df = _safe_call(fn, start_date=start, end_date=end)
                if df is not None and not df.empty:
                    return df
        # 备选：按日期
        for name in ("stock_lhb_detail_em",):
            fn = getattr(ak, name, None)
            if callable(fn):
                df = _safe_call(fn, date=end)
                if df is not None and not df.empty:
                    return df
        return None

    return cache.get_or_fetch(key, ttl, fetch)


def stock_individual_info(symbol: str, ttl: int = 300) -> pd.DataFrame | None:
    """个股基本信息。"""
    def fetch() -> pd.DataFrame | None:
        found = find_function("stock_individual_info_em")
        if not found:
            return None
        return _safe_call(found[0], symbol=symbol)

    return cache.get_or_fetch(f"info_{symbol}", ttl, fetch)


def _tx_symbol(code: str) -> str:
    """A股代码 -> 腾讯行情符号（600519 -> sh600519 / 000858 -> sz000858）。"""
    code = str(code).strip().zfill(6)
    if code.startswith(("60", "68", "9")):
        return f"sh{code}"
    if code.startswith(("00", "30", "20", "2")):
        return f"sz{code}"
    if code.startswith(("8", "4")):
        return f"bj{code}"
    return f"sh{code}"


def _normalize_history(df: pd.DataFrame | None, source: str) -> pd.DataFrame | None:
    """统一历史K线列名为英文，便于战法计算。

    统一列：date open close high low volume amount pct_change turnover
    """
    if df is None or df.empty:
        return None
    rename = {
        "日期": "date", "开盘": "open", "收盘": "close", "最高": "high",
        "最低": "low", "成交量": "volume", "成交额": "amount",
        "涨跌幅": "pct_change", "换手率": "turnover",
        # 腾讯源已是英文列
    }
    out = df.rename(columns={k: v for k, v in rename.items() if k in df.columns}).copy()
    for c in ("open", "close", "high", "low", "volume", "amount"):
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    out = out.dropna(subset=["open", "close", "high", "low"])
    out = out.sort_values("date").reset_index(drop=True)
    # 涨跌幅：东财直接给百分比；腾讯源需自行计算
    if "pct_change" not in out.columns or out["pct_change"].isna().all():
        out["pct_change"] = out["close"].pct_change() * 100
    out["pct_change"] = pd.to_numeric(out["pct_change"], errors="coerce").fillna(0)
    if "turnover" not in out.columns:
        out["turnover"] = 0.0
    out["turnover"] = pd.to_numeric(out["turnover"], errors="coerce").fillna(0)
    # 腾讯 turnover 是小数比例(0.0034)，东财是百分数(0.34)，统一为百分数
    if source == "tx":
        out["turnover"] = out["turnover"] * 100
    out.attrs["source"] = source
    return out


def stock_history(code: str, days: int = 130, ttl: int = 1800) -> pd.DataFrame | None:
    """个股日K历史（前复权），东财优先，腾讯降级。

    days: 需要的近似交易日数量（用于计算60日均线+形态，默认130≈半年）。
    统一返回列：date open close high low volume amount pct_change turnover
    """
    code = str(code).strip().zfill(6)
    # 统一口径：交易日取当日，休市/盘前取最近交易日（缓存，不会逐股请求）
    end = effective_trading_date()["date"]
    start = (datetime.strptime(end, "%Y%m%d") - timedelta(days=int(days * 1.8) + 30)).strftime("%Y%m%d")

    def fetch() -> pd.DataFrame | None:
        # 1) 东财历史（新接口名 stock_zh_a_hist_em，走 push2his，列名全）
        em = find_function("stock_zh_a_hist_em", "stock_zh_a_hist")
        if em:
            try:
                import inspect
                params = inspect.signature(em[0]).parameters
                kwargs: dict[str, Any] = {"symbol": code, "period": "daily",
                                          "start_date": start, "end_date": end}
                if "adjust" in params:
                    kwargs["adjust"] = "qfq"
                df = em[0](**kwargs)
                norm = _normalize_history(df, "em")
                if norm is not None and len(norm) >= 30:
                    return norm
            except Exception as exc:  # noqa: BLE001
                print(f"[datasource] 东财历史K线 {code} 失败: {str(exc)[:80]}")

        # 2) 腾讯历史（不同服务器，通常不受东财限流影响）
        tx = find_function("stock_zh_a_hist_tx")
        if tx:
            try:
                sym = _tx_symbol(code)
                import inspect
                params = inspect.signature(tx[0]).parameters
                kwargs = {"symbol": sym, "start_date": start.replace("-", ""), "end_date": end}
                if "adjust" in params:
                    kwargs["adjust"] = "qfq"
                df = tx[0](**kwargs)
                norm = _normalize_history(df, "tx")
                if norm is not None and len(norm) >= 30:
                    return norm
            except Exception as exc:  # noqa: BLE001
                print(f"[datasource] 腾讯历史K线 {code} 失败: {str(exc)[:80]}")
        return None

    return cache.get_or_fetch(f"hist_{code}", ttl, fetch)


def _normalize_minutes(df: pd.DataFrame | None) -> pd.DataFrame | None:
    """把新浪/东财分钟K线统一为 date/time/open/high/low/close/volume，只保留最近一个交易日。"""
    if df is None or df.empty:
        return None
    tcol = next((c for c in ("day", "时间", "date", "time") if c in df.columns), None)
    ocol = next((c for c in ("open", "开盘") if c in df.columns), None)
    hcol = next((c for c in ("high", "最高") if c in df.columns), None)
    lcol = next((c for c in ("low", "最低") if c in df.columns), None)
    ccol = next((c for c in ("close", "收盘") if c in df.columns), None)
    vcol = next((c for c in ("volume", "成交量") if c in df.columns), None)
    if not all((tcol, ocol, hcol, lcol, ccol, vcol)):
        return None
    out = df[[tcol, ocol, hcol, lcol, ccol, vcol]].copy()
    out.columns = ["datetime", "open", "high", "low", "close", "volume"]
    out["datetime"] = out["datetime"].astype(str)
    out["date"] = out["datetime"].str.slice(0, 10)
    last_day = out["date"].max()
    out = out[out["date"] == last_day].copy()
    out["time"] = out["datetime"].str.slice(11, 16)
    for col in ("open", "high", "low", "close", "volume"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["open", "high", "low", "close"])
    if out.empty:
        return None
    return out[["date", "time", "open", "high", "low", "close", "volume"]].reset_index(drop=True)


def stock_intraday_minutes(code: str, period: str = "5", ttl: int = 300) -> pd.DataFrame | None:
    """个股日内分钟K线：新浪优先（不走东财 push2，规避限流），东财降级。

    period: 1/5/15/30/60 分钟。返回最近一个交易日的分钟K线。
    """
    code = str(code).strip().zfill(6)
    period = str(period) if str(period) in ("1", "5", "15", "30", "60") else "5"

    def fetch() -> pd.DataFrame | None:
        sina = find_function("stock_zh_a_hist_min_sina")
        if sina:
            try:
                df = sina[0](symbol=_tx_symbol(code), period=period, adjust="")
                norm = _normalize_minutes(df)
                if norm is not None and len(norm) >= 5:
                    return norm
            except Exception as exc:  # noqa: BLE001
                print(f"[datasource] 新浪分钟K线 {code}/{period} 失败: {str(exc)[:80]}")

        em = find_function("stock_zh_a_hist_min_em")
        if em:
            try:
                # 休市时取最近交易日，避免用周末日期请求分钟线拿到空数据
                today = datetime.strptime(effective_trading_date()["date"], "%Y%m%d").strftime("%Y-%m-%d")
                df = em[0](symbol=code, period=period,
                           start_date=f"{today} 09:00:00", end_date=f"{today} 15:30:00",
                           adjust="")
                norm = _normalize_minutes(df)
                if norm is not None and len(norm) >= 5:
                    return norm
            except Exception as exc:  # noqa: BLE001
                print(f"[datasource] 东财分钟K线 {code}/{period} 失败: {str(exc)[:80]}")
        return None

    return cache.get_or_fetch(f"intraday:{code}:{period}", ttl, fetch)


# ----------------------------- 全市场扫描支撑 -----------------------------

def _trading_dates_back(days: int) -> list[str]:
    """取最近 days 个交易日日期（用上证指数日线反推，避开节假日）。"""
    found = find_function("stock_zh_index_daily_em", "stock_zh_index_daily")
    if found:
        df = _safe_call(found[0], symbol="sh000001")
        if df is not None and not df.empty:
            col = "date" if "date" in df.columns else df.columns[0]
            dates = [str(x)[:10].replace("-", "") for x in df[col].tail(days + 5).tolist()]
            dates = [d for d in dates if len(d) == 8 and d.isdigit()]
            return dates[-days:]
    # 降级：自然日回溯（周末跳过，法定节假日可能多取几次空涨停池，不影响结果）
    from datetime import timedelta
    out: list[str] = []
    cur = date.today()
    while len(out) < days:
        if cur.weekday() < 5:
            out.append(cur.strftime("%Y%m%d"))
        cur -= timedelta(days=1)
    return list(reversed(out))


def limit_up_pool_codes(days: int = 20, end_date: str | None = None,
                        ttl: int = 1800) -> tuple[list[str], dict[str, str], dict[str, float]]:
    """截止 end_date 的近 N 个交易日在涨停池出现过的股票（去重）。

    涨停回调战法的前提是「近期涨停过」（首板或连板均可），因此只需扫描
    近期涨停池股票，而非全市场 5000+ 只。返回 (代码列表, 代码->名称, 代码->最新价)。
    最新价取涨停池里最近一次记录的价格，用于价格区间筛选。

    end_date 为空时取最近交易日；非交易日扫描应显式传入有效交易日，
    避免把休市日当作候选窗口的终点。缓存按截止日隔离，跨日不串数据。
    """
    cutoff = str(end_date).strip() if end_date else ""
    cache_key = f"limitup_pool_codes:{days}:{cutoff or 'latest'}"

    def fetch() -> tuple[list[str], dict[str, str], dict[str, float]] | None:
        fn = find_function("stock_zt_pool_em")
        if not fn:
            return None
        codes: dict[str, str] = {}
        prices: dict[str, float] = {}
        dates = _trading_dates_back(days + 10)
        if cutoff:
            dates = [d for d in dates if d <= cutoff]
        dates = dates[-days:]
        for d in dates:
            df = _safe_call(fn[0], date=d)
            if df is None or df.empty or "代码" not in df.columns:
                continue
            nc = "名称" if "名称" in df.columns else None
            pc = "最新价" if "最新价" in df.columns else None
            for _, row in df.iterrows():
                code = str(row["代码"]).strip().zfill(6)
                if not code.isdigit():
                    continue
                codes[code] = str(row[nc]).strip() if nc else code
                if pc:
                    try:
                        prices[code] = float(row[pc])
                    except (TypeError, ValueError):
                        pass
        if not codes:
            return None
        return list(codes.keys()), codes, prices

    result = cache.get_or_fetch(cache_key, ttl, fetch)
    return result if result else ([], {}, {})


def limit_up_pool_detail(trade_date: str, ttl: int = 3600) -> list[dict[str, Any]]:
    """指定交易日涨停池明细（代码/名称/连板数/所属行业/炸板次数/封板资金）。"""
    key = f"limitup_pool_detail:{trade_date}"

    def fetch() -> list[dict[str, Any]] | None:
        fn = find_function("stock_zt_pool_em")
        if not fn:
            return None
        df = _safe_call(fn[0], date=trade_date)
        if df is None or df.empty or "代码" not in df.columns:
            return None
        out: list[dict[str, Any]] = []
        for _, row in df.iterrows():
            code = str(row["代码"]).strip().zfill(6)
            if not code.isdigit():
                continue
            def num(col: str) -> float:
                try:
                    v = pd.to_numeric(row[col], errors="coerce")
                    return 0.0 if pd.isna(v) else float(v)
                except (KeyError, TypeError, ValueError):
                    return 0.0
            out.append({
                "code": code,
                "name": str(row.get("名称", code)).strip(),
                "industry": str(row.get("所属行业", "--")).strip(),
                "streak": int(num("连板数")),
                "break_count": int(num("炸板次数")),
                "seal_fund": num("封板资金"),
                "limit_price": num("最新价"),
            })
        return out or None

    result = cache.get_or_fetch(key, ttl, fetch)
    return result or []


def _date_label(d: str) -> str:
    """YYYYMMDD -> 2026-11-20 周五（无法解析时原样返回）。"""
    try:
        wd = "周" + "一二三四五六日"[datetime.strptime(d, "%Y%m%d").weekday()]
        return f"{d[:4]}-{d[4:6]}-{d[6:]} {wd}"
    except (ValueError, IndexError):
        return d


def effective_trading_date(now: datetime | None = None) -> dict[str, Any]:
    """行情数据实际对应的交易日——全模块统一日期口径（带 5 分钟缓存）。

    显式传入 now 时不做缓存（供用例验证）；无参调用走缓存，避免全市场扫描
    对数百只股票取历史K线时反复请求指数日线。
    """
    if now is None:
        cached = cache.get_or_fetch(
            "effective_trading_date", 300,
            lambda: _effective_trading_date_impl(datetime.now()),
        )
        if cached:
            return cached
    return _effective_trading_date_impl(now or datetime.now())


def _effective_trading_date_impl(now: datetime) -> dict[str, Any]:
    """effective_trading_date 的实际计算逻辑。

    - 交易日盘中/午休/盘后：取当日；若指数日线没有当日记录（法定节假日），
      自动回退到最近交易日；
    - 盘前、周末、节假日：取最近一个已收盘交易日。

    返回 date(YYYYMMDD)、label(带星期)、is_closed(是否非交易时段)、
    market_status / market_label，供扫描状态与通知文案标注。
    """
    market = market_status(now)
    today = now.strftime("%Y%m%d")
    dates = _trading_dates_back(8)
    in_session = market["status"] in ("trading", "lunch", "after_hours")
    # 工作日但指数日线没有当日记录 → 法定节假日，按休市处理
    if in_session and dates and today not in dates:
        market = {**market, "status": "closed", "label": "休市（节假日）"}
        in_session = False
    if in_session and (not dates or today in dates):
        d, closed = today, False
    elif market["status"] == "pre_open":
        # 盘前当日尚未开盘，行情反映的不是今天，只能取严格早于今日的交易日
        earlier = [x for x in dates if x < today]
        d = earlier[-1] if earlier else (dates[-1] if dates else today)
        closed = True
    else:
        d = dates[-1] if dates else today
        closed = True
    return {
        "date": d,
        "label": _date_label(d),
        "is_closed": closed,
        "market_status": market["status"],
        "market_label": market["label"],
    }


def previous_trading_date() -> str:
    """最近一个已收盘的交易日（YYYYMMDD）。"""
    dates = _trading_dates_back(4)
    today = date.today().strftime("%Y%m%d")
    if dates and dates[-1] == today:
        return dates[-2] if len(dates) >= 2 else dates[-1]
    return dates[-1] if dates else today


def followup_date_pair(now: datetime | None = None) -> dict[str, str]:
    """「昨日涨停跟进」的日期对：涨停基准日 + 表现观察日。

    语义（用户口径）：今天看昨天的。表现观察日 = 行情接口当前实际反映的交易日；
    涨停基准日 = 表现观察日的前一个交易日。取基准日涨停池，看这些股票在观察日的表现。

    - 交易日盘中/午休/盘后：表现观察日 = 当日（如周五看周四涨停股在周五的表现）；
    - 交易日盘前、周末、节假日：行情接口反映最近已收盘交易日，表现观察日 = 该交易日
      （如周六看周四涨停股在周五收盘的表现，而不是周五涨停股自己跟自己比）。

    返回 base_date/observe_date（YYYYMMDD）及带星期几的中文标签。
    """
    now = now or datetime.now()
    dates = _trading_dates_back(8)  # 升序交易日（用指数日线反推，避开节假日）

    # 表现观察日 = 行情接口当前实际对应的交易日（与全市场扫描统一口径）
    observe = effective_trading_date(now)["date"]

    # 涨停基准日 = 表现观察日的前一个交易日
    base: str | None = None
    if observe in dates:
        idx = dates.index(observe)
        if idx >= 1:
            base = dates[idx - 1]
    if base is None:
        earlier = [d for d in dates if d < observe]
        base = earlier[-1] if earlier else (dates[-1] if dates else observe)

    return {
        "base_date": base,
        "observe_date": observe,
        "base_label": _date_label(base),
        "observe_label": _date_label(observe),
    }


def industry_map_full(ttl: int = 43200) -> dict[str, str]:
    """全市场代码→行业映射（业绩报表「所处行业」，东财 datacenter 口径）。

    与 push2 行情接口限流隔离；未披露财报的股票（次新等）缺失，
    板块聚合检测自动忽略缺失个股。12小时缓存。
    """
    def fetch() -> dict[str, str] | None:
        fn = find_function("stock_yjbb_em")
        if not fn:
            return None
        out: dict[str, str] = {}
        for rd in ("20260930", "20260630"):  # 最新已披露报告期优先
            df = _safe_call(fn[0], date=rd)
            if df is None or df.empty:
                continue
            for _, row in df.iterrows():
                code = str(row.get("股票代码", "")).strip().zfill(6)
                ind = str(row.get("所处行业", "")).strip()
                if code.isdigit() and ind:
                    out.setdefault(code, ind)
            if out:
                break
        return out or None

    return cache.get_or_fetch("industry_map_full", ttl, fetch) or {}


def fund_flow_map(ttl: int = 180) -> dict[str, dict[str, float | str]]:
    """全市场个股资金流映射：代码 -> 净额/流入/流出/换手/涨跌幅。

    优先同花顺（一次拿全市场约 5200 只，不受东财 push2 限流影响），
    东财 rank 接口作为补充。数值统一为「元」，便于前端展示万/亿。
    """
    def fetch() -> dict[str, dict[str, float | str]] | None:
        result: dict[str, dict[str, float | str]] = {}
        ths = find_function("stock_fund_flow_individual_ths")
        if ths:
            df = _safe_call(ths[0], symbol="即时")
            if df is not None and not df.empty and "股票代码" in df.columns:
                def num(row: Any, *cols: str) -> float:
                    for c in cols:
                        if c in df.columns:
                            try:
                                v = row[c]
                                if isinstance(v, str):
                                    v = v.replace(",", "")
                                    if v.endswith("亿"):
                                        return float(v[:-1]) * 1e8
                                    if v.endswith("万"):
                                        return float(v[:-1]) * 1e4
                                return float(v)
                            except (TypeError, ValueError):
                                return 0.0
                    return 0.0
                for _, row in df.iterrows():
                    code = str(row["股票代码"]).strip().zfill(6)
                    if not code.isdigit():
                        continue
                    result[code] = {
                        "net": num(row, "净额"),
                        "inflow": num(row, "流入资金"),
                        "outflow": num(row, "流出资金"),
                        "turnover": num(row, "换手率"),
                        "change_pct": num(row, "涨跌幅"),
                        "price": num(row, "最新价"),
                        "source": "ths",
                    }
        if result:
            return result
        # 降级：东财 rank（列结构随版本变化，尽量兼容）
        em = find_function("stock_fund_flow_individual_rank_em")
        if em:
            import inspect
            kwargs: dict[str, Any] = {}
            try:
                if "indicator" in inspect.signature(em[0]).parameters:
                    kwargs = {"indicator": "今日"}
            except Exception:  # noqa: BLE001
                pass
            df = _safe_call(em[0], **kwargs)
            if df is not None and not df.empty:
                cc = next((c for c in ("代码", "股票代码") if c in df.columns), None)
                if cc:
                    for _, row in df.iterrows():
                        code = str(row[cc]).strip().zfill(6)
                        if not code.isdigit():
                            continue
                        def g(*names: str) -> float:
                            for n in names:
                                if n in df.columns:
                                    try:
                                        return float(str(row[n]).replace(",", ""))
                                    except (TypeError, ValueError):
                                        return 0.0
                            return 0.0
                        result[code] = {
                            "net": g("主力净流入-净额", "主力净流入"),
                            "inflow": g("主力流入"),
                            "outflow": g("主力流出"),
                            "turnover": 0.0,
                            "change_pct": g("今日涨跌幅"),
                            "price": 0.0,
                            "source": "em",
                        }
        return result or None

    return cache.get_or_fetch("fund_flow_map", ttl, fetch) or {}


def enrich_market_metrics(code: str, hist: Any,
                          flows: dict[str, dict[str, float | str]] | None = None,
                          names: dict[str, str] | None = None) -> dict[str, Any]:
    """从日K与资金流映射补齐单个股票的展示指标。"""
    flows = flows if flows is not None else fund_flow_map()
    names = names if names is not None else stock_code_names()
    metrics: dict[str, Any] = {
        "name": names.get(code, code),
        "volume_ratio": None,
        "turnover": None,
        "net_inflow": None,
        "inflow": None,
        "outflow": None,
    }
    if hist is not None and not getattr(hist, "empty", True) and "volume" in hist.columns:
        try:
            vols = hist["volume"].astype(float)
            cur_vol = float(vols.iloc[-1])
            base = float(vols.iloc[-6:-1].mean()) if len(vols) >= 6 else 0.0
            if base > 0:
                metrics["volume_ratio"] = round(cur_vol / base, 2)
            if "turnover" in hist.columns:
                tv = hist["turnover"].iloc[-1]
                if pd.notna(tv):
                    # _normalize_history 已统一为百分数（腾讯源 ×100、东财原本即百分数），
                    # 这里不能再乘 100，否则会出现 463% 这类异常值。
                    metrics["turnover"] = round(float(tv), 2)
        except (TypeError, ValueError, IndexError):
            pass
    flow = flows.get(code)
    if flow:
        try:
            metrics["net_inflow"] = round(float(flow["net"]), 2)
            metrics["inflow"] = round(float(flow["inflow"]), 2)
            metrics["outflow"] = round(float(flow["outflow"]), 2)
            if metrics["turnover"] is None and float(flow.get("turnover", 0)) > 0:
                metrics["turnover"] = round(float(flow["turnover"]), 2)
        except (TypeError, ValueError, KeyError):
            pass
    return metrics


# ----------------------------- 交易时段 -----------------------------

def market_status(now: datetime | None = None) -> dict[str, Any]:
    """判断 A 股市场状态（北京时间，忽略法定节假日细节）。"""
    now = now or datetime.now()
    weekday = now.weekday()
    t = now.time()
    is_weekday = weekday < 5

    morning_open = datetime.strptime("09:30", "%H:%M").time()
    morning_close = datetime.strptime("11:30", "%H:%M").time()
    afternoon_open = datetime.strptime("13:00", "%H:%M").time()
    afternoon_close = datetime.strptime("15:00", "%H:%M").time()

    if not is_weekday:
        status, label = "closed", "休市（周末）"
    elif morning_open <= t <= morning_close or afternoon_open <= t <= afternoon_close:
        status, label = "trading", "交易中"
    elif morning_close < t < afternoon_open:
        status, label = "lunch", "午间休市"
    elif t < morning_open:
        status, label = "pre_open", "盘前"
    else:
        status, label = "after_hours", "盘后"

    return {
        "status": status,
        "label": label,
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "timezone": "Asia/Shanghai",
    }


def market_regime(ma_period: int = 20, slope_lookback: int = 5,
                  index_code: str = "sh000001", use_cache: bool = True) -> dict[str, Any]:
    """大盘环境：指数是否站上MA20、MA20是否向上（带10分钟缓存）。

    腾讯/sina 指数日线稳定且不限流（东财 push2 常被限流），优先腾讯。
    返回 bull=强市、close/ma/ma_prev/slope、label，供扫描层做开仓闸门。
    取数失败时返回 bull=False 但 degraded=True，由调用方决定是否降级放行。
    """
    key = f"market_regime:{index_code}:{ma_period}:{slope_lookback}"
    def fetch() -> dict[str, Any]:
        df: pd.DataFrame | None = None
        for name in ("index_zh_daily_tx", "index_zh_daily_sina", "index_zh_daily_em"):
            fn = find_function(name)
            if not fn:
                continue
            try:
                got = _safe_call(fn[0], symbol=index_code)
                if got is not None and len(got) >= ma_period + slope_lookback + 2:
                    df = got
                    break
            except Exception:  # noqa: BLE001
                continue
        if df is None:
            return {"bull": False, "degraded": True, "reason": "指数日线不可用",
                    "close": None, "ma": None, "slope": None, "label": "未知"}
        c = df["close"].astype(float).reset_index(drop=True)
        close = float(c.iloc[-1])
        ma = float(c.iloc[-ma_period:].mean())
        ma_prev = float(c.iloc[-ma_period - slope_lookback:-slope_lookback].mean())
        slope = (ma - ma_prev) / ma_prev if ma_prev else 0.0
        above = close >= ma
        rising = slope >= 0
        bull = bool(above and rising)
        label = "强市" if bull else ("弱势·破均线" if not above else "弱势·均线向下")
        return {"bull": bull, "degraded": False, "close": round(close, 2),
                "ma": round(ma, 2), "slope": round(slope * 100, 3),
                "above_ma": above, "rising_ma": rising, "label": label,
                "date": str(df["date"].iloc[-1])[:10]}
    if use_cache:
        return cache.get_or_fetch(key, 600, fetch)
    return fetch()


def ignition_premium(trade_date: str | None = None, ttl: int = 600) -> dict[str, Any]:
    """情绪点火指标：上一交易日涨停池股票今日的涨幅中位数/均值(%)。

    背景（2026-11-21 三个月全市场 2014 笔信号回测）：昨日涨停股今日的
    溢价是「情绪点火」探测器——点火日（中位溢价≥3%）买入信号胜率 44.9%
    （基线 22.8%），配合洗盘反包单战法 64.5%、平均收益 +5.36%（基线
    -3.92%）。8/9/10 月点火日分别为 6/21、4/21、2/17 天，即约 1/4 的
    交易日是情绪点火日，其余日子追高信号系统性亏钱。

    盘中扫描取实时涨幅（近似当日收盘溢价），休市时即收盘涨幅。
    trade_date: 数据交易日（eff['date']），取其前一交易日的涨停池。
    数据不可用时返回 degraded=True，由调用方决定放行（不阻塞扫描）。
    """
    key = f"ignition_premium:{trade_date or 'latest'}"

    def fetch() -> dict[str, Any] | None:
        dates = [d for d in _trading_dates_back(10) if not trade_date or d <= trade_date]
        if len(dates) < 2:
            return None
        prev_day = dates[-2]
        detail = limit_up_pool_detail(prev_day, ttl=3600)
        if not detail:
            return None
        codes = [x["code"] for x in detail if x.get("code")]
        if not codes:
            return None
        quotes = spot_quote_map(codes)
        vals: list[float] = []
        for c in codes:
            q = quotes.get(c)
            if not q:
                continue
            try:
                vals.append(float(q.get("change_pct")))
            except (TypeError, ValueError):
                continue
        if not vals:
            return None
        return {"prev_date": prev_day, "n": len(vals),
                "median": round(float(pd.Series(vals).median()), 2),
                "mean": round(sum(vals) / len(vals), 2)}

    result = cache.get_or_fetch(key, ttl, fetch)
    if result:
        return {**result, "degraded": False}
    return {"prev_date": None, "n": 0, "median": None, "mean": None,
            "degraded": True, "reason": "涨停池/行情不可用，点火闸门放行"}


def health() -> dict[str, Any]:
    """数据源健康状态"""
    result: dict[str, Any] = {
        "akshare_installed": False,
        "version": None,
        "interfaces": {},
        "error": None,
    }
    try:
        ak = get_ak()
        result["akshare_installed"] = True
        result["version"] = getattr(ak, "__version__", "unknown")
        for name in (
            "stock_zh_a_spot_em", "stock_fund_flow_individual_rank_em",
            "stock_board_industry_name_em", "stock_board_concept_name_em",
            "stock_lhb_detail_em", "stock_changes_em", "stock_zt_pool_em",
            "stock_fund_flow_sector_rank_em", "stock_board_change_em",
        ):
            result["interfaces"][name] = callable(getattr(ak, name, None))
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)

    # 实际接口调用层面：行情源可能被临时限流，健康状态要如实反映
    result["spot_degraded"] = cache.is_degraded("stock_spot")
    result["fund_degraded"] = cache.is_degraded("fund_flow")
    result["board_degraded"] = cache.is_degraded("industry_boards") or cache.is_degraded("concept_boards")
    result["degraded"] = any([
        result["spot_degraded"],
        result["fund_degraded"],
        result["board_degraded"],
    ])
    return result
