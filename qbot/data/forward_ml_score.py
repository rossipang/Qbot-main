# -*- coding: utf-8 -*-
"""前瞻短线 P1：轻量 GBDT（次日/3日收益）+ 因子贡献解释。

优先 sklearn HistGradientBoosting；无模型或样本不足时回退线性启发式。
SHAP 可选：已安装 shap 则用 TreeExplainer，否则用「中位数置换」近似贡献。
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

# 日K可复现特征（训练/推理共用）；名称供 GUI 解释
FEATURE_SPEC: List[Tuple[str, str]] = [
    ("ret_1", "近1日涨跌"),
    ("ret_3", "近3日涨跌"),
    ("ret_5", "近5日涨跌"),
    ("vol_ratio5", "量比vs5日"),
    ("dist_ma5", "距MA5%"),
    ("dist_ma10", "距MA10%"),
    ("dist_ma20", "距MA20%"),
    ("pct_from_high10", "距10日高%"),
    ("open_pos", "开盘位置"),
    ("close_pos", "收盘位置"),
    ("upper_wick", "上影占比"),
    ("lower_wick", "下影占比"),
    ("yang_streak", "连阳天数"),
    ("yin_streak", "连阴天数"),
    ("amplitude", "当日振幅%"),
]

FEATURE_KEYS = [k for k, _ in FEATURE_SPEC]
FEATURE_LABELS = {k: lab for k, lab in FEATURE_SPEC}

# 盘中附加因子（不进 GBDT 训练，只进解释与短线加成）
LIVE_SPEC: List[Tuple[str, str]] = [
    ("board_pct", "板块涨跌"),
    ("rs", "相对板块"),
    ("flow", "主力流入"),
    ("news_hits", "新闻命中"),
    ("risk_score", "风险值"),
    ("mild_up_days", "温和连涨日"),
]

_MODEL_CACHE: Dict[str, Any] = {"model": None, "medians": None, "mtime": None}


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


def features_from_bars(bars: Sequence[Any], *, end_idx: Optional[int] = None) -> Optional[Dict[str, float]]:
    """取 bars[0..end_idx] 末根为当日，构造特征。至少需要 21 根。"""
    if not bars:
        return None
    n = len(bars) if end_idx is None else int(end_idx) + 1
    if n < 21:
        return None
    sub = list(bars[:n])
    ohlcv = [_bar_ohlcv(b) for b in sub]
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

    def _ret(k: int) -> float:
        if len(closes) <= k or closes[-1 - k] <= 0:
            return 0.0
        return (c / closes[-1 - k] - 1.0) * 100.0

    ma5 = sum(closes[-5:]) / 5.0
    ma10 = sum(closes[-10:]) / 10.0
    ma20 = sum(closes[-20:]) / 20.0
    high10 = max(highs[-10:])
    v5 = sum(vols[-6:-1]) / 5.0 if len(vols) >= 6 else 0.0
    yang, yin = _streaks(closes, opens)

    return {
        "ret_1": _ret(1),
        "ret_3": _ret(3),
        "ret_5": _ret(5),
        "vol_ratio5": (vols[-1] / v5) if v5 > 0 else 1.0,
        "dist_ma5": (c / ma5 - 1.0) * 100.0 if ma5 > 0 else 0.0,
        "dist_ma10": (c / ma10 - 1.0) * 100.0 if ma10 > 0 else 0.0,
        "dist_ma20": (c / ma20 - 1.0) * 100.0 if ma20 > 0 else 0.0,
        "pct_from_high10": (c / high10 - 1.0) * 100.0 if high10 > 0 else 0.0,
        "open_pos": (o - l) / span,
        "close_pos": (c - l) / span,
        "upper_wick": (h - max(o, c)) / span,
        "lower_wick": (min(o, c) - l) / span,
        "yang_streak": float(yang),
        "yin_streak": float(yin),
        "amplitude": (span / c) * 100.0,
    }


def feature_vector(feat: Dict[str, float]) -> List[float]:
    return [_f(feat.get(k)) for k in FEATURE_KEYS]


def _forward_label(closes: Sequence[float], i: int) -> Optional[float]:
    """0.4*次日 + 0.6*三日收益（%）。"""
    if i + 3 >= len(closes) or closes[i] <= 0:
        return None
    r1 = (closes[i + 1] / closes[i] - 1.0) * 100.0
    r3 = (closes[i + 3] / closes[i] - 1.0) * 100.0
    return 0.4 * r1 + 0.6 * r3


def build_training_matrix(
    bars_by_code: Dict[str, Sequence[Any]],
    *,
    max_per_code: int = 40,
) -> Tuple[List[List[float]], List[float]]:
    xs: List[List[float]] = []
    ys: List[float] = []
    for _code, bars in (bars_by_code or {}).items():
        if not bars or len(bars) < 25:
            continue
        ohlcv = [_bar_ohlcv(b) for b in bars]
        closes = [x[3] for x in ohlcv]
        # 末尾留给标签；从足够长的位置往前取
        last_feat_i = len(bars) - 4  # need i+3
        first_i = 20
        idxs = list(range(first_i, last_feat_i + 1))
        if len(idxs) > max_per_code:
            idxs = idxs[-max_per_code:]
        for i in idxs:
            feat = features_from_bars(bars, end_idx=i)
            y = _forward_label(closes, i)
            if feat is None or y is None:
                continue
            # 裁掉极端涨停/跌停日标签噪声
            if y > 18 or y < -15:
                continue
            xs.append(feature_vector(feat))
            ys.append(y)
    return xs, ys


def _heuristic_score(feat: Dict[str, float], live: Optional[Dict[str, float]] = None) -> float:
    """无模型时的可解释线性分（大致对齐短持预期）。"""
    s = 0.0
    # 温和涨、未贴高、量能正常偏优；直拉/贴高扣分
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
    items: List[Tuple[str, float, float]] = []
    checks = [
        ("近1日涨跌", _f(feat.get("ret_1")), max(0.0, 2.5 - abs(_f(feat.get("ret_1")) - 1.0)) * 0.35),
        ("近5日涨跌", _f(feat.get("ret_5")), -max(0.0, _f(feat.get("ret_5")) - 12.0) * 0.35),
        ("距10日高%", _f(feat.get("pct_from_high10")), max(0.0, -_f(feat.get("pct_from_high10")) - 2.0) * 0.15),
        ("开盘位置", _f(feat.get("open_pos")), -max(0.0, _f(feat.get("open_pos")) - 0.55) * 4.0),
        ("上影占比", _f(feat.get("upper_wick")), max(0.0, 0.45 - _f(feat.get("upper_wick"))) * 2.0),
        ("连阳天数", _f(feat.get("yang_streak")), -min(_f(feat.get("yang_streak")), 5) * 0.35),
        ("连阴天数", _f(feat.get("yin_streak")), min(_f(feat.get("yin_streak")), 3) * 0.4),
        ("量比vs5日", _f(feat.get("vol_ratio5"), 1.0), (
            0.8 if 0.7 <= _f(feat.get("vol_ratio5"), 1.0) <= 1.8
            else (-1.2 if _f(feat.get("vol_ratio5"), 1.0) >= 2.8 else 0.0)
        )),
    ]
    items.extend(checks)
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


def train_short_gbdt(
    bars_by_code: Dict[str, Sequence[Any]],
    *,
    min_samples: int = 180,
    persist: bool = True,
) -> Dict[str, Any]:
    """训练并可选落盘；样本不足返回 heuristic 标记。"""
    xs, ys = build_training_matrix(bars_by_code)
    meta: Dict[str, Any] = {
        "n_samples": len(ys),
        "n_codes": len(bars_by_code or {}),
        "trained_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "backend": "heuristic",
    }
    if len(ys) < min_samples:
        meta["ok"] = False
        meta["why"] = f"样本{len(ys)}<{min_samples}，沿用启发式/旧模型"
        return meta

    try:
        import numpy as np
        from sklearn.ensemble import HistGradientBoostingRegressor
        from sklearn.model_selection import train_test_split
    except Exception as exc:  # noqa: BLE001
        meta["ok"] = False
        meta["why"] = f"sklearn不可用:{exc}"
        return meta

    X = np.asarray(xs, dtype=float)
    y = np.asarray(ys, dtype=float)
    medians = np.median(X, axis=0)
    try:
        Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=0.2, random_state=42)
    except Exception:
        Xtr, ytr, Xte, yte = X, y, X[:1], y[:1]

    model = HistGradientBoostingRegressor(
        max_depth=4,
        max_iter=120,
        learning_rate=0.06,
        min_samples_leaf=20,
        l2_regularization=0.1,
        random_state=42,
    )
    model.fit(Xtr, ytr)
    try:
        pred = model.predict(Xte)
        mae = float(np.mean(np.abs(pred - yte)))
        # 简易方向准确率
        hit = float(np.mean((pred > 0) == (yte > 0)))
    except Exception:
        mae, hit = None, None

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
    return payload["meta"]


def _load_model() -> Tuple[Any, Optional[List[float]], Dict[str, Any]]:
    if _MODEL_CACHE.get("model") is not None:
        return _MODEL_CACHE["model"], _MODEL_CACHE.get("medians"), {"cached": True}
    if not MODEL_PATH.exists():
        return None, None, {"missing": True}
    try:
        import joblib

        payload = joblib.load(MODEL_PATH)
        if isinstance(payload, dict) and "model" in payload:
            _MODEL_CACHE["model"] = payload["model"]
            _MODEL_CACHE["medians"] = payload.get("medians")
            return payload["model"], payload.get("medians"), payload.get("meta") or {}
        _MODEL_CACHE["model"] = payload
        return payload, None, {}
    except Exception as exc:  # noqa: BLE001
        return None, None, {"load_err": str(exc)}


def ensure_short_model(
    bars_by_code: Dict[str, Sequence[Any]],
    *,
    retrain: bool = True,
    max_age_sec: float = 6 * 3600,
) -> Dict[str, Any]:
    """刷新时调用：有新样本则重训；否则加载盘中模型。"""
    need = True
    if MODEL_PATH.exists() and not retrain:
        need = False
    elif MODEL_PATH.exists():
        age = time.time() - MODEL_PATH.stat().st_mtime
        if age < max_age_sec and _MODEL_CACHE.get("model") is not None:
            need = False
        elif age < max_age_sec:
            m, med, meta = _load_model()
            if m is not None:
                return {"ok": True, "backend": "cached_file", **(meta or {})}
    if need and bars_by_code:
        meta = train_short_gbdt(bars_by_code, persist=True)
        if meta.get("ok"):
            return meta
        # 训练失败仍尝试旧模型
        m, _, old = _load_model()
        if m is not None:
            return {"ok": True, "backend": "stale_file", "train": meta, **(old or {})}
        return meta
    m, _, meta = _load_model()
    return {"ok": m is not None, "backend": "file" if m else "heuristic", **(meta or {})}


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
        # 该特征相对中性值的贡献 ≈ base - p2
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
) -> Dict[str, Any]:
    """对单票打 ML 分 + 因子贡献。

    返回:
      ml_score: 预测混合收益（%），越高越偏多
      backend: hist_gbdt / heuristic
      因子贡献: 解释短句
      contribs: 明细
    """
    feat = features_from_bars(bars)
    live_f = None
    if live:
        live_f = {k: _f(live.get(k)) for k, _ in LIVE_SPEC}
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
            # 盘中加成：小权重，避免盖过价量结构
            live_boost = 0.0
            if live_f:
                live_boost = (
                    min(max(live_f.get("board_pct", 0.0), -2), 3) * 0.08
                    + min(max(live_f.get("rs", 0.0), -3), 3) * 0.06
                    + min(max(live_f.get("flow", 0.0), -1), 5) * 0.05
                    + min(live_f.get("news_hits", 0.0), 3) * 0.12
                    + max(min(live_f.get("risk_score", 0.0), 30), -30) * 0.01
                )
            score = pred + live_boost
            backend = "hist_gbdt"
            med = medians or [0.0] * len(FEATURE_KEYS)
            cons = _contrib_by_shap(model, x) or _contrib_by_median_replace(
                model, x, med
            )
            # 拼接盘中因子到解释
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
    rule_w: float = 0.55,
    ml_w: float = 0.45,
) -> float:
    """规则短线分与 ML 分融合。ML 以「预期%」量级，线性压到与规则分相近。"""
    # ml_score 常见约 -3～+4；映射到规则分尺度
    ml_scaled = 4.0 + float(ml_score) * 1.15
    return rule_w * float(rule_score) + ml_w * ml_scaled
