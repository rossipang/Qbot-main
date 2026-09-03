# -*- coding: utf-8 -*-
"""启动前预扫三连阳并落盘，供 GUI 点选直接读缓存。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    from qbot.data.industry_screener import warm_sanlianyang_cache

    t0 = time.time()
    print("warming 三连阳…", flush=True)
    df, date = warm_sanlianyang_cache(force=True)
    n = 0 if df is None else len(df)
    print(
        f"done count={n} date={date} sec={time.time() - t0:.1f}",
        flush=True,
    )
    return 0 if n > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
