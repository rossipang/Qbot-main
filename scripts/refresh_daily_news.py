# -*- coding: utf-8 -*-
"""夜间刷新：每日新闻大事（近3天）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from qbot.data.daily_news_digest import HTML_PATH, LATEST_PATH, build_daily_news_digest


def main() -> int:
    payload = build_daily_news_digest(days=3, persist=True, fast=True)
    print(
        json.dumps(
            {
                "asof": payload.get("asof"),
                "updated_at": payload.get("updated_at"),
                "total": payload.get("total"),
                "today_count": payload.get("today_count"),
                "categories": [
                    {"name": c.get("name"), "count": c.get("count")}
                    for c in (payload.get("categories") or [])
                ],
                "json": str(LATEST_PATH),
                "html": str(HTML_PATH),
                "errors": payload.get("errors") or "",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
