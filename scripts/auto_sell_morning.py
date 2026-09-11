# -*- coding: utf-8 -*-
"""
早盘「涨不动 / 高开低走」→ 券商客户端自动卖出（easytrader）。

默认只空跑；确认规则与账户后再加 --live，且配置 dry_run=false。

依赖：
  - 已登录可用的同花顺/华泰/海通等「独立委托」客户端（xiadan.exe）
  - pywinauto + 本仓库自带 easytrader

用法：
  set PYTHONPATH=.
  python -u scripts/auto_sell_morning.py --once
  python -u scripts/auto_sell_morning.py --once --live
  python -u scripts/auto_sell_morning.py
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

EASY_ROOT = ROOT / "qbot" / "engine" / "trade" / "easytrader"
if EASY_ROOT.exists() and str(EASY_ROOT) not in sys.path:
    sys.path.insert(0, str(EASY_ROOT))

from qbot.data.intraday import fetch_realtime_quote  # noqa: E402

CFG_LOCAL = ROOT / "qbot" / "gui" / "csv" / "auto_sell_local.json"
CFG_EXAMPLE = ROOT / "qbot" / "gui" / "csv" / "auto_sell.example.json"
STATE_PATH = ROOT / "qbot" / "gui" / "csv" / "auto_sell_state.json"


def _log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _load_cfg() -> Dict[str, Any]:
    path = CFG_LOCAL if CFG_LOCAL.exists() else CFG_EXAMPLE
    raw = json.loads(path.read_text(encoding="utf-8"))
    _log(f"配置: {path.name}")
    return raw


def _load_state() -> Dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: Dict[str, Any]) -> None:
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _parse_hhmm(s: str) -> dtime:
    hh, mm = str(s).strip().split(":")[:2]
    return dtime(int(hh), int(mm))


def _lot_size(code: str) -> int:
    c = str(code).zfill(6)
    return 200 if c.startswith("68") else 100


def _round_lot(shares: int, code: str) -> int:
    lot = _lot_size(code)
    n = int(shares) // lot * lot
    return max(0, n)


def _f(x: Any, default: float = 0.0) -> float:
    try:
        if x is None or x == "":
            return default
        return float(x)
    except Exception:
        return default


def _connect_trader(cfg: Dict[str, Any]):
    import easytrader

    broker = str(cfg.get("broker") or "universal_client")
    acct = dict(cfg.get("account") or {})
    user = easytrader.use(broker)
    user.prepare(
        user=str(acct.get("user") or ""),
        password=str(acct.get("password") or ""),
        exe_path=str(acct.get("exe_path") or ""),
        comm_password=acct.get("comm_password") or None,
    )
    return user


def _normalize_position_rows(raw: Any) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if raw is None:
        return rows
    if isinstance(raw, dict):
        # 少数接口返回 dict
        raw = [raw]
    for item in list(raw):
        if not isinstance(item, dict):
            continue
        code = (
            item.get("证券代码")
            or item.get("股票代码")
            or item.get("code")
            or item.get("股票代码 ")
            or ""
        )
        code = str(code).strip().zfill(6)[-6:]
        if not code.isdigit():
            continue
        name = str(item.get("证券名称") or item.get("股票名称") or item.get("name") or "")
        avail = item.get("可用余额")
        if avail is None:
            avail = item.get("可用股份")
        if avail is None:
            avail = item.get("股份余额")
        if avail is None:
            avail = item.get("股票余额")
        if avail is None:
            avail = item.get("enable_amount")
        if avail is None:
            avail = item.get("当前持仓")
        avail_i = int(_f(avail, 0))
        if avail_i <= 0:
            continue
        rows.append({"code": code, "name": name, "avail": avail_i, "raw": item})
    return rows


def _quote_metrics(code: str) -> Dict[str, float]:
    q = fetch_realtime_quote(code) or {}
    last = _f(q.get("最新价") or q.get("price") or q.get("f43"))
    pre = _f(q.get("昨收") or q.get("pre_close") or q.get("f60"))
    open_ = _f(q.get("开盘") or q.get("open") or q.get("f46"))
    high = _f(q.get("最高") or q.get("high") or q.get("f44"))
    pct = _f(q.get("涨跌幅") or q.get("pct_chg") or q.get("f170"))
    if pre > 0 and last > 0 and abs(pct) < 1e-9:
        pct = (last / pre - 1.0) * 100.0
    open_pct = ((open_ / pre) - 1.0) * 100.0 if pre > 0 and open_ > 0 else 0.0
    below_open = ((open_ - last) / open_ * 100.0) if open_ > 0 and last > 0 else 0.0
    from_high = ((high - last) / high * 100.0) if high > 0 and last > 0 else 0.0
    return {
        "last": last,
        "pre": pre,
        "open": open_,
        "high": high,
        "pct": pct,
        "open_pct": open_pct,
        "below_open": below_open,
        "from_high": from_high,
    }


def _decide(
    metrics: Dict[str, float], rules: Dict[str, Any], weekday: int
) -> Tuple[bool, str, float]:
    """返回 (是否卖, 原因, 卖出比例)。"""
    weak_lt = _f(rules.get("weak_pct_lt"), 0.5)
    open_ge = _f(rules.get("open_fade_open_pct_ge"), 1.0)
    below_open = _f(rules.get("open_fade_below_open_pct"), 0.3)
    from_high = _f(rules.get("from_high_fade_pct"), 1.5)
    ratio_weak = _f(rules.get("sell_ratio_weak"), 0.5)
    ratio_fade = _f(rules.get("sell_ratio_fade"), 1.0)
    thu_full = bool(rules.get("thursday_force_full", True))

    reasons: List[str] = []
    fade = False
    weak = False

    if metrics["pct"] < weak_lt:
        weak = True
        reasons.append(f"涨不动({metrics['pct']:.2f}%<{weak_lt}%)")
    if metrics["open_pct"] >= open_ge and metrics["below_open"] >= below_open:
        fade = True
        reasons.append(
            f"高开低走(开{metrics['open_pct']:.2f}%/低开盘{metrics['below_open']:.2f}%)"
        )
    if metrics["from_high"] >= from_high and metrics["high"] > 0:
        fade = True
        reasons.append(f"冲高回落(距高{metrics['from_high']:.2f}%)")

    if not reasons:
        return False, "", 0.0

    if fade:
        ratio = ratio_fade
    else:
        ratio = ratio_weak
    # 周四：触发即倾向清可卖
    if weekday == 3 and thu_full:
        ratio = max(ratio, 1.0)
    # 周五弱卖半仓、回落可全清（ratio_fade 已是 1）
    if weekday == 4 and weak and not fade:
        ratio = min(ratio, ratio_weak)

    return True, "+".join(reasons), max(0.0, min(1.0, ratio))


def _already_sold(state: Dict[str, Any], code: str, asof: str) -> bool:
    day = state.get(asof) or {}
    return bool((day.get(code) or {}).get("sold"))


def _mark_sold(
    state: Dict[str, Any], asof: str, code: str, info: Dict[str, Any]
) -> None:
    day = state.setdefault(asof, {})
    day[code] = {"sold": True, **info, "ts": datetime.now().strftime("%H:%M:%S")}
    _save_state(state)


def run_once(cfg: Dict[str, Any], *, live: bool) -> int:
    now = datetime.now()
    if now.weekday() >= 5:
        _log("周末，跳过")
        return 0

    sched = cfg.get("schedule") or {}
    weekdays = [int(x) for x in (sched.get("weekdays") or [3, 4])]
    if now.weekday() not in weekdays:
        _log(f"今日 weekday={now.weekday()} 不在执行日 {weekdays}，跳过")
        return 0

    t0 = _parse_hhmm(str(sched.get("evaluate_after") or "09:45"))
    t1 = _parse_hhmm(str(sched.get("evaluate_until") or "10:30"))
    if not (t0 <= now.time() <= t1):
        _log(f"不在评估窗 {t0.strftime('%H:%M')}-{t1.strftime('%H:%M')}，跳过")
        return 0

    dry = bool(cfg.get("dry_run", True)) and not live
    if live and bool(cfg.get("dry_run", True)):
        _log("警告: --live 已开，但配置 dry_run=true；仍按空跑。请改 local 配置。")
        dry = True
    if live and not bool(cfg.get("dry_run", True)):
        dry = False

    rules = cfg.get("rules") or {}
    white = [str(x).zfill(6) for x in (cfg.get("codes_whitelist") or []) if str(x).strip()]
    black = set(
        str(x).zfill(6) for x in (cfg.get("codes_blacklist") or []) if str(x).strip()
    )
    min_shares = int(cfg.get("min_sell_shares") or 100)

    trader = None
    positions: List[Dict[str, Any]] = []
    if dry and not CFG_LOCAL.exists():
        _log("空跑且无 auto_sell_local.json：无法读券商持仓，退出")
        return 1

    try:
        trader = _connect_trader(cfg)
        positions = _normalize_position_rows(trader.position)
    except Exception as exc:  # noqa: BLE001
        _log(f"连接/持仓失败: {exc}")
        if dry:
            _log("空跑可先手工确认客户端已登录、exe_path 正确")
        return 2

    if white:
        positions = [p for p in positions if p["code"] in white]
    positions = [p for p in positions if p["code"] not in black]

    if not positions:
        _log("无可处理持仓")
        return 0

    asof = now.strftime("%Y-%m-%d")
    state = _load_state()
    sold_n = 0

    for pos in positions:
        code = pos["code"]
        name = pos["name"]
        avail = int(pos["avail"])
        if _already_sold(state, code, asof):
            _log(f"{code} {name} 今日已处理，跳过")
            continue

        try:
            m = _quote_metrics(code)
        except Exception as exc:  # noqa: BLE001
            _log(f"{code} 行情失败: {exc}")
            continue

        ok, reason, ratio = _decide(m, rules, now.weekday())
        if not ok:
            _log(
                f"{code} {name} 不触发 涨={m['pct']:.2f}% 开={m['open_pct']:.2f}% "
                f"距高={m['from_high']:.2f}%"
            )
            continue

        qty = _round_lot(int(math.floor(avail * ratio)), code)
        if qty < max(min_shares, _lot_size(code)):
            _log(f"{code} {name} 触发[{reason}] 但可卖手数不足 qty={qty} avail={avail}")
            continue

        last = m["last"]
        discount = _f(rules.get("limit_price_discount"), 0.005)
        price = round(last * (1.0 - discount), 2) if last > 0 else 0.0
        use_mkt = bool(rules.get("market_sell"))

        _log(
            f"{'【空跑】' if dry else '【实卖】'}{code} {name} {reason} "
            f"ratio={ratio:.0%} qty={qty} last={last} px={price}"
        )

        if dry:
            _mark_sold(
                state,
                asof,
                code,
                {
                    "dry_run": True,
                    "reason": reason,
                    "qty": qty,
                    "price": price,
                    "pct": m["pct"],
                },
            )
            sold_n += 1
            continue

        assert trader is not None
        try:
            if use_mkt and hasattr(trader, "market_sell"):
                ret = trader.market_sell(code, qty)
            else:
                ret = trader.sell(code, price=price, amount=qty)
            _log(f"委托回报 {code}: {ret}")
            _mark_sold(
                state,
                asof,
                code,
                {
                    "dry_run": False,
                    "reason": reason,
                    "qty": qty,
                    "price": price,
                    "ret": str(ret),
                    "pct": m["pct"],
                },
            )
            sold_n += 1
        except Exception as exc:  # noqa: BLE001
            _log(f"{code} 卖出失败: {exc}")

    _log(f"本轮处理完成，触发 {sold_n} 只")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="早盘涨不动/高开低走自动卖")
    ap.add_argument("--once", action="store_true", help="只跑一轮")
    ap.add_argument(
        "--live",
        action="store_true",
        help="允许实盘（仍需 auto_sell_local.json 里 dry_run=false）",
    )
    args = ap.parse_args()
    cfg = _load_cfg()
    sched = cfg.get("schedule") or {}
    poll = int(sched.get("poll_seconds") or 20)

    if args.once:
        return run_once(cfg, live=bool(args.live))

    _log("循环模式启动（Ctrl+C 结束）")
    while True:
        now = datetime.now()
        if now.weekday() < 5:
            t0 = _parse_hhmm(str(sched.get("evaluate_after") or "09:45"))
            t1 = _parse_hhmm(str(sched.get("evaluate_until") or "10:30"))
            if t0 <= now.time() <= t1:
                run_once(cfg, live=bool(args.live))
            else:
                _log("等待评估窗口…")
        time.sleep(max(5, poll))


if __name__ == "__main__":
    raise SystemExit(main())
