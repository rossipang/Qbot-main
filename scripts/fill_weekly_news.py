# -*- coding: utf-8 -*-
"""只拉新闻+板块映射，写入 forward_watch_latest.weekly_news（不全量重建）。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from qbot.data.forward_watch import _build_weekly_news_view, load_latest_forward_watch
from qbot.data.industry_screener import fetch_hot_news, fetch_industry_boards

PATH = Path(__file__).resolve().parents[1] / "qbot" / "gui" / "csv" / "forward_watch_latest.json"


def main() -> None:
    print("fetching news...")
    news = fetch_hot_news(40)
    print("news rows", 0 if news is None else len(news))
    if news is not None and not news.empty:
        print(news.head(3).to_string())
    boards = pd.DataFrame()
    try:
        print("fetching boards for mapping...")
        boards = fetch_industry_boards()
        print("boards", 0 if boards is None else len(boards))
    except Exception as exc:
        print("boards fail", exc)
    weekly = _build_weekly_news_view(news, boards, limit=18)
    print("weekly_news", len(weekly))
    for item in weekly[:5]:
        print(item.get("时间"), item.get("标题")[:40], "=>", item.get("相关板块"))

    data = load_latest_forward_watch() or {}
    data["weekly_news"] = weekly
    data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not weekly:
        err = str(data.get("errors") or "")
        data["errors"] = (err + "; 新闻拉取为空").strip("; ")
    PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print("saved", PATH)


if __name__ == "__main__":
    main()
