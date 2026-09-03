"""个股资金流向：主力 / 超大单 / 大单 / 中单 / 散户(小单) / 北向资金。"""

from __future__ import annotations

import time
from typing import Optional, Tuple

import pandas as pd
import requests

from qbot.data.eastmoney_quote import (
    REQUEST_HEADERS,
    _session,
    _throttle_em,
    normalize_symbol,
    to_secid,
)

# 历史日 K 资金流：push2his 才有完整序列；push2delay 常只给 1 日，仅作兜底
FFLOW_HIS_URLS = (
    "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
    "https://90.push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
    "https://82.push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
)
FFLOW_DELAY_URL = "https://push2delay.eastmoney.com/api/qt/stock/fflow/daykline/get"
MIN_USEFUL_KLINES = 5
# 展示/拉取默认看近 30 个交易日即可
DEFAULT_LOOKBACK_DAYS = 30

NORTHBOUND_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def _fflow_sessions() -> list:
    """
    依次尝试：直连（trust_env=False）与系统代理（trust_env=True）。
    本机若配置了失效代理，akshare 默认会 ProxyError；直连又可能被东财短暂掐断。
    """
    sessions = []
    for trust in (False, True):
        s = requests.Session()
        s.trust_env = trust
        sessions.append(s)
    return sessions


def _parse_fflow_payload(payload: dict) -> pd.DataFrame:
    klines = ((payload or {}).get("data") or {}).get("klines") or []
    rows = []
    for line in klines:
        parts = line.split(",")
        if len(parts) < 6:
            continue
        rows.append(
            {
                "date": parts[0],
                "main_net": float(parts[1]),
                "retail_net": float(parts[2]),  # 小单≈散户
                "mid_net": float(parts[3]),
                "large_net": float(parts[4]),
                "super_net": float(parts[5]),
                "main_pct": float(parts[6]) if len(parts) > 6 else None,
                "retail_pct": float(parts[7]) if len(parts) > 7 else None,
            }
        )
    df = pd.DataFrame(rows)
    if df.empty:
        return df

    df["date"] = pd.to_datetime(df["date"])
    for col in ["main_net", "retail_net", "mid_net", "large_net", "super_net"]:
        df[col + "_yi"] = df[col] / 1e8
    return df


def _finalize_fund_flow(
    df: pd.DataFrame,
    begin: Optional[str] = None,
    end: Optional[str] = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> pd.DataFrame:
    """按日期过滤后取最近 N 日，并重算累计。"""
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.sort_values("date").reset_index(drop=True)
    if begin:
        out = out[out["date"] >= pd.to_datetime(begin)]
    if end:
        out = out[out["date"] <= pd.to_datetime(end)]
    if lookback_days and lookback_days > 0 and len(out) > lookback_days:
        out = out.tail(lookback_days)
    out = out.reset_index(drop=True)
    if out.empty:
        return out
    out["main_cum_yi"] = out["main_net_yi"].cumsum()
    out["retail_cum_yi"] = out["retail_net_yi"].cumsum()
    return out


def _request_fflow_klines(
    url: str,
    secid: str,
    timeout: int,
    session: requests.Session,
    lmt: int = DEFAULT_LOOKBACK_DAYS,
) -> Tuple[list, dict]:
    params = {
        "lmt": str(max(int(lmt), 0)),
        "klt": "101",
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
        "secid": secid,
        "ut": "b2884a393a59ad64002292a3e90d46a5",
        "_": int(time.time() * 1000),
    }
    headers = {
        "User-Agent": REQUEST_HEADERS["User-Agent"],
        "Referer": "https://data.eastmoney.com/zjlx/",
        "Accept": "*/*",
    }
    resp = session.get(url, params=params, headers=headers, timeout=timeout)
    resp.raise_for_status()
    payload = resp.json() or {}
    klines = ((payload.get("data") or {}).get("klines") or [])
    return klines, payload


def _df_from_bill_table(raw: pd.DataFrame) -> pd.DataFrame:
    """把 efinance/akshare 风格的资金流表转成内部字段。"""
    col_main = "主力净流入" if "主力净流入" in raw.columns else "主力净流入-净额"
    col_retail = "小单净流入" if "小单净流入" in raw.columns else "小单净流入-净额"
    col_mid = "中单净流入" if "中单净流入" in raw.columns else "中单净流入-净额"
    col_large = "大单净流入" if "大单净流入" in raw.columns else "大单净流入-净额"
    col_super = "超大单净流入" if "超大单净流入" in raw.columns else "超大单净流入-净额"
    col_main_pct = "主力净流入占比" if "主力净流入占比" in raw.columns else "主力净流入-净占比"
    col_retail_pct = "小单流入净占比" if "小单流入净占比" in raw.columns else "小单净流入-净占比"
    df = pd.DataFrame(
        {
            "date": pd.to_datetime(raw["日期"]),
            "main_net": pd.to_numeric(raw[col_main], errors="coerce"),
            "retail_net": pd.to_numeric(raw[col_retail], errors="coerce"),
            "mid_net": pd.to_numeric(raw[col_mid], errors="coerce"),
            "large_net": pd.to_numeric(raw[col_large], errors="coerce"),
            "super_net": pd.to_numeric(raw[col_super], errors="coerce"),
            "main_pct": pd.to_numeric(raw[col_main_pct], errors="coerce")
            if col_main_pct in raw.columns
            else None,
            "retail_pct": pd.to_numeric(raw[col_retail_pct], errors="coerce")
            if col_retail_pct in raw.columns
            else None,
        }
    ).dropna(subset=["date"])
    for col in ["main_net", "retail_net", "mid_net", "large_net", "super_net"]:
        df[col + "_yi"] = df[col] / 1e8
    return df


def fetch_stock_fund_flow(
    code: str,
    begin: Optional[str] = None,
    end: Optional[str] = None,
    timeout: int = 20,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> pd.DataFrame:
    """
    个股资金流向（日），默认最近 lookback_days（30）个交易日。

    优先 efinance.get_history_bill（东财同源，本机更稳）；
    失败再试 push2his / push2delay / akshare。

    字段单位：元
    - main_net: 主力净流入（超大单+大单）
    - super_net / large_net / mid_net / retail_net
    """
    symbol = normalize_symbol(code)
    last_error: Optional[Exception] = None

    # 1) efinance：实测在 push2his 被掐时仍可返回约 120 日
    try:
        import efinance as ef

        raw = ef.stock.get_history_bill(symbol)
        if raw is not None and not raw.empty:
            df = _df_from_bill_table(raw)
            clipped = _finalize_fund_flow(
                df, begin=begin, end=end, lookback_days=lookback_days
            )
            if clipped.empty:
                clipped = _finalize_fund_flow(df, lookback_days=lookback_days)
            if not clipped.empty:
                return clipped
    except Exception as exc:  # noqa: BLE001
        last_error = exc

    secid = to_secid(code)
    best_payload = None
    best_n = 0
    req_lmt = max(int(lookback_days or DEFAULT_LOOKBACK_DAYS), DEFAULT_LOOKBACK_DAYS)

    # 2) 东财 push2his
    for session in _fflow_sessions():
        for url in FFLOW_HIS_URLS:
            for attempt in range(2):
                _throttle_em(0.5)
                try:
                    klines, payload = _request_fflow_klines(
                        url, secid, timeout, session, lmt=req_lmt
                    )
                    n = len(klines)
                    if n > best_n:
                        best_payload = payload
                        best_n = n
                    if n >= MIN_USEFUL_KLINES:
                        break
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    time.sleep(0.35 * (attempt + 1))
            if best_n >= MIN_USEFUL_KLINES:
                break
        if best_n >= MIN_USEFUL_KLINES:
            break

    # 3) delay（常只有 1 日）
    if best_n < MIN_USEFUL_KLINES:
        for session in _fflow_sessions():
            _throttle_em(0.4)
            try:
                klines, payload = _request_fflow_klines(
                    FFLOW_DELAY_URL, secid, timeout, session, lmt=req_lmt
                )
                n = len(klines)
                if n > best_n:
                    best_payload = payload
                    best_n = n
                if n > 0:
                    break
            except Exception as exc:  # noqa: BLE001
                last_error = exc

    # 4) akshare
    if best_n < MIN_USEFUL_KLINES:
        try:
            import akshare as ak

            market = "sh" if secid.startswith("1.") else "sz"
            old_get = requests.get

            def _direct_get(*args, **kwargs):
                kwargs.setdefault("timeout", timeout)
                sess = requests.Session()
                sess.trust_env = False
                return sess.get(*args, **kwargs)

            requests.get = _direct_get  # type: ignore[assignment]
            try:
                raw = ak.stock_individual_fund_flow(stock=symbol, market=market)
            finally:
                requests.get = old_get  # type: ignore[assignment]
            if raw is not None and not raw.empty:
                df = _df_from_bill_table(raw)
                clipped = _finalize_fund_flow(
                    df, begin=begin, end=end, lookback_days=lookback_days
                )
                if clipped.empty:
                    clipped = _finalize_fund_flow(df, lookback_days=lookback_days)
                if not clipped.empty:
                    return clipped
        except Exception as exc:  # noqa: BLE001
            last_error = exc

    if not best_payload or best_n <= 0:
        raise RuntimeError(
            "资金流向获取失败（efinance/东财/akshare 均不可用）: "
            f"{last_error or '空数据'}"
        )

    df = _parse_fflow_payload(best_payload)
    clipped = _finalize_fund_flow(df, begin=begin, end=end, lookback_days=lookback_days)
    if clipped.empty:
        clipped = _finalize_fund_flow(df, lookback_days=lookback_days)
    return clipped


def _normalize_northbound_df(raw: pd.DataFrame) -> pd.DataFrame:
    """把东财/akshare 返回统一成内部字段。"""
    if raw is None or raw.empty:
        return pd.DataFrame(
            columns=["date", "north_net", "north_net_yi", "hold_ratio", "hold_shares", "north_cum_yi"]
        )

    colmap = {
        "持股日期": "date",
        "TRADE_DATE": "date",
        "HOLD_DATE": "date",
        "今日增持资金": "north_net",
        "ADD_MARKET_CAP": "north_net",
        "HOLD_MARKET_CAP_CHANGE": "north_net",
        "NET_BUY_AMT": "north_net",
        "今日持股市值变化": "north_net_alt",
        "持股数量占A股百分比": "hold_ratio",
        "HOLD_SHARES_RATIO": "hold_ratio",
        "FREESHARES_RATIO": "hold_ratio",
        "持股数量": "hold_shares",
        "HOLD_SHARES": "hold_shares",
        "HOLD_NUM": "hold_shares",
    }
    df = raw.rename(columns={k: v for k, v in colmap.items() if k in raw.columns}).copy()
    if "date" not in df.columns:
        return pd.DataFrame(
            columns=["date", "north_net", "north_net_yi", "hold_ratio", "hold_shares", "north_cum_yi"]
        )
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    if "north_net" not in df.columns:
        df["north_net"] = df["north_net_alt"] if "north_net_alt" in df.columns else 0.0
    df["north_net"] = pd.to_numeric(df["north_net"], errors="coerce").fillna(0.0)
    if "hold_ratio" in df.columns:
        df["hold_ratio"] = pd.to_numeric(df["hold_ratio"], errors="coerce")
    else:
        df["hold_ratio"] = pd.NA
    if "hold_shares" in df.columns:
        df["hold_shares"] = pd.to_numeric(df["hold_shares"], errors="coerce")
    else:
        df["hold_shares"] = pd.NA
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    # akshare/东财该字段通常已是“元”
    df["north_net_yi"] = df["north_net"] / 1e8
    df["north_cum_yi"] = df["north_net_yi"].cumsum()
    return df[
        ["date", "north_net", "north_net_yi", "hold_ratio", "hold_shares", "north_cum_yi"]
    ]


def fetch_northbound_flow(
    code: str,
    begin: Optional[str] = None,
    end: Optional[str] = None,
    timeout: int = 20,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> pd.DataFrame:
    """
    个股北向资金（沪深港通持股变动），默认最近 lookback_days（30）日。
    优先东方财富数据中心；失败则尝试 akshare。
    """
    symbol = normalize_symbol(code)
    headers = {
        **REQUEST_HEADERS,
        "Referer": f"https://data.eastmoney.com/hsgt/StockHdStatistics/{symbol}.html",
    }
    page_size = max(int(lookback_days or DEFAULT_LOOKBACK_DAYS), DEFAULT_LOOKBACK_DAYS)
    params = {
        "sortColumns": "TRADE_DATE",
        "sortTypes": "-1",
        "pageSize": str(page_size),
        "pageNumber": "1",
        "reportName": "RPT_MUTUAL_HOLDSTOCKNDATE_STA",
        "columns": "ALL",
        "source": "WEB",
        "client": "WEB",
        "filter": f'(SECURITY_CODE="{symbol}")(INTERVAL_TYPE="1")',
    }

    df = pd.DataFrame()
    try:
        _throttle_em(0.8)
        resp = _session().get(
            NORTHBOUND_URL, params=params, headers=headers, timeout=timeout
        )
        resp.raise_for_status()
        result = (resp.json() or {}).get("result") or {}
        data_list = list(result.get("data") or [])
        if data_list:
            df = _normalize_northbound_df(pd.DataFrame(data_list))
    except Exception:
        df = pd.DataFrame()

    if df.empty:
        try:
            import akshare as ak

            raw = ak.stock_hsgt_individual_em(symbol=symbol)
            df = _normalize_northbound_df(raw)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"北向资金获取失败: {exc}") from exc

    if df.empty:
        return df
    if begin:
        df = df[df["date"] >= pd.to_datetime(begin)]
    if end:
        df = df[df["date"] <= pd.to_datetime(end)]
    df = df.sort_values("date").reset_index(drop=True)
    if lookback_days and lookback_days > 0 and len(df) > lookback_days:
        df = df.tail(lookback_days).reset_index(drop=True)
    if not df.empty:
        df["north_cum_yi"] = df["north_net_yi"].cumsum()
    return df


def fetch_fund_flow_bundle(
    code: str,
    begin: Optional[str] = None,
    end: Optional[str] = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> dict:
    """同时拉取主力/散户资金流与北向资金（默认近 30 日），失败项返回空表。"""
    out = {
        "fund_flow": pd.DataFrame(),
        "northbound": pd.DataFrame(),
        "errors": [],
    }
    try:
        out["fund_flow"] = fetch_stock_fund_flow(
            code, begin=begin, end=end, lookback_days=lookback_days
        )
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"主力/散户资金流: {exc}")

    try:
        out["northbound"] = fetch_northbound_flow(
            code, begin=begin, end=end, lookback_days=lookback_days
        )
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"北向资金: {exc}")

    return out
