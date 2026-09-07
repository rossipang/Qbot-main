# -*- coding: utf-8 -*-
"""日更：个股日K + 主力流入 + 主题等权板涨跌 → 本地 SQLite。

用法:
  python scripts/refresh_ml_factor_store.py
  python scripts/refresh_ml_factor_store.py --bar-limit 40 --flow-days 30 --workers 8
  python scripts/refresh_ml_factor_store.py --bootstrap   # 库空时拉长窗口
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _today() -> str:
    return datetime.now().strftime("%Y%m%d")


def _watch_codes() -> List[str]:
    path = ROOT / "qbot" / "gui" / "csv" / "forward_watch_latest.json"
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    stocks = []
    if isinstance(raw, dict):
        stocks = raw.get("stocks") or raw.get("个股") or []
    out: List[str] = []
    seen = set()
    for row in stocks if isinstance(stocks, list) else []:
        if not isinstance(row, dict):
            continue
        code = str(row.get("代码") or row.get("code") or "").zfill(6)[-6:]
        if len(code) == 6 and code.isdigit() and code not in seen:
            seen.add(code)
            out.append(code)
    return out


def _universe() -> Tuple[List[str], int, int]:
    from qbot.data.forward_ml_score import list_theme_seed_codes

    seeds = list_theme_seed_codes()
    extra = _watch_codes()
    codes: List[str] = []
    seen = set()
    for c in seeds + extra:
        if c not in seen:
            seen.add(c)
            codes.append(c)
    return codes, len(seeds), len(extra)


def _fetch_bars(code: str, end: str, limit: int) -> List[Dict[str, Any]]:
    from qbot.data.industry_screener import _fetch_kline_bars

    try:
        return _fetch_kline_bars(code, end_yyyymmdd=end, limit=limit) or []
    except Exception:
        return []


def _fetch_flow(code: str, lookback: int) -> Tuple[List[Dict[str, Any]], str]:
    from qbot.data.fund_flow import fetch_stock_fund_flow

    try:
        df = fetch_stock_fund_flow(code, lookback_days=lookback)
    except Exception:
        return [], ""
    if df is None or df.empty:
        return [], ""
    src = str(getattr(df, "attrs", {}).get("source") or "flow")
    rows: List[Dict[str, Any]] = []
    for _, r in df.iterrows():
        try:
            d = r["date"]
            if hasattr(d, "strftime"):
                date = d.strftime("%Y%m%d")
            else:
                date = str(d).replace("-", "")[:8]
            main = r.get("main_net_yi")
            if main is None and r.get("main_net") is not None:
                main = float(r["main_net"]) / 1e8
            rows.append(
                {
                    "code": code,
                    "date": date,
                    "main_net_yi": float(main) if main is not None else None,
                    "main_pct": float(r["main_pct"])
                    if r.get("main_pct") is not None
                    else None,
                    "flow_source": src,
                }
            )
        except Exception:
            continue
    return rows, src


def _one_code(
    code: str, end: str, bar_limit: int, flow_days: int, want_flow: bool
) -> Dict[str, Any]:
    bars = _fetch_bars(code, end, bar_limit)
    bar_rows: List[Dict[str, Any]] = []
    for b in bars:
        bar_rows.append(
            {
                "code": code,
                "date": str(b.get("date") or "").replace("-", "")[:8],
                "open": b.get("open"),
                "high": b.get("high"),
                "low": b.get("low"),
                "close": b.get("close"),
                "volume": b.get("volume"),
                "pct": b.get("pct"),
            }
        )
    flow_rows: List[Dict[str, Any]] = []
    flow_src = ""
    if want_flow:
        flow_rows, flow_src = _fetch_flow(code, flow_days)
    return {
        "code": code,
        "bar_rows": bar_rows,
        "flow_rows": flow_rows,
        "flow_src": flow_src,
        "n_bars": len(bar_rows),
        "n_flow": len(flow_rows),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Daily refresh ML factor SQLite store")
    ap.add_argument("--asof", default="", help="YYYYMMDD")
    ap.add_argument("--bar-limit", type=int, default=40, help="日更默认近 N 根K")
    ap.add_argument("--flow-days", type=int, default=30, help="资金流回看交易日")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--bootstrap", action="store_true", help="强制长窗口拉K")
    ap.add_argument("--no-flow", action="store_true", help="只更日K不拉资金")
    ap.add_argument("--skip-index", action="store_true")
    args = ap.parse_args()
    end = (args.asof or _today()).replace("-", "")[:8]

    from qbot.data import ml_factor_store as store
    from qbot.data.forward_ml_score import INDEX_CODE

    db = store.init_db()
    codes, n_seed, n_watch = _universe()
    # 主题映射
    n_map = store.sync_code_themes(store.theme_seed_pairs_from_hints(), replace=True)

    existing = store.count_stock_days()
    bar_limit = args.bar_limit
    if args.bootstrap or existing < 2000:
        bar_limit = max(bar_limit, 220)
        print(f"[ml_store] bootstrap bars limit={bar_limit} (rows_now={existing})")

    print(
        f"[ml_store] db={db} asof={end} codes={len(codes)} "
        f"(seeds={n_seed} watch={n_watch}) map={n_map} "
        f"bar_limit={bar_limit} flow_days={args.flow_days}"
    )
    t0 = time.time()

    bar_upsert = flow_upsert = 0
    ok_bars = ok_flow = 0
    want_flow = not args.no_flow

    with ThreadPoolExecutor(max_workers=max(2, args.workers)) as pool:
        futs = {
            pool.submit(_one_code, c, end, bar_limit, args.flow_days, want_flow): c
            for c in codes
        }
        done = 0
        for fut in as_completed(futs):
            done += 1
            try:
                pack = fut.result()
            except Exception as exc:  # noqa: BLE001
                print(f"  ERR {futs[fut]}: {exc}")
                continue
            if pack["bar_rows"]:
                bar_upsert += store.upsert_stock_days(pack["bar_rows"])
                ok_bars += 1
            if pack["flow_rows"]:
                flow_upsert += store.upsert_stock_days(pack["flow_rows"])
                ok_flow += 1
            if done % 25 == 0 or done == len(codes):
                print(
                    f"  progress {done}/{len(codes)} "
                    f"ok_bars={ok_bars} ok_flow={ok_flow}"
                )

    # 指数 ETF
    if not args.skip_index:
        idx_bars = _fetch_bars(INDEX_CODE, end, max(bar_limit, 240))
        idx_rows = [
            {
                "code": INDEX_CODE,
                "date": str(b.get("date") or "").replace("-", "")[:8],
                "open": b.get("open"),
                "high": b.get("high"),
                "low": b.get("low"),
                "close": b.get("close"),
                "volume": b.get("volume"),
                "pct": b.get("pct"),
            }
            for b in idx_bars
        ]
        if idx_rows:
            store.upsert_stock_days(idx_rows)
            print(f"  index {INDEX_CODE} upsert={len(idx_rows)}")

    # 主题等权板
    theme_n = store.recompute_theme_days()
    st = store.stats()
    detail = {
        "asof": end,
        "codes": len(codes),
        "ok_bars": ok_bars,
        "ok_flow": ok_flow,
        "bar_upsert": bar_upsert,
        "flow_upsert": flow_upsert,
        "theme_rows_written": theme_n,
        "stats": st,
        "elapsed_sec": round(time.time() - t0, 1),
    }
    store.log_sync("daily_refresh", True, detail)
    print(json.dumps(detail, ensure_ascii=False, indent=2))
    print(f"[ml_store] done in {detail['elapsed_sec']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
