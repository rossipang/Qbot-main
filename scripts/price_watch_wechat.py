# -*- coding: utf-8 -*-
"""
盘中限价盯盘 → 微信 PushPlus 推送（不下单）。

提醒类型：
  1) 挂单通知：开盘前约5分钟（默认09:20）提醒去挂限价
  2) 接近到价：现价落到限价上方一定比例内（提前盯）
  3) 到价通知：现价/最低价触及 buy_below
  4) 撤单通知：现价远离限价（防误成交）或尾盘提醒撤未成交单

白天人在单位：家里电脑开着跑本脚本。

用法：
  set PYTHONPATH=.
  python -u scripts/price_watch_wechat.py
  python -u scripts/price_watch_wechat.py --test
  python -u scripts/price_watch_wechat.py --once
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qbot.data.intraday import fetch_realtime_quote, is_cn_trading_session  # noqa: E402
from qbot.notify.wechat_push import push_from_cfg  # noqa: E402

CFG_LOCAL = ROOT / "qbot" / "gui" / "csv" / "price_watch_local.json"
CFG_EXAMPLE = ROOT / "qbot" / "gui" / "csv" / "price_watch.example.json"
STATE_PATH = ROOT / "qbot" / "gui" / "csv" / "price_watch_state.json"


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_cfg() -> Dict[str, Any]:
    if CFG_LOCAL.exists():
        return _load_json(CFG_LOCAL)
    if CFG_EXAMPLE.exists():
        print(
            f"[!] 未找到 {CFG_LOCAL.name}，请先复制 example 并填入 pushplus_token",
            flush=True,
        )
        return _load_json(CFG_EXAMPLE)
    raise FileNotFoundError("缺少 price_watch 配置文件")


def _to_float(v: Any) -> Optional[float]:
    try:
        if v is None or v == "" or v == "-":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _alert_key(code: str, kind: str) -> str:
    return f"{str(code).zfill(6)}:{kind}:{datetime.now().strftime('%Y-%m-%d')}"


def _day_key(kind: str) -> str:
    return f"day:{kind}:{datetime.now().strftime('%Y-%m-%d')}"


def _cooldown_ok(state: Dict[str, Any], key: str, cooldown_min: int) -> bool:
    last = (state.get("last_push") or {}).get(key)
    if not last:
        return True
    try:
        ts = datetime.fromisoformat(str(last))
    except ValueError:
        return True
    return (datetime.now() - ts).total_seconds() >= max(1, cooldown_min) * 60


def _once_per_day_ok(state: Dict[str, Any], key: str) -> bool:
    return key not in (state.get("last_push") or {})


def _mark_pushed(state: Dict[str, Any], key: str) -> None:
    state.setdefault("last_push", {})[key] = datetime.now().isoformat(timespec="seconds")
    _save_json(STATE_PATH, state)


def _push(cfg: Dict[str, Any], title: str, body: str) -> None:
    ch = push_from_cfg(title, body, cfg)
    print(f"[push:{ch}] {title}", flush=True)


def _in_hm_window(start: dtime, end: dtime, now: Optional[datetime] = None) -> bool:
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return start <= t <= end


def _watch_lines(watches: List[Dict[str, Any]]) -> str:
    rows = []
    for w in watches:
        rows.append(
            f"- {w.get('name')}({w.get('code')}) 挂买 ≤ {w.get('buy_below')}"
            + (f"（远离>{w.get('cancel_above')}建议撤）" if w.get("cancel_above") else "")
        )
    return "\n".join(rows)


def _maybe_schedule_alerts(cfg: Dict[str, Any], state: Dict[str, Any]) -> None:
    """
    挂单通知：默认 09:27~09:35（集合竞价结束、能看到开盘意向后再说）。
    - 现价仍高于限价：提醒按限价挂买单等待回踩
    - 大低开已≤限价：先不催挂原限价（避免一开盘就按高价意愿成交），改发「低开观察」
    尾盘撤单：默认 14:50~14:57
    """
    watches = list(cfg.get("watches") or [])
    if not watches:
        return

    hang_start = str(cfg.get("hang_remind_start") or "09:27")
    hang_end = str(cfg.get("hang_remind_end") or "09:35")
    hs = datetime.strptime(hang_start, "%H:%M").time()
    he = datetime.strptime(hang_end, "%H:%M").time()
    if _in_hm_window(hs, he):
        key = _day_key("hang_order")
        if _once_per_day_ok(state, key):
            can_hang: List[Dict[str, Any]] = []
            gap_down: List[str] = []
            unknown: List[str] = []
            for w in watches:
                code = str(w.get("code") or "").zfill(6)
                name = str(w.get("name") or code)
                buy_below = _to_float(w.get("buy_below"))
                if not code or buy_below is None:
                    continue
                q = fetch_realtime_quote(code)
                px = _to_float(q.get("price"))
                open_px = _to_float(q.get("open"))
                ref = open_px if open_px is not None else px
                disp = f"{name}({code})"
                if ref is None:
                    unknown.append(f"- {disp} 暂无开盘价，自行看盘")
                    continue
                # 开盘价/现价已低于等于限价 = 大低开相对挂单价
                if ref <= buy_below:
                    gap_down.append(
                        f"- {disp} 开/现约 {ref:.2f} ≤ 原限价 {buy_below:.2f} → 先别急挂原限价"
                    )
                else:
                    can_hang.append(w)

            parts: List[str] = []
            title = "【挂单通知】"
            if can_hang:
                title = "【挂单通知】可按限价挂单"
                parts.append("下面这些还在限价上方，可挂买单等回踩：")
                parts.append(_watch_lines(can_hang))
            if gap_down:
                if not can_hang:
                    title = "【低开观察】先不挂原限价"
                parts.append("")
                parts.append("大低开已低于/等于原限价，先观察分时，勿机械挂原价：")
                parts.append("\n".join(gap_down))
                parts.append("等企稳后再决定更低挂单价或放弃。")
            if unknown:
                parts.append("")
                parts.append("\n".join(unknown))
            if not parts:
                parts.append("暂无有效标的。")
            parts.append(f"\n时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            parts.append("（仅提醒，不会自动下单）")
            _push(cfg, title, "\n".join(parts).strip())
            _mark_pushed(state, key)

    cx_start = str(cfg.get("cancel_eod_start") or "14:50")
    cx_end = str(cfg.get("cancel_eod_end") or "14:57")
    cs = datetime.strptime(cx_start, "%H:%M").time()
    ce = datetime.strptime(cx_end, "%H:%M").time()
    if _in_hm_window(cs, ce):
        key = _day_key("cancel_eod")
        if _once_per_day_ok(state, key):
            body = (
                "尾盘将至：若限价买单仍未成交，建议撤单，避免尾盘乱跳误成交。\n"
                f"{_watch_lines(watches)}\n"
                f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            _push(cfg, "【撤单通知】尾盘请检查限价单", body)
            _mark_pushed(state, key)


def run_once(cfg: Dict[str, Any], state: Dict[str, Any]) -> None:
    watches: List[Dict[str, Any]] = list(cfg.get("watches") or [])
    cooldown = int(cfg.get("cooldown_min") or 45)
    # 接近限价：现价落到「限价×(1+比例)」以内算提前盯（默认 2%）
    approach_pct = float(cfg.get("approach_pct") or 2.0) / 100.0
    # 远离限价自动建议撤：默认限价上方 2.5%
    default_cancel_pct = float(cfg.get("cancel_above_pct") or 2.5) / 100.0

    _maybe_schedule_alerts(cfg, state)

    if not watches:
        print("[!] watches 为空", flush=True)
        return

    for w in watches:
        code = str(w.get("code") or "").zfill(6)
        name = str(w.get("name") or code)
        buy_below = _to_float(w.get("buy_below"))
        note = str(w.get("note") or "")
        if not code or buy_below is None:
            continue

        cancel_above = _to_float(w.get("cancel_above"))
        if cancel_above is None:
            cancel_above = round(buy_below * (1.0 + default_cancel_pct), 2)
        approach_line = round(buy_below * (1.0 + approach_pct), 2)

        q = fetch_realtime_quote(code)
        px = _to_float(q.get("price"))
        pct = _to_float(q.get("pct"))
        low = _to_float(q.get("low"))
        disp = name if name else str(q.get("name") or code)
        if px is None:
            print(f"[skip] {code} 无报价", flush=True)
            continue

        msg = (
            f"{disp}({code}) 现价{px:.2f} 涨跌{pct if pct is not None else '-'}% "
            f"最低{low if low is not None else '-'}"
        )
        print(msg, flush=True)
        now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 1) 到价：现价或当日最低触及限价
        hit_buy = px <= buy_below or (low is not None and low <= buy_below)
        # 接近：现价或最低进入提前带（尾盘砸下来也要喊）
        near = (not hit_buy) and (
            px <= approach_line or (low is not None and low <= approach_line)
        )

        if hit_buy:
            key = _alert_key(code, "buy_hit")
            if _cooldown_ok(state, key, cooldown):
                body = (
                    f"{msg}\n"
                    f"已触及挂买限价 ≤ {buy_below:.2f}\n"
                    f"{note}\n时间：{now_s}\n"
                    "请打开交易软件查看是否已成交；不想买立刻撤单！"
                )
                _push(cfg, f"【到价/将成交】{disp}", body)
                _mark_pushed(state, key)
            else:
                print(f"[cooldown] 到价 {disp}", flush=True)

        elif near:
            key = _alert_key(code, "approach")
            if _cooldown_ok(state, key, max(20, min(cooldown, 30))):
                body = (
                    f"{msg}\n"
                    f"已接近挂买限价 {buy_below:.2f}（提前带 ≤{approach_line:.2f}）\n"
                    f"挂单可能随时成交——不想买请马上撤单！\n时间：{now_s}"
                )
                _push(cfg, f"【提前撤/盯】{disp}接近限价", body)
                _mark_pushed(state, key)

        # 3) 撤单：现价远离挂单价
        if px >= cancel_above:
            key = _alert_key(code, "cancel_far")
            if _cooldown_ok(state, key, cooldown):
                body = (
                    f"{msg}\n"
                    f"现价已远离挂买限价 {buy_below:.2f}（≥ {cancel_above:.2f}）\n"
                    "建议撤掉未成交买单，避免盘中急杀误成交。\n"
                    f"时间：{now_s}"
                )
                _push(cfg, f"【撤单通知】{disp}已远离限价", body)
                _mark_pushed(state, key)


def _should_run_now(force: bool) -> bool:
    if force:
        return True
    if is_cn_trading_session():
        return True
    # 开盘前挂单窗、尾盘撤单窗（即便边界上 trading_session 判断略有出入）
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return (dtime(9, 15) <= t <= dtime(9, 30)) or (dtime(14, 45) <= t <= dtime(15, 5))


def main() -> int:
    ap = argparse.ArgumentParser(description="限价盯盘微信提醒")
    ap.add_argument("--once", action="store_true", help="只跑一轮")
    ap.add_argument("--test", action="store_true", help="发送测试推送后退出")
    ap.add_argument("--force", action="store_true", help="非交易时段也盯（测试用）")
    args = ap.parse_args()

    cfg = _load_cfg()
    state = _load_json(STATE_PATH) if STATE_PATH.exists() else {}

    if args.test:
        watches = cfg.get("watches") or []
        _push(
            cfg,
            "Qbot盯盘·测试",
            "微信推送正常。\n"
            f"时间 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            "明日将推送：挂单通知 / 提前接近 / 到价 / 撤单通知\n"
            + _watch_lines(list(watches)),
        )
        return 0

    if cfg.get("startup_ping") and not args.once:
        try:
            _push(
                cfg,
                "Qbot盯盘已启动",
                "家里脚本已运行（PushPlus）。\n"
                "· 09:27 后【挂单通知】（能看开盘；大低开低于限价则先不催挂）\n"
                "· 接近限价【提前盯盘】\n"
                "· 触及限价【到价通知】\n"
                "· 远离限价/尾盘【撤单通知】\n"
                + _watch_lines(list(cfg.get("watches") or [])),
            )
        except Exception as exc:  # noqa: BLE001
            # 启动推送失败不能退出，否则计划任务秒退、全天无盯盘
            print(f"[!] 启动推送失败（继续盯盘）: {exc}", flush=True)

    poll = max(15, int(cfg.get("poll_sec") or 60))
    print(f"poll={poll}s cfg={CFG_LOCAL if CFG_LOCAL.exists() else CFG_EXAMPLE}", flush=True)

    while True:
        try:
            if _should_run_now(args.force) or args.once:
                print(
                    f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] tick",
                    flush=True,
                )
                run_once(cfg, state)
            else:
                print(
                    f"[{datetime.now().strftime('%H:%M:%S')}] 非交易时段，休眠…",
                    flush=True,
                )
        except Exception as exc:  # noqa: BLE001
            print(f"[error] {exc}", flush=True)

        if args.once:
            break
        time.sleep(poll)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
