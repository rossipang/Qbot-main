# -*- coding: utf-8 -*-
"""
分情形盘中盯盘 → 微信 PushPlus（不下单）。

情形（每票独立判定）：
  1) 高开高走：视为错过，不追、不给买价
  2) 平开回踩：开盘确认后给出保守限价，到价提醒
  3) 低开：上午只观察；午后若回升且未破止损位，再给保守买价；跌破止损则放弃
  4) 浅回踩企稳可挂：未砸穿挂价时，到挂价区缩量横住→可挂/可变通
  5) 深砸穿挂价：放量下杀→撤单；禁止在挂价下方半死不活喊「可挂」
  6) 真V才可挂回：须收复跌幅过半 + 流出/量能收窄 + 现价回到挂价附近；微弹不到挂价不推
  7) 可买类同日每种只推1次；「挂着等」默认不推，避免弱势票刷屏

周五建议更保守；仅提醒，不自动下单。

用法：
  set PYTHONPATH=.
  python -u scripts/price_watch_scenarios.py
  python -u scripts/price_watch_scenarios.py --test
  python -u scripts/price_watch_scenarios.py --once
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qbot.data.intraday import fetch_realtime_quote  # noqa: E402
from qbot.notify.wechat_push import push_from_cfg  # noqa: E402

CFG_LOCAL = ROOT / "qbot" / "gui" / "csv" / "price_watch_local.json"
STATE_PATH = ROOT / "qbot" / "gui" / "csv" / "price_watch_scenario_state.json"
LOCK_PATH = ROOT / "qbot" / "gui" / "csv" / "price_watch_scenario.lock"


def _acquire_lock() -> bool:
    """防止 9:00/9:15 两个计划任务各起一个循环。"""
    import os

    if LOCK_PATH.exists():
        try:
            old_pid = int(LOCK_PATH.read_text(encoding="utf-8").strip().split("\n")[0])
            # Windows: 进程还在则认为已有实例
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(0x1000, False, old_pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if handle:
                kernel32.CloseHandle(handle)
                return False
        except Exception:
            pass
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOCK_PATH.write_text(f"{os.getpid()}\n{datetime.now().isoformat()}", encoding="utf-8")
    return True


def _release_lock() -> None:
    try:
        if LOCK_PATH.exists():
            LOCK_PATH.unlink()
    except Exception:
        pass


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _to_float(v: Any) -> Optional[float]:
    try:
        if v is None or v == "" or v == "-":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _day() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _alert_key(code: str, kind: str) -> str:
    return f"{str(code).zfill(6)}:{kind}:{_day()}"


def _cooldown_ok(state: Dict[str, Any], key: str, cooldown_min: int) -> bool:
    last = (state.get("last_push") or {}).get(key)
    if not last:
        return True
    try:
        ts = datetime.fromisoformat(str(last))
    except ValueError:
        return True
    return (datetime.now() - ts).total_seconds() >= max(1, cooldown_min) * 60


def _once_ok(state: Dict[str, Any], key: str) -> bool:
    return key not in (state.get("last_push") or {})


def _mark(state: Dict[str, Any], key: str) -> None:
    state.setdefault("last_push", {})[key] = datetime.now().isoformat(timespec="seconds")
    _save_json(STATE_PATH, state)


def _push(cfg: Dict[str, Any], title: str, body: str) -> None:
    ch = push_from_cfg(title, body, cfg)
    print(f"[push:{ch}] {title}", flush=True)


def _in_window(start: str, end: str, now: Optional[datetime] = None) -> bool:
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    s = datetime.strptime(start, "%H:%M").time()
    e = datetime.strptime(end, "%H:%M").time()
    t = now.time()
    return s <= t <= e


def _classify_open(
    open_px: float, pre: float, *, gap_up_pct: float, gap_down_pct: float
) -> str:
    """返回 gap_up / flat / gap_down。"""
    chg = (open_px / pre - 1.0) * 100.0
    if chg >= gap_up_pct:
        return "gap_up"
    if chg <= -abs(gap_down_pct):
        return "gap_down"
    return "flat"


def _is_running_strong(px: float, open_px: float, high: Optional[float]) -> bool:
    """高开后仍偏强：现价不弱于开盘，且未从最高大回撤。"""
    if px < open_px * 0.997:
        return False
    if high is not None and high > 0 and (px / high - 1.0) * 100.0 <= -2.5:
        return False  # 高开后已明显冲高回落，改观察
    return px >= open_px


def _minute_tail_stats(code: str, st: Dict[str, Any]) -> Dict[str, Any]:
    """拉分时尾部（带短缓存），用于接近挂价时的缩量/放量判断。"""
    import time as _time

    now_ts = _time.time()
    cache = st.get("_min_cache") or {}
    if (
        cache.get("code") == code
        and now_ts - float(cache.get("ts") or 0) < 55
        and cache.get("stats")
    ):
        return dict(cache["stats"])

    stats: Dict[str, Any] = {
        "ok": False,
        "vol_shrink": False,
        "vol_dump": False,
        "flat_up": False,
        "dumping": False,
        "tail_n": 0,
        "why": "",
    }
    try:
        from qbot.data.intraday import fetch_minute_trends

        df, _meta = fetch_minute_trends(code)
        if df is None or df.empty or len(df) < 12:
            st["_min_cache"] = {"code": code, "ts": now_ts, "stats": stats}
            return stats
        prices = df["price"].astype(float)
        vols = df["volume"].astype(float)
        n = len(df)
        tail = 8
        mid = max(12, min(25, n // 3))
        p_tail = prices.iloc[-tail:]
        v_tail = vols.iloc[-tail:]
        v_base = vols.iloc[max(0, n - mid - tail) : n - tail]
        if v_base.empty:
            v_base = vols.iloc[: max(8, n - tail)]
        p0 = float(p_tail.iloc[0])
        p1 = float(p_tail.iloc[-1])
        p_hi = float(p_tail.max())
        p_lo = float(p_tail.min())
        chg = (p1 / p0 - 1.0) * 100.0 if p0 > 0 else 0.0
        rng = (p_hi / p_lo - 1.0) * 100.0 if p_lo > 0 else 0.0
        from_local_hi = (p1 / p_hi - 1.0) * 100.0 if p_hi > 0 else 0.0
        v_t = float(v_tail.median()) if len(v_tail) else 0.0
        v_b = float(v_base.median()) if len(v_base) else 0.0
        vol_ratio = (v_t / v_b) if v_b > 1e-9 else 1.0
        vol_shrink = vol_ratio <= 0.88
        vol_dump = vol_ratio >= 1.35 and from_local_hi <= -0.45
        flat_up = rng <= 0.85 and chg >= -0.15 and from_local_hi >= -0.55
        dumping = from_local_hi <= -0.7 and chg <= -0.35
        # 近端向上：用于 V 反转确认
        rising = chg >= 0.25 and from_local_hi >= -0.4
        stats.update(
            {
                "ok": True,
                "vol_shrink": vol_shrink,
                "vol_dump": vol_dump,
                "flat_up": flat_up,
                "dumping": dumping,
                "rising": rising,
                "tail_n": int(tail),
                "vol_ratio": round(vol_ratio, 2),
                "chg": round(chg, 2),
                "rng": round(rng, 2),
                "from_local_hi": round(from_local_hi, 2),
                "why": (
                    f"近{tail}分 涨跌{chg:+.2f}% 振幅{rng:.2f}% "
                    f"量比基线{vol_ratio:.2f}"
                ),
            }
        )
    except Exception as exc:  # noqa: BLE001
        stats["why"] = f"分时暂不可用:{exc}"
    st["_min_cache"] = {"code": code, "ts": now_ts, "stats": stats}
    return stats


def _pierced_hang(
    *,
    px: float,
    buy: float,
    sess_low: Optional[float],
    st: Dict[str, Any],
) -> bool:
    """今低/现价已明显砸穿挂价 → 不能再当「浅回踩到价」。"""
    if buy <= 0:
        return False
    if sess_low is not None and sess_low > 0 and sess_low <= buy * 0.985:
        return True
    if px > 0 and px <= buy * 0.985:
        return True
    if st.get("saw_deep") and sess_low is not None and sess_low <= buy * 0.99:
        return True
    return False


def _near_hang_zone(px: float, buy: float, flex_ceil: float) -> bool:
    """现价必须回到挂价附近（含浅变通上沿），才允许喊可挂/可买。"""
    if buy <= 0 or px <= 0:
        return False
    return buy * 0.992 <= px <= flex_ceil * 1.001


def _judge_buy_zone(
    *,
    code: str,
    px: float,
    buy: float,
    open_px: float,
    high: Optional[float],
    low: Optional[float],
    avg: Optional[float],
    pre: Optional[float],
    invalidate: Optional[float],
    st: Dict[str, Any],
    flex_pct: float = 0.008,
) -> Tuple[str, str, float]:
    """
    浅回踩到挂价区的变通判定（未砸穿挂价）。
    返回 (action, why, suggest_px)
      flex_buy  — 略高于挂价也可成交（缩量横住/企稳）
      buy_ok    — 到挂价可试
      cancel    — 放量下杀，撤单
      wait      — 还在回落/未企稳，先挂着别急
      skip      — 离挂价还远 / 已砸穿改走真V / 瀑布不买
    """
    if buy <= 0 or px <= 0:
        return "skip", "报价异常", buy
    sess_low = _to_float(st.get("session_low")) or (float(low) if low else None)
    flex_ceil = buy * (1.0 + max(0.004, flex_pct))

    # 已砸穿挂价：禁止在挂价下方喊「到价可挂」（中船 305→289 假案）
    # 深砸后只允许真V回到挂价区再提示；此处最多给撤单
    if _pierced_hang(px=px, buy=buy, sess_low=sess_low, st=st):
        ms0 = _minute_tail_stats(code, st)
        if ms0.get("ok") and (ms0.get("vol_dump") or ms0.get("dumping")):
            return (
                "cancel",
                f"已砸穿挂价{buy:.2f}且放量下杀·撤单勿接；{ms0.get('why')}",
                buy,
            )
        if not _near_hang_zone(px, buy, flex_ceil):
            return (
                "skip",
                f"已砸穿挂价{buy:.2f}（今低{sess_low} 现价{px:.2f}），"
                f"半死不活反弹不推可挂；须真V回到挂价附近",
                buy,
            )
        # 价格已回到挂价区：仍交给 V 判定，避免浅回踩通道误放行
        return (
            "skip",
            f"曾砸穿挂价，回到挂价区也须走真V确认，不走浅回踩到价",
            buy,
        )

    # 瀑布/贴止损：到挂价也不推可买（只允许撤单）
    if _is_waterfall(
        px=px,
        open_px=open_px,
        pre=float(pre or open_px),
        high=high,
        sess_low=sess_low,
        invalidate=invalidate,
        buy=buy,
    ):
        ms0 = _minute_tail_stats(code, st)
        if ms0.get("ok") and (ms0.get("vol_dump") or ms0.get("dumping")):
            return "cancel", f"瀑布下跌中放量下杀·撤单勿接；{ms0.get('why')}", buy
        return "skip", "瀑布/贴止损途中，挂价无效不推可买", buy

    # 离挂价太远（上方）：不打扰
    if px > flex_ceil * 1.002:
        return "skip", f"现价{px:.2f}仍高于变通区≤{flex_ceil:.2f}", buy

    ms = _minute_tail_stats(code, st)
    mwhy = str(ms.get("why") or "")

    # 放量下杀优先：撤单
    if ms.get("ok") and (ms.get("vol_dump") or (ms.get("dumping") and ms.get("vol_ratio", 1) >= 1.15)):
        return (
            "cancel",
            f"接近挂价但放量下杀·建议撤单勿接；{mwhy}",
            buy,
        )

    # 略高于挂价：缩量横着/企稳 → 可变通现价成交
    if px > buy:
        stable = False
        if ms.get("ok") and (ms.get("flat_up") or ms.get("vol_shrink")) and not ms.get("dumping"):
            stable = True
        # 分时均价附近横住也算
        if avg and avg > 0 and abs(px / avg - 1.0) <= 0.006 and ms.get("vol_shrink"):
            stable = True
        # 回落后不再贴死今低、近端横住
        if (
            low
            and high
            and high > low * 1.015
            and px >= low * 1.004
            and ms.get("ok")
            and ms.get("flat_up")
        ):
            stable = True
        if stable:
            return (
                "flex_buy",
                f"接近挂价{buy:.2f}·缩量企稳/横着走，现价{px:.2f}也可成交不必死守；{mwhy}",
                px,
            )
        return (
            "wait",
            f"在变通区({buy:.2f}~{flex_ceil:.2f})但未确认缩量企稳，先挂着观察；{mwhy}",
            buy,
        )

    # 浅回踩：仅「刚到/略低于挂价」才算到价（禁止远低于挂价）
    if px < buy * 0.992:
        return (
            "skip",
            f"现价{px:.2f}低于挂价过深，不算浅回踩到价；等回挂价或走真V",
            buy,
        )

    if ms.get("ok") and ms.get("vol_dump"):
        return "cancel", f"到价但放量下杀·撤单；{mwhy}", buy
    if ms.get("ok") and (ms.get("flat_up") or ms.get("vol_shrink")):
        return "buy_ok", f"浅回踩到价且缩量企稳可试；{mwhy}", min(px, buy)
    # 刚砸下来还没企稳：不推可买（默认不推 wait）
    if (
        high
        and high > 0
        and (px / high - 1.0) * 100.0 <= -2.0
        and ms.get("ok")
        and ms.get("dumping")
    ):
        return (
            "wait",
            f"到价附近但仍在回落段，等缩量横住再成交；{mwhy}",
            buy,
        )
    # 分时佐证不足：不默认「可试」，避免弱势票瞎提示
    return "wait", f"到挂价附近但企稳未确认，先观察；{mwhy}", buy


def _track_dump_and_low(
    st: Dict[str, Any],
    *,
    px: float,
    open_px: float,
    high: Optional[float],
    low: Optional[float],
    buy: float,
    code: str,
) -> None:
    """记录今低、是否出现过放量/深砸，供 V 反转用。"""
    if low is not None and low > 0:
        prev = _to_float(st.get("session_low"))
        new_low = low if prev is None else min(float(prev), float(low))
        # 推过 V 后又创新低 → 假 V 失败，当日禁再推可挂
        if (
            st.get("v_alerted")
            and prev is not None
            and float(new_low) < float(prev) * 0.998
        ):
            st["v_failed"] = True
        st["session_low"] = new_low
    sess_low = _to_float(st.get("session_low"))
    if sess_low and open_px > 0 and sess_low <= open_px * 0.985:
        st["saw_deep"] = True
    if sess_low and buy > 0 and sess_low <= buy * 0.992:
        st["saw_deep"] = True
    if high and open_px > 0 and high >= open_px * 1.01 and px <= open_px * 0.99:
        st["saw_fade"] = True
    ms = _minute_tail_stats(code, st)
    if ms.get("ok") and (ms.get("vol_dump") or ms.get("dumping")):
        st["saw_dump"] = True


def _is_waterfall(
    *,
    px: float,
    open_px: float,
    pre: float,
    high: Optional[float],
    sess_low: Optional[float],
    invalidate: Optional[float],
    buy: float,
) -> bool:
    """
    瀑布/破位途中：碰到挂价也不算买点（区别于真 V）。
    """
    if open_px <= 0 or px <= 0:
        return False
    day_pct = (px / pre - 1.0) * 100.0 if pre and pre > 0 else 0.0
    from_open = (px / open_px - 1.0) * 100.0
    # 贴近止损：只等放弃，不再可挂
    if invalidate is not None and invalidate > 0 and px <= invalidate * 1.025:
        return True
    # 相对开盘仍深亏，且未收复跌幅的一半 → 下跌中继，不是回踩买
    if sess_low and sess_low > 0 and sess_low < open_px:
        dump = open_px - sess_low
        recovered = px - sess_low
        if dump > 0 and recovered / dump < 0.45 and from_open <= -1.5:
            return True
    # 当日明显走弱且仍在开盘下方
    if day_pct <= -3.0 and px < open_px * 0.995:
        return True
    if high and high > 0 and (px / high - 1.0) * 100.0 <= -4.0 and px < open_px:
        return True
    # 挂价本身已在深跌区（买点相对开盘低太多且现价还在往下磨）
    if buy > 0 and buy <= open_px * 0.97 and px <= buy * 1.01 and from_open <= -2.5:
        return True
    return False


def _judge_v_reversal(
    *,
    code: str,
    px: float,
    buy: float,
    open_px: float,
    high: Optional[float],
    low: Optional[float],
    avg: Optional[float],
    pre: Optional[float],
    invalidate: Optional[float],
    st: Dict[str, Any],
    flex_pct: float = 0.008,
) -> Tuple[str, str, float]:
    """
    真 V：深砸后向上修复，量能/抛压收窄，且现价回到挂价附近才可挂/可买。
    假 V（中船）：挂价305、现价289 微弹 → 绝不推可挂。
    """
    if buy <= 0 or px <= 0 or open_px <= 0:
        return "skip", "", buy
    if st.get("v_failed"):
        return "skip", "", buy
    sess_low = _to_float(st.get("session_low")) or (float(low) if low else None)
    if not sess_low or sess_low <= 0:
        return "skip", "", buy

    had_dump = bool(st.get("saw_dump") or st.get("saw_deep") or st.get("saw_fade"))
    if not had_dump:
        return "skip", "", buy

    flex_ceil = buy * (1.0 + max(0.004, flex_pct))
    # 硬门槛：不到挂价附近，再漂亮的反弹也不推可挂（防 289 瞎喊）
    if not _near_hang_zone(px, buy, flex_ceil):
        return "skip", "", buy

    # 贴止损 / 瀑布途中：禁止 V 可挂
    if _is_waterfall(
        px=px,
        open_px=open_px,
        pre=float(pre or open_px),
        high=high,
        sess_low=sess_low,
        invalidate=invalidate,
        buy=buy,
    ):
        return "skip", "", buy

    bounce = (px / sess_low - 1.0) * 100.0
    depth_vs_open = (sess_low / open_px - 1.0) * 100.0
    dump = open_px - sess_low
    reclaim_frac = ((px - sess_low) / dump) if dump > 1e-9 else 0.0
    # 真 V：至少从低点弹 2.5%，且收复开盘→低点跌幅的 ≥55%
    deep_ok = depth_vs_open <= -1.8 or (buy > 0 and sess_low <= buy * 0.985)
    if not deep_ok or bounce < 2.5 or reclaim_frac < 0.55:
        return "skip", "", buy

    ms = _minute_tail_stats(code, st)
    mwhy = str(ms.get("why") or "")
    if ms.get("ok") and (ms.get("vol_dump") or ms.get("dumping")):
        return "skip", "", buy
    # 近端必须明确向上，不允许横着微弹就算 V
    rising_ok = ms.get("ok") and (
        ms.get("rising") or (float(ms.get("chg") or 0) >= 0.35 and not ms.get("dumping"))
    )
    if not rising_ok:
        return "skip", "", buy
    # 抛压/量能收窄：不允许放量赶顶式假抽
    flow_ok = (not ms.get("ok")) or bool(
        ms.get("vol_shrink")
        or float(ms.get("vol_ratio") or 99) <= 1.08
        or (ms.get("rising") and float(ms.get("vol_ratio") or 99) <= 1.2)
    )
    if not flow_ok:
        return "skip", "", buy

    # 必须站回开盘附近，或站回挂价并站上分时均价（新易盛式回抽）
    reclaim_open = px >= open_px * 0.995
    reclaim_avg = avg is not None and avg > 0 and px >= avg * 0.998
    why = (
        f"今低{sess_low:.2f}→现价{px:.2f} 反弹{bounce:.1f}% "
        f"收复跌幅{reclaim_frac*100:.0f}%；"
        f"低点相对开盘{depth_vs_open:.1f}%；已回挂价区≤{buy:.2f}；{mwhy}"
    )

    if not (reclaim_open or (reclaim_avg and reclaim_frac >= 0.7)):
        return "skip", "", buy

    # 已回到挂价变通区 → 可买/可挂
    if px <= buy:
        return "v_hang", f"真V回到挂价，可挂回≤{buy:.2f}；{why}", buy
    return "v_buy", f"真V站上挂价附近，可按约{min(px, flex_ceil):.2f}成交；{why}", min(px, flex_ceil)


def _zone_push(
    *,
    action: str,
    name: str,
    code: str,
    px: float,
    buy: float,
    suggest: float,
    high: Optional[float],
    low: Optional[float],
    invalidate: Optional[float],
    note: str,
    why: str,
    now: datetime,
    tag: str = "",
) -> Tuple[str, str]:
    inv = f"{invalidate:.2f}" if invalidate is not None else "-"
    if action == "flex_buy":
        title = f"【可变通成交】{name}"
        body = (
            f"{name}({code}) 不必死守挂价 {buy:.2f}\n"
            f"现价 {px:.2f} 已可按约 {suggest:.2f} 成交（浅挂变通）\n"
            f"{why}\n"
            f"今高{high} 今低{low}；破 {inv} 仍放弃\n"
            f"{tag}{note}\n{now.strftime('%H:%M:%S')}"
        )
    elif action == "buy_ok":
        title = f"【到价可试】{name}"
        body = (
            f"{name}({code}) 现价 {px:.2f} / 挂价 ≤ {buy:.2f}\n"
            f"{why}\n"
            f"今高{high} 今低{low}；破 {inv} 放弃\n"
            f"{tag}{note}\n{now.strftime('%H:%M:%S')}"
        )
    elif action == "v_hang":
        title = f"【V反转可挂】{name}"
        body = (
            f"{name}({code}) 早盘砸后出现 V 反转\n"
            f"现价 {px:.2f} → 建议挂回 ≤ {suggest:.2f} 接回抽\n"
            f"{why}\n"
            f"今高{high} 今低{low}；若再放量下杀听撤单提示\n"
            f"破 {inv} 放弃\n"
            f"{tag}{note}\n{now.strftime('%H:%M:%S')}"
        )
    elif action == "v_buy":
        title = f"【V反转可买】{name}"
        body = (
            f"{name}({code}) V 反转已回到挂价附近\n"
            f"现价 {px:.2f}，可按约 {suggest:.2f} 成交\n"
            f"{why}\n"
            f"今高{high} 今低{low}；破 {inv} 放弃\n"
            f"{tag}{note}\n{now.strftime('%H:%M:%S')}"
        )
    elif action == "cancel":
        title = f"【撤单】{name} 放量下杀"
        body = (
            f"{name}({code}) 现价 {px:.2f}（挂价 {buy:.2f}）\n"
            f"{why}\n"
            f"建议立刻撤挂，别在下杀里成交。\n"
            f"本日该票撤单只提醒这一次，走坏别反复接。\n"
            f"今高{high} 今低{low}\n"
            f"{tag}{note}\n{now.strftime('%H:%M:%S')}"
        )
    else:  # wait
        title = f"【挂着等企稳】{name}"
        body = (
            f"{name}({code}) 现价 {px:.2f} / 挂价 {buy:.2f}\n"
            f"{why}\n"
            f"先别点成交；缩量横住→可变通；V反转→可挂回。\n"
            f"放量下杀→撤单。\n"
            f"{tag}{note}\n{now.strftime('%H:%M:%S')}"
        )
    return title, body


def _emit_zone_alert(
    *,
    cfg: Dict[str, Any],
    state: Dict[str, Any],
    st: Dict[str, Any],
    code: str,
    name: str,
    action: str,
    why: str,
    suggest: float,
    px: float,
    buy: float,
    high: Optional[float],
    low: Optional[float],
    invalidate: Optional[float],
    note: str,
    now: datetime,
    cooldown: int,
    tag: str = "",
    key_prefix: str = "zone",
) -> None:
    if action == "skip":
        return
    # 「挂着等」只吵人，默认不推（需要时看撤单/真V/到价）
    if action == "wait":
        return
    if action == "cancel":
        st["saw_dump"] = True
    if action in ("v_hang", "v_buy"):
        st["v_alerted"] = True
    # 可买/撤单/放弃类：同日每种只推 1 次（避免华发式「放量下杀」刷屏）
    once_actions = {"buy_ok", "flex_buy", "v_hang", "v_buy", "cancel"}
    key = _alert_key(code, f"{key_prefix}_{action}")
    if action in once_actions:
        if not _once_ok(state, key):
            return
    else:
        if not _cooldown_ok(state, key, cooldown):
            return
    title, body = _zone_push(
        action=action,
        name=name,
        code=code,
        px=px,
        buy=buy,
        suggest=suggest,
        high=high,
        low=low,
        invalidate=invalidate,
        note=note,
        why=why,
        now=now,
        tag=tag,
    )
    _push(cfg, title, body)
    _mark(state, key)


def _fetch_metals(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """拉沪金/沪银主力连续报价（新浪），用于贵金属股联动监控。"""
    mw = cfg.get("metal_watch") or {}
    if not mw.get("enabled"):
        return []
    symbols = str(mw.get("symbols") or "AU0,AG0")
    try:
        import akshare as ak

        df = ak.futures_zh_spot(symbol=symbols, market="CF", adjust="0")
        out: List[Dict[str, Any]] = []
        for _, r in df.iterrows():
            cur = _to_float(r.get("current_price"))
            settle = _to_float(r.get("last_settle_price"))
            pct = None
            if cur and settle and settle > 0:
                pct = round((cur / settle - 1.0) * 100.0, 2)
            out.append({"name": str(r.get("symbol") or ""), "price": cur, "pct": pct})
        return out
    except Exception as exc:  # noqa: BLE001
        print(f"[metal] fetch fail: {exc}", flush=True)
        return []


def _metal_watch_check(cfg: Dict[str, Any], state: Dict[str, Any]) -> None:
    """盘中金银联动：沪金/沪银日内跌幅穿阈值 → 推预警（每档每日一次）。"""
    mw = cfg.get("metal_watch") or {}
    if not mw.get("enabled"):
        return
    warn = float(mw.get("warn_drop_pct") or 1.0)
    danger = float(mw.get("danger_drop_pct") or 2.0)
    for m in _fetch_metals(cfg):
        if m.get("pct") is None:
            continue
        name, pct, price = m["name"], m["pct"], m.get("price")
        if pct <= -danger:
            key = _alert_key(name, "metal_danger")
            if _once_ok(state, key):
                _push(
                    cfg,
                    f"【{name}破位警戒】贵金属逻辑受损",
                    f"{name} 日内 {pct:+.2f}%（现价 {price}）\n"
                    f"跌穿 -{danger}% 警戒线：黄金/白银股减仓，勿接回踩。",
                )
                _mark(state, key)
        elif pct <= -warn:
            key = _alert_key(name, "metal_warn")
            if _once_ok(state, key):
                _push(
                    cfg,
                    f"【{name}回落提醒】",
                    f"{name} 日内 {pct:+.2f}%（现价 {price}）\n"
                    f"跌穿 -{warn}%：贵金属股今日不开新仓，持仓看个股线。",
                )
                _mark(state, key)


def run_once(cfg: Dict[str, Any], state: Dict[str, Any]) -> None:
    all_watches: List[Dict[str, Any]] = list(cfg.get("scenario_watches") or [])
    now0 = datetime.now()
    today = _day()
    # 任务按天过滤：只执行 date==今天(或无date)的任务；非当日任务留在配置里，不推也不删
    watches = [w for w in all_watches if str(w.get("date") or "") in ("", today)]
    stale = [w for w in all_watches if w not in watches]
    if stale:
        print(
            "[skip] %d 条非当日任务不推：%s"
            % (len(stale), "、".join(str(w.get("name") or w.get("code")) for w in stale)),
            flush=True,
        )
    # 金银联动监控：常驻模块，不受个股任务日期影响
    _metal_watch_check(cfg, state)
    # 收盘即收工：15:00 后不再处理个股场景，当天任务自然作废（次日由新 date 任务接管）
    if now0.time() >= dtime(15, 0):
        return
    if not watches:
        print("[!] 无当日盯盘任务", flush=True)
        return

    cooldown = int(cfg.get("cooldown_min") or 40)
    gap_up_pct = float(cfg.get("gap_up_pct") or 1.5)
    gap_down_pct = float(cfg.get("gap_down_pct") or 1.2)
    afternoon_start = str(cfg.get("afternoon_start") or "13:00")
    classify_start = str(cfg.get("classify_start") or "09:30")
    classify_end = str(cfg.get("classify_end") or "09:45")
    eod_start = str(cfg.get("cancel_eod_start") or "14:50")
    eod_end = str(cfg.get("cancel_eod_end") or "14:57")
    now = datetime.now()

    day_st = state.setdefault("days", {}).setdefault(_day(), {})

    # 竞价速报：9:20 起汇总并给出「先挂哪只」(按 priority + 开盘质量)，一天一次
    if watches and _in_window("09:20", "09:40"):
        key = _alert_key("ALL", "open_auction")
        if _once_ok(state, key):
            auction_rows: List[Dict[str, Any]] = []
            for w in watches:
                acode = str(w.get("code") or "").zfill(6)
                aname = str(w.get("name") or acode)
                q = fetch_realtime_quote(acode)
                aopen = _to_float(q.get("open"))
                apre = _to_float(q.get("pre_close"))
                if not aopen or not apre or apre <= 0:
                    continue
                ochg = (aopen / apre - 1.0) * 100.0
                pri = int(w.get("priority") or 99)
                deep = bool(w.get("deep_only"))
                if str(w.get("side") or "buy") == "sell":
                    sell_at = _to_float(w.get("sell_at"))
                    stop_below = _to_float(w.get("stop_below"))
                    if sell_at is not None and aopen >= sell_at:
                        act = f"已到卖点{sell_at}上方→竞价/开盘直接挂单走"
                        hangable = False
                    elif stop_below is not None and aopen <= stop_below:
                        act = f"竞价破止损{stop_below}→挂单价走，别等"
                        hangable = False
                    else:
                        act = "区间内→等盘中触发"
                        hangable = False
                else:
                    fb = _to_float(w.get("flat_buy_below"))
                    if fb is not None and ochg > gap_up_pct:
                        act = f"高开(买点{fb})→不追，等回踩"
                        hangable = False
                    elif deep:
                        act = f"深回踩票(≤{fb})→开盘先不挂，到价再说"
                        hangable = False
                    elif fb is not None and aopen <= fb * 1.01:
                        act = f"→可优先浅挂≤{fb}"
                        hangable = True
                    else:
                        act = "→按计划价位，开盘后看回踩再挂"
                        hangable = ochg <= 1.0
                auction_rows.append(
                    {
                        "pri": pri,
                        "hangable": hangable,
                        "ochg": ochg,
                        "line": f"P{pri} {aname} 开{aopen:.2f}({ochg:+.1f}%) {act}",
                    }
                )
            auction_rows.sort(key=lambda r: (r["pri"], r["ochg"]))
            first_hang = next((r for r in auction_rows if r.get("hangable")), None)
            head = (
                f"先挂：{first_hang['line'].split(' ', 1)[1] if first_hang else '暂无(高开居多/深回踩未到)'}；其余看回踩\n"
            )
            if auction_rows:
                _push(
                    cfg,
                    "【竞价速报】先挂判断",
                    head
                    + "\n".join(r["line"] for r in auction_rows)
                    + f"\n{now.strftime('%H:%M:%S')}",
                )
            _mark(state, key)

    # 尾盘提醒
    if _in_window(eod_start, eod_end):
        key = _alert_key("ALL", "eod")
        if _once_ok(state, key):
            _push(
                cfg,
                "【撤单检查】尾盘",
                "周五尾盘：若有未成交限价单请检查撤单，避免尾盘乱跳。\n"
                f"时间：{now.strftime('%Y-%m-%d %H:%M:%S')}",
            )
            _mark(state, key)

    for w in watches:
        code = str(w.get("code") or "").zfill(6)
        name = str(w.get("name") or code)
        flat_buy = _to_float(w.get("flat_buy_below"))
        afternoon_buy = _to_float(w.get("afternoon_buy_below")) or flat_buy
        invalidate = _to_float(w.get("invalidate_below"))
        note = str(w.get("note") or "")
        side = str(w.get("side") or "buy")
        if not code:
            continue
        if side == "buy" and flat_buy is None:
            continue

        q = fetch_realtime_quote(code)
        px = _to_float(q.get("price"))
        open_px = _to_float(q.get("open"))
        high = _to_float(q.get("high"))
        low = _to_float(q.get("low"))
        pre = _to_float(q.get("pre_close"))
        avg = _to_float(q.get("avg"))
        if px is None or open_px is None or pre is None or pre <= 0:
            print(f"[skip] {name} 报价不全", flush=True)
            continue

        # —— 卖出盯盘：反抽到目标价提醒卖 / 破止损线提醒割，独立于买入分类 ——
        if side == "sell":
            sell_at = _to_float(w.get("sell_at"))
            stop_below = _to_float(w.get("stop_below"))
            if sell_at is not None and ((high is not None and high >= sell_at) or px >= sell_at):
                key = _alert_key(code, "sell_target")
                if _once_ok(state, key):
                    _push(
                        cfg,
                        f"【到价卖】{name}",
                        f"{name}({code}) 盘中触及卖出目标 {sell_at:.2f}\n"
                        f"现价 {px:.2f} 今高 {high}\n"
                        f"按计划执行卖出/减仓，到价别犹豫。\n{note}\n"
                        f"{now.strftime('%H:%M:%S')}",
                    )
                    _mark(state, key)
            if stop_below is not None and ((low is not None and low <= stop_below) or px <= stop_below):
                key = _alert_key(code, "sell_stop")
                if _once_ok(state, key):
                    _push(
                        cfg,
                        f"【止损】{name}",
                        f"{name}({code}) 跌破止损线 {stop_below:.2f}\n"
                        f"现价 {px:.2f} 今低 {low}\n"
                        f"无条件执行，别等反弹。\n{note}\n"
                        f"{now.strftime('%H:%M:%S')}",
                    )
                    _mark(state, key)
            print(f"[sell-watch] {name} px={px} target={sell_at} stop={stop_below}", flush=True)
            continue

        open_chg = (open_px / pre - 1.0) * 100.0
        from_high = ((px / high) - 1.0) * 100.0 if high and high > 0 else None
        st = day_st.setdefault(code, {})

        # 跌破止损：全天放弃
        if invalidate is not None and (
            (low is not None and low <= invalidate) or px <= invalidate
        ):
            if not st.get("abandoned"):
                st["abandoned"] = True
                st["mode"] = "abandon"
                key = _alert_key(code, "abandon")
                if _once_ok(state, key):
                    _push(
                        cfg,
                        f"【放弃】{name}",
                        f"{name}({code}) 已跌破止损位 {invalidate:.2f}\n"
                        f"现价 {px:.2f} 今低 {low}\n"
                        f"按计划放弃，不再给买价。\n{note}\n"
                        f"{now.strftime('%H:%M:%S')}",
                    )
                    _mark(state, key)
            print(f"[abandon] {name} px={px} inv={invalidate}", flush=True)
            continue

        if st.get("abandoned"):
            continue

        # 开盘后分类（09:30~09:45 定调，之后沿用）
        mode = st.get("mode")
        if mode is None and _in_window(classify_start, "14:57"):
            # 开盘初期用开盘价分类；若稍后从高开变成冲高回落，可降级为 flat_watch
            kind = _classify_open(
                open_px, pre, gap_up_pct=gap_up_pct, gap_down_pct=gap_down_pct
            )
            if kind == "gap_up" and _is_running_strong(px, open_px, high):
                mode = "miss_strong"
            elif kind == "gap_up":
                # 高开但已走弱 → 当平开回踩观察，但买价更保守用 afternoon 档
                mode = "flat_pullback"
                st["from_gap_up_fade"] = True
            elif kind == "gap_down":
                mode = "gap_down_wait"
            else:
                mode = "flat_pullback"
            st["mode"] = mode
            st["open"] = open_px
            st["open_chg"] = round(open_chg, 2)
            print(f"[class] {name} open={open_px} ({open_chg:+.2f}%) → {mode}", flush=True)

        if mode is None:
            print(f"[wait-open] {name} px={px}", flush=True)
            continue

        # —— 1) 高开高走：错过 ——
        if mode == "miss_strong":
            # 若盘中转为冲高回落，降级
            if not _is_running_strong(px, open_px, high):
                st["mode"] = "flat_pullback"
                st["from_gap_up_fade"] = True
                mode = "flat_pullback"
                key = _alert_key(code, "fade_to_flat")
                if _once_ok(state, key):
                    buy = afternoon_buy if st.get("from_gap_up_fade") else flat_buy
                    _push(
                        cfg,
                        f"【降档】{name} 高开转弱",
                        f"{name} 高开后走弱，改为回踩观察\n"
                        f"保守挂买 ≤ {buy:.2f}（破 {invalidate} 放弃）\n"
                        f"现价 {px:.2f} 开盘 {open_px:.2f}\n{note}",
                    )
                    _mark(state, key)
            else:
                key = _alert_key(code, "miss")
                if _in_window(classify_start, classify_end) and _once_ok(state, key):
                    _push(
                        cfg,
                        f"【错过】{name} 高开高走",
                        f"{name}({code}) 高开约 {open_chg:+.1f}% 且偏强\n"
                        f"现价 {px:.2f} → 当错过，不追不挂。\n"
                        f"若冲高回落会再通知改回踩。\n{note}\n"
                        f"{now.strftime('%H:%M:%S')}",
                    )
                    _mark(state, key)
                print(f"[miss] {name} px={px}", flush=True)
                continue

        # —— 2) 平开回踩：给保守限价 ——
        if mode == "flat_pullback":
            buy = float(afternoon_buy) if st.get("from_gap_up_fade") else float(flat_buy)
            flex_pct = float(w.get("flex_pct") or cfg.get("flex_pct") or 0.008)
            _track_dump_and_low(
                st,
                px=px,
                open_px=open_px,
                high=high,
                low=low,
                buy=buy,
                code=code,
            )
            # 开盘确认窗口：浅挂观察（深回踩票不在此时催挂）
            key_h = _alert_key(code, "flat_hang")
            if (
                not bool(w.get("deep_only"))
                and _in_window("09:32", "09:50")
                and _once_ok(state, key_h)
            ):
                ceil = buy * (1.0 + flex_pct)
                _push(
                    cfg,
                    f"【平开观察】{name} 浅挂可变通",
                    f"{name}({code}) 开盘约 {open_chg:+.1f}%\n"
                    f"观察挂 ≤ {buy:.2f}；接近区约到 {ceil:.2f}\n"
                    f"· 浅回踩到挂价+缩量企稳 → 「可挂/可变通」\n"
                    f"· 放量下杀 → 「撤单」(本日只提醒一次)\n"
                    f"· 砸穿挂价后：须真V回到挂价附近才可挂；半空微弹不推\n"
                    f"破 {invalidate} 放弃。\n"
                    f"现价 {px:.2f}（今高{high} 今低{low}）\n{note}\n"
                    f"{now.strftime('%H:%M:%S')}",
                )
                _mark(state, key_h)

            # 先判真 V（假 V / 瀑布不推）
            v_act, v_why, v_sug = _judge_v_reversal(
                code=code,
                px=px,
                buy=buy,
                open_px=open_px,
                high=high,
                low=low,
                avg=avg,
                pre=pre,
                invalidate=invalidate,
                st=st,
                flex_pct=flex_pct,
            )
            _emit_zone_alert(
                cfg=cfg,
                state=state,
                st=st,
                code=code,
                name=name,
                action=v_act,
                why=v_why,
                suggest=v_sug,
                px=px,
                buy=buy,
                high=high,
                low=low,
                invalidate=invalidate,
                note=note,
                now=now,
                cooldown=cooldown,
                key_prefix="v",
            )

            # 进入挂价~变通上沿：缩量/下杀变通（瀑布中不推可买）
            action, why, suggest = _judge_buy_zone(
                code=code,
                px=px,
                buy=buy,
                open_px=open_px,
                high=high,
                low=low,
                avg=avg,
                pre=pre,
                invalidate=invalidate,
                st=st,
                flex_pct=flex_pct,
            )
            _emit_zone_alert(
                cfg=cfg,
                state=state,
                st=st,
                code=code,
                name=name,
                action=action,
                why=why,
                suggest=suggest,
                px=px,
                buy=buy,
                high=high,
                low=low,
                invalidate=invalidate,
                note=note,
                now=now,
                cooldown=cooldown,
                key_prefix="zone",
            )
            print(
                f"[flat] {name} px={px} buy≤{buy} v={v_act} act={action}",
                flush=True,
            )
            continue

        # —— 3) 低开：上午观察，午后回升再给价 ——
        if mode == "gap_down_wait":
            buy = float(afternoon_buy) if afternoon_buy is not None else float(flat_buy)
            flex_pct = float(w.get("flex_pct") or cfg.get("flex_pct") or 0.008)
            _track_dump_and_low(
                st,
                px=px,
                open_px=open_px,
                high=high,
                low=low,
                buy=buy,
                code=code,
            )
            st["saw_deep"] = True  # 低开本身算深砸一侧
            am_key = _alert_key(code, "gap_down_am")
            if _in_window("09:32", "09:50") and _once_ok(state, am_key):
                _push(
                    cfg,
                    f"【低开观察】{name}",
                    f"{name} 低开约 {open_chg:+.1f}%\n"
                    f"上午先不追；破 {invalidate} 放弃。\n"
                    f"若真V修复并回到挂价 ≤ {buy:.2f} 才会推「V反转可挂」\n"
                    f"（半空微弹不到挂价不推）\n"
                    f"现价 {px:.2f}\n{note}\n"
                    f"{now.strftime('%H:%M:%S')}",
                )
                _mark(state, am_key)

            # 低开日全天可识别真 V，不必干等到下午
            v_act, v_why, v_sug = _judge_v_reversal(
                code=code,
                px=px,
                buy=buy,
                open_px=open_px,
                high=high,
                low=low,
                avg=avg,
                pre=pre,
                invalidate=invalidate,
                st=st,
                flex_pct=flex_pct,
            )
            _emit_zone_alert(
                cfg=cfg,
                state=state,
                st=st,
                code=code,
                name=name,
                action=v_act,
                why=v_why,
                suggest=v_sug,
                px=px,
                buy=buy,
                high=high,
                low=low,
                invalidate=invalidate,
                note=note,
                now=now,
                cooldown=cooldown,
                key_prefix="v",
                tag="低开·",
            )
            if v_act in ("v_hang", "v_buy"):
                st["afternoon_armed"] = True
                st["mode"] = "afternoon_buy"

            aft = datetime.strptime(afternoon_start, "%H:%M").time()
            if now.time() >= aft and not st.get("afternoon_armed"):
                morning_low = _to_float(st.get("morning_low")) or low
                if low is not None:
                    st["morning_low"] = min(
                        morning_low if morning_low is not None else low, low
                    )
                    morning_low = st["morning_low"]
                rebound = (
                    px >= open_px * 0.998
                    or (
                        morning_low is not None
                        and px >= float(morning_low) * 1.008
                        and px > open_px * 0.99
                    )
                )
                if rebound:
                    st["afternoon_armed"] = True
                    st["mode"] = "afternoon_buy"
                    key = _alert_key(code, "afternoon_arm")
                    if _once_ok(state, key):
                        _push(
                            cfg,
                            f"【午后回升】{name} 给保守买价",
                            f"{name} 低开后午后有回升迹象\n"
                            f"可挂 ≤ {buy:.2f}（破 {invalidate} 放弃）\n"
                            f"现价 {px:.2f} 开盘 {open_px:.2f} 今低 {low}\n"
                            f"{note}\n"
                            f"{now.strftime('%H:%M:%S')}",
                        )
                        _mark(state, key)
                else:
                    print(f"[gap_down wait] {name} px={px} no rebound yet", flush=True)
            else:
                if low is not None:
                    ml = _to_float(st.get("morning_low"))
                    st["morning_low"] = low if ml is None else min(ml, low)
                print(f"[gap_down am] {name} px={px} v={v_act}", flush=True)
            continue

        if mode == "afternoon_buy":
            buy = float(afternoon_buy)
            flex_pct = float(w.get("flex_pct") or cfg.get("flex_pct") or 0.008)
            _track_dump_and_low(
                st,
                px=px,
                open_px=open_px,
                high=high,
                low=low,
                buy=buy,
                code=code,
            )
            v_act, v_why, v_sug = _judge_v_reversal(
                code=code,
                px=px,
                buy=buy,
                open_px=open_px,
                high=high,
                low=low,
                avg=avg,
                pre=pre,
                invalidate=invalidate,
                st=st,
                flex_pct=flex_pct,
            )
            _emit_zone_alert(
                cfg=cfg,
                state=state,
                st=st,
                code=code,
                name=name,
                action=v_act,
                why=v_why,
                suggest=v_sug,
                px=px,
                buy=buy,
                high=high,
                low=low,
                invalidate=invalidate,
                note=note,
                now=now,
                cooldown=cooldown,
                key_prefix="v",
                tag="午后·",
            )
            action, why, suggest = _judge_buy_zone(
                code=code,
                px=px,
                buy=buy,
                open_px=open_px,
                high=high,
                low=low,
                avg=avg,
                pre=pre,
                invalidate=invalidate,
                st=st,
                flex_pct=flex_pct,
            )
            _emit_zone_alert(
                cfg=cfg,
                state=state,
                st=st,
                code=code,
                name=name,
                action=action,
                why=why,
                suggest=suggest,
                px=px,
                buy=buy,
                high=high,
                low=low,
                invalidate=invalidate,
                note=note,
                now=now,
                cooldown=cooldown,
                key_prefix="aft_zone",
                tag="午后档·",
            )
            print(
                f"[afternoon] {name} px={px} buy≤{buy} v={v_act} act={action}",
                flush=True,
            )
            continue

    state.setdefault("days", {})[_day()] = day_st
    _save_json(STATE_PATH, state)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()

    if not CFG_LOCAL.exists():
        print(f"缺少配置 {CFG_LOCAL}", flush=True)
        return 1
    cfg = _load_json(CFG_LOCAL)
    state = _load_json(STATE_PATH)

    if args.test:
        names = "、".join(
            str(w.get("name") or w.get("code") or "")
            for w in (cfg.get("scenario_watches") or [])
        ) or "场景盯盘"
        _push(
            cfg,
            f"【场景盯盘测试】{names}",
            "分情形脚本测试推送成功。\n"
            "高开高走→错过；平开→保守限价；低开→午后再说。",
        )
        return 0

    if not args.once:
        if not _acquire_lock():
            print("[exit] another scenario watch already running", flush=True)
            return 0

    try:
        _today = _day()
        _all_w = cfg.get("scenario_watches") or []
        _today_w = [w for w in _all_w if str(w.get("date") or "") in ("", _today)]
        _stale_n = len(_all_w) - len(_today_w)
        watch_names = "、".join(
            str(w.get("name") or w.get("code") or "") for w in _today_w
        ) or "无当日任务"
        # 启动告知（无当日任务且无金属监控则不推，避免空名单骚扰）
        _mw = cfg.get("metal_watch") or {}
        if cfg.get("startup_ping", True) and (_today_w or _mw.get("enabled")):
            key = f"startup:{_day()}:scenario"
            if _once_ok(state, key):
                try:
                    lines = []
                    for w in _today_w:
                        if str(w.get("side") or "buy") == "sell":
                            lines.append(
                                f"- {w.get('name')}(卖) 到{w.get('sell_at')}卖 "
                                f"破{w.get('stop_below')}割"
                                + (f"\n  {w.get('note')}" if w.get("note") else "")
                            )
                        else:
                            lines.append(
                                f"- {w.get('name')} 平开≤{w.get('flat_buy_below')} "
                                f"午后≤{w.get('afternoon_buy_below')} "
                                f"破{w.get('invalidate_below')}放弃"
                                + (f"\n  {w.get('note')}" if w.get("note") else "")
                            )
                    if _stale_n:
                        lines.append(f"（另有 {_stale_n} 条非当日任务，不推）")
                    metal_seg = ""
                    if _mw.get("enabled"):
                        ms = _fetch_metals(cfg)
                        if ms:
                            metal_seg = "\n金银联动：" + "；".join(
                                f"{m['name']} {m['price']} ({m['pct']:+.2f}%)"
                                if m.get("pct") is not None
                                else f"{m['name']} {m['price']}"
                                for m in ms
                            ) + f"\n日内≤-{float(_mw.get('warn_drop_pct') or 1.0)}%提醒 / ≤-{float(_mw.get('danger_drop_pct') or 2.0)}%警戒"
                    body_head = (
                        "节奏：9:00计划价 → 9:20竞价先挂判断 → 9:30-9:45回踩观察\n"
                        "高开高走→错过不追；放量下杀/放弃→当日该票只提醒1次\n"
                        "深回踩票(生益/高澜)不到价不催挂\n\n"
                        + "\n".join(lines)
                        if lines
                        else "今日无个股盯盘\n"
                    )
                    _push(
                        cfg,
                        f"【场景盯盘已启动】{watch_names}",
                        body_head + metal_seg,
                    )
                    _mark(state, key)
                except Exception as exc:
                    print(f"[startup push fail] {exc}", flush=True)

        if args.once:
            run_once(cfg, state)
            return 0

        poll = int(cfg.get("poll_sec") or 60)
        print(f"scenario watch loop poll={poll}s", flush=True)
        while True:
            try:
                if datetime.now().weekday() < 5 and dtime(
                    9, 20
                ) <= datetime.now().time() <= dtime(15, 5):
                    run_once(cfg, _load_json(STATE_PATH))
                else:
                    print(f"[idle] {datetime.now().strftime('%H:%M:%S')}", flush=True)
                    # 收盘后退出，避免空挂到晚上
                    if datetime.now().time() >= dtime(15, 10):
                        print("[exit] session over", flush=True)
                        return 0
            except Exception as exc:
                print(f"[err] {exc}", flush=True)
            time.sleep(poll)
    finally:
        if not args.once:
            _release_lock()


if __name__ == "__main__":
    raise SystemExit(main())
