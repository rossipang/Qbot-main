# -*- coding: utf-8 -*-
"""时效快讯源：财联社电报 + 华尔街见闻（多频道）。

目的：每晚刷新能刷出「近一周」催化，映射到涨的板块；
不是事后用「戴尔/钻石」关键词搜几个月前旧稿。

规则：
- 只收近 NEWS_LOOKBACK_DAYS 天
- 不做题材关键词搜索（避免旧闻占坑）
- 快讯通道不过严苛科技白名单（由上游单独合并）
"""
from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import requests

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 前瞻只认近一周；再久的当旧闻，不当当日催化
NEWS_LOOKBACK_DAYS = 7


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    return s


def _fmt_ts(ts) -> str:
    try:
        v = int(ts)
        if v > 10**12:
            v //= 1000
        if v > 10**11:
            v //= 1000
        if v <= 0:
            return ""
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(v))
    except (TypeError, ValueError, OSError):
        return ""


def parse_news_time(text: str) -> Optional[datetime]:
    """解析新闻 time 字段；解析失败返回 None。"""
    s = str(text or "").strip()
    if not s:
        return None
    s = s.replace("/", "-")
    for fmt, n in (
        ("%Y-%m-%d %H:%M:%S", 19),
        ("%Y-%m-%d %H:%M", 16),
        ("%Y-%m-%d", 10),
    ):
        try:
            return datetime.strptime(s[:n], fmt)
        except ValueError:
            continue
    return None


def within_lookback(time_text: str, *, days: int = NEWS_LOOKBACK_DAYS) -> bool:
    dt = parse_news_time(time_text)
    if dt is None:
        return False
    return dt >= datetime.now() - timedelta(days=int(days))


def _cls_sign(params: Dict[str, str]) -> Dict[str, str]:
    items = sorted((k, str(v)) for k, v in params.items() if v is not None)
    qs = "&".join(f"{k}={v}" for k, v in items)
    sha1 = hashlib.sha1(qs.encode("utf-8")).hexdigest()
    out = dict(params)
    out["sign"] = hashlib.md5(sha1.encode("utf-8")).hexdigest()
    return out


def fetch_cls_telegraph(limit: int = 40) -> List[dict]:
    """财联社电报（近实时；akshare 旧接口已 404）。"""
    sess = _session()
    signed = _cls_sign(
        {
            "app": "CailianpressWeb",
            "os": "web",
            "sv": "8.7.9",
            "refresh_type": "1",
            "rn": str(max(20, min(int(limit), 50))),
        }
    )
    r = sess.get(
        "https://www.cls.cn/api/cache",
        params={"name": "telegraph", **signed},
        headers={"User-Agent": _UA, "Referer": "https://www.cls.cn/telegraph"},
        timeout=12,
    )
    r.raise_for_status()
    items = (((r.json() or {}).get("data") or {}).get("roll_data")) or []
    rows: List[dict] = []
    for it in items:
        title = str(it.get("title") or "").strip()
        brief = str(it.get("brief") or it.get("content") or "").strip()
        text = title or brief
        if not text:
            continue
        # 标题空时用 brief；两者都有时用更长的那段当标题
        if title and brief and len(brief) > len(title) + 8:
            text = brief
        elif not title:
            text = brief
        share = str(it.get("shareurl") or it.get("share_url") or "")
        if not share and it.get("id"):
            share = f"https://www.cls.cn/detail/{it.get('id')}"
        t = _fmt_ts(it.get("ctime") or it.get("time"))
        if not within_lookback(t):
            continue
        rows.append(
            {
                "time": t,
                "source": "财联社",
                "title": text[:120],
                "url": share or "https://www.cls.cn/telegraph",
                "channel": "快讯",
            }
        )
        if len(rows) >= int(limit):
            break
    return rows


def fetch_wallstreet_lives(limit: int = 40, *, channels: Optional[List[str]] = None) -> List[dict]:
    """华尔街见闻直播：A股 + 全球（美股财报/英伟达/戴尔等多在这里）。"""
    sess = _session()
    chans = channels or ["a-stock-channel", "global"]
    per = max(15, int(limit) // max(1, len(chans)))
    rows: List[dict] = []
    seen = set()
    for ch in chans:
        try:
            r = sess.get(
                "https://api-one.wallstcn.com/apiv1/content/lives",
                params={"channel": ch, "limit": per},
                headers={
                    "User-Agent": _UA,
                    "Referer": "https://wallstreetcn.com/live",
                },
                timeout=12,
            )
            r.raise_for_status()
            items = (((r.json() or {}).get("data") or {}).get("items")) or []
        except Exception:
            continue
        for it in items:
            title = str(
                it.get("title") or it.get("content_text") or it.get("content") or ""
            ).strip()
            title = re.sub(r"<[^>]+>", "", title)
            title = re.sub(r"\s+", " ", title).strip()
            if not title or title in seen:
                continue
            t = _fmt_ts(it.get("display_time") or it.get("created_at"))
            if not within_lookback(t):
                continue
            uri = str(it.get("uri") or "")
            url = (
                uri
                if uri.startswith("http")
                else (f"https://wallstreetcn.com{uri}" if uri else "https://wallstreetcn.com/live")
            )
            seen.add(title)
            rows.append(
                {
                    "time": t,
                    "source": "华尔街见闻",
                    "title": title[:120],
                    "url": url,
                    "channel": "快讯",
                }
            )
    rows.sort(key=lambda x: str(x.get("time") or ""), reverse=True)
    return rows[: int(limit)]


def fetch_cctv_news(limit: int = 12) -> List[dict]:
    """央视新闻（近一周）。"""
    import akshare as ak

    raw = ak.news_cctv()
    if raw is None or raw.empty:
        return []
    rows: List[dict] = []
    for _, r in raw.head(limit * 2).iterrows():
        title = str(r.get("title") or "").strip()
        if not title:
            continue
        day = str(r.get("date") or "")[:10]
        t = f"{day} 19:00" if day else ""
        if not within_lookback(t):
            continue
        rows.append(
            {
                "time": t,
                "source": "央视新闻",
                "title": title[:120],
                "url": "https://tv.cctv.com/",
                "channel": "快讯",
            }
        )
        if len(rows) >= int(limit):
            break
    return rows


def fetch_cross_platform_theme_news(*, fast: bool = True) -> List[dict]:
    """每日刷新用时效快讯（近一周）：财联社 + 见闻；非 fast 再加央视。

    故意不做题材关键词搜索——那会把几个月前旧稿塞进池子。
    """
    rows: List[dict] = []
    try:
        rows.extend(fetch_cls_telegraph(limit=40 if fast else 50))
    except Exception:
        pass
    try:
        rows.extend(
            fetch_wallstreet_lives(
                limit=40 if fast else 60,
                channels=["a-stock-channel", "global"],
            )
        )
    except Exception:
        pass
    if not fast:
        try:
            rows.extend(fetch_cctv_news(limit=12))
        except Exception:
            pass
    seen = set()
    out: List[dict] = []
    for r in rows:
        title = str(r.get("title") or "").strip()
        if not title or title in seen:
            continue
        if not within_lookback(str(r.get("time") or "")):
            continue
        seen.add(title)
        out.append(r)
    out.sort(key=lambda x: str(x.get("time") or ""), reverse=True)
    return out
