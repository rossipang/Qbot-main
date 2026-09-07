# -*- coding: utf-8 -*-
"""前瞻短线 GBDT 训练。

默认：从本地 SQLite 因子库周训（含资金流/主题板）。
回退：联网拉日K（无库或 --online）。

用法:
  python scripts/train_forward_gbdt.py --from-store
  python scripts/train_forward_gbdt.py --from-store --if-due 7
  python scripts/train_forward_gbdt.py --online --limit 220
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _today_yyyymmdd() -> str:
    return datetime.now().strftime("%Y%m%d")


def _extra_codes_from_watch() -> List[str]:
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
    elif isinstance(raw, list):
        stocks = raw
    out: List[str] = []
    seen = set()
    for row in stocks:
        if not isinstance(row, dict):
            continue
        code = str(row.get("代码") or row.get("code") or "").zfill(6)[-6:]
        if len(code) == 6 and code.isdigit() and code not in seen:
            seen.add(code)
            out.append(code)
    return out


def _fetch_one(
    code: str, end: str, limit: int
) -> Tuple[str, List[Dict[str, Any]]]:
    from qbot.data.industry_screener import _fetch_kline_bars

    try:
        bars = _fetch_kline_bars(code, end_yyyymmdd=end, limit=limit) or []
    except Exception:
        bars = []
    return code, bars


def _fetch_index_bars(end: str, limit: int) -> List[Dict[str, Any]]:
    from qbot.data.forward_ml_score import INDEX_CODE

    _code, bars = _fetch_one(INDEX_CODE, end, limit)
    return bars


def _should_skip_due(if_due_days: float) -> Tuple[bool, str]:
    from qbot.data.forward_ml_score import FEATURE_KEYS, META_PATH, days_since_last_train

    if if_due_days <= 0:
        return False, "force"
    # 特征集变更必须重训
    if META_PATH.exists():
        try:
            old = json.loads(META_PATH.read_text(encoding="utf-8"))
            if list(old.get("feature_keys") or []) != list(FEATURE_KEYS):
                return False, "feature_mismatch"
            if not old.get("ok"):
                return False, "last_train_failed"
        except Exception:
            return False, "meta_unreadable"
    age = days_since_last_train()
    if age is None:
        return False, "no_prior_train"
    if age < float(if_due_days):
        return True, f"age={age:.1f}d < due={if_due_days}d"
    return False, f"age={age:.1f}d >= due={if_due_days}d"


def _last_friday_start(now: Optional[datetime] = None) -> datetime:
    """本周（或刚过去）周五 00:00。周日跑时指向刚过去的周五。"""
    d = now or datetime.now()
    # Mon=0 … Fri=4 Sun=6
    delta = (d.weekday() - 4) % 7
    fri = d - timedelta(days=delta)
    return fri.replace(hour=0, minute=0, second=0, microsecond=0)


def _should_skip_catchup_missed_friday() -> Tuple[bool, str]:
    """周日补训：若本周五以来已有成功 weekly_train 则跳过。"""
    from qbot.data import ml_factor_store as store
    from qbot.data.forward_ml_score import FEATURE_KEYS, META_PATH

    # 特征变更仍强制训
    if META_PATH.exists():
        try:
            old = json.loads(META_PATH.read_text(encoding="utf-8"))
            if list(old.get("feature_keys") or []) != list(FEATURE_KEYS):
                return False, "feature_mismatch"
        except Exception:
            pass

    fri = _last_friday_start()
    last = store.last_sync("weekly_train")
    if not last or not last.get("ok"):
        return False, f"no_ok_weekly_train_since_need_catchup (friday={fri.date()})"
    try:
        ran = datetime.strptime(str(last["ran_at"])[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return False, "weekly_train_timestamp_bad"
    if ran >= fri:
        return True, f"already_trained {ran} >= friday {fri.date()}"
    return False, f"missed_friday last={ran} friday={fri.date()}"


def _train_from_store(args: argparse.Namespace) -> Dict[str, Any]:
    from qbot.data import ml_factor_store as store
    from qbot.data.forward_ml_score import train_short_gbdt_from_store

    st = store.stats()
    print(f"[train_gbdt] from_store {json.dumps(st, ensure_ascii=False)}")
    if int(st.get("stock_rows") or 0) < 3000:
        return {
            "ok": False,
            "why": f"因子库行数{st.get('stock_rows')}过少，请先跑 refresh_ml_factor_store.py --bootstrap",
            "store_stats": st,
        }
    meta = train_short_gbdt_from_store(
        min_samples=args.min_samples,
        persist=True,
        max_per_code=args.max_per_code,
        force=True,
    )
    store.log_sync("weekly_train", bool(meta.get("ok")), meta)
    return meta


def _train_online(args: argparse.Namespace) -> Dict[str, Any]:
    from qbot.data.forward_ml_score import (
        INDEX_CODE,
        index_close_map_from_bars,
        list_theme_seed_codes,
        save_index_closes,
        train_short_gbdt,
    )

    end = (args.asof or _today_yyyymmdd()).replace("-", "")[:8]
    seeds = list_theme_seed_codes()
    extra = [] if args.no_watch else _extra_codes_from_watch()
    codes: List[str] = []
    seen = set()
    for c in seeds + extra:
        if c not in seen:
            seen.add(c)
            codes.append(c)

    print(
        f"[train_gbdt] online asof={end} codes={len(codes)} "
        f"(seeds={len(seeds)} watch_extra={len(extra)}) limit={args.limit}"
    )
    bars_by_code: Dict[str, List[Dict[str, Any]]] = {}
    ok_n = 0
    with ThreadPoolExecutor(max_workers=max(2, args.workers)) as pool:
        futs = {pool.submit(_fetch_one, c, end, args.limit): c for c in codes}
        done = 0
        for fut in as_completed(futs):
            done += 1
            code, bars = fut.result()
            if bars and len(bars) >= 65:
                bars_by_code[code] = bars
                ok_n += 1
            if done % 20 == 0 or done == len(codes):
                print(f"  fetch {done}/{len(codes)} ok_bars={ok_n}")

    idx_map: Optional[Dict[str, float]] = None
    idx_bars = _fetch_index_bars(end, args.limit + 20)
    if idx_bars:
        idx_map = index_close_map_from_bars(idx_bars)
        save_index_closes(idx_map)
        print(f"  index {INDEX_CODE} bars={len(idx_bars)}")
    else:
        print(f"  WARN: index {INDEX_CODE} empty")

    print(f"[train_gbdt] train on {len(bars_by_code)} codes …")
    return train_short_gbdt(
        bars_by_code,
        min_samples=args.min_samples,
        persist=True,
        max_per_code=args.max_per_code,
        index_close_by_date=idx_map,
        force=True,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Train forward short GBDT")
    ap.add_argument(
        "--from-store",
        action="store_true",
        default=True,
        help="从 SQLite 因子库训练（默认）",
    )
    ap.add_argument(
        "--online",
        action="store_true",
        help="忽略因子库，联网拉日K训练",
    )
    ap.add_argument(
        "--if-due",
        type=float,
        default=0,
        help="距上次成功训练不足 N 天则跳过（周训用 7）",
    )
    ap.add_argument(
        "--catchup-missed-friday",
        action="store_true",
        help="周日补训：仅当本周五以来没有成功 weekly_train 时才训练",
    )
    ap.add_argument("--limit", type=int, default=220, help="online 日K根数")
    ap.add_argument("--max-per-code", type=int, default=120)
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--asof", type=str, default="")
    ap.add_argument("--no-watch", action="store_true")
    ap.add_argument("--min-samples", type=int, default=400)
    args = ap.parse_args()

    if args.catchup_missed_friday:
        skip, reason = _should_skip_catchup_missed_friday()
        if skip:
            print(f"[train_gbdt] sunday_catchup skip ({reason})")
            return 0
        print(f"[train_gbdt] sunday_catchup run ({reason})")
    else:
        skip, reason = _should_skip_due(args.if_due)
        if skip:
            print(f"[train_gbdt] skip ({reason})")
            return 0
        print(f"[train_gbdt] due_check={reason}")

    t0 = time.time()
    if args.online:
        meta = _train_online(args)
    else:
        meta = _train_from_store(args)
        # 库不足时回退 online
        if not meta.get("ok") and "过少" in str(meta.get("why") or ""):
            print("[train_gbdt] store thin → fallback online")
            meta = _train_online(args)

    elapsed = time.time() - t0
    print(json.dumps(meta, ensure_ascii=False, indent=2, default=str))
    print(f"[train_gbdt] done in {elapsed:.1f}s ok={meta.get('ok')}")
    return 0 if meta.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
