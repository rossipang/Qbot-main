"""短线买点/持有时机增强（日K代理 + V5 出场规则）。

不跑 Backtrader，只做可解释的加减分与否决，供 forward_watch 写入风险值/买点/持有建议。
分钟级拥挤暂用当日 OHLC 结构代理（全市场拉分时成本过高）；有 trends 时可叠加。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple


def _f(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        v = float(x)
        if v != v:
            return None
        return v
    except Exception:
        return None


def _bar_ohlc(bar: Any) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    if bar is None:
        return None, None, None, None
    if isinstance(bar, dict):
        return _f(bar.get("open")), _f(bar.get("high")), _f(bar.get("low")), _f(bar.get("close"))
    try:
        return _f(bar[1]), _f(bar[2]), _f(bar[3]), _f(bar[4])
    except Exception:
        return None, None, None, None


def ohlc_intraday_structure(
    open_: Optional[float],
    high: Optional[float],
    low: Optional[float],
    close: Optional[float],
    prev_close: Optional[float] = None,
) -> Dict[str, Any]:
    """用日 K 的开高低收代理「分时结构」：高开直拉 / 低开挖坑再起 / 冲高回落等。"""
    o, h, l, c = _f(open_), _f(high), _f(low), _f(close)
    pc = _f(prev_close)
    out: Dict[str, Any] = {
        "tag": "",
        "label": "",
        "risk_delta": 0.0,
        "veto_chase": False,
        "favors_early_buy": False,
        "notes": [],
    }
    if o is None or h is None or l is None or c is None or h <= l:
        return out

    span = h - l
    open_pos = (o - l) / span  # 0=开在最低，1=开在最高
    close_pos = (c - l) / span
    upper_wick = (h - max(o, c)) / span
    lower_wick = (min(o, c) - l) / span
    pct = ((c / pc) - 1.0) * 100.0 if pc and pc > 0 else None
    gap = ((o / pc) - 1.0) * 100.0 if pc and pc > 0 else None

    # 高开高走 / 开在日内高位且收在高位 → 直线票，隔夜浅挂易全成，禁止当回踩买
    if open_pos >= 0.62 and close_pos >= 0.75 and (pct is None or pct >= 1.2):
        out["tag"] = "rocket"
        out["label"] = "高开直拉/开盘必成带"
        out["risk_delta"] = -18.0
        out["veto_chase"] = True
        out["notes"].append("开盘已在日内偏高位，浅挂易一开就成，属中位接力勿追")
    # 低开或开在低位，收中上 → 更像挖坑/止跌再起可挂深价
    elif open_pos <= 0.28 and close_pos >= 0.55 and upper_wick < 0.35:
        out["tag"] = "dip_reclaim"
        out["label"] = "低位收回/挖坑再起"
        out["risk_delta"] = 8.0
        out["favors_early_buy"] = True
        out["notes"].append("开盘偏弱后收回，利于深挂试错而非贴昨收")
    # 冲高回落：上影长、收在下半
    elif upper_wick >= 0.35 and close_pos <= 0.45:
        out["tag"] = "reject"
        out["label"] = "冲高回落"
        out["risk_delta"] = -12.0
        out["veto_chase"] = True
        out["notes"].append("冲高回落，当日勿追；次日看缩量止跌再议")
    # 高开低走
    elif gap is not None and gap >= 1.0 and c < o and close_pos <= 0.45:
        out["tag"] = "gap_fade"
        out["label"] = "高开低走"
        out["risk_delta"] = -14.0
        out["veto_chase"] = True
        out["notes"].append("高开低走日优先锁浮盈，不宜新开")
    # 缩量小阳形态由外层量比判断；这里只标温和
    elif pct is not None and abs(pct) <= 2.0 and close_pos >= 0.4 and open_pos <= 0.55:
        out["tag"] = "calm"
        out["label"] = "温和震荡"
        out["risk_delta"] = 3.0
        out["notes"].append("日内结构不极端")

    out["open_pos"] = round(open_pos, 3)
    out["close_pos"] = round(close_pos, 3)
    out["upper_wick"] = round(upper_wick, 3)
    out["lower_wick"] = round(lower_wick, 3)
    if gap is not None:
        out["gap_pct"] = round(gap, 2)
    return out


def relative_strength_vs_board(
    stock_pct: Optional[float],
    board_pct: Optional[float],
    *,
    dull_gap: float = 2.2,
) -> Dict[str, Any]:
    """同板块相对强弱：板强个弱 → 钝票降权（防「板内补涨」叙事）。"""
    sp, bp = _f(stock_pct), _f(board_pct)
    out: Dict[str, Any] = {
        "rs": None,
        "label": "",
        "risk_delta": 0.0,
        "is_dull": False,
        "is_leader": False,
        "notes": [],
    }
    if sp is None or bp is None:
        return out
    rs = sp - bp
    out["rs"] = round(rs, 2)
    if bp >= 1.5 and rs <= -dull_gap:
        out["is_dull"] = True
        out["label"] = "板强个弱"
        out["risk_delta"] = -12.0 - min(8.0, abs(rs) - dull_gap)
        out["notes"].append(f"板块{bp:+.1f}%个股{sp:+.1f}%（相对{rs:+.1f}），忌当补涨买")
    elif bp >= 1.0 and rs >= 1.5 and sp >= 2.0:
        out["is_leader"] = True
        out["label"] = "板内领涨"
        # 领涨若已大阳，不鼓励追；只作描述，风险交给连阳/结构
        out["risk_delta"] = -4.0 if sp >= 5.0 else 2.0
        out["notes"].append(f"相对板块偏强({rs:+.1f})，大阳勿追、回踩再议")
    elif abs(rs) < 1.0:
        out["label"] = "跟板"
        out["risk_delta"] = 1.0
    else:
        out["label"] = "偏弱" if rs < 0 else "偏强"
        out["risk_delta"] = -3.0 if rs < -1.5 else 1.5
    return out


def hold_exit_hint(
    bars: Sequence[Any],
    *,
    cost: Optional[float] = None,
    peak_dd_pct: float = 8.0,
    hard_loss_pct: float = 12.0,
    ma_win: int = 20,
) -> Dict[str, Any]:
    """V5 风格持有出场提示（峰值回撤 / 双阴放量 / 破均线 / 硬止损）。

    无成本时用近窗最高价作峰值参考，只给观察级提示，不代替真实持仓成本止损。
    """
    out: Dict[str, Any] = {
        "action": "hold_ok",
        "label": "可继续观察/持有",
        "reasons": [],
        "peak_dd": None,
        "pnl_pct": None,
        "urgency": 0,  # 0观察 1减仓 2清仓倾向
    }
    if not bars or len(bars) < 5:
        out["label"] = "日K未拉到"
        return out

    closes: List[float] = []
    highs: List[float] = []
    vols: List[float] = []
    opens: List[float] = []
    for b in bars:
        o, h, l, c = _bar_ohlc(b)
        if c is None:
            continue
        closes.append(c)
        highs.append(h if h is not None else c)
        opens.append(o if o is not None else c)
        if isinstance(b, dict):
            vols.append(float(b.get("volume") or b.get("vol") or 0) or 0.0)
        else:
            try:
                vols.append(float(b[5] or 0))
            except Exception:
                vols.append(0.0)

    if len(closes) < 5:
        out["label"] = "日K未拉到"
        return out

    last = closes[-1]
    peak = max(highs[-10:]) if len(highs) >= 10 else max(highs)
    peak_dd = (last / peak - 1.0) * 100.0 if peak > 0 else 0.0
    out["peak_dd"] = round(peak_dd, 2)

    cost_v = _f(cost)
    if cost_v and cost_v > 0:
        pnl = (last / cost_v - 1.0) * 100.0
        out["pnl_pct"] = round(pnl, 2)
        if pnl <= -hard_loss_pct:
            out["action"] = "exit"
            out["label"] = "硬止损区"
            out["urgency"] = 2
            out["reasons"].append(f"相对成本约{pnl:.1f}%≤-{hard_loss_pct:.0f}%")
            return out

    # 近 2 日阴阳 + 放量
    def _is_yin(i: int) -> bool:
        return closes[i] < opens[i]

    vol_blow = False
    if len(vols) >= 7:
        base = sum(vols[-7:-2]) / 5.0 if sum(vols[-7:-2]) > 0 else 0.0
        if base > 0 and vols[-1] >= 1.8 * base and vols[-2] >= 1.5 * base:
            vol_blow = True
    two_yin = len(closes) >= 2 and _is_yin(-1) and _is_yin(-2)

    ma = None
    if len(closes) >= ma_win:
        ma = sum(closes[-ma_win:]) / float(ma_win)

    if peak_dd <= -peak_dd_pct:
        out["action"] = "reduce"
        out["label"] = "峰值回撤减仓"
        out["urgency"] = 2 if peak_dd <= -peak_dd_pct - 3 else 1
        out["reasons"].append(f"距近高约{peak_dd:.1f}%")
    if two_yin and vol_blow:
        out["action"] = "reduce" if out["action"] == "hold_ok" else out["action"]
        out["label"] = "放量双阴减仓"
        out["urgency"] = max(out["urgency"], 1)
        out["reasons"].append("连续阴线且量能放大")
    if ma is not None and last < ma * 0.995:
        # 仅当已有回撤或双阴时升级；单独破均线给提示
        out["reasons"].append(f"收盘低于MA{ma_win}")
        if out["urgency"] == 0:
            out["action"] = "watch"
            out["label"] = "破均线观察减"
            out["urgency"] = 1
        else:
            out["urgency"] = max(out["urgency"], 1)
            if out["action"] == "hold_ok":
                out["action"] = "reduce"

    if not out["reasons"]:
        out["reasons"].append("未触发峰值回撤/放量双阴/硬止损")
    return out


def apply_timing_to_risk(
    risk_score: float,
    *,
    structure: Optional[Dict[str, Any]] = None,
    rs: Optional[Dict[str, Any]] = None,
) -> Tuple[float, List[str]]:
    """把结构/相对强弱折进风险分，返回新分与说明。"""
    notes: List[str] = []
    score = float(risk_score)
    if structure:
        d = float(structure.get("risk_delta") or 0)
        if d:
            score += d
            lab = structure.get("label") or structure.get("tag") or "结构"
            notes.append(f"盘口结构[{lab}]{d:+.0f}")
        for n in structure.get("notes") or []:
            if n not in notes:
                notes.append(str(n))
    if rs:
        d = float(rs.get("risk_delta") or 0)
        if d:
            score += d
            lab = rs.get("label") or "相对强弱"
            notes.append(f"相对强弱[{lab}]{d:+.0f}")
        for n in rs.get("notes") or []:
            if n not in notes:
                notes.append(str(n))
    return max(-100.0, min(100.0, score)), notes


def enrich_buy_setup_with_structure(
    setup: Dict[str, Any],
    structure: Dict[str, Any],
) -> Dict[str, Any]:
    """直线/冲高回落日：否决追买；挖坑收回可抬一档早买优先级。"""
    out = dict(setup or {})
    if not structure:
        return out
    reasons = list(out.get("reasons") or [])
    if structure.get("veto_chase") and out.get("buy_ok"):
        # 已有明确止跌/挖坑方法时，仅降星不整笔否决；无方法或追涨类则否决
        method = str(out.get("method") or "")
        early = method.startswith("E") or method.startswith("B") or "止跌" in method or "挖坑" in method
        if early and structure.get("tag") in ("reject", "gap_fade"):
            out["buy_ok"] = False
            out["buy_stars"] = 0
            reasons.append(f"盘口否决:{structure.get('label')}")
        elif structure.get("tag") == "rocket":
            out["buy_ok"] = False
            out["buy_stars"] = 0
            reasons.append(f"盘口否决:{structure.get('label')}(勿追直拉)")
        else:
            stars = int(out.get("buy_stars") or 0)
            out["buy_stars"] = max(0, stars - 1)
            reasons.append(f"盘口降权:{structure.get('label')}")
    elif structure.get("favors_early_buy") and out.get("buy_ok"):
        stars = int(out.get("buy_stars") or 0)
        out["buy_stars"] = min(5, max(stars, 3))
        reasons.append(f"盘口加分:{structure.get('label')}")
    elif structure.get("favors_early_buy") and not out.get("buy_ok"):
        # 结构偏早买但方法未点亮：不强制改 buy_ok（仍靠 _detect_buy_setup），只留痕迹
        reasons.append(f"结构偏早买:{structure.get('label')}，看止跌/微涨方法")
    out["reasons"] = reasons
    out["盘口结构"] = structure.get("label") or ""
    return out


def format_hold_cell(hint: Dict[str, Any]) -> str:
    if not hint:
        return ""
    lab = str(hint.get("label") or "")
    urg = int(hint.get("urgency") or 0)
    dd = hint.get("peak_dd")
    extra = f" 峰值{dd}%" if dd is not None else ""
    if lab in ("K线不足", "日K未拉到"):
        return "日K缺失·暂算出场"
    if urg >= 2:
        return f"清/减 · {lab}{extra}"
    if urg == 1:
        return f"减仓留意 · {lab}{extra}"
    return f"持有观察 · {lab}"
