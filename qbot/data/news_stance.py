# -*- coding: utf-8 -*-
"""新闻五档多空：重大利好 / 普通利好 / 不相关 / 普通利空 / 重大利空。

设计目标（对齐短线候选门）：
- 禁止「只要有新闻就算 has_cat」；只有利好档才算催化
- 重大利空硬否买入候选；普通利空禁止当催化，并否决 E 止跌类 buy_ok
- 优先大模型（本机 Cursor 登录态）判多空；关键词仅作硬兜底与 LLM 失败回退
- 磁盘缓存标题→档位，避免每次刷新重复打模型
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# 五档（对外中文）
TIER_MAJOR_BULL = "重大利好"
TIER_BULL = "普通利好"
TIER_IRREL = "不相关"
TIER_BEAR = "普通利空"
TIER_MAJOR_BEAR = "重大利空"

TIERS = (
    TIER_MAJOR_BULL,
    TIER_BULL,
    TIER_IRREL,
    TIER_BEAR,
    TIER_MAJOR_BEAR,
)

# 分值：用于风险/概率加减与汇总
TIER_SCORE = {
    TIER_MAJOR_BULL: 2.0,
    TIER_BULL: 1.0,
    TIER_IRREL: 0.0,
    TIER_BEAR: -1.0,
    TIER_MAJOR_BEAR: -2.0,
}

CACHE_PATH = (
    Path(__file__).resolve().parents[1] / "gui" / "csv" / "news_stance_cache.json"
)

# 硬利空：命中则不得标成利好（关键词兜底）
_HARD_BEAR_KW = (
    "去宁化",
    "去宁德",
    "切换供应商",
    "切换宁德",
    "替代宁德",
    "分流",
    "减持",
    "清仓式减持",
    "立案",
    "调查",
    "处罚",
    "问询",
    "违约",
    "爆雷",
    "造假",
    "退市",
    "预亏",
    "业绩预减",
    "不及预期",
    "下调评级",
    "指引下调",
    "终止合作",
    "取消订单",
    "无订单",
    "澄清不实",
    "风险警示",
    "ST",
    "跌停",
    "闪崩",
    "市值蒸发",
    "创一年新低",
    "创年内新低",
    "回购尚未实施",
    "尚未实施股份回购",
)

# 客户切电池等组合利空（需同时沾边）
_BEAR_COMBO = (
    (("理想", "小米", "华为", "车企"), ("自研电池", "自研电芯", "切电池", "换电芯", "欣旺达", "中创新航")),
    (("核心客户", "大客户"), ("流失", "切换", "分流", "去宁")),
)

_HARD_BULL_KW = (
    "超预期",
    "大幅增长",
    "扭亏",
    "获批",
    "核准",
    "量产",
    "中标",
    "签署合同",
    "重大合同",
    "订单饱满",
    "涨价",
    "提价",
    "回购注销",
    "增持",
    "国常会",
)

_SOFT_BEAR_KW = (
    "担忧",
    "承压",
    "走弱",
    "下滑",
    "放缓",
    "降温",
    "减持计划",
    "竞争加剧",
    "价格战",
    "产能过剩",
)

_SOFT_BULL_KW = (
    "增长",
    "突破",
    "创新高",
    "流入",
    "景气",
    "扩产",
    "合作",
    "投资",
)

_CACHE: Dict[str, Any] = {"mtime": None, "map": {}}
_LLM_ENABLED_DEFAULT = True


def llm_enabled() -> bool:
    v = str(os.environ.get("QBOT_NEWS_STANCE_LLM", "1") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _norm_tier(raw: Any) -> str:
    s = str(raw or "").strip()
    if s in TIERS:
        return s
    # 容错别名
    aliases = {
        "重大利多": TIER_MAJOR_BULL,
        "利好": TIER_BULL,
        "普通利多": TIER_BULL,
        "中性": TIER_IRREL,
        "无关": TIER_IRREL,
        "利空": TIER_BEAR,
        "普通利空": TIER_BEAR,
        "重大利空": TIER_MAJOR_BEAR,
        "major_bull": TIER_MAJOR_BULL,
        "bull": TIER_BULL,
        "irrel": TIER_IRREL,
        "irrelevant": TIER_IRREL,
        "bear": TIER_BEAR,
        "major_bear": TIER_MAJOR_BEAR,
    }
    return aliases.get(s, TIER_IRREL)


def _title_key(title: str, *, stock: str = "") -> str:
    t = re.sub(r"\s+", "", str(title or "").strip())
    sk = str(stock or "").strip()
    raw = f"{sk}||{t}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def _load_cache() -> Dict[str, Any]:
    try:
        if not CACHE_PATH.exists():
            return {}
        mtime = CACHE_PATH.stat().st_mtime
        if _CACHE.get("map") is not None and _CACHE.get("mtime") == mtime:
            return _CACHE.get("map") or {}
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        m = data if isinstance(data, dict) else {}
        _CACHE["map"] = m
        _CACHE["mtime"] = mtime
        return m
    except Exception:
        return {}


def _save_cache(m: Dict[str, Any]) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(
            json.dumps(m, ensure_ascii=False, indent=0),
            encoding="utf-8",
        )
        _CACHE["map"] = m
        _CACHE["mtime"] = time.time()
    except Exception:
        pass


def _keyword_tier(title: str) -> Tuple[str, str]:
    """关键词兜底：硬利空优先于硬利好，避免利空标成利好。"""
    t = str(title or "")
    if not t:
        return TIER_IRREL, "空标题"

    hard_bear = [k for k in _HARD_BEAR_KW if k in t]
    for lefts, rights in _BEAR_COMBO:
        if any(a in t for a in lefts) and any(b in t for b in rights):
            hard_bear.append("客户切供/自研电池组合")
            break
    if hard_bear:
        # 市值蒸发+大跌叙事、去宁化等 → 重大
        majorish = any(
            k in t
            for k in (
                "去宁化",
                "去宁德",
                "立案",
                "退市",
                "爆雷",
                "造假",
                "跌停",
                "市值蒸发",
                "创一年新低",
                "创一年新低",
            )
        ) or ("客户切供" in " ".join(hard_bear))
        if majorish:
            return TIER_MAJOR_BEAR, f"硬利空({','.join(hard_bear[:3])})"
        return TIER_BEAR, f"利空词({','.join(hard_bear[:3])})"

    soft_bear = [k for k in _SOFT_BEAR_KW if k in t]
    hard_bull = [k for k in _HARD_BULL_KW if k in t]
    # 同时有软空+硬多 → 偏中性，宁缺毋滥
    if soft_bear and hard_bull:
        return TIER_IRREL, "利好利空并存，按不相关"
    if hard_bull:
        if any(k in t for k in ("超预期", "获批", "核准", "重大合同", "涨价", "提价")):
            return TIER_MAJOR_BULL, f"硬利好({','.join(hard_bull[:3])})"
        return TIER_BULL, f"利好词({','.join(hard_bull[:3])})"
    if soft_bear:
        return TIER_BEAR, f"偏空词({','.join(soft_bear[:3])})"
    soft_bull = [k for k in _SOFT_BULL_KW if k in t]
    if soft_bull and not soft_bear:
        return TIER_BULL, f"偏多词({','.join(soft_bull[:3])})"
    return TIER_IRREL, "无明确多空"


def classify_title_keyword(title: str) -> Dict[str, Any]:
    tier, why = _keyword_tier(title)
    return {
        "tier": tier,
        "score": float(TIER_SCORE[tier]),
        "why": why,
        "source": "keyword",
    }


def _parse_llm_json(text: str) -> List[Dict[str, Any]]:
    s = str(text or "").strip()
    if not s:
        return []
    # 抽第一个 JSON 数组
    m = re.search(r"\[[\s\S]*\]", s)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return []
    if not isinstance(arr, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        out.append(item)
    return out


def _llm_classify_batch(
    titles: Sequence[str],
    *,
    stock_hint: str = "",
) -> Dict[int, Dict[str, Any]]:
    """一次请求判一批标题；失败返回空 dict。"""
    clean = [str(t or "").strip()[:120] for t in titles]
    if not clean:
        return {}
    try:
        from qbot.ai.cursor_ide_chat import chat as ide_chat
    except Exception:
        return {}

    lines = "\n".join(f"{i}. {t}" for i, t in enumerate(clean))
    stock_line = f"标的语境：{stock_hint}\n" if stock_hint else ""
    prompt = (
        "你是A股短线新闻多空标注器。对下列标题逐条判定五档之一：\n"
        "重大利好 / 普通利好 / 不相关 / 普通利空 / 重大利空\n"
        "规则：\n"
        "1) 客户流失、去宁化、减持、立案、不及预期、大跌叙事等偏空；\n"
        "2) 超预期业绩、获批量产、涨价、重大订单等偏多；\n"
        "3) 软广、市值排名、复盘、含糊宏观=不相关；\n"
        "4) 有硬利空词时绝不能标成利好；\n"
        "5) 只输出 JSON 数组，元素含 i(序号)、tier、why(极短)。\n"
        f"{stock_line}"
        f"标题列表：\n{lines}"
    )
    try:
        text = ide_chat(
            [{"role": "user", "content": prompt}],
            model="default",
        )
    except Exception:
        return {}
    parsed = _parse_llm_json(text)
    out: Dict[int, Dict[str, Any]] = {}
    for item in parsed:
        try:
            i = int(item.get("i"))
        except Exception:
            continue
        if i < 0 or i >= len(clean):
            continue
        tier = _norm_tier(item.get("tier"))
        # LLM 若把硬利空标成利好，用关键词纠正
        kw_tier, kw_why = _keyword_tier(clean[i])
        if TIER_SCORE[kw_tier] < 0 and TIER_SCORE[tier] > 0:
            tier = kw_tier
            why = f"LLM纠偏→{kw_why}"
        else:
            why = str(item.get("why") or "llm")
        out[i] = {
            "tier": tier,
            "score": float(TIER_SCORE[tier]),
            "why": why,
            "source": "llm",
        }
    return out


def classify_titles(
    titles: Sequence[str],
    *,
    stock_hint: str = "",
    use_llm: Optional[bool] = None,
) -> List[Dict[str, Any]]:
    """批量分类；带缓存。use_llm 默认读环境变量。"""
    use_llm = llm_enabled() if use_llm is None else bool(use_llm)
    cache = _load_cache()
    results: List[Optional[Dict[str, Any]]] = [None] * len(titles)
    need_llm: List[Tuple[int, str]] = []

    for i, title in enumerate(titles):
        t = str(title or "").strip()
        if not t:
            results[i] = {
                "tier": TIER_IRREL,
                "score": 0.0,
                "why": "空",
                "source": "empty",
                "title": "",
            }
            continue
        key = _title_key(t, stock=stock_hint)
        hit = cache.get(key)
        if isinstance(hit, dict) and hit.get("tier") in TIERS:
            results[i] = {
                "tier": hit["tier"],
                "score": float(TIER_SCORE[hit["tier"]]),
                "why": str(hit.get("why") or "cache"),
                "source": str(hit.get("source") or "cache"),
                "title": t[:80],
            }
            continue
        # 硬利空关键词直接落盘，不必等 LLM（防飞刀优先）
        kw_tier, kw_why = _keyword_tier(t)
        if kw_tier == TIER_MAJOR_BEAR or (
            kw_tier == TIER_BEAR and any(k in t for k in ("去宁化", "减持", "立案", "跌停"))
        ):
            pack = {
                "tier": kw_tier,
                "score": float(TIER_SCORE[kw_tier]),
                "why": kw_why,
                "source": "keyword",
                "title": t[:80],
            }
            results[i] = pack
            cache[key] = {
                "tier": kw_tier,
                "why": kw_why,
                "source": "keyword",
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            continue
        if use_llm:
            need_llm.append((i, t))
        else:
            pack = {
                "tier": kw_tier,
                "score": float(TIER_SCORE[kw_tier]),
                "why": kw_why,
                "source": "keyword",
                "title": t[:80],
            }
            results[i] = pack
            cache[key] = {
                "tier": kw_tier,
                "why": kw_why,
                "source": "keyword",
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            }

    # LLM 分批
    if need_llm and use_llm:
        batch_size = 12
        for start in range(0, len(need_llm), batch_size):
            chunk = need_llm[start : start + batch_size]
            idxs = [x[0] for x in chunk]
            texts = [x[1] for x in chunk]
            llm_map = _llm_classify_batch(texts, stock_hint=stock_hint)
            for j, idx in enumerate(idxs):
                t = texts[j]
                if j in llm_map:
                    pack = dict(llm_map[j])
                else:
                    tier, why = _keyword_tier(t)
                    pack = {
                        "tier": tier,
                        "score": float(TIER_SCORE[tier]),
                        "why": why,
                        "source": "keyword_fallback",
                    }
                pack["title"] = t[:80]
                results[idx] = pack
                cache[_title_key(t, stock=stock_hint)] = {
                    "tier": pack["tier"],
                    "why": pack.get("why"),
                    "source": pack.get("source"),
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
        _save_cache(cache)
    elif cache:
        _save_cache(cache)

    out: List[Dict[str, Any]] = []
    for i, r in enumerate(results):
        if r is None:
            t = str(titles[i] or "")
            tier, why = _keyword_tier(t)
            r = {
                "tier": tier,
                "score": float(TIER_SCORE[tier]),
                "why": why,
                "source": "keyword",
                "title": t[:80],
            }
        out.append(r)
    return out


def classify_title(title: str, *, stock_hint: str = "", use_llm: Optional[bool] = None) -> Dict[str, Any]:
    return classify_titles([title], stock_hint=stock_hint, use_llm=use_llm)[0]


def aggregate_stances(
    packs: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """汇总多条新闻 → 催化门/否决/打分用。"""
    if not packs:
        return {
            "tier": TIER_IRREL,
            "score": 0.0,
            "bull_n": 0,
            "bear_n": 0,
            "major_bull": False,
            "major_bear": False,
            "has_bull_cat": False,
            "hard_veto_buy": False,
            "block_stabilize": False,
            "why": "无新闻",
            "details": [],
        }

    scores = [float(p.get("score") or 0.0) for p in packs]
    tiers = [str(p.get("tier") or TIER_IRREL) for p in packs]
    bull_n = sum(1 for s in scores if s > 0)
    bear_n = sum(1 for s in scores if s < 0)
    major_bull = TIER_MAJOR_BULL in tiers
    major_bear = TIER_MAJOR_BEAR in tiers
    # 最差档优先（防飞刀）
    worst = min(scores) if scores else 0.0
    best = max(scores) if scores else 0.0
    if major_bear or worst <= -2.0:
        tier = TIER_MAJOR_BEAR
    elif worst <= -1.0:
        tier = TIER_BEAR
    elif major_bull or best >= 2.0:
        tier = TIER_MAJOR_BULL
    elif best >= 1.0:
        tier = TIER_BULL
    else:
        tier = TIER_IRREL

    has_bull_cat = bull_n > 0 and not major_bear
    # 有明确利空时，利好不能单独当催化过银之杰门
    if bear_n > 0 and not major_bull:
        has_bull_cat = major_bull  # 仅重大利好可对冲普通利空的「无催化」；重大利空仍否

    hard_veto = bool(major_bear)
    block_stabilize = bool(major_bear or (bear_n > 0 and worst < 0))

    whys = []
    for p in packs[:4]:
        whys.append(f"{p.get('tier')}:{str(p.get('title') or '')[:28]}")

    return {
        "tier": tier,
        "score": float(TIER_SCORE[tier]),
        "sum_score": float(sum(scores)),
        "bull_n": bull_n,
        "bear_n": bear_n,
        "major_bull": major_bull,
        "major_bear": major_bear,
        "has_bull_cat": bool(has_bull_cat and bull_n > 0 and not hard_veto),
        "hard_veto_buy": hard_veto,
        "block_stabilize": block_stabilize,
        "why": "；".join(whys) if whys else tier,
        "details": list(packs),
    }


def risk_delta_from_aggregate(agg: Dict[str, Any]) -> Tuple[float, List[str]]:
    """映射到风险值修正。"""
    why: List[str] = []
    delta = 0.0
    bull_n = int(agg.get("bull_n") or 0)
    bear_n = int(agg.get("bear_n") or 0)
    if agg.get("major_bull"):
        delta += 8.0
        why.append("重大利好新闻 +8")
    elif bull_n:
        add = min(4.0, 2.0 * bull_n)
        delta += add
        why.append(f"普通利好×{bull_n} +{add:.0f}")
    if agg.get("major_bear"):
        delta -= 15.0
        why.append("重大利空新闻 -15")
    elif bear_n:
        sub = min(10.0, 4.0 * bear_n)
        delta -= sub
        why.append(f"普通利空×{bear_n} -{sub:.0f}")
    if delta > 8.0:
        delta = 8.0
    if delta < -18.0:
        delta = -18.0
    return delta, why


def filter_bullish_titles(titles: Sequence[str], packs: Sequence[Dict[str, Any]]) -> List[str]:
    """只保留利好档标题（主题质量加分用）。"""
    out: List[str] = []
    for t, p in zip(titles, packs):
        if float(p.get("score") or 0.0) > 0:
            out.append(str(t))
    return out
