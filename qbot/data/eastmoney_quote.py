"""行情数据拉取：优先东方财富，失败时降级腾讯财经，再降级新浪财经。"""

from __future__ import annotations

import time
from typing import Optional, Tuple, Union

import pandas as pd
import requests

EASTMONEY_KLINE_URLS = (
    "https://push2his.eastmoney.com/api/qt/stock/kline/get",
    "https://60.push2his.eastmoney.com/api/qt/stock/kline/get",
    "https://42.push2his.eastmoney.com/api/qt/stock/kline/get",
)
EASTMONEY_QUOTE_URL = "https://push2.eastmoney.com/api/qt/stock/get"
TENCENT_DAY_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
TENCENT_MIN_KLINE_URL = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
SINA_KLINE_URL = (
    "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "CN_MarketData.getKLineData"
)

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "*/*",
    "Connection": "close",
}

PERIOD_MAP = {
    "1分钟": 1,
    "5分钟": 5,
    "15分钟": 15,
    "30分钟": 30,
    "60分钟": 60,
    "日线": 101,
    "周线": 102,
    "月线": 103,
}

ADJUST_MAP = {
    "不复权": 0,
    "前复权": 1,
    "后复权": 2,
}

KLINE_COLUMNS = [
    "date",
    "open",
    "close",
    "high",
    "low",
    "volume",
    "amount",
    "amplitude",
    "pct_chg",
    "change",
    "turnover",
]

_SESSION: Optional[requests.Session] = None
_LAST_EM_CALL = 0.0
_EM_BLOCKED = False  # push2his 被风控后，本进程内跳过东财 K 线以加快降级


def _session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session()
        # 避免系统代理干扰；东财限流时代理也常会放大失败
        _SESSION.trust_env = False
    return _SESSION


def _throttle_em(min_interval: float = 1.0) -> None:
    global _LAST_EM_CALL
    now = time.time()
    wait = min_interval - (now - _LAST_EM_CALL)
    if wait > 0:
        time.sleep(wait)
    _LAST_EM_CALL = time.time()


def normalize_symbol(code: str) -> str:
    """去掉交易所后缀，只保留数字代码。"""
    code = (code or "").strip().upper()
    if "." in code:
        code = code.split(".")[0]
    return code


def market_prefix(code: str) -> str:
    """返回腾讯行情前缀：sh / sz。"""
    raw = (code or "").strip().upper()
    if "." in raw:
        symbol, suffix = raw.split(".", 1)
        if suffix in ("SH", "SS"):
            return "sh"
        if suffix in ("SZ", "BJ"):
            return "sz"
        raise ValueError(f"不支持的交易所后缀: {suffix}")
    symbol = raw
    if symbol.startswith(("6", "5", "9")):
        return "sh"
    return "sz"


def to_secid(code: str) -> str:
    """
    将股票/指数代码转为东方财富 secid。
    优先使用 .SH / .SZ 后缀；无后缀时按代码规则推断。
    """
    prefix = market_prefix(code)
    symbol = normalize_symbol(code)
    if not symbol.isdigit():
        raise ValueError(f"无效的股票代码: {code}")
    return ("1." if prefix == "sh" else "0.") + symbol


def _parse_period_adjust(
    period: Union[str, int], adjust: Union[str, int]
) -> Tuple[int, int]:
    if isinstance(period, str):
        if period not in PERIOD_MAP:
            raise ValueError(f"不支持的股票周期: {period}")
        klt = PERIOD_MAP[period]
    else:
        klt = int(period)

    if isinstance(adjust, str):
        if adjust not in ADJUST_MAP:
            raise ValueError(f"不支持的复权方式: {adjust}")
        fqt = ADJUST_MAP[adjust]
    else:
        fqt = int(adjust)
    return klt, fqt


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=["name", "code"] + KLINE_COLUMNS)


def _finalize_frame(df: pd.DataFrame, name: str, symbol: str, source: str) -> pd.DataFrame:
    if df.empty:
        out = _empty_frame()
    else:
        out = df.copy()
        if "code" not in out.columns:
            out.insert(0, "code", symbol)
        if "name" not in out.columns:
            out.insert(0, "name", name or "")
        # 统一列顺序
        cols = ["name", "code"] + [c for c in KLINE_COLUMNS if c in out.columns]
        extras = [c for c in out.columns if c not in cols]
        out = out[cols + extras]
    out.attrs["source"] = source
    return out


def fetch_stock_name(code: str, timeout: int = 10) -> str:
    """通过东方财富实时接口获取证券简称（该接口通常比 K 线更稳定）。"""
    try:
        _throttle_em(0.3)
        resp = _session().get(
            EASTMONEY_QUOTE_URL,
            params={
                "secid": to_secid(code),
                "fields": "f57,f58",
                "ut": "fa5fd1943c7b386f172d6893dbfba10b",
            },
            headers=REQUEST_HEADERS,
            timeout=timeout,
        )
        resp.raise_for_status()
        data = (resp.json() or {}).get("data") or {}
        return str(data.get("f58") or "")
    except Exception:
        return ""


def _parse_em_klines(payload: dict, code: str) -> pd.DataFrame:
    data = payload.get("data") or {}
    name = data.get("name") or ""
    symbol = data.get("code") or normalize_symbol(code)
    klines = data.get("klines") or []
    if not klines:
        return _finalize_frame(_empty_frame(), name, symbol, "东方财富")

    rows = [item.split(",") for item in klines]
    df = pd.DataFrame(rows, columns=KLINE_COLUMNS)
    for col in [c for c in KLINE_COLUMNS if c != "date"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return _finalize_frame(df, name, symbol, "东方财富")


def fetch_kline_eastmoney(
    code: str,
    begin: str,
    end: str,
    period: Union[str, int] = "日线",
    adjust: Union[str, int] = "不复权",
    timeout: int = 15,
) -> pd.DataFrame:
    """仅从东方财富 push2his 拉取 K 线。"""
    klt, fqt = _parse_period_adjust(period, adjust)
    params = {
        "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "beg": begin,
        "end": end,
        "rtntype": 6,
        "secid": to_secid(code),
        "klt": klt,
        "fqt": fqt,
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
    }

    last_error: Optional[Exception] = None
    for url in EASTMONEY_KLINE_URLS:
        _throttle_em(1.0)
        try:
            resp = _session().get(
                url,
                params=params,
                headers=REQUEST_HEADERS,
                timeout=timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
            df = _parse_em_klines(payload, code)
            if df.empty:
                raise RuntimeError("东方财富返回空 K 线")
            return df
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(0.3)
    raise RuntimeError(f"东方财富行情接口请求失败: {last_error}") from last_error


def _tencent_symbol(code: str) -> str:
    return f"{market_prefix(code)}{normalize_symbol(code)}"


def _tencent_adjust_tag(fqt: int) -> str:
    if fqt == 1:
        return "qfq"
    if fqt == 2:
        return "hfq"
    return ""


def _filter_by_date(df: pd.DataFrame, begin: str, end: str) -> pd.DataFrame:
    if df.empty or "date" not in df.columns:
        return df
    begin_s = f"{begin[:4]}-{begin[4:6]}-{begin[6:8]}"
    end_s = f"{end[:4]}-{end[4:6]}-{end[6:8]}"
    dates = df["date"].astype(str)
    # 分钟线日期形如 202405011030，截前 8/10 位比较
    day = dates.str.replace("-", "").str.replace(" ", "").str[:8]
    begin_n = begin.replace("-", "")[:8]
    end_n = end.replace("-", "")[:8]
    mask = (day >= begin_n) & (day <= end_n)
    # 同时兼容标准 YYYY-MM-DD
    mask2 = (dates >= begin_s) & (dates <= end_s)
    return df.loc[mask | mask2].reset_index(drop=True)


def _tencent_parse_day_payload(payload: dict, t_symbol: str, unit: str, cand: str):
    node = ((payload.get("data") or {}).get(t_symbol) or {})
    if cand == "qfq":
        keys = ["qfq" + unit, "qfqday", unit, "day"]
    elif cand == "hfq":
        keys = ["hfq" + unit, "hfqday", unit, "day"]
    else:
        keys = [unit, "day", "qfq" + unit, "qfqday"]
    for key in keys:
        if node.get(key):
            return node.get(key) or []
    return []


def _tencent_request_day_rows(
    t_symbol: str,
    unit: str,
    begin_dash: str,
    end_dash: str,
    adj: str,
    headers: dict,
    timeout: int,
):
    """请求腾讯日/周/月 K，返回 (rows, payload, used_adj)。"""
    import json

    # 腾讯 fqkline 对“不复权”经常直接返回空，需带 qfq/hfq
    adj_candidates = [adj] if adj else ["qfq", "hfq", ""]
    # 若用户指定了复权，仍把该复权放首位
    if adj and adj not in adj_candidates:
        adj_candidates = [adj] + adj_candidates

    # 带日期的请求易触发腾讯 WAF(501)；空日期区间更稳，再本地按 begin/end 过滤
    param_styles = [
        ("ranged", begin_dash, end_dash, 640),
        ("undated", "", "", 640),
    ]

    last_payload = {}
    for cand in adj_candidates:
        for _style, b0, e0, cnt in param_styles:
            if cand:
                param = f"{t_symbol},{unit},{b0},{e0},{cnt},{cand}"
            else:
                param = f"{t_symbol},{unit},{b0},{e0},{cnt}"
            try:
                resp = _session().get(
                    TENCENT_DAY_KLINE_URL,
                    params={"param": param},
                    headers=headers,
                    timeout=timeout,
                )
            except Exception:
                continue
            if resp.status_code >= 400:
                continue
            text = (resp.text or "").strip()
            if not text or text.startswith("<!") or text.startswith("<html"):
                continue
            if text.startswith("kline_") or text.startswith("ifzq_"):
                text = text.split("=", 1)[-1]
            try:
                payload = json.loads(text)
            except Exception:
                continue
            last_payload = payload
            rows = _tencent_parse_day_payload(payload, t_symbol, unit, cand)
            if rows:
                return rows, payload, cand or "none"
    return [], last_payload, adj or "none"


def fetch_kline_tencent(
    code: str,
    begin: str,
    end: str,
    period: Union[str, int] = "日线",
    adjust: Union[str, int] = "不复权",
    timeout: int = 30,
) -> pd.DataFrame:
    """腾讯财经免费 K 线（东财被风控时的可靠降级源）。"""
    klt, fqt = _parse_period_adjust(period, adjust)
    symbol = normalize_symbol(code)
    t_symbol = _tencent_symbol(code)
    adj = _tencent_adjust_tag(fqt)
    name = ""
    source_label = "腾讯财经"

    begin_dash = f"{begin[:4]}-{begin[4:6]}-{begin[6:8]}"
    end_dash = f"{end[:4]}-{end[4:6]}-{end[6:8]}"

    headers = {
        "User-Agent": REQUEST_HEADERS["User-Agent"],
        "Referer": "https://finance.qq.com/",
        "Accept": "*/*",
    }

    payload = {}
    if klt in (101, 102, 103):
        unit = {101: "day", 102: "week", 103: "month"}[klt]
        rows, payload, used_adj = _tencent_request_day_rows(
            t_symbol, unit, begin_dash, end_dash, adj, headers, timeout
        )
        if used_adj == "qfq" and fqt == 0:
            source_label = "腾讯财经(前复权降级)"
        elif used_adj == "hfq" and fqt == 0:
            source_label = "腾讯财经(后复权降级)"
    else:
        # 分钟线
        unit = {1: "m1", 5: "m5", 15: "m15", 30: "m30", 60: "m60"}.get(klt)
        if not unit:
            raise ValueError(f"腾讯接口不支持该周期: {period}")
        param = f"{t_symbol},{unit},,640"
        resp = _session().get(
            TENCENT_MIN_KLINE_URL,
            params={"param": param},
            headers=headers,
            timeout=timeout,
        )
        resp.raise_for_status()
        text = (resp.text or "").strip()
        if not text:
            raise RuntimeError("腾讯财经返回空响应")
        payload = resp.json()
        node = ((payload.get("data") or {}).get(t_symbol) or {})
        rows = node.get(unit) or []

    if not rows:
        return _finalize_frame(_empty_frame(), name, symbol, source_label)

    parsed = []
    for item in rows:
        # ["2024-05-06","15.342","15.732","15.942","15.342","524046.000"]
        if not isinstance(item, (list, tuple)) or len(item) < 6:
            continue
        date, open_, close, high, low, volume = item[:6]
        parsed.append(
            {
                "date": str(date),
                "open": float(open_),
                "close": float(close),
                "high": float(high),
                "low": float(low),
                "volume": float(volume),
            }
        )

    df = pd.DataFrame(parsed)
    if df.empty:
        return _finalize_frame(_empty_frame(), name, symbol, source_label)

    if not name:
        qt = ((payload.get("data") or {}).get(t_symbol) or {}).get("qt") or {}
        qt_row = qt.get(t_symbol) or []
        if isinstance(qt_row, list) and len(qt_row) > 1:
            name = str(qt_row[1] or "")

    df = _filter_by_date(df, begin, end)
    return _finalize_frame(df, name, symbol, source_label)


def _sina_symbol(code: str) -> str:
    return f"{market_prefix(code)}{normalize_symbol(code)}"


def _resample_ohlcv_month(df: pd.DataFrame) -> pd.DataFrame:
    """由日K聚合月K。"""
    if df is None or df.empty:
        return pd.DataFrame(columns=["date", "open", "close", "high", "low", "volume"])
    x = df.copy()
    x["_dt"] = pd.to_datetime(x["date"], errors="coerce")
    x = x.dropna(subset=["_dt"]).sort_values("_dt")
    if x.empty:
        return pd.DataFrame(columns=["date", "open", "close", "high", "low", "volume"])
    x["_ym"] = x["_dt"].dt.to_period("M")
    rows = []
    for _, g in x.groupby("_ym", sort=True):
        rows.append(
            {
                "date": g["_dt"].iloc[-1].strftime("%Y-%m-%d"),
                "open": float(g["open"].iloc[0]),
                "close": float(g["close"].iloc[-1]),
                "high": float(g["high"].max()),
                "low": float(g["low"].min()),
                "volume": float(pd.to_numeric(g["volume"], errors="coerce").fillna(0).sum()),
            }
        )
    return pd.DataFrame(rows)


def fetch_kline_sina(
    code: str,
    begin: str,
    end: str,
    period: Union[str, int] = "日线",
    adjust: Union[str, int] = "不复权",
    timeout: int = 20,
) -> pd.DataFrame:
    """
    新浪财经 K 线兜底（东财/腾讯均失败时）。
    scale: 240=日，1200=周；月线由日K聚合。新浪接口本身不区分复权参数。
    """
    klt, _fqt = _parse_period_adjust(period, adjust)
    symbol = normalize_symbol(code)
    s_symbol = _sina_symbol(code)
    source_label = "新浪财经"

    if klt not in (101, 102, 103):
        # 分钟线暂不走新浪日K接口
        return _finalize_frame(_empty_frame(), "", symbol, source_label)

    # 估算根数：日约按自然日/0.7，周/月放大
    try:
        b_dt = pd.Timestamp(f"{begin[:4]}-{begin[4:6]}-{begin[6:8]}")
        e_dt = pd.Timestamp(f"{end[:4]}-{end[4:6]}-{end[6:8]}")
        span_days = max(int((e_dt - b_dt).days), 30)
    except Exception:
        span_days = 400

    if klt == 101:
        scale, datalen = 240, min(max(int(span_days * 0.8) + 20, 120), 1023)
    elif klt == 102:
        scale, datalen = 1200, min(max(int(span_days / 5) + 10, 80), 1023)
    else:
        # 月：先拉足够日K再聚合
        scale, datalen = 240, min(max(int(span_days * 0.8) + 40, 250), 1023)

    headers = {
        "User-Agent": REQUEST_HEADERS["User-Agent"],
        "Referer": "https://finance.sina.com.cn/",
        "Accept": "*/*",
    }
    try:
        resp = _session().get(
            SINA_KLINE_URL,
            params={"symbol": s_symbol, "scale": scale, "ma": "no", "datalen": datalen},
            headers=headers,
            timeout=timeout,
        )
        resp.raise_for_status()
        rows = resp.json()
    except Exception:
        return _finalize_frame(_empty_frame(), "", symbol, source_label)

    if not isinstance(rows, list) or not rows:
        return _finalize_frame(_empty_frame(), "", symbol, source_label)

    parsed = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        try:
            parsed.append(
                {
                    "date": str(item.get("day") or item.get("date") or "")[:10],
                    "open": float(item["open"]),
                    "close": float(item["close"]),
                    "high": float(item["high"]),
                    "low": float(item["low"]),
                    "volume": float(item.get("volume") or 0),
                }
            )
        except (TypeError, ValueError, KeyError):
            continue

    df = pd.DataFrame(parsed)
    if df.empty:
        return _finalize_frame(_empty_frame(), "", symbol, source_label)

    if klt == 103:
        df = _resample_ohlcv_month(df)
        source_label = "新浪财经(日K聚合月)"

    # 补涨跌幅等可选字段，便于详情页展示
    if not df.empty and "close" in df.columns:
        prev = df["close"].shift(1)
        df["pct_chg"] = ((df["close"] / prev - 1.0) * 100.0).where(prev.notna(), 0.0)
        df["change"] = (df["close"] - prev).where(prev.notna(), 0.0)

    df = _filter_by_date(df, begin, end)
    return _finalize_frame(df, "", symbol, source_label)


def fetch_kline(
    code: str,
    begin: str,
    end: str,
    period: Union[str, int] = "日线",
    adjust: Union[str, int] = "不复权",
    timeout: int = 30,
) -> pd.DataFrame:
    """
    拉取 K 线：优先东方财富 → 腾讯财经 → 新浪财经。

    返回的 DataFrame.attrs['source'] 标明实际数据源。
    """
    global _EM_BLOCKED
    errors = []

    if not _EM_BLOCKED:
        try:
            df = fetch_kline_eastmoney(
                code=code,
                begin=begin,
                end=end,
                period=period,
                adjust=adjust,
                timeout=min(timeout, 12),
            )
            if not df.empty:
                return df
            errors.append("东方财富返回空数据")
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "RemoteDisconnected" in msg or "Connection aborted" in msg:
                _EM_BLOCKED = True
            errors.append(f"东方财富: {exc}")
    else:
        errors.append("东方财富: 本机IP疑似被风控，已跳过")

    try:
        df = fetch_kline_tencent(
            code=code,
            begin=begin,
            end=end,
            period=period,
            adjust=adjust,
            timeout=timeout,
        )
        if not df.empty:
            return df
        errors.append("腾讯财经返回空数据")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"腾讯财经: {exc}")

    try:
        df = fetch_kline_sina(
            code=code,
            begin=begin,
            end=end,
            period=period,
            adjust=adjust,
            timeout=min(timeout, 20),
        )
        if not df.empty:
            return df
        errors.append("新浪财经返回空数据")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"新浪财经: {exc}")

    raise RuntimeError("；".join(errors) if errors else "行情获取失败")
