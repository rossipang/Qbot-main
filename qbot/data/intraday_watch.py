# -*- coding: utf-8 -*-
"""今日盯盘：前瞻选股后放入小名单，盘中只刷新报价/买点（不重跑主题发现）。"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from qbot.data.forward_watch import (
    _day_kline_structure,
    _detect_buy_setup,
    _detect_t1_short_setup,
    _get_risk_bars,
    _near_price_buy_band,
    _resolve_buy_method,
    _suggest_buy_plan,
    _to_float,
    _today,
)
from qbot.data.forward_timing import format_hold_cell, hold_exit_hint
from qbot.data.industry_screener import _fetch_ulist_quote_map

WATCH_PATH = (
    Path(__file__).resolve().parents[1] / "gui" / "csv" / "intraday_watch.json"
)
MAX_WATCH = 15

_FILE_LOCK = threading.RLock()
_BOARD_CACHE: Dict[str, Any] = {"ts": 0.0, "map": {}}
_BOARD_TTL_SEC = 180.0  # 板块表缓存 3 分钟，避免拖慢持仓刷新


def _norm_code(code: str) -> str:
    c = str(code or "").strip().zfill(6)[-6:]
    return c if c.isdigit() and len(c) == 6 else ""


def load_intraday_watch() -> Dict[str, Any]:
    empty = {
        "items": [],
        "last_rows": [],
        "holdings": [],
        "holdings_last_rows": [],
        "updated_at": "",
    }
    with _FILE_LOCK:
        if not WATCH_PATH.exists():
            return dict(empty)
        try:
            data = json.loads(WATCH_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("items", [])
                data.setdefault("last_rows", [])
                data.setdefault("holdings", [])
                data.setdefault("holdings_last_rows", [])
                data.setdefault("updated_at", "")
                return data
            if isinstance(data, list):
                out = dict(empty)
                out["items"] = data
                return out
        except Exception:
            pass
        return dict(empty)


def save_intraday_watch(payload: Dict[str, Any]) -> Path:
    WATCH_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _FILE_LOCK:
        WATCH_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return WATCH_PATH


def _read_watch_file_unlocked() -> Dict[str, Any]:
    empty = {
        "items": [],
        "last_rows": [],
        "holdings": [],
        "holdings_last_rows": [],
        "updated_at": "",
    }
    if not WATCH_PATH.exists():
        return dict(empty)
    try:
        data = json.loads(WATCH_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    data.setdefault("items", [])
    data.setdefault("last_rows", [])
    data.setdefault("holdings", [])
    data.setdefault("holdings_last_rows", [])
    data.setdefault("updated_at", "")
    return data


def _write_watch_file_unlocked(data: Dict[str, Any]) -> None:
    WATCH_PATH.parent.mkdir(parents=True, exist_ok=True)
    WATCH_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _patch_watch_file(**fields: Any) -> Dict[str, Any]:
    """并发安全地只更新部分字段，避免观察池/持仓互相覆盖。"""
    with _FILE_LOCK:
        data = _read_watch_file_unlocked()
        data.update(fields)
        _write_watch_file_unlocked(data)
        return data


def _commit_holdings_refresh(
    state_updates: Dict[str, Dict[str, Any]],
    h_rows: List[Dict[str, Any]],
    now: str,
) -> Dict[str, Any]:
    """写回持仓刷新结果：以磁盘上最新 holdings 为准，不把已删股票写回来。"""
    with _FILE_LOCK:
        data = _read_watch_file_unlocked()
        holdings = list(data.get("holdings") or [])
        live_codes = {_norm_code(it.get("code")) for it in holdings}
        live_codes.discard("")
        for it in holdings:
            code = _norm_code(it.get("code"))
            upd = state_updates.get(code)
            if upd:
                it.update(upd)
        rows = [
            r
            for r in h_rows
            if _norm_code(r.get("代码")) in live_codes
        ]
        data["holdings"] = holdings
        data["holdings_last_rows"] = rows
        data["holdings_updated_at"] = now
        data["updated_at"] = now
        _write_watch_file_unlocked(data)
    return {
        "holdings": holdings,
        "holdings_rows": rows,
        "updated_at": now,
    }


def list_intraday_items() -> List[Dict[str, Any]]:
    return list(load_intraday_watch().get("items") or [])


def add_to_intraday_watch(
    code: str,
    name: str = "",
    theme: str = "",
) -> Tuple[List[Dict[str, Any]], str]:
    """加入盯盘。返回 (items, err)。满员或重复时 err 非空。"""
    code = _norm_code(code)
    if not code:
        return list_intraday_items(), "代码无效"
    data = load_intraday_watch()
    items = list(data.get("items") or [])
    if any(str(it.get("code")) == code for it in items):
        return items, ""
    if len(items) >= MAX_WATCH:
        return items, f"今日盯盘最多 {MAX_WATCH} 只，请先删一只"
    items.append(
        {
            "code": code,
            "name": name or code,
            "theme": theme or "",
            "added_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    data["items"] = items
    save_intraday_watch(data)
    return items, ""


def remove_from_intraday_watch(code: str) -> List[Dict[str, Any]]:
    code = _norm_code(code)
    data = load_intraday_watch()
    items = [it for it in (data.get("items") or []) if str(it.get("code")) != code]
    data["items"] = items
    rows = [r for r in (data.get("last_rows") or []) if str(r.get("代码")) != code]
    data["last_rows"] = rows
    save_intraday_watch(data)
    return items


def is_cn_session(now: Optional[datetime] = None) -> bool:
    """A股连续竞价附近（含午休前后一点缓冲）。"""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 100 + now.minute
    return (915 <= hm <= 1135) or (1255 <= hm <= 1510)


def _stock_grade(pct: Optional[float], flow: Optional[float]) -> str:
    if pct is not None and float(pct) <= -2.0:
        return "偏弱"
    if pct is not None and float(pct) >= 1.0 and (flow or 0) > 0:
        return "走强"
    return "偏好"


def _score_one(item: Dict[str, Any], quote: Dict[str, Any]) -> Dict[str, Any]:
    code = str(item.get("code") or "")
    name = str(quote.get("名称") or item.get("name") or code)
    px = _to_float(quote.get("最新价"))
    pct = _to_float(quote.get("涨跌幅"))
    pct5 = _to_float(quote.get("涨跌幅_5日"))
    flow = _to_float(quote.get("主力净流入_亿"))
    flow5 = _to_float(quote.get("主力净流入_5日_亿"))
    vr = _to_float(quote.get("量比"))
    open_ = _to_float(quote.get("开盘"))
    high = _to_float(quote.get("最高"))
    low = _to_float(quote.get("最低"))
    prev = _to_float(quote.get("昨收"))
    close = px
    grade = _stock_grade(pct, flow)
    ks = _day_kline_structure(
        open_=open_,
        high=high,
        low=low,
        close=close,
        prev_close=prev,
        vol_ratio=vr,
    )
    # 盯盘已由人选定主题，盘中只判买点：给弱确认以免「无新闻且板块不热」整票否掉
    buy_setup = _detect_buy_setup(
        theme_ok=True,
        theme_grade="偏好",
        stock_grade=grade,
        stock_pct=pct,
        stock_pct_5d=pct5,
        stock_flow=flow,
        stock_flow_5d=flow5,
        vol_ratio=vr,
        open_=open_,
        high=high,
        low=low,
        close=close,
        prev_close=prev,
        bar_struct=ks,
    )
    t1 = _detect_t1_short_setup(
        news_hits=1,
        board_pct=1.2,
        pct=pct,
        pct5=pct5,
        flow=flow,
        flow5=flow5,
        vol_ratio=vr,
        open_=open_,
        high=high,
        low=low,
        close=close,
        prev_close=prev,
        bar_struct=ks,
    )
    method_setup = t1 if t1.get("buy_ok") else buy_setup
    buy_ok = bool(method_setup.get("buy_ok"))
    method = _resolve_buy_method(method_setup, pct=pct, news_hits=1) if buy_ok else ""
    band, action = _suggest_buy_plan(
        px,
        pct,
        pct5,
        stock_flow=flow,
        theme_grade="偏好",
        stock_grade=grade,
        stars=3 if buy_ok else 2,
        buy_setup=method_setup,
    )
    if buy_ok and px:
        band = _near_price_buy_band(px)
    sig = "红" if buy_ok else ("橙" if str(method_setup.get("kind") or "") not in ("", "none") else "黄")
    if method_setup.get("kind") in ("reject_bar", "fake_pullback"):
        sig = "绿"
    why = str(method_setup.get("why") or method_setup.get("label") or "")
    kline = str(ks.get("why") or "")
    return {
        "代码": code,
        "名称": name,
        "主题": str(item.get("theme") or ""),
        "最新价": px,
        "涨跌幅%": pct,
        "5日涨跌%": pct5,
        "开盘": open_,
        "最高": high,
        "最低": low,
        "昨收": prev,
        "主力净流入亿": flow,
        "量比": vr,
        "K线": kline,
        "买点": str(method_setup.get("label") or "无明确买点"),
        "买点形态": str(method_setup.get("kind") or "none"),
        "买入候选": "是" if buy_ok else "否",
        "买入方法": method,
        "建议买入": band,
        "操作建议": action,
        "信号": sig,
        "依据": why or action,
        "加入时间": str(item.get("added_at") or ""),
    }


def refresh_watch_pool() -> Dict[str, Any]:
    """只刷新观察池买点（轻量，不拉全市场板块表）。"""
    data = load_intraday_watch()
    items = list(data.get("items") or [])
    codes = [_norm_code(it.get("code")) for it in items]
    codes = [c for c in codes if c]
    qmap = _fetch_ulist_quote_map(codes) if codes else {}
    rows: List[Dict[str, Any]] = []
    for it in items:
        code = _norm_code(it.get("code"))
        q = qmap.get(code) or {}
        if q.get("名称") and not it.get("name"):
            it["name"] = q.get("名称")
        rows.append(_score_one(it, q))
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    _patch_watch_file(items=items, last_rows=rows, watch_updated_at=now, updated_at=now)
    return {"items": items, "rows": rows, "updated_at": now}


def refresh_holdings() -> Dict[str, Any]:
    """只刷新持仓卖点；板块表带缓存，行业只在缺失时补。"""
    data = load_intraday_watch()
    holdings = list(data.get("holdings") or [])
    codes = [_norm_code(it.get("code")) for it in holdings]
    codes = [c for c in codes if c]
    codes += ["399006", "000001"]
    codes = list(dict.fromkeys(codes))
    qmap = _fetch_ulist_quote_map(codes) if codes else {}
    board_map = _load_board_pct_map()
    market = _market_context(qmap)
    h_rows: List[Dict[str, Any]] = []
    state_updates: Dict[str, Dict[str, Any]] = {}
    today = time.strftime("%Y-%m-%d")
    for it in holdings:
        code = _norm_code(it.get("code"))
        q = qmap.get(code) or {}
        if q.get("名称"):
            it["name"] = q.get("名称")
        # 已有行业则不重复打接口，避免拖慢
        if not str(it.get("industry") or "").strip():
            industry = _fetch_stock_industry(code)
            if industry:
                it["industry"] = industry
        row = _score_holding(it, q, board_map=board_map, market=market)
        it["phase"] = row.get("走势阶段") or it.get("phase") or "持有"
        it["confirm"] = int(row.get("_confirm") or 0)
        it["day_high"] = row.get("_day_high")
        it["max_pnl_pct"] = row.get("_max_pnl_pct")
        it["session_date"] = today
        state_updates[code] = {
            "name": it.get("name"),
            "industry": it.get("industry"),
            "phase": it.get("phase"),
            "confirm": it.get("confirm"),
            "day_high": it.get("day_high"),
            "max_pnl_pct": it.get("max_pnl_pct"),
            "session_date": it.get("session_date"),
        }
        show = {k: v for k, v in row.items() if not str(k).startswith("_")}
        h_rows.append(show)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    return _commit_holdings_refresh(state_updates, h_rows, now)


def refresh_intraday_watch() -> Dict[str, Any]:
    """兼容旧调用：顺序刷两边（GUI 已改为并行分别调用）。"""
    w = refresh_watch_pool()
    h = refresh_holdings()
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    return {
        "items": w.get("items") or [],
        "rows": w.get("rows") or [],
        "holdings": h.get("holdings") or [],
        "holdings_rows": h.get("holdings_rows") or [],
        "updated_at": now,
    }


def alert_new_buy_ok(
    old_rows: List[Dict[str, Any]],
    new_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """从否→是的票，用于弹一次提醒。"""
    old_ok = {
        str(r.get("代码")): str(r.get("买入候选") or "") == "是"
        for r in (old_rows or [])
    }
    fired: List[Dict[str, Any]] = []
    for r in new_rows or []:
        code = str(r.get("代码") or "")
        if str(r.get("买入候选") or "") != "是":
            continue
        if old_ok.get(code):
            continue
        fired.append(r)
    return fired


# ---------- 持仓盯盘：卖出 ----------
# A 涨不动回吐（主） / B 昨强今低开下杀 / C 破成本（底线）

MAX_HOLDINGS = 10
# C 破成本硬止损
_HARD_STOP_PCT = -2.5
_PULL_HARD_STOP = -3.5
# A 回吐「上涨段」的比例（不是股价再跌20%）
_GIVEBACK_WATCH = 0.20
_GIVEBACK_SELL = 0.40
# B 盛美型：昨涨透 + 今低开
_YEST_EXTEND_PCT = 4.0
_GAP_DOWN_OPEN_PCT = -1.0
# 旧：放量/缩量辅助（次要）
_DUMP_WATCH_DD = 2.0
_DUMP_SELL_DD = 3.0
_DUMP_VR = 1.25
_PULL_WATCH_DD = 4.0
_PULL_SELL_DD = 5.5
_CONFIRM_NEED = 2


def list_holdings() -> List[Dict[str, Any]]:
    return list(load_intraday_watch().get("holdings") or [])


def resolve_stock_query(query: str) -> Tuple[str, str, str]:
    """名称或代码 → (code, name, err)。"""
    q = str(query or "").strip()
    if not q:
        return "", "", "请填写股票名称或代码"
    digits = "".join(ch for ch in q if ch.isdigit())
    if len(digits) == 6:
        code = _norm_code(digits)
        qmap = _fetch_ulist_quote_map([code])
        name = str((qmap.get(code) or {}).get("名称") or "")
        return code, name or code, ""
    # 东财联想搜索
    try:
        import requests

        r = requests.get(
            "https://searchapi.eastmoney.com/api/suggest/get",
            params={
                "input": q,
                "type": "14",
                "token": "D43XXQ4CAVNGVRBEO",
            },
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        payload = r.json() or {}
        data = ((payload.get("QuotationCodeTable") or {}).get("Data")) or []
        for it in data:
            if str(it.get("Classify") or "") not in ("AStock", "AKStock", ""):
                # 仍允许 A 股
                pass
            code = _norm_code(it.get("Code") or it.get("UnifiedCode"))
            name = str(it.get("Name") or "")
            if code and name:
                # 优先名称完全匹配 / 包含
                if name == q or q in name or name in q:
                    return code, name, ""
        if data:
            it = data[0]
            code = _norm_code(it.get("Code") or it.get("UnifiedCode"))
            name = str(it.get("Name") or "")
            if code:
                return code, name or code, ""
    except Exception as exc:  # noqa: BLE001
        return "", "", f"搜索失败: {exc}"
    return "", "", f"未找到：{q}"


def add_holding(
    query: str,
    cost: float,
    theme: str = "",
) -> Tuple[List[Dict[str, Any]], str]:
    """加入持仓。名称或代码均可；成本价必填。"""
    try:
        cost_f = float(cost)
    except (TypeError, ValueError):
        return list_holdings(), "成本价无效"
    if cost_f <= 0:
        return list_holdings(), "成本价必须大于 0"
    code, name, err = resolve_stock_query(query)
    if err:
        return list_holdings(), err
    data = load_intraday_watch()
    holdings = list(data.get("holdings") or [])
    if any(str(it.get("code")) == code for it in holdings):
        return holdings, "已在持仓中"
    if len(holdings) >= MAX_HOLDINGS:
        return holdings, f"持仓最多 {MAX_HOLDINGS} 只"
    holdings.append(
        {
            "code": code,
            "name": name or code,
            "cost": round(cost_f, 4),
            "theme": str(theme or "").strip(),
            "industry": "",
            "added_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "phase": "持有",
            "confirm": 0,
            "day_high": None,
            "max_pnl_pct": None,
            "session_date": time.strftime("%Y-%m-%d"),
        }
    )
    data["holdings"] = holdings
    save_intraday_watch(data)
    return holdings, ""


def mark_holding_sold(code: str) -> List[Dict[str, Any]]:
    """已卖出：从持仓与展示行一并删除（刷新不会再写回）。"""
    code = _norm_code(code)
    if not code:
        return list_holdings()
    with _FILE_LOCK:
        data = _read_watch_file_unlocked()
        holdings = [
            it
            for it in (data.get("holdings") or [])
            if _norm_code(it.get("code")) != code
        ]
        rows = [
            r
            for r in (data.get("holdings_last_rows") or [])
            if _norm_code(r.get("代码")) != code
        ]
        data["holdings"] = holdings
        data["holdings_last_rows"] = rows
        _write_watch_file_unlocked(data)
    return holdings


def _fetch_stock_industry(code: str) -> str:
    code = _norm_code(code)
    if not code:
        return ""
    try:
        import requests

        secid = f"1.{code}" if code.startswith(("5", "6", "9")) else f"0.{code}"
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
        for host in (
            "https://push2delay.eastmoney.com/api/qt/stock/get",
            "https://push2.eastmoney.com/api/qt/stock/get",
        ):
            try:
                r = requests.get(
                    host,
                    params={"secid": secid, "fields": "f127,f58"},
                    timeout=8,
                    headers=headers,
                )
                d = (r.json() or {}).get("data") or {}
                name = str(d.get("f127") or "").strip()
                if name:
                    return name
            except Exception:
                continue
    except Exception:
        return ""
    return ""


def _load_board_pct_map(force: bool = False) -> Dict[str, Dict[str, Any]]:
    """板块名 → {pct, flow}。带短缓存，避免每次持仓刷新都全量拉板块。"""
    now = time.time()
    cached = _BOARD_CACHE.get("map") or {}
    if (
        not force
        and cached
        and (now - float(_BOARD_CACHE.get("ts") or 0)) < _BOARD_TTL_SEC
    ):
        return cached
    out: Dict[str, Dict[str, Any]] = {}
    try:
        from qbot.data.industry_screener import fetch_industry_boards

        df = fetch_industry_boards()
        if df is None or getattr(df, "empty", True):
            _BOARD_CACHE["ts"] = now
            _BOARD_CACHE["map"] = out
            return out
        for _, r in df.iterrows():
            name = str(r.get("板块名称") or "").strip()
            if not name:
                continue
            out[name] = {
                "pct": _to_float(r.get("涨跌幅")),
                "flow": _to_float(r.get("主力净流入_亿")),
            }
    except Exception:
        # 失败时尽量沿用旧缓存
        if cached:
            return cached
        return out
    _BOARD_CACHE["ts"] = now
    _BOARD_CACHE["map"] = out
    return out


_THEME_BOARD_ALIAS = {
    "交换机": "通信设备",
    "锐捷": "通信设备",
    "光迅": "通信设备",
    "光模块": "光通信模块",
    "CPO": "CPO概念",
    "CPO概念": "CPO概念",
    "MLCC": "MLCC",
    "被动": "被动元件概念",
    "三环": "MLCC",
    "PCB": "印制电路板",
    "算电": "液冷服务器",
    "服务器": "液冷服务器",
}


def _match_board(
    industry: str,
    theme: str,
    board_map: Dict[str, Dict[str, Any]],
) -> Tuple[str, Optional[float], Optional[float]]:
    """优先主题，再行业，模糊包含匹配；主题可用别名映射到东财板块名。"""
    raw = [str(theme or "").strip(), str(industry or "").strip()]
    keys: List[str] = []
    for k in raw:
        if not k:
            continue
        keys.append(k)
        alias = _THEME_BOARD_ALIAS.get(k)
        if alias:
            keys.append(alias)
        for ak, av in _THEME_BOARD_ALIAS.items():
            if ak in k or k in ak:
                keys.append(av)
    # 去重保序
    seen = set()
    keys = [x for x in keys if not (x in seen or seen.add(x))]
    for k in keys:
        if k in board_map:
            b = board_map[k]
            return k, b.get("pct"), b.get("flow")
    for k in keys:
        for bn, b in board_map.items():
            if k in bn or bn in k:
                return bn, b.get("pct"), b.get("flow")
    return "", None, None


def _market_context(qmap: Dict[str, Any]) -> Dict[str, Any]:
    cyb = qmap.get("399006") or {}
    sh = qmap.get("000001") or {}
    cyb_pct = _to_float(cyb.get("涨跌幅"))
    sh_pct = _to_float(sh.get("涨跌幅"))
    # 弱/中/强
    refs = [x for x in (cyb_pct, sh_pct) if x is not None]
    avg = sum(refs) / len(refs) if refs else 0.0
    if avg <= -1.0:
        grade = "弱"
    elif avg >= 0.8:
        grade = "强"
    else:
        grade = "中"
    return {"cyb_pct": cyb_pct, "sh_pct": sh_pct, "grade": grade, "avg": avg}


def _board_grade(board_pct: Optional[float], board_flow: Optional[float]) -> str:
    if board_pct is None and board_flow is None:
        return "中"
    p = float(board_pct or 0.0)
    f = float(board_flow or 0.0)
    if p <= -1.0 or (p < 0 and f < 0):
        return "弱"
    if p >= 1.0 and f >= 0:
        return "强"
    if p >= 0.5:
        return "强"
    return "中"


def _prev_day_stats(code: str) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """昨涨跌%、昨收、昨开。今日K若已出则取倒数第二根。"""
    code = _norm_code(code)
    if not code:
        return None, None, None
    try:
        from qbot.data.industry_screener import _fetch_kline_bars

        end = time.strftime("%Y%m%d")
        bars = _fetch_kline_bars(code, end, limit=5) or []
        if not bars:
            return None, None, None
        today = end
        if str(bars[-1].get("date") or "") == today and len(bars) >= 2:
            y = bars[-2]
        else:
            y = bars[-1]
        return (
            _to_float(y.get("pct")),
            _to_float(y.get("close")),
            _to_float(y.get("open")),
        )
    except Exception:
        return None, None, None


def _ensure_yest_stats(item: Dict[str, Any], quote: Dict[str, Any]) -> None:
    """写入持仓的昨涨信息（按自然日缓存，避免每次刷都打K线）。"""
    today = time.strftime("%Y-%m-%d")
    if str(item.get("yest_cache_date") or "") == today and item.get("yest_pct") is not None:
        return
    code = _norm_code(item.get("code"))
    ypct, yclose, yopen = _prev_day_stats(code)
    # 昨收兜底用行情昨收
    if yclose is None:
        yclose = _to_float(quote.get("昨收"))
    item["yest_pct"] = ypct
    item["yest_close"] = yclose
    item["yest_open"] = yopen
    item["yest_cache_date"] = today


def _classify_move(
    px: Optional[float],
    open_: Optional[float],
    high: Optional[float],
    low: Optional[float],
    pct: Optional[float],
    vr: Optional[float],
    flow: Optional[float],
) -> Tuple[str, float]:
    """
    区分上升中 / 缩量回踩 / 放量下跌 / 高位滞涨。
    返回 (类型, 距今日高点回撤%)。
    """
    if not px or not high or high <= 0:
        return "不明", 0.0
    dd = (float(high) - float(px)) / float(high) * 100.0
    vr_v = float(vr) if vr is not None else 1.0
    pct_v = float(pct) if pct is not None else 0.0
    flow_v = float(flow) if flow is not None else 0.0
    mid = None
    if high is not None and low is not None:
        mid = (float(high) + float(low)) / 2.0

    if dd < 1.2 and pct_v >= -0.3:
        return "上升中", dd
    below_open = open_ is not None and float(px) < float(open_)
    near_low = (
        low is not None
        and high is not None
        and float(high) > float(low)
        and (float(px) - float(low)) / (float(high) - float(low)) <= 0.35
    )
    dump = vr_v >= _DUMP_VR and dd >= _DUMP_WATCH_DD and (
        pct_v < 0 or below_open or flow_v < 0 or near_low
    )
    if dump or (vr_v >= 1.5 and dd >= 1.8 and pct_v < 0):
        return "放量下跌", dd
    if dd >= 1.5 and vr_v <= 1.05 and (pct_v >= -1.0 or (mid and px >= mid * 0.995)):
        return "缩量回踩", dd
    if dd >= _PULL_WATCH_DD and vr_v < 1.15:
        return "缩量回踩", dd
    if dd >= 2.5:
        return "高位滞涨", dd
    if dd >= 1.0 and vr_v <= 1.1:
        return "缩量回踩", dd
    return "上升中", dd


def _score_holding(
    item: Dict[str, Any],
    quote: Dict[str, Any],
    board_map: Optional[Dict[str, Dict[str, Any]]] = None,
    market: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    持仓卖出评分（简单三层）：
    A 涨不动回吐20%/40% → 黄/红
    B 昨涨透+今低开下杀 → 黄/红
    C 破成本 → 红
    """
    code = _norm_code(item.get("code"))
    name = str(quote.get("名称") or item.get("name") or code)
    cost = _to_float(item.get("cost"))
    px = _to_float(quote.get("最新价"))
    pct = _to_float(quote.get("涨跌幅"))
    flow = _to_float(quote.get("主力净流入_亿"))
    vr = _to_float(quote.get("量比"))
    open_ = _to_float(quote.get("开盘"))
    high = _to_float(quote.get("最高"))
    low = _to_float(quote.get("最低"))
    prev = _to_float(quote.get("昨收"))
    board_map = board_map or {}
    market = market or {"grade": "中", "avg": 0.0}

    _ensure_yest_stats(item, quote)
    yest_pct = _to_float(item.get("yest_pct"))
    yest_close = _to_float(item.get("yest_close")) or prev
    yest_open = _to_float(item.get("yest_open"))

    pnl = None
    if px is not None and cost and cost > 0:
        pnl = (float(px) / float(cost) - 1.0) * 100.0

    today = time.strftime("%Y-%m-%d")
    if str(item.get("session_date") or "") != today:
        day_high = high
        max_pnl = pnl
    else:
        day_high = _to_float(item.get("day_high"))
        if high is not None:
            day_high = max([x for x in (day_high, high) if x is not None], default=high)
        max_pnl = _to_float(item.get("max_pnl_pct"))
        if pnl is not None:
            max_pnl = max([x for x in (max_pnl, pnl) if x is not None], default=pnl)

    track_high = day_high if day_high is not None else high
    move_kind, dd = _classify_move(px, open_, track_high, low, pct, vr, flow)

    industry = str(item.get("industry") or "")
    theme = str(item.get("theme") or "")
    bname, bpct, bflow = _match_board(industry, theme, board_map)
    bgrade = _board_grade(bpct, bflow)
    mgrade = str(market.get("grade") or "中")

    # —— A：当日冲高回吐（相对「今涨段」）——
    # 涨段 = 今高 - max(开盘, 昨收)；回吐比例 = (今高-现价)/涨段
    base = None
    if open_ is not None and prev is not None:
        base = max(float(open_), float(prev))
    elif open_ is not None:
        base = float(open_)
    elif prev is not None:
        base = float(prev)
    day_rise = 0.0
    day_giveback_r = 0.0
    if track_high is not None and base is not None and px is not None:
        day_rise = max(0.0, float(track_high) - float(base))
        # 今涨段太小（相对股价 <1.5%）不算「冲高」，避免毛刺误判
        if base > 0 and day_rise / float(base) >= 0.015 and day_rise > 1e-6:
            day_giveback_r = max(0.0, (float(track_high) - float(px)) / day_rise)
        else:
            day_rise = 0.0

    # —— A2：隔日回吐昨涨（相对昨涨金额）——
    yest_gain_amt = 0.0
    yest_giveback_r = 0.0
    if (
        yest_close is not None
        and yest_pct is not None
        and float(yest_pct) >= 1.5
    ):
        if yest_open is not None and float(yest_close) > float(yest_open):
            yest_gain_amt = float(yest_close) - float(yest_open)
        else:
            yest_gain_amt = float(yest_close) * float(yest_pct) / 100.0
        if px is not None and yest_gain_amt > 1e-6 and float(px) < float(yest_close):
            yest_giveback_r = (float(yest_close) - float(px)) / yest_gain_amt

    # —— B：昨涨透 + 今低开 ——
    open_vs_prev = None
    if open_ is not None and prev is not None and float(prev) > 0:
        open_vs_prev = (float(open_) / float(prev) - 1.0) * 100.0
    gap_risk = bool(
        yest_pct is not None
        and float(yest_pct) >= _YEST_EXTEND_PCT
        and open_vs_prev is not None
        and float(open_vs_prev) <= _GAP_DOWN_OPEN_PCT
    )
    gap_dump = bool(
        gap_risk
        and (
            (pct is not None and float(pct) <= -1.5)
            or (vr is not None and float(vr) >= 1.2 and pct is not None and float(pct) < 0)
            or (px is not None and open_ is not None and float(px) < float(open_))
        )
    )

    ext_notes = []
    if bgrade == "弱":
        ext_notes.append(f"板块弱({bname or industry or '-'})")
    elif bgrade == "强":
        ext_notes.append(f"板块强({bname or '-'})")
    if mgrade == "弱":
        ext_notes.append("大盘弱")
    elif mgrade == "强":
        ext_notes.append("大盘偏强")

    advice = "持有"
    phase = "上升中"
    why_parts: List[str] = []
    if day_rise > 0:
        why_parts.append(f"今涨回吐{day_giveback_r*100:.0f}%")
    if yest_pct is not None:
        why_parts.append(f"昨{yest_pct:+.1f}%")
    if yest_giveback_r > 0:
        why_parts.append(f"昨涨回吐{yest_giveback_r*100:.0f}%")
    if open_vs_prev is not None:
        why_parts.append(f"开盘{open_vs_prev:+.1f}%")
    why_parts.append(move_kind)
    if pnl is not None:
        why_parts.append(f"浮盈{pnl:+.1f}%")
    if vr is not None:
        why_parts.append(f"量比{vr:.2f}")
    why_parts.extend(ext_notes)

    # 新仓 45 分钟：只防硬止损（C）和明确盛美下杀（B）
    fresh_protect = False
    try:
        added = datetime.strptime(
            str(item.get("added_at") or item.get("added_at") or ""),
            "%Y-%m-%d %H:%M:%S",
        )
        fresh_protect = (datetime.now() - added).total_seconds() < 45 * 60
    except Exception:
        fresh_protect = False

    hard = False
    if pnl is not None and pnl <= _HARD_STOP_PCT:
        if move_kind == "缩量回踩" and pnl > _PULL_HARD_STOP:
            hard = False
        else:
            hard = True

    # 优先级：C 硬止损 > B 盛美确认 > A 回吐40% > B 低开预警 > A 回吐20% > 辅助形态
    if hard:
        advice = "卖出"
        phase = "破成本止损"
        why_parts.append("触发成本硬止损")
    elif gap_dump:
        advice = "卖出"
        phase = "昨强今砸"
        why_parts.append("昨涨透今低开下杀(盛美型)")
    elif day_giveback_r >= _GIVEBACK_SELL or yest_giveback_r >= _GIVEBACK_SELL:
        advice = "卖出"
        phase = "涨不动回吐"
        why_parts.append("回吐达约40%，建议兑现")
    elif gap_risk and not fresh_protect:
        advice = "减仓" if (pct is not None and float(pct) < 0) else "观察"
        phase = "低开预警"
        why_parts.append("昨涨透今低开，防下杀")
    elif day_giveback_r >= _GIVEBACK_WATCH or yest_giveback_r >= _GIVEBACK_WATCH:
        advice = "观察"
        phase = "卖区回吐"
        why_parts.append("回吐约20%，进入卖区")
        if bgrade == "弱" or mgrade == "弱" or move_kind == "放量下跌":
            advice = "减仓"
            why_parts.append("外部转弱/放量，建议减")
    elif fresh_protect and not gap_risk:
        advice = "持有"
        phase = "新仓保护"
        why_parts.append("买入未满45分钟，仅防硬止损")
    elif move_kind == "放量下跌":
        phase = "放量转弱"
        if dd >= _DUMP_SELL_DD:
            advice = "减仓"
            why_parts.append("放量离开高点")
        else:
            advice = "观察"
    elif move_kind == "上升中":
        advice = "持有"
        phase = "上升中"
    elif move_kind == "缩量回踩":
        phase = "缩量回踩"
        advice = "持有"
        why_parts.append("缩量回踩，未到20%回吐")
    else:
        phase = move_kind
        advice = "持有"

    # 连续确认：减仓/卖出需累计（硬止损、盛美确认除外）
    prev_confirm = int(item.get("confirm") or 0)
    prev_phase = str(item.get("phase") or "")
    confirm = prev_confirm
    skip_confirm = hard or gap_dump
    if advice in ("减仓", "卖出") and not skip_confirm:
        if prev_phase in (
            "卖区回吐",
            "涨不动回吐",
            "低开预警",
            "昨强今砸",
            "放量转弱",
            "破成本止损",
            phase,
        ):
            confirm = prev_confirm + 1
        else:
            confirm = 1
        if confirm < _CONFIRM_NEED and advice == "卖出":
            advice = "减仓"
            why_parts.append(f"待确认{confirm}/{_CONFIRM_NEED}")
        elif confirm < _CONFIRM_NEED and advice == "减仓":
            advice = "观察"
            why_parts.append(f"待确认{confirm}/{_CONFIRM_NEED}")
    elif advice == "观察":
        confirm = max(prev_confirm, 1)
    else:
        confirm = 0

    # V5 持有出场（相对成本）：只挂在持仓盯盘，有成本才有意义
    hold_cell = ""
    hold_label = ""
    try:
        bars = _get_risk_bars(code, _today(), limit=28, fast_fetch=True)
        hint = hold_exit_hint(bars, cost=cost)
        hold_cell = format_hold_cell(hint)
        hold_label = str(hint.get("label") or "")
        urg = int(hint.get("urgency") or 0)
        # 与盘中卖点合并：更严的一侧生效（新仓保护期内不因「近高」抬卖）
        if not fresh_protect:
            if urg >= 2 and advice in ("持有", "观察"):
                advice = "减仓"
                phase = hold_label or "峰值/双阴减仓"
                why_parts.append(f"V5出场:{hold_label}")
            elif urg == 1 and advice == "持有":
                advice = "观察"
                phase = hold_label or phase
                why_parts.append(f"V5留意:{hold_label}")
            elif hold_cell:
                why_parts.append(f"出场:{hold_cell}")
        elif hold_cell:
            why_parts.append(f"出场参考:{hold_cell}(新仓未满45分)")
    except Exception:
        hold_cell = ""

    sig = {"持有": "绿", "观察": "黄", "减仓": "橙", "卖出": "红"}.get(advice, "黄")
    action_map = {
        "持有": "继续持有：上涨/浅回，勿因毛刺卖",
        "观察": "卖区：涨不动回吐约20%或低开预警，准备减",
        "减仓": "建议减仓：回吐加重/外部转弱/低开走弱",
        "卖出": "建议卖出：回吐深、昨强今砸或破成本",
    }

    return {
        "代码": code,
        "名称": name,
        "成本": cost,
        "最新价": px,
        "浮盈%": None if pnl is None else round(pnl, 2),
        "涨跌幅%": pct,
        "量比": vr,
        "主力净流入亿": flow,
        "行业": industry or "-",
        "主题": theme or "-",
        "板块": bname or industry or "-",
        "板块涨跌%": bpct,
        "走势类型": move_kind,
        "高点回撤%": round(dd, 2),
        "今涨回吐%": round(day_giveback_r * 100, 1) if day_rise > 0 else None,
        "昨涨回吐%": round(yest_giveback_r * 100, 1) if yest_giveback_r > 0 else None,
        "走势阶段": phase,
        "卖出建议": advice,
        "信号": sig,
        "持有出场": hold_cell,
        "操作建议": action_map.get(advice, ""),
        "依据": "；".join(str(x) for x in why_parts if x),
        "大盘": mgrade,
        "加入时间": str(item.get("added_at") or ""),
        "_confirm": confirm,
        "_day_high": day_high,
        "_max_pnl_pct": max_pnl,
    }


def alert_new_sell_ok(
    old_rows: List[Dict[str, Any]],
    new_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """持仓卖出建议升到减仓/卖出时提醒一次。"""
    rank = {"持有": 0, "观察": 1, "减仓": 2, "卖出": 3}
    old_lv = {
        str(r.get("代码")): rank.get(str(r.get("卖出建议") or ""), 0)
        for r in (old_rows or [])
    }
    fired = []
    for r in new_rows or []:
        code = str(r.get("代码") or "")
        lv = rank.get(str(r.get("卖出建议") or ""), 0)
        if lv < 2:
            continue
        if old_lv.get(code, 0) >= lv:
            continue
        fired.append(r)
    return fired


# 兼容别名（部分面板/旧代码可能用另一套命名）
add_holding = add_holding
mark_holding_sold = mark_holding_sold
resolve_stock_query = resolve_stock_query
