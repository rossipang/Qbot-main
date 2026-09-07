# -*- coding: utf-8 -*-
"""查询本地 ML 因子库（SQLite）。

用法:
  python scripts/query_ml_factor_store.py
  python scripts/query_ml_factor_store.py --code 600487 --days 15
  python scripts/query_ml_factor_store.py --theme fiber_optic --days 20
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description="Query ml_factor_store.sqlite")
    ap.add_argument("--code", default="", help="个股代码")
    ap.add_argument("--theme", default="", help="主题 id")
    ap.add_argument("--days", type=int, default=20)
    args = ap.parse_args()

    from qbot.data import ml_factor_store as store

    print(json.dumps(store.stats(), ensure_ascii=False, indent=2))
    last = store.last_sync("daily_refresh")
    if last:
        print("last_daily_refresh:", json.dumps(last, ensure_ascii=False, indent=2))
    last_t = store.last_sync("weekly_train")
    if last_t:
        print("last_weekly_train:", json.dumps(last_t, ensure_ascii=False, indent=2))

    if args.code:
        rows = store.query_stock_history(args.code, days=args.days)
        print(f"\nstock {args.code} last {args.days}:")
        for r in rows:
            print(
                f"  {r.get('date')} close={r.get('close')} pct={r.get('pct')} "
                f"flow={r.get('main_net_yi')} src={r.get('flow_source')}"
            )
    if args.theme:
        rows = store.query_theme_history(args.theme, days=args.days)
        print(f"\ntheme {args.theme} last {args.days}:")
        for r in rows:
            print(
                f"  {r.get('date')} board_pct={r.get('board_pct')} "
                f"n={r.get('n_members')}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
