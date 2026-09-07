# -*- coding: utf-8 -*-
"""前瞻短线：轻量多因子 GBDT（次日/3日混合收益）+ 因子贡献解释。

设计目标（对齐单位「多因子客观分」）：
- 特征以日K可复现因子为主（动量/位置/波动/量能/形态 + 相对指数）
- 训练宇宙：THEME_HINTS 全量种子（可叠加观察池代码），长窗口日K
- 验证：按交易日时间切分 + MAE / 方向命中 / RankIC
- 盘中资金/新闻等无完整历史的量，仅作小权重 live 加成，不进训练硬门槛

优先 sklearn HistGradientBoosting；无模型或样本不足时回退线性启发式。
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

MODEL_PATH = (
    Path(__file__).resolve().parents[1] / "gui" / "csv" / "forward_short_gbdt.joblib"
)
META_PATH = (
    Path(__file__).resolve().parents[1] / "gui" / "csv" / "forward_short_gbdt_meta.json"
)
INDEX_CACHE_PATH = (
    Path(__file__).resolve().parents[1] / "gui" / "csv" / "forward_ml_index_510300.json"
)

# 日K可复现特征（训练/推理共用）；名称供 GUI 解释
FEATURE_SPEC: List[Tuple[str, str]] = [
    ("ret_1", "近1日涨跌"),
    ("ret_3", "近3日涨跌"),
    ("ret_5", "近5日涨跌"),
    ("ret_10", "近10日涨跌"),
    ("vol_ratio5", "量比vs5日"),
    ("vol_ratio10", "量比vs10日"),
    ("dist_ma5", "距MA5%"),
    ("dist_ma10", "距MA10%"),
    ("dist_ma20", "距MA20%"),
    ("ma5_slope", "MA5斜率%"),
    ("pct_from_high10", "距10日高%"),
    ("pct_from_high20", "距20日高%"),
    ("pct_from_high60", "距60日高%"),
    ("open_pos", "开盘位置"),
    ("close_pos", "收盘位置"),
    ("upper_wick", "上影占比"),
    ("lower_wick", "下影占比"),
    ("body_ratio", "实体占比"),
    ("yang_streak", "连阳天数"),
    ("yin_streak", "连阴天数"),
    ("amplitude", "当日振幅%"),
    ("vol_std10", "10日波动"),
    ("downside10", "10日下行偏差"),
    ("max_dd20", "20日最大回撤%"),
    ("rs_index5", "相对300ETF_5日"),
    ("rs_index10", "相对300ETF_10日"),
    ("flow_1d", "主力流入1日亿"),
    ("flow_3d", "主力流入3日亿"),
    ("flow_5d", "主力流入5日亿"),
    ("flow_accel", "资金流入加速度"),
    ("board_pct_1", "主题板涨跌1日"),
    ("board_pct_5", "主题板涨跌5日"),
    ("rs_board_1", "相对主题板1日"),
    ("pct_rank_theme", "主题内涨跌分位"),
]

FEATURE_KEYS = [k for k, _ in FEATURE_SPEC]
FEATURE_LABELS = {k: lab for k, lab in FEATURE_SPEC}

# 盘中附加因子（新闻/风险值历史难对齐，不进 GBDT；推理小权重加成）
# 资金/板涨已进 FEATURE_SPEC，推理时用 live 或因子库填入。
LIVE_SPEC: List[Tuple[str, str]] = [
    ("board_pct", "板块涨跌"),
    ("rs", "相对板块"),
    ("flow", "主力流入"),
    ("news_hits", "新闻命中"),
    ("risk_score", "风险值"),
    ("mild_up_days", "温和连涨日"),
]

# 训练默认：每票最多取最近 N 个可打标签交易日
DEFAULT_MAX_PER_CODE = 120
DEFAULT_MIN_BARS = 65
INDEX_CODE = "510300"  # 沪深300ETF，作相对强弱基准（指数接口不稳定）

_MODEL_CACHE: Dict[str, Any] = {"model": None, "medians": None, "mtime": None, "keys": None}
_INDEX_CACHE: Dict[str, Any] = {"map": None, "mtime": None}


def load_cached_index_closes() -> Dict[str, float]:
    """读取训练脚本落盘的沪深300收盘价；失败返回空 dict。"""
    try:
        if not INDEX_CACHE_PATH.exists():
            return {}
        mtime = INDEX_CACHE_PATH.stat().st_mtime
        if _INDEX_CACHE.get("map") is not None and _INDEX_CACHE.get("mtime") == mtime:
            return _INDEX_CACHE["map"] or {}
        raw = json.loads(INDEX_CACHE_PATH.read_text(encoding="utf-8"))
        m = {str(k): float(v) for k, v in (raw or {}).items() if v}
        _INDEX_CACHE["map"] = m
        _INDEX_CACHE["mtime"] = mtime
        return m
    except Exception:
        return {}


def save_index_closes(close_by_date: Dict[str, float]) -> None:
    try:
        INDEX_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        INDEX_CACHE_PATH.write_text(
            json.dumps(close_by_date, ensure_ascii=False),
            encoding="utf-8",
        )
        _INDEX_CACHE["map"] = dict(close_by_date)
        _INDEX_CACHE["mtime"] = time.time()
    except Exception:
        pass


def _f(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        v = float(x)
        if v != v or math.isinf(v):
            return default
        return v
    except Exception:
        return default


def _bar_ohlcv(bar: Any) -> Tuple[float, float, float, float, float]:
    if isinstance(bar, dict):
        o = _f(bar.get("open"))
        h = _f(bar.get("high"), o)
        l = _f(bar.get("low"), o)
        c = _f(bar.get("close"), o)
        v = _f(bar.get("volume") or bar.get("vol"))
        return o, h, l, c, v
    try:
        return (
            _f(bar[1]),
            _f(bar[2]),
            _f(bar[3]),
            _f(bar[4]),
            _f(bar[5] if len(bar) > 5 else 0),
        )
    except Exception:
        return 0.0, 0.0, 0.0, 0.0, 0.0


def _bar_date(bar: Any) -> str:
    if isinstance(bar, dict):
        return str(bar.get("date") or "").replace("-", "")[:8]
    try:
        return str(bar[0] or "").replace("-", "")[:8]
    except Exception:
        return ""


def _streaks(closes: Sequence[float], opens: Sequence[float]) -> Tuple[int, int]:
    """收盘>开盘算阳，连阴对称；不要求开盘抬高（训练用简化）。"""
    yang = yin = 0
    for i in range(len(closes) - 1, -1, -1):
        o, c = opens[i], closes[i]
        if c > o:
            if yin:
                break
            yang += 1
        elif c < o:
            if yang:
                break
            yin += 1
        else:
            break
    return yang, yin


def _std(xs: Sequence[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def _index_ret(
    index_close_by_date: Optional[Dict[str, float]],
    date: str,
    closes: Sequence[float],
    dates: Sequence[str],
    end_idx: int,
    k: int,
) -> float:
    """个股 k 日收益 − 指数同日历窗口收益；缺指数则 0。"""
    if not index_close_by_date or end_idx < k or closes[end_idx] <= 0:
        return 0.0
    stock_r = (closes[end_idx] / closes[end_idx - k] - 1.0) * 100.0
    d1 = date
    d0 = dates[end_idx - k] if end_idx - k >= 0 else ""
    c1 = index_close_by_date.get(d1)
    c0 = index_close_by_date.get(d0) if d0 else None
    if c1 is None or c0 is None or c0 <= 0:
        return 0.0
    idx_r = (c1 / c0 - 1.0) * 100.0
    return stock_r - idx_r


def features_from_bars(
    bars: Sequence[Any],
    *,
    end_idx: Optional[int] = None,
    index_close_by_date: Optional[Dict[str, float]] = None,
) -> Optional[Dict[str, float]]:
    """取 bars[0..end_idx] 末根为当日，构造特征。建议 ≥65 根；短窗口因子自动降级。"""
    if not bars:
        return None
    n = len(bars) if end_idx is None else int(end_idx) + 1
    if n < 21:
        return None
    sub = list(bars[:n])
    ohlcv = [_bar_ohlcv(b) for b in sub]
    dates = [_bar_date(b) for b in sub]
    opens = [x[0] for x in ohlcv]
    highs = [x[1] for x in ohlcv]
    lows = [x[2] for x in ohlcv]
    closes = [x[3] for x in ohlcv]
    vols = [x[4] for x in ohlcv]
    c = closes[-1]
    if c <= 0:
        return None
    o, h, l = opens[-1], highs[-1], lows[-1]
    span = max(h - l, 1e-6)
    i = n - 1

    def _ret(k: int) -> float:
        if len(closes) <= k or closes[-1 - k] <= 0:
            return 0.0
        return (c / closes[-1 - k] - 1.0) * 100.0

    ma5 = sum(closes[-5:]) / 5.0
    ma10 = sum(closes[-10:]) / 10.0
    ma20 = sum(closes[-20:]) / 20.0 if len(closes) >= 20 else ma10
    ma5_prev = sum(closes[-6:-1]) / 5.0 if len(closes) >= 6 else ma5
    high10 = max(highs[-10:])
    high20 = max(highs[-20:]) if len(highs) >= 20 else high10
    high60 = max(highs[-60:]) if len(highs) >= 60 else high20
    v5 = sum(vols[-6:-1]) / 5.0 if len(vols) >= 6 else 0.0
    v10 = sum(vols[-11:-1]) / 10.0 if len(vols) >= 11 else v5
    yang, yin = _streaks(closes, opens)

    # 日收益序列（末 10/20）
    rets: List[float] = []
    for j in range(1, min(11, len(closes))):
        if closes[-1 - j] > 0:
            rets.append((closes[-j] / closes[-1 - j] - 1.0) * 100.0)
    downside = [r for r in rets if r < 0]
    # 20 日最大回撤
    window = closes[-20:] if len(closes) >= 20 else closes
    peak = window[0]
    max_dd = 0.0
    for px in window:
        if px > peak:
            peak = px
        if peak > 0:
            max_dd = min(max_dd, (px / peak - 1.0) * 100.0)

    body = abs(c - o) / span
    date = dates[-1]
    rs5 = _index_ret(index_close_by_date, date, closes, dates, i, 5)
    rs10 = _index_ret(index_close_by_date, date, closes, dates, i, 10)

    return {
        "ret_1": _ret(1),
        "ret_3": _ret(3),
        "ret_5": _ret(5),
        "ret_10": _ret(10),
        "vol_ratio5": (vols[-1] / v5) if v5 > 0 else 1.0,
        "vol_ratio10": (vols[-1] / v10) if v10 > 0 else 1.0,
        "dist_ma5": (c / ma5 - 1.0) * 100.0 if ma5 > 0 else 0.0,
        "dist_ma10": (c / ma10 - 1.0) * 100.0 if ma10 > 0 else 0.0,
        "dist_ma20": (c / ma20 - 1.0) * 100.0 if ma20 > 0 else 0.0,
        "ma5_slope": (ma5 / ma5_prev - 1.0) * 100.0 if ma5_prev > 0 else 0.0,
        "pct_from_high10": (c / high10 - 1.0) * 100.0 if high10 > 0 else 0.0,
        "pct_from_high20": (c / high20 - 1.0) * 100.0 if high20 > 0 else 0.0,
        "pct_from_high60": (c / high60 - 1.0) * 100.0 if high60 > 0 else 0.0,
        "open_pos": (o - l) / span,
        "close_pos": (c - l) / span,
        "upper_wick": (h - max(o, c)) / span,
        "lower_wick": (min(o, c) - l) / span,
        "body_ratio": body,
        "yang_streak": float(yang),
        "yin_streak": float(yin),
        "amplitude": (span / c) * 100.0,
        "vol_std10": _std(rets),
        "downside10": _std(downside) if downside else 0.0,
        "max_dd20": max_dd,
        "rs_index5": rs5,
        "rs_index10": rs10,
        "flow_1d": 0.0,
        "flow_3d": 0.0,
        "flow_5d": 0.0,
        "flow_accel": 0.0,
        "board_pct_1": 0.0,
        "board_pct_5": 0.0,
        "rs_board_1": 0.0,
        "pct_rank_theme": 0.5,
    }


def apply_flow_board_features(
    feat: Dict[str, float],
    *,
    panel_row: Optional[Dict[str, Any]] = None,
    live: Optional[Dict[str, Any]] = None,
) -> Dict[str, float]:
    """把因子库/盘中资金与主题板填进特征（训练与推理共用键）。"""
    out = dict(feat or {})
    if panel_row:
        if panel_row.get("main_net_yi") is not None:
            out["flow_1d"] = _f(panel_row.get("main_net_yi"))
        if panel_row.get("flow_3d") is not None:
            out["flow_3d"] = _f(panel_row.get("flow_3d"))
        if panel_row.get("flow_5d") is not None:
            out["flow_5d"] = _f(panel_row.get("flow_5d"))
        if panel_row.get("flow_accel") is not None:
            out["flow_accel"] = _f(panel_row.get("flow_accel"))
        if panel_row.get("board_pct") is not None:
            out["board_pct_1"] = _f(panel_row.get("board_pct"))
        if panel_row.get("board_pct_5") is not None:
            out["board_pct_5"] = _f(panel_row.get("board_pct_5"))
        if panel_row.get("rs_board") is not None:
            out["rs_board_1"] = _f(panel_row.get("rs_board"))
        if panel_row.get("pct_rank_theme") is not None:
            out["pct_rank_theme"] = _f(panel_row.get("pct_rank_theme"), 0.5)
    if live:
        # live 可覆盖当日（盘中更新）
        if live.get("flow") is not None:
            out["flow_1d"] = _f(live.get("flow"))
        if live.get("board_pct") is not None:
            out["board_pct_1"] = _f(live.get("board_pct"))
        if live.get("rs") is not None:
            out["rs_board_1"] = _f(live.get("rs"))
    return out


def feature_vector(feat: Dict[str, float]) -> List[float]:
    return [_f(feat.get(k)) for k in FEATURE_KEYS]


def _forward_label(
    closes: Sequence[float],
    i: int,
    *,
    dates: Optional[Sequence[str]] = None,
    index_close_by_date: Optional[Dict[str, float]] = None,
    theme_board_by_date: Optional[Dict[str, float]] = None,
) -> Optional[float]:
    """0.4*次日 + 0.6*三日收益（%）。

    优先减主题等权板同期收益（截面超额）；否则减 300ETF；都没有则用绝对收益。
    """
    if i + 3 >= len(closes) or closes[i] <= 0:
        return None
    r1 = (closes[i + 1] / closes[i] - 1.0) * 100.0
    r3 = (closes[i + 3] / closes[i] - 1.0) * 100.0
    lab = 0.4 * r1 + 0.6 * r3
    if theme_board_by_date and dates and i + 3 < len(dates):
        b1 = theme_board_by_date.get(dates[i + 1])
        b2 = theme_board_by_date.get(dates[i + 2])
        b3 = theme_board_by_date.get(dates[i + 3])
        if b1 is not None and b2 is not None and b3 is not None:
            br1 = float(b1)
            br3 = float(b1) + float(b2) + float(b3)
            return lab - (0.4 * br1 + 0.6 * br3)
    if (
        index_close_by_date
        and dates
        and i + 3 < len(dates)
        and dates[i]
        and dates[i + 1]
        and dates[i + 3]
    ):
        c0 = index_close_by_date.get(dates[i])
        c1 = index_close_by_date.get(dates[i + 1])
        c3 = index_close_by_date.get(dates[i + 3])
        if c0 and c1 and c3 and c0 > 0:
            ir1 = (c1 / c0 - 1.0) * 100.0
            ir3 = (c3 / c0 - 1.0) * 100.0
            lab = lab - (0.4 * ir1 + 0.6 * ir3)
    return lab


def build_training_matrix(
    bars_by_code: Dict[str, Sequence[Any]],
    *,
    max_per_code: int = DEFAULT_MAX_PER_CODE,
    index_close_by_date: Optional[Dict[str, float]] = None,
    panel_by_code: Optional[Dict[str, Dict[str, Dict[str, Any]]]] = None,
    theme_boards: Optional[Dict[str, Dict[str, float]]] = None,
) -> Tuple[List[List[float]], List[float], List[str]]:
    """返回 X, y, dates（YYYYMMDD，用于时间切分）。

    panel_by_code: code -> date -> {main_net_yi, flow_3d, board_pct, ...}
    theme_boards: theme_id -> date -> board_pct（用于主题超额标签）
    """
    xs: List[List[float]] = []
    ys: List[float] = []
    ds: List[str] = []
    for code, bars in (bars_by_code or {}).items():
        if not bars or len(bars) < DEFAULT_MIN_BARS:
            continue
        code6 = str(code).zfill(6)[-6:]
        ohlcv = [_bar_ohlcv(b) for b in bars]
        closes = [x[3] for x in ohlcv]
        dates_b = [_bar_date(b) for b in bars]
        panel = (panel_by_code or {}).get(code6) or {}
        # 任取一日的 theme_id
        tid = None
        for _d, row in panel.items():
            if row.get("theme_id"):
                tid = str(row.get("theme_id"))
                break
        tboard = (theme_boards or {}).get(tid or "") if tid else None
        last_feat_i = len(bars) - 4  # need i+3
        first_i = max(20, DEFAULT_MIN_BARS - 1)
        idxs = list(range(first_i, last_feat_i + 1))
        if len(idxs) > max_per_code:
            idxs = idxs[-max_per_code:]
        for i in idxs:
            feat = features_from_bars(
                bars, end_idx=i, index_close_by_date=index_close_by_date
            )
            y = _forward_label(
                closes,
                i,
                dates=dates_b,
                index_close_by_date=index_close_by_date,
                theme_board_by_date=tboard,
            )
            if feat is None or y is None:
                continue
            if y > 18 or y < -15:
                continue
            d = dates_b[i] if i < len(dates_b) else ""
            feat = apply_flow_board_features(feat, panel_row=panel.get(d))
            xs.append(feature_vector(feat))
            ys.append(y)
            ds.append(d or f"{i:08d}")
    return xs, ys, ds


def build_training_matrix_from_store(
    *,
    max_per_code: int = DEFAULT_MAX_PER_CODE,
    index_code: str = INDEX_CODE,
    min_bars: int = DEFAULT_MIN_BARS,
) -> Tuple[
    Dict[str, List[Dict[str, Any]]],
    Dict[str, float],
    Dict[str, Dict[str, Dict[str, Any]]],
    Dict[str, Dict[str, float]],
]:
    """从 SQLite 装载 bars / 指数 / 资金板 panel / 主题板。"""
    from qbot.data import ml_factor_store as store

    codes = store.list_store_codes(min_bars=min_bars)
    codes = [c for c in codes if c != str(index_code).zfill(6)[-6:]]
    bars_by_code: Dict[str, List[Dict[str, Any]]] = {}
    panel_by_code: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for code in codes:
        bars = store.load_bars_from_store(code, limit=max(max_per_code + 80, 220))
        if len(bars) < min_bars:
            continue
        bars_by_code[code] = bars
        panel_by_code[code] = store.load_flow_board_panel(
            code, limit=max(max_per_code + 80, 220)
        )
    store.attach_theme_pct_ranks(panel_by_code)
    idx_map = store.load_index_close_map(index_code)
    theme_boards = store.load_theme_board_maps()
    return bars_by_code, idx_map, panel_by_code, theme_boards


def _heuristic_score(feat: Dict[str, float], live: Optional[Dict[str, float]] = None) -> float:
    """无模型时的可解释线性分（大致对齐短持预期）。"""
    s = 0.0
    s += max(0.0, 2.5 - abs(_f(feat.get("ret_1")) - 1.0)) * 0.35
    s += max(0.0, 4.0 - abs(_f(feat.get("ret_3")) - 2.0)) * 0.25
    s -= max(0.0, _f(feat.get("ret_5")) - 12.0) * 0.35
    s += max(0.0, -_f(feat.get("pct_from_high10")) - 2.0) * 0.15
    s -= max(0.0, _f(feat.get("open_pos")) - 0.55) * 4.0
    s += max(0.0, 0.45 - _f(feat.get("upper_wick"))) * 2.0
    s += min(_f(feat.get("yin_streak")), 3) * 0.4
    s -= min(_f(feat.get("yang_streak")), 5) * 0.35
    vr = _f(feat.get("vol_ratio5"), 1.0)
    if 0.7 <= vr <= 1.8:
        s += 0.8
    elif vr >= 2.8:
        s -= 1.2
    s += min(max(_f(feat.get("rs_index5")), -6), 6) * 0.12
    s -= max(0.0, -_f(feat.get("max_dd20")) - 12.0) * 0.08
    if live:
        s += min(max(_f(live.get("board_pct")), -2), 4) * 0.25
        s += min(max(_f(live.get("rs")), -4), 4) * 0.2
        s += min(max(_f(live.get("flow")), -1), 6) * 0.15
        s += min(_f(live.get("news_hits")), 3) * 0.35
        s += max(min(_f(live.get("risk_score")), 40), -40) * 0.03
        s += min(_f(live.get("mild_up_days")), 3) * 0.25
    return s


def _heuristic_contribs(
    feat: Dict[str, float], live: Optional[Dict[str, float]] = None
) -> List[Tuple[str, float, float]]:
    """返回 (label, raw_value, contrib) 按 |contrib| 降序。"""
    items: List[Tuple[str, float, float]] = [
        ("近1日涨跌", _f(feat.get("ret_1")), max(0.0, 2.5 - abs(_f(feat.get("ret_1")) - 1.0)) * 0.35),
        ("近5日涨跌", _f(feat.get("ret_5")), -max(0.0, _f(feat.get("ret_5")) - 12.0) * 0.35),
        ("距10日高%", _f(feat.get("pct_from_high10")), max(0.0, -_f(feat.get("pct_from_high10")) - 2.0) * 0.15),
        ("开盘位置", _f(feat.get("open_pos")), -max(0.0, _f(feat.get("open_pos")) - 0.55) * 4.0),
        ("上影占比", _f(feat.get("upper_wick")), max(0.0, 0.45 - _f(feat.get("upper_wick"))) * 2.0),
        ("连阳天数", _f(feat.get("yang_streak")), -min(_f(feat.get("yang_streak")), 5) * 0.35),
        ("连阴天数", _f(feat.get("yin_streak")), min(_f(feat.get("yin_streak")), 3) * 0.4),
        (
            "量比vs5日",
            _f(feat.get("vol_ratio5"), 1.0),
            (
                0.8
                if 0.7 <= _f(feat.get("vol_ratio5"), 1.0) <= 1.8
                else (-1.2 if _f(feat.get("vol_ratio5"), 1.0) >= 2.8 else 0.0)
            ),
        ),
        ("相对300ETF_5日", _f(feat.get("rs_index5")), min(max(_f(feat.get("rs_index5")), -6), 6) * 0.12),
    ]
    if live:
        items.extend(
            [
                ("板块涨跌", _f(live.get("board_pct")), min(max(_f(live.get("board_pct")), -2), 4) * 0.25),
                ("相对板块", _f(live.get("rs")), min(max(_f(live.get("rs")), -4), 4) * 0.2),
                ("主力流入", _f(live.get("flow")), min(max(_f(live.get("flow")), -1), 6) * 0.15),
                ("新闻命中", _f(live.get("news_hits")), min(_f(live.get("news_hits")), 3) * 0.35),
                ("风险值", _f(live.get("risk_score")), max(min(_f(live.get("risk_score")), 40), -40) * 0.03),
            ]
        )
    items.sort(key=lambda t: abs(t[2]), reverse=True)
    return items


def _time_split(
    X, y, dates: Sequence[str], test_ratio: float = 0.2
):
    """按日期排序后取最后 test_ratio 作验证（避免随机切分泄漏）。"""
    import numpy as np

    order = sorted(range(len(dates)), key=lambda i: dates[i])
    X = X[order]
    y = y[order]
    n = len(y)
    if n < 40:
        return X, y, X[:1], y[:1]
    cut = max(1, int(n * (1.0 - test_ratio)))
    if cut >= n:
        cut = n - 1
    return X[:cut], y[:cut], X[cut:], y[cut:]


def _rank_ic(pred, actual) -> Optional[float]:
    """Spearman 近似：用秩相关。"""
    try:
        import numpy as np

        p = np.asarray(pred, dtype=float)
        a = np.asarray(actual, dtype=float)
        if len(p) < 8:
            return None
        pr = p.argsort().argsort().astype(float)
        ar = a.argsort().argsort().astype(float)
        pr = pr - pr.mean()
        ar = ar - ar.mean()
        denom = float(np.sqrt((pr ** 2).sum() * (ar ** 2).sum()))
        if denom <= 1e-12:
            return None
        return float((pr * ar).sum() / denom)
    except Exception:
        return None


def train_short_gbdt_from_store(
    *,
    min_samples: int = 400,
    persist: bool = True,
    max_per_code: int = DEFAULT_MAX_PER_CODE,
    force: bool = True,
) -> Dict[str, Any]:
    """从本地 SQLite 因子库训练（周训入口）。"""
    bars_by_code, idx_map, panel_by_code, theme_boards = build_training_matrix_from_store(
        max_per_code=max_per_code
    )
    if idx_map:
        save_index_closes(idx_map)
    meta = train_short_gbdt(
        bars_by_code,
        min_samples=min_samples,
        persist=persist,
        max_per_code=max_per_code,
        index_close_by_date=idx_map or None,
        panel_by_code=panel_by_code,
        theme_boards=theme_boards,
        force=force,
    )
    meta["source"] = "ml_factor_store"
    meta["store_codes"] = len(bars_by_code)
    try:
        from qbot.data import ml_factor_store as store

        meta["store_stats"] = store.stats()
    except Exception:
        pass
    return meta


def days_since_last_train() -> Optional[float]:
    """距上次成功落盘训练的天数；无则 None。"""
    if not META_PATH.exists():
        return None
    try:
        meta = json.loads(META_PATH.read_text(encoding="utf-8"))
        if not meta.get("ok"):
            return None
        ts = str(meta.get("trained_at") or "")
        t = time.mktime(time.strptime(ts[:19], "%Y-%m-%d %H:%M:%S"))
        return max(0.0, (time.time() - t) / 86400.0)
    except Exception:
        return None


def train_short_gbdt(
    bars_by_code: Dict[str, Sequence[Any]],
    *,
    min_samples: int = 400,
    persist: bool = True,
    max_per_code: int = DEFAULT_MAX_PER_CODE,
    index_close_by_date: Optional[Dict[str, float]] = None,
    panel_by_code: Optional[Dict[str, Dict[str, Dict[str, Any]]]] = None,
    theme_boards: Optional[Dict[str, Dict[str, float]]] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """训练并可选落盘；样本不足返回 heuristic 标记。

    若磁盘已有更强模型（样本更多且特征集一致）且非 force，则跳过弱覆盖。
    """
    xs, ys, ds = build_training_matrix(
        bars_by_code,
        max_per_code=max_per_code,
        index_close_by_date=index_close_by_date,
        panel_by_code=panel_by_code,
        theme_boards=theme_boards,
    )
    meta: Dict[str, Any] = {
        "n_samples": len(ys),
        "n_codes": len(bars_by_code or {}),
        "trained_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "backend": "heuristic",
        "feature_keys": list(FEATURE_KEYS),
        "n_features": len(FEATURE_KEYS),
        "label": "0.4*ret1+0.6*ret3 excess_vs_theme_else_510300",
        "split": "time_last_20pct",
    }
    if len(ys) < min_samples:
        meta["ok"] = False
        meta["why"] = f"样本{len(ys)}<{min_samples}，沿用启发式/旧模型"
        return meta

    # 防止短池小样本覆盖夜间大模型
    if not force and META_PATH.exists() and MODEL_PATH.exists():
        try:
            old = json.loads(META_PATH.read_text(encoding="utf-8"))
            old_n = int(old.get("n_samples") or 0)
            old_keys = old.get("feature_keys") or []
            if (
                old.get("ok")
                and old_n >= len(ys) * 1.2
                and list(old_keys) == list(FEATURE_KEYS)
            ):
                meta["ok"] = False
                meta["why"] = (
                    f"跳过弱覆盖：新样本{len(ys)} < 旧模型{old_n}*1.2；"
                    "请用 scripts/train_forward_gbdt.py 扩宇宙重训"
                )
                meta["kept_old_samples"] = old_n
                return meta
        except Exception:
            pass

    try:
        import numpy as np
        from sklearn.ensemble import HistGradientBoostingRegressor
    except Exception as exc:  # noqa: BLE001
        meta["ok"] = False
        meta["why"] = f"sklearn不可用:{exc}"
        return meta

    X = np.asarray(xs, dtype=float)
    y = np.asarray(ys, dtype=float)
    medians = np.median(X, axis=0)
    Xtr, ytr, Xte, yte = _time_split(X, y, ds, test_ratio=0.2)

    model = HistGradientBoostingRegressor(
        max_depth=5,
        max_iter=180,
        learning_rate=0.05,
        min_samples_leaf=25,
        l2_regularization=0.15,
        random_state=42,
    )
    model.fit(Xtr, ytr)
    try:
        pred = model.predict(Xte)
        mae = float(np.mean(np.abs(pred - yte)))
        hit = float(np.mean((pred > 0) == (yte > 0)))
        ric = _rank_ic(pred, yte)
        # 多空组差：预测最高20% − 最低20% 的真实标签均值
        order = np.argsort(pred)
        k = max(1, len(pred) // 5)
        spread = float(np.mean(yte[order[-k:]]) - np.mean(yte[order[:k]]))
    except Exception:
        mae, hit, ric, spread = None, None, None, None

    payload = {
        "model": model,
        "medians": medians.tolist(),
        "feature_keys": list(FEATURE_KEYS),
        "meta": {
            **meta,
            "ok": True,
            "backend": "hist_gbdt",
            "mae": mae,
            "dir_hit": hit,
            "rank_ic": ric,
            "long_short_spread": spread,
            "n_train": int(len(ytr)),
            "n_test": int(len(yte)),
            "date_min": min(ds) if ds else None,
            "date_max": max(ds) if ds else None,
        },
    }
    if persist:
        try:
            import joblib

            MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
            joblib.dump(payload, MODEL_PATH)
            META_PATH.write_text(
                json.dumps(payload["meta"], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            payload["meta"]["persist_err"] = str(exc)
    _MODEL_CACHE["model"] = payload["model"]
    _MODEL_CACHE["medians"] = payload["medians"]
    _MODEL_CACHE["mtime"] = time.time()
    _MODEL_CACHE["keys"] = list(FEATURE_KEYS)
    return payload["meta"]


def _load_model() -> Tuple[Any, Optional[List[float]], Dict[str, Any]]:
    if _MODEL_CACHE.get("model") is not None:
        keys = _MODEL_CACHE.get("keys")
        if keys is None or list(keys) == list(FEATURE_KEYS):
            return _MODEL_CACHE["model"], _MODEL_CACHE.get("medians"), {"cached": True}
    if not MODEL_PATH.exists():
        return None, None, {"missing": True}
    try:
        import joblib

        payload = joblib.load(MODEL_PATH)
        if isinstance(payload, dict) and "model" in payload:
            keys = payload.get("feature_keys") or []
            if keys and list(keys) != list(FEATURE_KEYS):
                return None, None, {
                    "feature_mismatch": True,
                    "old_keys": keys,
                    "new_n": len(FEATURE_KEYS),
                }
            _MODEL_CACHE["model"] = payload["model"]
            _MODEL_CACHE["medians"] = payload.get("medians")
            _MODEL_CACHE["keys"] = list(FEATURE_KEYS)
            return payload["model"], payload.get("medians"), payload.get("meta") or {}
        _MODEL_CACHE["model"] = payload
        return payload, None, {}
    except Exception as exc:  # noqa: BLE001
        return None, None, {"load_err": str(exc)}


def ensure_short_model(
    bars_by_code: Dict[str, Sequence[Any]],
    *,
    retrain: bool = False,
    max_age_sec: float = 36 * 3600,
    index_close_by_date: Optional[Dict[str, float]] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """默认加载已训模型；仅缺失/过期/特征不匹配时才用传入 bars 重训。

    短线池刷新应 retrain=False，避免用几十只×28根覆盖夜间大宇宙模型。
    """
    m, _, meta = _load_model()
    if m is not None and not retrain and not force:
        age_ok = True
        if MODEL_PATH.exists():
            age_ok = (time.time() - MODEL_PATH.stat().st_mtime) < max_age_sec
        if age_ok or not bars_by_code:
            return {"ok": True, "backend": "file", **(meta or {})}

    need = force or retrain or m is None
    if MODEL_PATH.exists() and not need and not retrain:
        age = time.time() - MODEL_PATH.stat().st_mtime
        if age < max_age_sec:
            return {"ok": True, "backend": "cached_file", **(meta or {})}
        need = True

    if need and bars_by_code:
        new_meta = train_short_gbdt(
            bars_by_code,
            persist=True,
            index_close_by_date=index_close_by_date,
            force=force,
        )
        if new_meta.get("ok"):
            return new_meta
        m2, _, old = _load_model()
        if m2 is not None:
            return {"ok": True, "backend": "stale_file", "train": new_meta, **(old or {})}
        return new_meta

    m3, _, meta3 = _load_model()
    return {
        "ok": m3 is not None,
        "backend": "file" if m3 else "heuristic",
        **(meta3 or {}),
    }


def _contrib_by_median_replace(
    model: Any,
    x: List[float],
    medians: Sequence[float],
) -> List[Tuple[str, float, float]]:
    import numpy as np

    base = float(model.predict(np.asarray([x], dtype=float))[0])
    out: List[Tuple[str, float, float]] = []
    for i, key in enumerate(FEATURE_KEYS):
        x2 = list(x)
        x2[i] = float(medians[i]) if i < len(medians) else 0.0
        p2 = float(model.predict(np.asarray([x2], dtype=float))[0])
        out.append((FEATURE_LABELS.get(key, key), x[i], base - p2))
    out.sort(key=lambda t: abs(t[2]), reverse=True)
    return out


def _contrib_by_shap(
    model: Any, x: List[float]
) -> Optional[List[Tuple[str, float, float]]]:
    try:
        import numpy as np
        import shap  # type: ignore
    except Exception:
        return None
    try:
        explainer = shap.Explainer(model)
        sv = explainer(np.asarray([x], dtype=float))
        vals = list(sv.values[0])
        out = [
            (FEATURE_LABELS.get(FEATURE_KEYS[i], FEATURE_KEYS[i]), x[i], float(vals[i]))
            for i in range(len(FEATURE_KEYS))
        ]
        out.sort(key=lambda t: abs(t[2]), reverse=True)
        return out
    except Exception:
        return None


def format_factor_explain(
    contribs: Sequence[Tuple[str, float, float]],
    *,
    top_k: int = 4,
) -> str:
    """「因催化+止跌+流入」风格短句。"""
    if not contribs:
        return ""
    pos = [c for c in contribs if c[2] > 0.02][: max(1, top_k // 2 + 1)]
    neg = [c for c in contribs if c[2] < -0.02][: max(1, top_k // 2)]
    bits: List[str] = []
    if pos:
        bits.append("利多:" + "+".join(f"{n}" for n, _, _ in pos[:3]))
    if neg:
        bits.append("拖累:" + "+".join(f"{n}" for n, _, _ in neg[:2]))
    return "；".join(bits)


def score_short_ml(
    bars: Sequence[Any],
    *,
    live: Optional[Dict[str, Any]] = None,
    index_close_by_date: Optional[Dict[str, float]] = None,
    code: Optional[str] = None,
) -> Dict[str, Any]:
    """对单票打 ML 分 + 因子贡献。

    返回:
      ml_score: 预测混合收益（%），越高越偏多
      backend: hist_gbdt / heuristic
      因子贡献: 解释短句
      contribs: 明细
    """
    feat = features_from_bars(
        bars,
        index_close_by_date=index_close_by_date
        if index_close_by_date is not None
        else load_cached_index_closes(),
    )
    live_f = None
    if live:
        live_f = {k: _f(live.get(k)) for k, _ in LIVE_SPEC}

    panel_row = None
    if code and bars:
        try:
            from qbot.data import ml_factor_store as store

            panel = store.load_flow_board_panel(str(code), limit=40)
            panel_row = panel.get(_bar_date(bars[-1]))
        except Exception:
            panel_row = None
    if feat is not None:
        feat = apply_flow_board_features(feat, panel_row=panel_row, live=live)

    if feat is None:
        h = _heuristic_score({}, live_f)
        cons = _heuristic_contribs({}, live_f)
        return {
            "ml_score": round(h, 3),
            "backend": "heuristic",
            "因子贡献": format_factor_explain(cons),
            "contribs": cons[:8],
            "feat": {},
        }

    model, medians, _meta = _load_model()
    x = feature_vector(feat)
    backend = "heuristic"
    score = _heuristic_score(feat, live_f)
    cons = _heuristic_contribs(feat, live_f)

    if model is not None:
        try:
            import numpy as np

            pred = float(model.predict(np.asarray([x], dtype=float))[0])
            # 资金/板已入模；live 仅保留新闻与风险值小加成，避免双计
            live_boost = 0.0
            if live_f:
                live_boost = (
                    min(live_f.get("news_hits", 0.0), 3) * 0.12
                    + max(min(live_f.get("risk_score", 0.0), 30), -30) * 0.01
                    + min(live_f.get("mild_up_days", 0.0), 3) * 0.08
                )
            score = pred + live_boost
            backend = "hist_gbdt"
            med = medians or [0.0] * len(FEATURE_KEYS)
            cons = _contrib_by_shap(model, x) or _contrib_by_median_replace(
                model, x, med
            )
            if live_f:
                extra = _heuristic_contribs({}, live_f)
                cons = list(cons) + [
                    (n, v, c) for n, v, c in extra if abs(c) >= 0.05
                ]
                cons.sort(key=lambda t: abs(t[2]), reverse=True)
        except Exception:
            backend = "heuristic"
            score = _heuristic_score(feat, live_f)
            cons = _heuristic_contribs(feat, live_f)

    return {
        "ml_score": round(float(score), 3),
        "backend": backend,
        "因子贡献": format_factor_explain(cons),
        "contribs": [
            {"name": n, "value": round(float(v), 3), "contrib": round(float(c), 3)}
            for n, v, c in cons[:8]
        ],
        "feat": {k: round(_f(feat.get(k)), 4) for k in FEATURE_KEYS},
    }


def blend_short_rank(
    rule_score: float,
    ml_score: float,
    *,
    rule_w: float = 0.25,
    ml_w: float = 0.75,
) -> float:
    """规则短线分与 ML 分融合；ML 主导排序。"""
    ml_scaled = 5.0 + float(ml_score) * 2.2
    return rule_w * float(rule_score) + ml_w * ml_scaled


def list_theme_seed_codes() -> List[str]:
    """THEME_HINTS 全量种子代码（去重）。"""
    try:
        from qbot.data.forward_watch import THEME_HINTS
    except Exception:
        return []
    out: List[str] = []
    seen = set()
    for h in THEME_HINTS or []:
        for c, _n in list(h.get("seed_stocks") or []):
            code = str(c or "").zfill(6)[-6:]
            if len(code) == 6 and code.isdigit() and code not in seen:
                seen.add(code)
                out.append(code)
    return out


def index_close_map_from_bars(bars: Sequence[Any]) -> Dict[str, float]:
    """日K → {date: close}。"""
    m: Dict[str, float] = {}
    for b in bars or []:
        d = _bar_date(b)
        c = _bar_ohlcv(b)[3]
        if d and c > 0:
            m[d] = c
    return m
