# -*- coding: utf-8 -*-
"""前瞻观察固定刷新入口（与 GUI「刷新前瞻分析」同一套逻辑）。

用法（项目根目录）:
  set PYTHONPATH=.
  python scripts/refresh_forward_watch.py

闭环:
  新闻 + 板块行情 → 动态发现主题 → 自动补龙头股 → 打星/信号 → 写入缓存
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qbot.data.forward_watch import PIPELINE_VERSION, build_forward_watch  # noqa: E402


def main() -> int:
    print(f"pipeline={PIPELINE_VERSION}", flush=True)
    t0 = time.time()
    payload = build_forward_watch(persist=True)
    secs = time.time() - t0
    themes = payload.get("themes") or []
    stocks = payload.get("stocks") or []
    print(f"done {secs:.1f}s asof={payload.get('asof')} updated={payload.get('updated_at')}")
    print(f"themes={len(themes)} stocks={len(stocks)} errors={payload.get('errors') or '-'}")
    print("top themes:")
    for row in themes[:12]:
        print(
            f"  {row.get('信号色')} {row.get('板块主题')} | {row.get('匹配板块')} "
            f"星{row.get('星级显示')} 5日{row.get('板块5日%')} 新闻{row.get('新闻条数')}"
        )
    n_new_hi = sum(
        1
        for r in stocks
        if int(r.get("星级") or 0) >= 4 and int(r.get("连入天数") or 0) < 2
    )
    if n_new_hi:
        print(f"warn: {n_new_hi} stocks still >=4星 with 连入<2 (should be capped)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
