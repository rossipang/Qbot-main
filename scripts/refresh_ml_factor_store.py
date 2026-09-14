# -*- coding: utf-8 -*-
"""日更：个股日K + 主力流入 + 主题等权板涨跌 → 本地 SQLite。

只存有用数据：OHLC 无效的 K 线不入；主力净流入为空的资金行不入。
拉空则换源重试；仍空记原因，不写垃圾行。

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
from collections import Counter
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


def _fnum(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return v


def _is_useful_bar(row: Dict[str, Any]) -> bool:
    """有用日K：合法日期 + 收盘>0，且开高低至少能自洽。"""
    date = str(row.get("date") or "").replace("-", "")[:8]
    if len(date) != 8 or not date.isdigit():
        return False
    c = _fnum(row.get("close"))
    if c is None or c <= 0:
        return False
    o = _fnum(row.get("open"))
    h = _fnum(row.get("high"))
    l = _fnum(row.get("low"))
    # 允许缺开高低，但三者齐全时要基本自洽
    if o is not None and h is not None and l is not None:
        if h < max(o, c, l) * 0.999 or l > min(o, c, h) * 1.001:
            if h < l:
                return False
    return True


def _is_useful_flow(row: Dict[str, Any]) -> bool:
    """有用资金：合法日期 + 主力净流入（亿）有数。0 也算有用（真没流入≠空）。"""
    date = str(row.get("date") or "").replace("-", "")[:8]
    if len(date) != 8 or not date.isdigit():
        return False
    return _fnum(row.get("main_net_yi")) is not None


def _filter_bars(rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    keep = [r for r in rows if _is_useful_bar(r)]
    return keep, len(rows) - len(keep)


def _filter_flows(rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    keep = [r for r in rows if _is_useful_flow(r)]
    return keep, len(rows) - len(keep)


def _fetch_bars(code: str, end: str, limit: int) -> Tuple[List[Dict[str, Any]], str, str]:
    """换源拉日K。返回 (rows, source, fail_reason)。"""
    from qbot.data.industry_screener import (
        _fetch_kline_bars,
        _fetch_kline_bars_fast,
        _fetch_kline_bars_once,
        _fetch_kline_bars_tencent,
    )

    attempts = (
        ("em_multi", lambda: _fetch_kline_bars(code, end_yyyymmdd=end, limit=limit)),
        ("em_once", lambda: _fetch_kline_bars_once(code, end_yyyymmdd=end, limit=limit)),
        ("em_fast", lambda: _fetch_kline_bars_fast(code, end_yyyymmdd=end, limit=limit)),
        ("tencent", lambda: _fetch_kline_bars_tencent(code, end_yyyymmdd=end, limit=limit)),
    )
    last_err = "empty"
    for src, fn in attempts:
        try:
            raw = fn() or []
        except Exception as exc:  # noqa: BLE001
            last_err = f"{src}:{type(exc).__name__}"
            time.sleep(0.12)
            continue
        rows: List[Dict[str, Any]] = []
        for b in raw:
            rows.append(
                {
                    "code": code,
                    "date": str(b.get("date") or "").replace("-", "")[:8],
                    "open": b.get("open"),
                    "high": b.get("high"),
                    "low": b.get("low"),
                    "close": b.get("close"),
                    "volume": b.get("volume"),
                    "pct": b.get("pct"),
                    "bar_source": src,
                }
            )
        keep, dropped = _filter_bars(rows)
        if keep:
            return keep, src, "" if not dropped else f"dropped_bad_ohlc={dropped}"
        last_err = f"{src}:no_useful_bars"
        time.sleep(0.1)
    return [], "", last_err


def _fetch_flow(code: str, lookback: int) -> Tuple[List[Dict[str, Any]], str, str]:
    """换源拉资金（fund_flow 内已多源）；空则短睡重试一次。只保留有主力净流入的行。"""
    from qbot.data.fund_flow import fetch_stock_fund_flow

    last_err = "empty"
    for attempt in range(2):
        try:
            df = fetch_stock_fund_flow(code, lookback_days=lookback)
        except Exception as exc:  # noqa: BLE001
            last_err = f"exc:{type(exc).__name__}"
            time.sleep(0.25 * (attempt + 1))
            continue
        if df is None or df.empty:
            last_err = "all_sources_empty"
            time.sleep(0.2 * (attempt + 1))
            continue
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
        keep, dropped = _filter_flows(rows)
        if keep:
            note = "" if not dropped else f"dropped_null_flow={dropped}"
            return keep, src, note
        last_err = f"{src}:rows_but_main_null"
        time.sleep(0.2 * (attempt + 1))
    return [], "", last_err


def _one_code(
    code: str, end: str, bar_limit: int, flow_days: int, want_flow: bool
) -> Dict[str, Any]:
    bar_rows, bar_src, bar_note = _fetch_bars(code, end, bar_limit)
    flow_rows: List[Dict[str, Any]] = []
    flow_src = ""
    flow_note = ""
    if want_flow:
        flow_rows, flow_src, flow_note = _fetch_flow(code, flow_days)
    return {
        "code": code,
        "bar_rows": bar_rows,
        "flow_rows": flow_rows,
        "bar_src": bar_src,
        "flow_src": flow_src,
        "bar_note": bar_note,
        "flow_note": flow_note,
        "n_bars": len(bar_rows),
        "n_flow": len(flow_rows),
        "bar_fail": "" if bar_rows else (bar_note or "no_useful_bars"),
        "flow_fail": ""
        if (flow_rows or not want_flow)
        else (flow_note or "no_useful_flow"),
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
    ap.add_argument(
        "--retry-failed",
        action="store_true",
        default=True,
        help="首轮失败代码串行换源再拉（默认开）",
    )
    ap.add_argument("--no-retry-failed", action="store_true")
    args = ap.parse_args()
    end = (args.asof or _today()).replace("-", "")[:8]
    do_retry = bool(args.retry_failed) and not bool(args.no_retry_failed)

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
        f"bar_limit={bar_limit} flow_days={args.flow_days} "
        f"filter=useful_only retry={do_retry}"
    )
    t0 = time.time()

    bar_upsert = flow_upsert = 0
    ok_bars = ok_flow = 0
    want_flow = not args.no_flow
    skip_reasons: Counter = Counter()
    failed_bar: List[str] = []
    failed_flow: List[str] = []
    packs_by_code: Dict[str, Dict[str, Any]] = {}

    def _apply_pack(pack: Dict[str, Any]) -> None:
        nonlocal bar_upsert, flow_upsert, ok_bars, ok_flow
        code = pack["code"]
        packs_by_code[code] = pack
        if pack["bar_rows"]:
            n = store.upsert_stock_days(pack["bar_rows"], require_useful=True)
            bar_upsert += n
            if n:
                ok_bars += 1
            else:
                skip_reasons["bars_filtered_at_upsert"] += 1
        else:
            skip_reasons[f"bars:{pack.get('bar_fail') or 'empty'}"] += 1
            failed_bar.append(code)
        if want_flow:
            if pack["flow_rows"]:
                n = store.upsert_stock_days(pack["flow_rows"], require_useful=True)
                flow_upsert += n
                if n:
                    ok_flow += 1
                else:
                    skip_reasons["flow_filtered_at_upsert"] += 1
            else:
                skip_reasons[f"flow:{pack.get('flow_fail') or 'empty'}"] += 1
                failed_flow.append(code)

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
                skip_reasons[f"exc:{type(exc).__name__}"] += 1
                failed_bar.append(futs[fut])
                continue
            _apply_pack(pack)
            if done % 25 == 0 or done == len(codes):
                print(
                    f"  progress {done}/{len(codes)} "
                    f"ok_bars={ok_bars} ok_flow={ok_flow} "
                    f"fail_bars={len(failed_bar)} fail_flow={len(failed_flow)}"
                )

    # 失败代码串行再拉一轮（降并发，换源更容易活）
    if do_retry:
        retry_codes = sorted(set(failed_bar + (failed_flow if want_flow else [])))
        if retry_codes:
            print(f"[ml_store] retry failed codes={len(retry_codes)} (serial)")
            need_bar = set(failed_bar)
            need_flow = set(failed_flow) if want_flow else set()
            recovered_b = recovered_f = 0
            still_b: List[str] = []
            still_f: List[str] = []
            for code in retry_codes:
                time.sleep(0.15)
                pack = _one_code(code, end, bar_limit, args.flow_days, want_flow)
                prev = packs_by_code.get(code) or {}
                if code in need_bar:
                    if pack["bar_rows"]:
                        n = store.upsert_stock_days(
                            pack["bar_rows"], require_useful=True
                        )
                        if n:
                            bar_upsert += n
                            ok_bars += 1
                            recovered_b += 1
                        else:
                            still_b.append(code)
                    else:
                        still_b.append(code)
                if code in need_flow:
                    if pack["flow_rows"]:
                        n = store.upsert_stock_days(
                            pack["flow_rows"], require_useful=True
                        )
                        if n:
                            flow_upsert += n
                            ok_flow += 1
                            recovered_f += 1
                        else:
                            still_f.append(code)
                    else:
                        still_f.append(code)
                packs_by_code[code] = {**prev, **pack}
            failed_bar = still_b
            failed_flow = still_f
            print(
                f"  retry recovered bars={recovered_b} flow={recovered_f} "
                f"still_fail_bars={len(failed_bar)} still_fail_flow={len(failed_flow)}"
            )
            if failed_bar[:8]:
                print(f"  still no bars: {','.join(failed_bar[:8])}")
            if failed_flow[:8]:
                print(f"  still no flow: {','.join(failed_flow[:8])}")

    # 指数 ETF（同样过滤）
    if not args.skip_index:
        idx_rows, idx_src, idx_note = _fetch_bars(INDEX_CODE, end, max(bar_limit, 240))
        if idx_rows:
            store.upsert_stock_days(idx_rows, require_useful=True)
            print(f"  index {INDEX_CODE} upsert={len(idx_rows)} src={idx_src} {idx_note}")
        else:
            print(f"  index {INDEX_CODE} SKIP useless/empty ({idx_note})")
            skip_reasons[f"index:{idx_note or 'empty'}"] += 1

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
        "fail_bars": len(set(failed_bar)),
        "fail_flow": len(set(failed_flow)),
        "skip_reasons": dict(skip_reasons.most_common(20)),
        "theme_rows_written": theme_n,
        "stats": st,
        "filter": "useful_ohlc_and_main_net_only",
        "elapsed_sec": round(time.time() - t0, 1),
    }
    store.log_sync("daily_refresh", True, detail)
    print(json.dumps(detail, ensure_ascii=False, indent=2))
    print(f"[ml_store] done in {detail['elapsed_sec']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
