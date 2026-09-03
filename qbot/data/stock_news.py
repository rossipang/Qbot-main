# -*- coding: utf-8 -*-
"""个股近一周新闻 / 公告（东财）。"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from typing import Optional, Tuple

import pandas as pd
import requests

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    return s


def _norm_code(code: str) -> str:
    code = str(code or "").strip().upper()
    code = code.replace("SH", "").replace("SZ", "").replace("BJ", "")
    code = re.sub(r"\D", "", code)
    return code.zfill(6) if code else ""


def _parse_time(text: str) -> Optional[datetime]:
    s = str(text or "").strip()
    if not s:
        return None
    s = s.replace("/", "-")
    # 2026-07-17 21:26:22:242 -> 截到秒
    if len(s) >= 19 and s[4] == "-" and s[10] == " ":
        s = s[:19]
        try:
            return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    if len(s) >= 16 and s[4] == "-" and s[10] == " ":
        try:
            return datetime.strptime(s[:16], "%Y-%m-%d %H:%M")
        except ValueError:
            pass
    if len(s) >= 10 and s[4] == "-":
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            pass
    try:
        return pd.to_datetime(s).to_pydatetime()
    except Exception:
        return None


def _within_days(dt: Optional[datetime], days: int) -> bool:
    if dt is None:
        return False
    return dt >= datetime.now() - timedelta(days=days)


def fetch_stock_news(code: str, days: int = 7, limit: int = 40) -> pd.DataFrame:
    """东财个股相关新闻（按关键词搜索），默认近一周。"""
    code = _norm_code(code)
    if not code:
        return pd.DataFrame(columns=["time", "source", "title", "summary", "url", "kind"])

    rows = []
    # 1) 搜索接口（与 akshare.stock_news_em 同类）
    try:
        sess = _session()
        inner = {
            "uid": "",
            "keyword": code,
            "type": ["cmsArticleWebOld"],
            "client": "web",
            "clientType": "web",
            "clientVersion": "curr",
            "param": {
                "cmsArticleWebOld": {
                    "searchScope": "default",
                    "sort": "default",
                    "pageIndex": 1,
                    "pageSize": min(100, max(limit, 20)),
                    "preTag": "",
                    "postTag": "",
                }
            },
        }
        cb = f"jQuery_{int(time.time() * 1000)}"
        r = sess.get(
            "https://search-api-web.eastmoney.com/search/jsonp",
            params={
                "cb": cb,
                "param": json.dumps(inner, ensure_ascii=False),
                "_": str(int(time.time() * 1000)),
            },
            headers={
                "User-Agent": _UA,
                "Referer": f"https://so.eastmoney.com/news/s?keyword={code}",
            },
            timeout=20,
        )
        text = (r.text or "").strip()
        if text.startswith(cb + "(") and text.endswith(")"):
            text = text[len(cb) + 1 : -1]
        payload = json.loads(text)
        items = (((payload or {}).get("result") or {}).get("cmsArticleWebOld")) or []
        for it in items:
            t = str(it.get("date") or "")
            dt = _parse_time(t)
            if days > 0 and not _within_days(dt, days):
                continue
            art = str(it.get("code") or "")
            url = str(it.get("url") or "")
            if not url and art:
                url = f"http://finance.eastmoney.com/a/{art}.html"
            title = re.sub(r"</?em>", "", str(it.get("title") or ""))
            summary = re.sub(r"</?em>", "", str(it.get("content") or ""))[:180]
            rows.append(
                {
                    "time": (dt.strftime("%Y-%m-%d %H:%M") if dt else t[:16]),
                    "source": str(it.get("mediaName") or "东财"),
                    "title": title,
                    "summary": summary,
                    "url": url,
                    "kind": "新闻",
                }
            )
    except Exception:
        rows = []

    # 2) akshare 兜底
    if not rows:
        try:
            import akshare as ak

            raw = ak.stock_news_em(symbol=code)
            if raw is not None and not raw.empty:
                for _, r in raw.iterrows():
                    t = str(r.get("发布时间") or "")
                    dt = _parse_time(t)
                    if days > 0 and not _within_days(dt, days):
                        continue
                    rows.append(
                        {
                            "time": (dt.strftime("%Y-%m-%d %H:%M") if dt else t[:16]),
                            "source": str(r.get("文章来源") or "东财"),
                            "title": str(r.get("新闻标题") or ""),
                            "summary": str(r.get("新闻内容") or "")[:180],
                            "url": str(r.get("新闻链接") or ""),
                            "kind": "新闻",
                        }
                    )
        except Exception:
            pass

    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["time", "source", "title", "summary", "url", "kind"])
    df = df.drop_duplicates(subset=["title"], keep="first")
    df = df.sort_values("time", ascending=False).head(limit).reset_index(drop=True)
    return df


def fetch_stock_announcements(code: str, days: int = 7, limit: int = 40) -> pd.DataFrame:
    """东财个股公告，默认近一周。"""
    code = _norm_code(code)
    if not code:
        return pd.DataFrame(columns=["time", "source", "title", "summary", "url", "kind"])

    rows = []
    try:
        sess = _session()
        r = sess.get(
            "https://np-anotice-stock.eastmoney.com/api/security/ann",
            params={
                "sr": "-1",
                "page_size": "50",
                "page_index": "1",
                "ann_type": "A",
                "client_source": "web",
                "stock_list": code,
                "f_node": "0",
                "s_node": "0",
            },
            headers={
                "User-Agent": _UA,
                "Referer": "https://data.eastmoney.com/notices/",
            },
            timeout=20,
        )
        r.raise_for_status()
        items = ((r.json() or {}).get("data") or {}).get("list") or []
        for it in items:
            t = str(it.get("display_time") or it.get("notice_date") or "")
            dt = _parse_time(t)
            if days > 0 and not _within_days(dt, days):
                continue
            cols = "、".join(
                str(c.get("column_name") or "") for c in (it.get("columns") or []) if c
            )
            art = str(it.get("art_code") or "")
            title = str(it.get("title_ch") or it.get("title") or "")
            url = (
                f"https://data.eastmoney.com/notices/detail/{code}/{art}.html"
                if art
                else ""
            )
            rows.append(
                {
                    "time": (dt.strftime("%Y-%m-%d %H:%M") if dt else t[:16]),
                    "source": cols or "公告",
                    "title": title,
                    "summary": cols,
                    "url": url,
                    "kind": "公告",
                }
            )
    except Exception:
        rows = []

    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["time", "source", "title", "summary", "url", "kind"])
    df = df.drop_duplicates(subset=["title"], keep="first")
    df = df.sort_values("time", ascending=False).head(limit).reset_index(drop=True)
    return df


def fetch_stock_news_bundle(code: str, days: int = 7) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """返回 (新闻, 公告)。"""
    return fetch_stock_news(code, days=days), fetch_stock_announcements(code, days=days)
