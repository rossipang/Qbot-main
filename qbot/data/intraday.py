# -*- coding: utf-8 -*-
"""个股分时行情 + 分时主力资金（东财，可盘中刷新）。"""

from __future__ import annotations

import time
from datetime import datetime, time as dtime
from typing import Any, Dict, Optional, Tuple

import pandas as pd
import requests

from qbot.data.eastmoney_quote import REQUEST_HEADERS, to_secid

_UA = REQUEST_HEADERS


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    return s


def is_cn_trading_session(now: Optional[datetime] = None) -> bool:
    """A 股常规交易时段（含集合竞价缓冲），用于是否自动刷新。"""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    # 09:15-11:35 / 12:55-15:05
    am = dtime(9, 15) <= t <= dtime(11, 35)
    pm = dtime(12, 55) <= t <= dtime(15, 5)
    return am or pm


def fetch_realtime_quote(code: str) -> Dict[str, Any]:
    """盘口快照：价、涨跌、量、估值、主力净占比等。"""
    secid = to_secid(code)
    out: Dict[str, Any] = {"code": str(code).zfill(6) if str(code).isdigit() else code}
    try:
        sess = _session()
        r = sess.get(
            "https://push2delay.eastmoney.com/api/qt/stock/get",
            params={
                "secid": secid,
                "fltt": "2",
                "invt": "2",
                "fields": (
                    "f57,f58,f43,f169,f170,f168,f48,f47,f116,f117,"
                    "f162,f167,f184,f60,f44,f45,f46,f71"
                ),
            },
            headers={**_UA, "Referer": "https://quote.eastmoney.com/"},
            timeout=12,
        )
        d = (r.json() or {}).get("data") or {}
        if not d:
            return out
        out.update(
            {
                "name": d.get("f58") or "",
                "price": d.get("f43"),
                "change": d.get("f169"),
                "pct": d.get("f170"),
                "turnover": d.get("f168"),  # 换手%
                "amount": d.get("f48"),  # 成交额
                "volume": d.get("f47"),
                "mcap": (float(d["f116"]) / 1e8) if d.get("f116") not in (None, "-") else None,
                "pe_ttm": d.get("f162"),
                "pb": d.get("f167"),
                "main_pct": d.get("f184"),
                "pre_close": d.get("f60"),
                "high": d.get("f44"),
                "low": d.get("f45"),
                "open": d.get("f46"),
                "avg": d.get("f71"),
            }
        )
    except Exception:
        pass
    return out


def fetch_minute_trends(code: str) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """
    分时走势。
    返回 (df, meta)，df 列: time, price, avg, volume, amount, open, high, low
    """
    secid = to_secid(code)
    meta: Dict[str, Any] = {}
    rows = []
    try:
        sess = _session()
        r = sess.get(
            "https://push2delay.eastmoney.com/api/qt/stock/trends2/get",
            params={
                "secid": secid,
                "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
                "iscr": "0",
                "ndays": "1",
                "iscca": "0",
                "_": int(time.time() * 1000),
            },
            headers={**_UA, "Referer": "https://quote.eastmoney.com/"},
            timeout=15,
        )
        data = (r.json() or {}).get("data") or {}
        meta = {
            "name": data.get("name") or "",
            "pre_close": data.get("prePrice") or data.get("preClose"),
            "code": data.get("code") or "",
        }
        for line in data.get("trends") or []:
            parts = str(line).split(",")
            if len(parts) < 8:
                continue
            rows.append(
                {
                    "time": parts[0],
                    "open": float(parts[1]),
                    "price": float(parts[2]),
                    "high": float(parts[3]),
                    "low": float(parts[4]),
                    "volume": float(parts[5]),
                    "amount": float(parts[6]),
                    "avg": float(parts[7]),
                }
            )
    except Exception:
        return pd.DataFrame(), meta

    df = pd.DataFrame(rows)
    if df.empty:
        return df, meta
    df["datetime"] = pd.to_datetime(df["time"])
    df["time_label"] = df["datetime"].dt.strftime("%H:%M")
    pre = meta.get("pre_close")
    if pre not in (None, "-", ""):
        try:
            pre_f = float(pre)
            df["pct"] = (df["price"] / pre_f - 1.0) * 100.0
            meta["pre_close"] = pre_f
        except (TypeError, ValueError):
            df["pct"] = None
    else:
        df["pct"] = None
    return df, meta


def fetch_minute_fflow(code: str) -> pd.DataFrame:
    """
    分时资金流向（当日累计，元）。
    列: datetime, time_label, main_net, retail_net, mid_net, large_net, super_net (+ _yi)
    """
    secid = to_secid(code)
    rows = []
    bases = (
        "https://push2delay.eastmoney.com/api/qt/stock/fflow/kline/get",
        "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get",
        "https://82.push2.eastmoney.com/api/qt/stock/fflow/kline/get",
    )
    sess = _session()
    last_err = None
    for url in bases:
        try:
            r = sess.get(
                url,
                params={
                    "lmt": "0",
                    "klt": "1",
                    "secid": secid,
                    "fields1": "f1,f2,f3,f7",
                    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
                    "ut": "b2884a393a59ad64002292a3e90d46a5",
                    "_": int(time.time() * 1000),
                },
                headers={**_UA, "Referer": "https://data.eastmoney.com/zjlx/"},
                timeout=15,
            )
            r.raise_for_status()
            klines = ((r.json() or {}).get("data") or {}).get("klines") or []
            for line in klines:
                parts = str(line).split(",")
                if len(parts) < 6:
                    continue
                rows.append(
                    {
                        "time": parts[0],
                        "main_net": float(parts[1]),
                        "retail_net": float(parts[2]),
                        "mid_net": float(parts[3]),
                        "large_net": float(parts[4]),
                        "super_net": float(parts[5]),
                    }
                )
            if rows:
                break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            continue
    del last_err
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["datetime"] = pd.to_datetime(df["time"])
    df["time_label"] = df["datetime"].dt.strftime("%H:%M")
    for col in ["main_net", "retail_net", "mid_net", "large_net", "super_net"]:
        df[col + "_yi"] = df[col] / 1e8
    return df


def fetch_intraday_bundle(code: str) -> Dict[str, Any]:
    """一次拉齐：快照 + 分时价 + 分时资金。"""
    quote = fetch_realtime_quote(code)
    trends, tmeta = fetch_minute_trends(code)
    fflow = fetch_minute_fflow(code)
    if not quote.get("name") and tmeta.get("name"):
        quote["name"] = tmeta["name"]
    if quote.get("pre_close") is None and tmeta.get("pre_close") is not None:
        quote["pre_close"] = tmeta.get("pre_close")
    main_yi = None
    if fflow is not None and not fflow.empty:
        main_yi = float(fflow["main_net_yi"].iloc[-1])
    quote["main_net_yi"] = main_yi
    return {
        "quote": quote,
        "trends": trends,
        "fflow": fflow,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "trading": is_cn_trading_session(),
    }
