# -*- coding: utf-8 -*-
"""每日新闻大事：近 1～3 日硬叙事催化 → 分栏 + 相关板块利好/利空。

初衷：提前看见可交易叙事，而不是A股事后行情战报。
保留示例：英伟达财报/大会、科技重大发明与量产、厄尔尼诺/粮价、战争→军工航天、金价突破；
以及美股/亚太盘前隔夜（费城半导体、纳指涨跌）——作A股开盘参考。
丢掉示例：凯莱英冲高近X%、摩尔跌多少、境内ETF标的指数涨跌软广、A股板块午后纷纷拉升。

启动默认刷近 3 天（优先当天），写入 json + html，供 GUI 网页版面展示。
"""
from __future__ import annotations

import html
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from qbot.data.theme_news import (
    fetch_cross_platform_theme_news,
    within_lookback,
)

DIGEST_DAYS = 3
LATEST_PATH = (
    Path(__file__).resolve().parents[1] / "gui" / "csv" / "daily_news_digest.json"
)
HTML_PATH = (
    Path(__file__).resolve().parents[1] / "gui" / "csv" / "daily_news_digest.html"
)

# 展示分栏顺序（用户关心的主线都单独成栏）
CATEGORY_ORDER = [
    "硬科技",
    "AI应用",
    "贵金属",
    "医药生物",
    "新能源电力",
    "军工航天",
    "消费农业",
    "宏观美股",
    "其他",
]

# 标题关键词 → 分栏（先匹配先生效）
_CATEGORY_RULES: List[Tuple[str, Tuple[str, ...]]] = (
    (
        "硬科技",
        (
            "芯片", "半导体", "光模块", "CPO", "光通信", "硅光", "HBM", "存储",
            "先进封装", "液冷", "服务器", "PCB", "覆铜板", "算力", "数据中心",
            "交换机", "英伟达", "NVIDIA", "戴尔", "Dell", "Rubin", "GB300",
            "金刚石", "培育钻石", "热沉", "散热", "中际旭创", "酷冷",
        ),
    ),
    (
        "AI应用",
        (
            "AIGC", "短剧", "人工智能应用", "大模型", "OpenAI", "Anthropic",
            "Hugging Face", "机器人", "具身", "AI语料", "数字媒体", "软件",
            "办公软件", "金山",
        ),
    ),
    (
        "贵金属",
        ("黄金", "白银", "金价", "贵金属", "避险", "期金", "COMEX"),
    ),
    (
        "医药生物",
        (
            "创新药", "医药", "CXO", "医保", "药企", "生物药", "临床", "获批",
            "ADC", "GLP-1", "减肥药", "疫苗", "凯莱英", "药明",
        ),
    ),
    (
        "新能源电力",
        (
            "光伏", "新能源", "核电", "电网", "电力", "储能", "风电", "逆变器",
            "特高压", "变压器",
        ),
    ),
    (
        "军工航天",
        (
            "军工", "国防", "航天", "卫星", "低空", "导弹", "雷达", "商业航天", "星网",
            "战争", "冲突", "袭击", "霍尔木兹", "制裁", "开战",
        ),
    ),
    (
        "消费农业",
        (
            "白酒", "消费", "零售", "农业", "种业", "生猪", "饲料", "乳业", "旅游",
            "厄尔尼诺", "拉尼娜", "粮价", "粮食", "大豆", "玉米", "小麦", "棉花",
        ),
    ),
    (
        "宏观美股",
        (
            "美联储", "美股", "标普", "纳斯达克", "纳指", "道指", "非农", "CPI", "降息",
            "加息", "美元", "美债", "特斯拉", "SpaceX", "财报", "指引",
            "费城半导体", "隔夜", "盘前", "日经", "韩股", "台股", "亚太",
        ),
    ),
)

# 新闻 → 相关板块映射（客观标签，不预测涨跌）
_BOARD_MAP: List[Tuple[str, Tuple[str, ...]]] = (
    ("液冷服务器", ("液冷", "冷板", "浸没式", "酷冷", "CDU")),
    ("CPO/光模块", ("CPO", "光模块", "光通信", "硅光", "中际旭创", "共封装")),
    ("国产服务器", ("服务器", "算力", "浪潮", "紫光")),
    ("培育钻石/金刚石散热", ("培育钻石", "金刚石", "热沉", "金刚石散热")),
    ("半导体", ("芯片", "半导体", "先进封装", "HBM", "存储")),
    ("PCB/覆铜板", ("PCB", "覆铜板", "HDI")),
    ("人形机器人", ("机器人", "具身")),
    ("短剧/AIGC", ("短剧", "AIGC", "AI语料")),
    ("AI应用软件", ("办公软件", "金山", "大模型应用")),
    ("黄金/贵金属", ("黄金", "白银", "金价", "贵金属", "期金")),
    ("创新药/CXO", ("创新药", "CXO", "医保", "凯莱英", "药明", "ADC", "GLP")),
    ("光伏", ("光伏", "硅料", "组件", "逆变器")),
    ("核电/电网", ("核电", "电网", "特高压", "变压器")),
    ("军工/国防", ("军工", "国防", "导弹", "雷达", "战争", "袭击", "制裁", "开战")),
    ("商业航天", ("航天", "卫星", "星网", "低空")),
    ("电力/发电", ("电力", "火电", "水电", "发电", "电价", "华电", "大唐")),
    ("农业/种植", ("农业", "种业", "粮食", "生猪", "厄尔尼诺", "拉尼娜", "粮价", "大豆", "玉米", "小麦")),
    ("美股科技/算力链", (
        "戴尔", "Dell", "英伟达", "NVIDIA", "Rubin", "美股", "纳斯达克", "纳指",
        "费城半导体", "美国半导体", "美股芯片", "美股科技",
    )),
)

# 板块 → 前瞻主题 id（取种子作强相关个股）
_BOARD_THEME_IDS: Dict[str, Tuple[str, ...]] = {
    "液冷服务器": ("liquid_cooling",),
    "CPO/光模块": ("cpo_optical", "fiber_cable"),
    "国产服务器": ("domestic_server",),
    "培育钻石/金刚石散热": ("lab_diamond",),
    "半导体": ("memory_storage", "semi_materials", "semi_equipment"),
    "PCB/覆铜板": ("pcb_ccl",),
    "人形机器人": ("humanoid_robot",),
    "短剧/AIGC": ("short_drama_aigc",),
    "AI应用软件": ("ai_app_soft",),
    "黄金/贵金属": ("precious_metals",),
    "创新药/CXO": ("innovative_drug",),
    "光伏": ("pv_solar",),
    "核电/电网": ("nuclear_power", "grid_power"),
    "电力/发电": ("nuclear_power",),
    "军工/国防": ("defense_military",),
    "商业航天": ("aerospace",),
    "农业/种植": ("agriculture",),
    "美股科技/算力链": ("liquid_cooling", "cpo_optical", "domestic_server"),
}

_STANCE_SCORE = {
    "利好": 2.0,
    "中性偏多": 1.0,
    "中性": 0.0,
    "中性偏空": -1.0,
    "利空": -2.0,
}

# 硬利空：命中即不得标成利好/中性偏多（避免「跌停+新高误匹配」类反标）
_HARD_BEAR = (
    "跌停",
    "暴跌",
    "闪崩",
    "崩盘",
    "立案",
    "退市",
    "净流出",
    "不及预期",
    "下调评级",
    "指引下调",
    "业绩变脸",
    "预亏",
    "巨亏",
    "爆雷",
    "造假",
    "处罚",
    "违约",
    "裁员",
    "终止收购",
    "终止重大资产",
    "无订单",
    "尚未形成订单",
    "暂未形成订单",
    "提示风险",
    "风险提示",
    "注意风险",
    "创上市以来新低",
    "创历史新低",
    "阶段新低",
    "新低",
)

# 硬利好：无硬利空并存时，可直接偏多
_HARD_BULL = (
    "超预期",
    "获批上市",
    "新药获批",
    "药品获批",
    "器械获批",
    "指引上调",
    "上调评级",
    "首次覆盖",
    "创历史新高",
    "量产",
    "中标",
    "扩产",
)

_BULL = (
    "超预期",
    "大涨",
    "创历史新高",
    "阶段新高",
    "获批",
    "放量",
    "涨价",
    "突破",
    "上调",
    "指引上调",
    "净利增",
    "营收增",
    "收购",
    "合作",
    "量产",
    "交付",
    "中标",
    "扩产",
    "景气",
    "利好",
    "签署",
    "订单",
)
_BEAR = (
    "不及预期",
    "下调",
    "减持",
    "质押",
    "亏损",
    "跌停",
    "暴跌",
    "核查",
    "立案",
    "警示",
    "风险提示",
    "提示风险",
    "澄清",
    "尚未形成",
    "无订单",
    "终止",
    "推迟",
    "裁员",
    "违约",
    "处罚",
    "退市",
    "利空",
    "净流出",
    "新低",
    "预亏",
    "爆雷",
)
_NOISE = (
    "官方售价",
    "贴水",
    "溢价",
    "LME期",
    "抵押贷款利率",
    "酒类广告",
)

# 硬叙事：应提前看见的催化（打分加权；与事后涨跌战报对立）
_HARD_NARRATIVE = (
    "英伟达",
    "NVIDIA",
    "财报",
    "业绩指引",
    "指引上调",
    "指引下调",
    "不及预期",
    "超预期",
    "Vera Rubin",
    "Rubin",
    "量产",
    "发明",
    "专利",
    "突破",
    "新品发布",
    "芯片管制",
    "厄尔尼诺",
    "拉尼娜",
    "粮价",
    "粮食危机",
    "战争",
    "开战",
    "冲突升级",
    "导弹袭击",
    "袭击",
    "霍尔木兹",
    "制裁",
    "金价",
    "现货黄金",
    "美元/盎司",
    "COMEX",
    "涨价",
    "提价",
    "缺货",
    "国常会",
    "核准",
    "发改委",
    "扩产",
    "获批上市",
    "新药获批",
)

# 低信息量公司日历/资本运作：不是硬叙事（由 _is_digest_fluff 组合判断）
_DIGEST_FLUFF_MEETING = (
    "业绩说明会",
    "集体业绩说明会",
    "参加科创板",
)
_DIGEST_FLUFF_OPS = (
    "销售生猪",
    "生猪销量",
    "生猪销售",
)
_DIGEST_FLUFF_BOARD_CHASE = (
    "两连板",
    "三连板",
    "严重异常波动",
    "停牌核查",
)

_HARD_NARRATIVE_RE = re.compile(
    r"("
    r"现货黄金.{0,12}突破"
    r"|金价.{0,8}(突破|大涨|创)"
    r"|英伟达.{0,20}(财报|大会|量产|指引|Rubin)"
    r"|NVIDIA.{0,20}(earnings|guidance|Rubin)"
    r"|厄尔尼诺|拉尼娜"
    r"|(导弹|空袭|袭击).{0,16}(油轮|基地|港口|霍尔木兹)"
    r"|(染料|稀土|锂|铜|铝|钢材|硅料|玉米|大豆|小麦).{0,40}(涨价|提价|上调|上涨)"
    r"|价格加速上涨"
    r")"
)

# 子串伪阳性：出现否定式时，不计对应利好词
_BULL_NEGATIONS = (
    ("订单", ("无订单", "尚未形成订单", "暂未形成订单", "没有订单", "未获订单")),
    ("收购", ("终止收购", "取消收购", "收购失败", "未披露收购")),
    ("合作", ("终止合作", "取消合作")),
    ("上调", ("不及预期", "下调")),
    ("获批", ("未获批", "不予批准", "获批立项", "获批编制")),
    ("大涨", ("最大涨幅", "涨幅居", "涨幅靠")),
)


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _categorize(title: str) -> str:
    t = str(title or "")
    for cat, keys in _CATEGORY_RULES:
        if any(k in t for k in keys):
            return cat
    return "其他"


def _related_boards(title: str) -> List[str]:
    t = str(title or "")
    out: List[str] = []
    for board, keys in _BOARD_MAP:
        if any(k in t for k in keys):
            out.append(board)
        if len(out) >= 4:
            break
    return out


def _count_stance_hits(title: str) -> Tuple[int, int]:
    """统计多空词，处理否定式与涨停/跌停并存。"""
    t = str(title or "")
    bull = 0
    for k in _BULL:
        if k not in t:
            continue
        negated = False
        for pos, negs in _BULL_NEGATIONS:
            if k == pos and any(n in t for n in negs):
                negated = True
                break
        # 「新高」不得在「新低」标题里误计；已用独立词，这里防「历史新高」残留
        if k in ("创历史新高", "阶段新高") and "新低" in t:
            negated = True
        if not negated:
            bull += 1
    bear = sum(1 for k in _BEAR if k in t)
    # 涨停单独出现且夹带风险/澄清/跌停 → 不计多，并加强空
    if "涨停" in t:
        if any(k in t for k in ("跌停", "提示风险", "风险提示", "澄清", "立案", "亏损")):
            bear += 1
        else:
            # 纯涨停偏弱多，最多 +1，避免盘面词主导
            bull += 1
    return bull, bear


def _stance(title: str) -> Tuple[str, str]:
    """客观多空：利好 / 利空 / 中性偏多 / 中性偏空 / 中性。"""
    t = str(title or "")
    hard_bear = [k for k in _HARD_BEAR if k in t]
    hard_bull = [k for k in _HARD_BULL if k in t]
    # 硬利空优先：跌停/立案/新低/无订单等绝不能标成利好
    if hard_bear:
        # 仅当硬利好多且无跌停/立案/新低等极端词时，才允许对冲为中性
        extreme = any(
            k in t
            for k in (
                "跌停",
                "暴跌",
                "立案",
                "退市",
                "新低",
                "无订单",
                "终止收购",
                "提示风险",
                "风险提示",
            )
        )
        if extreme or not hard_bull:
            why = f"标题含硬利空（{'/'.join(hard_bear[:3])}），不作利好"
            if len(hard_bear) >= 2 or extreme:
                return "利空", why
            return "中性偏空", why
    bull, bear = _count_stance_hits(t)
    if any(k in t for k in ("尚未形成", "暂未形成", "未对公司", "注意风险")):
        bear += 2
    if hard_bull and not hard_bear and bull >= bear:
        return "利好", f"标题含硬利好（{'/'.join(hard_bull[:3])}）"
    if bull > bear + 1:
        return "利好", "标题含超预期/获批/量产等偏多表述"
    if bear > bull + 1:
        return "利空", "标题含不及预期/减持/澄清无订单/风险提示等偏空表述"
    if bull > bear:
        return "中性偏多", "多空并存，偏多措辞略多"
    if bear > bull:
        return "中性偏空", "多空并存，偏空措辞略多"
    return "中性", "信息增量或方向不明，不作单边定性"


def _is_hard_narrative(title: str) -> bool:
    """英伟达财报、发明量产、厄尔尼诺/粮价、战争军工、金价等硬叙事。"""
    t = str(title or "")
    if not t:
        return False
    try:
        from qbot.data.industry_screener import news_title_is_overseas_premarket_ref

        if news_title_is_overseas_premarket_ref(t):
            return True
    except Exception:
        pass
    if _HARD_NARRATIVE_RE.search(t):
        return True
    hits = sum(1 for k in _HARD_NARRATIVE if k in t)
    return hits >= 2 or (
        hits >= 1
        and any(
            k in t
            for k in (
                "英伟达",
                "NVIDIA",
                "财报",
                "金价",
                "现货黄金",
                "厄尔尼诺",
                "霍尔木兹",
                "国常会",
                "量产",
                "涨价",
            )
        )
    )


def _is_digest_fluff(title: str) -> bool:
    """业绩说明会、解除质押、连板异动回应等：低信息量，不当大事。"""
    t = str(title or "")
    if not t:
        return True
    if _is_hard_narrative(t):
        return False
    if any(k in t for k in _DIGEST_FLUFF_MEETING):
        return True
    if any(k in t for k in _DIGEST_FLUFF_OPS):
        return True
    if "质押" in t and any(k in t for k in ("解除", "股票质押", "股权质押")):
        return True
    if any(k in t for k in _DIGEST_FLUFF_BOARD_CHASE):
        if any(k in t for k in ("澄清", "风险提示", "提示风险", "立案")):
            return False
        return True
    return False


def _importance(title: str, source: str, boards: List[str], stance: str) -> float:
    t = str(title or "")
    score = 0.0
    if boards:
        score += 2.5
    if stance in ("利好", "利空"):
        score += 1.5
    if stance.startswith("中性偏"):
        score += 0.5
    if source in ("财联社", "华尔街见闻", "央视新闻"):
        score += 1.0
    if _is_hard_narrative(t):
        score += 3.5
    elif any(k in t for k in ("英伟达", "戴尔", "Dell", "财报", "获批", "国常会", "涨价", "量产")):
        score += 1.5
    if _is_digest_fluff(t):
        score -= 4.0
    if any(k in t for k in _NOISE) and not boards:
        score -= 3.0
    if len(t) < 18:
        score -= 0.5
    return score


def _seeds_for_board(board: str, limit: int = 3) -> List[Dict[str, str]]:
    """从前瞻主题种子取中军兜底（最多 limit，有几只算几只）。"""
    try:
        from qbot.data.forward_watch import THEME_HINTS
    except Exception:
        return []
    ids = _BOARD_THEME_IDS.get(board) or ()
    out: List[Dict[str, str]] = []
    seen = set()
    for tid in ids:
        for h in THEME_HINTS:
            if str(h.get("id") or "") != tid:
                continue
            for pair in h.get("seed_stocks") or []:
                if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                    continue
                code, name = str(pair[0]).zfill(6)[-6:], str(pair[1])
                if code in seen:
                    continue
                seen.add(code)
                out.append({"代码": code, "名称": name})
                if len(out) >= limit:
                    return out
            break
    return out


def _seed_alias_catalog() -> List[Tuple[str, str, str]]:
    """全部主题种子： (匹配串, 代码, 全称)，按匹配串长度降序。"""
    try:
        from qbot.data.forward_watch import THEME_HINTS
    except Exception:
        return []
    rows: List[Tuple[str, str, str]] = []
    seen_pair = set()
    for h in THEME_HINTS:
        for pair in h.get("seed_stocks") or []:
            if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                continue
            code, name = str(pair[0]).zfill(6)[-6:], str(pair[1]).strip()
            if not code or not name:
                continue
            aliases = [name]
            short = name
            for suf in (
                "股份有限公司",
                "有限公司",
                "股份",
                "科技",
                "集团",
                "控股",
                "电子",
                "生物",
                "医药",
                "网络",
                "信息",
                "国际",
                "环境",
            ):
                if short.endswith(suf) and len(short) > len(suf) + 1:
                    short = short[: -len(suf)]
                    if len(short) >= 2:
                        aliases.append(short)
                    break
            for a in aliases:
                key = (a, code)
                if key in seen_pair or len(a) < 2:
                    continue
                seen_pair.add(key)
                rows.append((a, code, name))
    rows.sort(key=lambda x: (-len(x[0]), x[0]))
    return rows


# 标题点名：代码 / 【欧陆通： / 欧陆通(300870 / XX股份
_CODE_IN_TITLE_RE = re.compile(
    r"(?<!\d)([0-9]{6})(?:\.(?:SZ|SH|sz|sh))?",
    re.I,
)
_BRACKET_CO_RE = re.compile(r"【\s*([\u4e00-\u9fffA-Za-z0-9]{2,12})\s*[：:]")
_NAME_BEFORE_CODE_RE = re.compile(
    r"([\u4e00-\u9fff]{2,12})\s*[\(（]\s*[0-9]{6}(?:\.(?:SZ|SH))?\s*[\)）]",
    re.I,
)
_CO_SUFFIXES = (
    "股份有限公司",
    "有限公司",
    "股份",
    "科技",
    "通信",
    "电子",
    "医药",
    "生物",
    "电气",
    "网络",
    "信息",
    "材料",
    "集团",
    "微",
)
_CO_SUFFIX_RE = re.compile(
    "|".join(re.escape(s) for s in sorted(_CO_SUFFIXES, key=len, reverse=True))
)


def _iter_suffix_company_names(title: str) -> List[str]:
    """后缀处向左取短名，避免「豆包…长鑫科技」整段误吞。"""
    t = str(title or "")
    out: List[str] = []
    seen = set()
    for m in _CO_SUFFIX_RE.finditer(t):
        suf_start, end_i = m.start(), m.end()
        picked = ""
        for plen in (2, 3, 4, 5, 6):
            start_i = suf_start - plen
            if start_i < 0:
                continue
            name = t[start_i:end_i]
            if len(name) < 3:
                continue
            if not all('\u4e00' <= ch <= '\u9fff' for ch in name):
                continue
            picked = name
            break
        if picked and picked not in seen:
            seen.add(picked)
            out.append(picked)
    return out

# 新闻点名离线表（不依赖东财联想；避免 GUI 进程内失败缓存导致只剩种子股）
_OFFLINE_NAME_MAP: Dict[str, Tuple[str, str]] = {
    "长鑫科技": ("688825", "长鑫科技"),
    "长鑫": ("688825", "长鑫科技"),
    "康希通信": ("688653", "康希通信"),
    "欧陆通": ("300870", "欧陆通"),
    "江波龙": ("301308", "江波龙"),
    "君正股份": ("300223", "北京君正"),
    "北京君正": ("300223", "北京君正"),
    "卓胜微": ("300782", "卓胜微"),
    "唯捷创芯": ("688153", "唯捷创芯"),
    "慧智微": ("688512", "慧智微"),
    "国博电子": ("688375", "国博电子"),
    "集泰股份": ("002909", "集泰股份"),
}
_FOREIGN_NAME_BLOCK = {
    "三星电子", "三星", "英伟达", "NVIDIA", "戴尔", "Dell",
    "台积电", "苹果", "微软", "谷歌", "Google", "Arm", "ARM",
}

_RESOLVE_NAME_CACHE: Dict[str, Dict[str, str]] = {}


def _is_a_share_code(code: str) -> bool:
    c = str(code or "").zfill(6)[-6:]
    if len(c) != 6 or not c.isdigit():
        return False
    return c.startswith(("60", "68", "00", "30", "83", "87", "43"))


def _resolve_mentioned_name(name: str) -> Optional[Dict[str, str]]:
    """新闻点名 → A股代码；离线表优先，失败不缓存空结果。"""
    q = str(name or "").strip()
    if len(q) < 2:
        return None
    q = q.strip("《》【】[]（）() ·")
    if len(q) < 2:
        return None
    if q in _FOREIGN_NAME_BLOCK:
        return None
    if q in _OFFLINE_NAME_MAP:
        code, nm = _OFFLINE_NAME_MAP[q]
        return {"代码": code, "名称": nm}
    if q in _RESOLVE_NAME_CACHE:
        return _RESOLVE_NAME_CACHE[q]
    got: Optional[Dict[str, str]] = None
    try:
        import requests

        r = requests.get(
            "https://searchapi.eastmoney.com/api/suggest/get",
            params={
                "input": q,
                "type": "14",
                "token": "D43XXQ4CAVNGVRBEO",
            },
            timeout=8,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        data = (((r.json() or {}).get("QuotationCodeTable") or {}).get("Data")) or []
        exact = None
        fuzzy = None
        for it in data:
            classify = str(it.get("Classify") or "")
            if classify not in ("AStock", "AKStock"):
                continue
            code = str(it.get("Code") or it.get("UnifiedCode") or "").zfill(6)[-6:]
            nm = str(it.get("Name") or "")
            if not _is_a_share_code(code) or not nm:
                continue
            if nm == q:
                exact = {"代码": code, "名称": nm}
                break
            if fuzzy is None and (q in nm or nm in q):
                fuzzy = {"代码": code, "名称": nm}
        got = exact or fuzzy
    except Exception:
        got = None
    if got:
        _RESOLVE_NAME_CACHE[q] = got
    return got


def _resolve_code(code: str) -> Optional[Dict[str, str]]:
    code = str(code or "").zfill(6)[-6:]
    if not _is_a_share_code(code):
        return None
    for _a, c, full in _seed_alias_catalog():
        if c == code:
            return {"代码": code, "名称": full}
    for _nm, (c, full) in _OFFLINE_NAME_MAP.items():
        if c == code:
            return {"代码": code, "名称": full}
    return {"代码": code, "名称": code}



def _extract_title_mentions(
    title: str, *, keep_score: bool = False
) -> List[Dict[str, str]]:
    """从单条标题抽真正点名的股票（代码/公司名）。"""
    t = str(title or "")
    if not t:
        return []
    catalog = _seed_alias_catalog()
    out: List[Dict[str, str]] = []
    by_code: Dict[str, Dict[str, str]] = {}

    def _push(code: str, name: str, *, score: int) -> None:
        code = str(code).zfill(6)[-6:]
        if not code:
            return
        name = str(name or code)
        prev = by_code.get(code)
        if prev and int(prev.get("_score") or 0) >= score:
            return
        item = {"代码": code, "名称": name, "_score": score}
        by_code[code] = item

    # 1) 显式代码（最强）
    for m in _CODE_IN_TITLE_RE.finditer(t):
        got = _resolve_code(m.group(1))
        if got:
            _push(got["代码"], got["名称"], score=100)

    # 2) 欧陆通(300870.SZ) / 【欧陆通：
    for m in _NAME_BEFORE_CODE_RE.finditer(t):
        raw = m.group(1).strip()
        got = _resolve_mentioned_name(raw)
        if got:
            _push(got["代码"], got["名称"], score=95)
    for m in _BRACKET_CO_RE.finditer(t):
        raw = m.group(1).strip()
        if raw in ("快讯", "独家", "一线", "早报", "收评", "午评"):
            continue
        got = _resolve_mentioned_name(raw)
        if got:
            _push(got["代码"], got["名称"], score=92)

    # 3) 种子全称命中（短别名降权，防「航天」误打航天电子）
    for alias, code, full in catalog:
        if alias not in t:
            continue
        if alias == full or len(alias) >= 4:
            _push(code, full, score=90)
        elif len(alias) == 3:
            _push(code, full, score=78)
        else:
            _push(code, full, score=55)

    # 4) XX科技/通信/股份：短名优先，解析成功才收
    for raw in _iter_suffix_company_names(t):
        hit_seed = False
        for a, code, full in catalog:
            if a == raw or raw == full:
                _push(code, full, score=88)
                hit_seed = True
                break
        if hit_seed:
            continue
        got = _resolve_mentioned_name(raw)
        if got:
            _push(got["代码"], got["名称"], score=88)

    out = sorted(by_code.values(), key=lambda x: -int(x.get("_score") or 0))
    if not keep_score:
        for it in out:
            it.pop("_score", None)
    return out



def _stocks_for_board_summary(
    board: str,
    titles: List[Any],
    *,
    limit: int = 3,
    prefer_stance: str = "",
) -> Tuple[List[Dict[str, str]], List[str]]:
    """返回 (强相关个股, 对应新闻标题)。
    个股按点名强度 TopN；新闻只保留支撑这些个股的标题，一一对应，不另拼无关快讯。
    整板块都没点名时，个股退回中军，新闻取重要分最高的几条。
    prefer_stance：板块净多空，优先展示同向标题，避免「利好板」挂跌停稿。"""
    limit = max(1, min(int(limit), 3))
    rows: List[Tuple[str, float, str]] = []
    for raw in titles or []:
        if isinstance(raw, dict):
            title = str(raw.get("title") or raw.get("标题") or "").strip()
            try:
                imp = float(raw.get("imp") or raw.get("重要分") or 0)
            except (TypeError, ValueError):
                imp = 0.0
            st = str(raw.get("stance") or raw.get("多空") or "")
        else:
            title = str(raw or "").strip()
            imp = 0.0
            st = ""
        if title:
            rows.append((title, imp, st))

    def _stance_align_bonus(st: str) -> float:
        pref = str(prefer_stance or "")
        if not pref or not st:
            return 0.0
        if pref == "利好":
            if st == "利好":
                return 8.0
            if st == "中性偏多":
                return 4.0
            if st in ("利空", "中性偏空"):
                return -20.0
        if pref == "利空":
            if st == "利空":
                return 8.0
            if st == "中性偏空":
                return 4.0
            if st in ("利好", "中性偏多"):
                return -20.0
        if pref == "中性偏多" and st in ("利空",):
            return -12.0
        if pref == "中性偏空" and st in ("利好",):
            return -12.0
        return 0.0

    soft_noise = ("互动平台", "互动表示", "投资者关系", "截至发稿")
    best: Dict[str, Dict[str, Any]] = {}
    for title, imp, st in rows:
        soft = any(k in title for k in soft_noise)
        for hit in _extract_title_mentions(title, keep_score=True):
            code = str(hit.get("代码") or "").zfill(6)[-6:]
            if not code or not _is_a_share_code(code):
                continue
            score = int(hit.get("_score") or 0) + min(imp, 5.0) * 2.0
            score += _stance_align_bonus(st)
            if soft:
                score -= 25
            prev = best.get(code)
            if prev and float(prev.get("_score") or 0) >= score:
                continue
            best[code] = {
                "代码": code,
                "名称": str(hit.get("名称") or code),
                "_score": score,
                "_title": title[:120],
            }

    ranked = sorted(best.values(), key=lambda x: -float(x.get("_score") or 0))
    # 丢掉明显反标的点名稿（利好板挂跌停）
    if prefer_stance in ("利好", "中性偏多"):
        ranked = [
            r
            for r in ranked
            if not any(k in str(r.get("_title") or "") for k in ("跌停", "暴跌", "新低", "立案"))
        ] or ranked
    if prefer_stance in ("利空", "中性偏空"):
        ranked = [
            r
            for r in ranked
            if not any(k in str(r.get("_title") or "") for k in ("获批上市", "创历史新高"))
        ] or ranked
    picked = ranked[:limit]
    if picked:
        stocks = [{"代码": r["代码"], "名称": r["名称"]} for r in picked]
        headlines: List[str] = []
        seen_h = set()
        for r in picked:
            h = str(r.get("_title") or "").strip()
            if h and h not in seen_h:
                seen_h.add(h)
                headlines.append(h)
        return stocks, headlines

    # 无点名：中军 + 按重要分取新闻（同向优先）
    seeds = _seeds_for_board(board, limit=limit)
    rows_sorted = sorted(
        rows,
        key=lambda x: (-(_stance_align_bonus(x[2]) + x[1]),),
    )
    if prefer_stance in ("利好", "中性偏多"):
        aligned = [
            (t, imp, st)
            for t, imp, st in rows_sorted
            if st not in ("利空", "中性偏空")
            and not any(k in t for k in ("跌停", "暴跌", "新低", "立案"))
        ]
        rows_sorted = aligned or rows_sorted
    if prefer_stance in ("利空", "中性偏空"):
        aligned = [
            (t, imp, st)
            for t, imp, st in rows_sorted
            if st not in ("利好", "中性偏多")
        ]
        rows_sorted = aligned or rows_sorted
    headlines = [t[:120] for t, _imp, _st in rows_sorted[:limit]]
    return seeds, headlines


def _net_stance_label(score: float, bull_n: int, bear_n: int) -> str:
    if score >= 2.5 or (bull_n >= 2 and bull_n > bear_n):
        return "利好"
    if score <= -2.5 or (bear_n >= 2 and bear_n > bull_n):
        return "利空"
    if score >= 0.8:
        return "中性偏多"
    if score <= -0.8:
        return "中性偏空"
    return "中性"


def _build_board_summary(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按相关板块汇总多空 + 相关新闻 + 种子股。"""
    buckets: Dict[str, Dict[str, Any]] = {}
    for it in items:
        boards = it.get("相关板块") or []
        if not boards:
            continue
        stance = str(it.get("多空") or "中性")
        w = float(_STANCE_SCORE.get(stance, 0.0))
        imp = float(it.get("重要分") or 1.0)
        title = str(it.get("标题") or "")
        for board in boards:
            b = buckets.setdefault(
                board,
                {
                    "板块": board,
                    "score": 0.0,
                    "bull_n": 0,
                    "bear_n": 0,
                    "neu_n": 0,
                    "news_n": 0,
                    "headlines": [],
                    "titles": [],
                },
            )
            b["score"] += w * max(imp, 0.5)
            b["news_n"] += 1
            if stance == "利好" or stance == "中性偏多":
                b["bull_n"] += 1
            elif stance == "利空" or stance == "中性偏空":
                b["bear_n"] += 1
            else:
                b["neu_n"] += 1
            if title:
                b["titles"].append(
                    {"title": title, "imp": float(imp), "stance": stance}
                )

    rows: List[Dict[str, Any]] = []
    for board, b in buckets.items():
        net = _net_stance_label(float(b["score"]), int(b["bull_n"]), int(b["bear_n"]))
        stocks, headlines = _stocks_for_board_summary(
            board,
            list(b.get("titles") or []),
            limit=3,
            prefer_stance=net,
        )
        stock_txt = "、".join(f"{s['名称']}({s['代码']})" for s in stocks) if stocks else "—"
        rows.append(
            {
                "板块": board,
                "多空": net,
                "利好条数": int(b["bull_n"]),
                "利空条数": int(b["bear_n"]),
                "中性条数": int(b["neu_n"]),
                "新闻条数": int(b["news_n"]),
                "净分": round(float(b["score"]), 2),
                "关键新闻": "；".join(headlines),
                "相关个股": stock_txt,
                "个股列表": stocks,
            }
        )
    # 利好靠前，再按净分绝对值
    order = {"利好": 0, "中性偏多": 1, "中性": 2, "中性偏空": 3, "利空": 4}
    rows.sort(
        key=lambda r: (
            order.get(str(r.get("多空")), 9),
            -abs(float(r.get("净分") or 0)),
            -int(r.get("新闻条数") or 0),
        )
    )
    return rows


def _load_news_pool(*, days: int = DIGEST_DAYS, fast: bool = True) -> pd.DataFrame:
    """拉快讯 + 东财/新浪前瞻池，再裁近 N 天。"""
    from qbot.data.industry_screener import (
        fetch_forward_news,
        news_title_is_market_noise,
    )

    frames: List[pd.DataFrame] = []
    try:
        flash = fetch_cross_platform_theme_news(fast=fast)
        if flash:
            frames.append(pd.DataFrame(flash))
    except Exception:
        pass
    try:
        base = fetch_forward_news(
            finance_limit=25, tech_limit=30, pharma_limit=15, fast=fast
        )
        if base is not None and not base.empty:
            frames.append(base)
    except Exception:
        pass
    if not frames:
        return pd.DataFrame(columns=["time", "source", "title", "url", "channel"])
    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(subset=["title"]).reset_index(drop=True)
    df = df[~df["title"].map(lambda x: news_title_is_market_noise(str(x or "")))].copy()
    df = df[df["time"].map(lambda x: within_lookback(str(x or ""), days=days))].copy()
    df["_ord"] = df["time"].astype(str)
    df = df.sort_values("_ord", ascending=False).drop(columns=["_ord"])
    return df.reset_index(drop=True)


def build_daily_news_digest(
    *,
    days: int = DIGEST_DAYS,
    persist: bool = True,
    fast: bool = True,
    min_score: float = 1.2,
) -> Dict[str, Any]:
    """生成每日新闻大事。"""
    _RESOLVE_NAME_CACHE.clear()
    asof = _today()
    updated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    err = ""
    try:
        news = _load_news_pool(days=days, fast=fast)
    except Exception as exc:  # noqa: BLE001
        news = pd.DataFrame()
        err = str(exc)

    items: List[Dict[str, Any]] = []
    from qbot.data.industry_screener import news_title_is_market_noise

    for _, r in news.iterrows():
        title = str(r.get("title") or "").strip()
        if not title or news_title_is_market_noise(title):
            continue
        if _is_digest_fluff(title):
            continue
        boards = _related_boards(title)
        stance, why = _stance(title)
        score = _importance(title, str(r.get("source") or ""), boards, stance)
        # 无板块映射且非硬叙事：门槛抬高，避免杂讯占坑
        if score < min_score and not boards:
            continue
        if not boards and not _is_hard_narrative(title) and score < (min_score + 1.5):
            continue
        cat = _categorize(title)
        # 宏观美股若已映射到算力链，仍可留在宏观栏（戴尔属于宏观美股触发）
        items.append(
            {
                "时间": str(r.get("time") or "")[:16],
                "来源": str(r.get("source") or ""),
                "频道": str(r.get("channel") or ""),
                "标题": title[:140],
                "url": str(r.get("url") or ""),
                "分栏": cat,
                "相关板块": boards,
                "多空": stance,
                "分析": why,
                "重要分": round(score, 2),
            }
        )

    # 分栏内按重要分+时间
    items.sort(
        key=lambda x: (float(x.get("重要分") or 0), str(x.get("时间") or "")),
        reverse=True,
    )

    by_cat: Dict[str, List[Dict[str, Any]]] = {c: [] for c in CATEGORY_ORDER}
    for it in items:
        cat = str(it.get("分栏") or "其他")
        if cat not in by_cat:
            by_cat[cat] = []
        # 每栏最多 12 条，避免刷屏
        if len(by_cat[cat]) < 12:
            by_cat[cat].append(it)

    today_n = sum(1 for it in items if str(it.get("时间") or "").startswith(asof))
    board_summary = _build_board_summary(items)
    payload: Dict[str, Any] = {
        "asof": asof,
        "updated_at": updated,
        "days": int(days),
        "total": len(items),
        "today_count": today_n,
        "errors": err,
        "board_summary": board_summary,
        "categories": [
            {"name": c, "count": len(by_cat.get(c) or []), "items": by_cat.get(c) or []}
            for c in CATEGORY_ORDER
            if by_cat.get(c)
        ],
        "note": (
            f"默认近{days}天硬叙事新闻（优先当天）：财报/发明量产/粮价气候/战争军工/金价商品涨价，"
            "以及美股亚太盘前隔夜（费城半导体等，作A股开盘参考）；"
            "过滤A股事后个股冲高跌幅战报与境内ETF软广。"
            "上方为板块多空总结+相关新闻+强相关个股；多空为标题客观定性；不构成荐股承诺。"
        ),
    }
    html_doc = render_daily_news_html(payload)
    payload["html_path"] = str(HTML_PATH)
    if persist:
        LATEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        LATEST_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        HTML_PATH.write_text(html_doc, encoding="utf-8")
    return payload


def load_latest_daily_news_digest() -> Optional[Dict[str, Any]]:
    if not LATEST_PATH.exists():
        return None
    try:
        return json.loads(LATEST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def _stance_class(stance: str) -> str:
    if stance == "利好":
        return "bull"
    if stance == "利空":
        return "bear"
    if "偏多" in stance:
        return "soft-bull"
    if "偏空" in stance:
        return "soft-bear"
    return "neutral"


def render_daily_news_html(payload: Dict[str, Any]) -> str:
    """网站风格单页。"""
    asof = html.escape(str(payload.get("asof") or ""))
    updated = html.escape(str(payload.get("updated_at") or ""))
    days = int(payload.get("days") or DIGEST_DAYS)
    total = int(payload.get("total") or 0)
    today_n = int(payload.get("today_count") or 0)
    note = html.escape(str(payload.get("note") or ""))
    cats = payload.get("categories") or []
    summary_rows = payload.get("board_summary") or []

    sum_rows_html = []
    for r in summary_rows:
        stance = str(r.get("多空") or "中性")
        sc = _stance_class(stance)
        stocks = r.get("个股列表") or []
        if stocks:
            stock_html = "".join(
                f'<span class="stk">{html.escape(s.get("名称") or "")}'
                f'<i>{html.escape(s.get("代码") or "")}</i></span>'
                for s in stocks
            )
        else:
            stock_html = '<span class="muted">—</span>'
        sum_rows_html.append(
            f"""
<tr>
  <td class="board">{html.escape(str(r.get("板块") or ""))}</td>
  <td><span class="stance {sc}">{html.escape(stance)}</span></td>
  <td class="num">{int(r.get("利好条数") or 0)}</td>
  <td class="num">{int(r.get("利空条数") or 0)}</td>
  <td class="headline">{html.escape(str(r.get("关键新闻") or ""))}</td>
  <td class="stocks">{stock_html}</td>
</tr>"""
        )
    summary_section = ""
    if sum_rows_html:
        summary_section = f"""
<section class="sec" id="board-summary">
  <div class="sec-hd">
    <h2>板块多空总结</h2>
    <span class="sec-count">{len(sum_rows_html)} 个板块</span>
  </div>
  <p class="sum-tip">按近{days}天新闻聚合；相关新闻在前；个股与相关新闻一一对应（最多3对，不凑数）；无点名才退回中军。</p>
  <div class="table-wrap">
    <table class="sum-table">
      <thead>
        <tr>
          <th>板块</th>
          <th>多空</th>
          <th>利好</th>
          <th>利空</th>
          <th>相关新闻</th>
          <th>强相关个股</th>
        </tr>
      </thead>
      <tbody>
        {''.join(sum_rows_html)}
      </tbody>
    </table>
  </div>
</section>"""

    nav_bits = (
        [
            f'<a class="nav-pill accent" href="#board-summary">板块总结'
            f'<span>{len(sum_rows_html)}</span></a>'
        ]
        if sum_rows_html
        else []
    )
    sections = []
    for block in cats:
        name = str(block.get("name") or "")
        count = int(block.get("count") or 0)
        if not name or count <= 0:
            continue
        aid = f"cat-{hash(name) & 0xFFFF:x}"
        nav_bits.append(
            f'<a class="nav-pill" href="#{aid}">{html.escape(name)}'
            f'<span>{count}</span></a>'
        )
        cards = []
        for it in block.get("items") or []:
            stance = str(it.get("多空") or "中性")
            sc = _stance_class(stance)
            boards = it.get("相关板块") or []
            board_html = (
                "".join(f'<em class="tag">{html.escape(b)}</em>' for b in boards)
                if boards
                else '<em class="tag muted">未映射到具体板块</em>'
            )
            url = str(it.get("url") or "").strip()
            title = html.escape(str(it.get("标题") or ""))
            title_html = (
                f'<a href="{html.escape(url)}" target="_blank" rel="noreferrer">{title}</a>'
                if url.startswith("http")
                else title
            )
            cards.append(
                f"""
<article class="card">
  <header class="card-hd">
    <span class="time">{html.escape(str(it.get('时间') or ''))}</span>
    <span class="src">{html.escape(str(it.get('来源') or ''))}</span>
    <span class="stance {sc}">{html.escape(stance)}</span>
  </header>
  <h3>{title_html}</h3>
  <div class="boards"><span class="lbl">相关板块</span>{board_html}</div>
  <p class="why"><span class="lbl">客观分析</span>{html.escape(str(it.get('分析') or ''))}</p>
</article>"""
            )
        sections.append(
            f"""
<section class="sec" id="{aid}">
  <div class="sec-hd">
    <h2>{html.escape(name)}</h2>
    <span class="sec-count">{count} 条</span>
  </div>
  <div class="grid">{''.join(cards)}</div>
</section>"""
        )

    if sections:
        body = summary_section + "\n".join(sections)
    elif summary_section:
        body = summary_section
    else:
        body = '<div class="empty">近几天暂无重点新闻，点右上角刷新重试。</div>'

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>每日新闻大事 · {asof}</title>
<style>
:root {{
  --bg: #0f1419;
  --bg2: #171e26;
  --card: #1c2530;
  --line: #2a3544;
  --text: #e8eef6;
  --muted: #8b9bb0;
  --accent: #3db8a0;
  --bull: #e85d4c;
  --bear: #3ecf8e;
  --soft-bull: #c47a52;
  --soft-bear: #5aa88a;
  --neutral: #7a8899;
  --font: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
}}
* {{ box-sizing: border-box; }}
html, body {{
  margin: 0; padding: 0;
  background: var(--bg); color: var(--text);
  font-family: var(--font); line-height: 1.55;
}}
.wrap {{ max-width: 1240px; margin: 0 auto; padding: 28px 22px 60px; }}
.hero {{
  background:
    radial-gradient(900px 280px at 10% -10%, rgba(61,184,160,.18), transparent 60%),
    linear-gradient(180deg, #15202b 0%, var(--bg) 100%);
  border: 1px solid var(--line);
  border-radius: 18px;
  padding: 28px 28px 22px;
  margin-bottom: 22px;
}}
.hero .kicker {{
  color: var(--accent); letter-spacing: .14em; font-size: 12px;
  text-transform: uppercase; font-weight: 600;
}}
.hero h1 {{
  margin: 8px 0 10px; font-size: 30px; font-weight: 700; letter-spacing: .02em;
}}
.hero .sub {{ color: var(--muted); font-size: 14px; max-width: 780px; }}
.stats {{
  display: flex; flex-wrap: wrap; gap: 10px; margin-top: 18px;
}}
.stat {{
  background: rgba(255,255,255,.04); border: 1px solid var(--line);
  border-radius: 999px; padding: 6px 14px; font-size: 13px; color: var(--muted);
}}
.stat b {{ color: var(--text); font-weight: 600; margin-right: 4px; }}
.nav {{
  display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 26px;
  position: sticky; top: 0; z-index: 5;
  padding: 10px 0; background: linear-gradient(180deg, var(--bg) 70%, transparent);
}}
.nav-pill {{
  text-decoration: none; color: var(--muted);
  border: 1px solid var(--line); background: var(--bg2);
  border-radius: 999px; padding: 7px 12px; font-size: 13px;
}}
.nav-pill.accent {{
  color: var(--text); border-color: rgba(61,184,160,.55);
  background: rgba(61,184,160,.12);
}}
.nav-pill:hover {{ color: var(--text); border-color: var(--accent); }}
.nav-pill span {{
  margin-left: 6px; color: var(--accent); font-variant-numeric: tabular-nums;
}}
.sec {{ margin-bottom: 34px; }}
.sec-hd {{
  display: flex; align-items: baseline; gap: 12px;
  border-bottom: 1px solid var(--line); padding-bottom: 10px; margin-bottom: 14px;
}}
.sec-hd h2 {{ margin: 0; font-size: 20px; }}
.sec-count {{ color: var(--muted); font-size: 13px; }}
.sum-tip {{ color: var(--muted); font-size: 13px; margin: -6px 0 14px; }}
.table-wrap {{
  overflow-x: auto; border: 1px solid var(--line); border-radius: 14px;
  background: var(--card);
}}
.sum-table {{
  width: 100%; border-collapse: collapse; font-size: 13px;
}}
.sum-table th {{
  text-align: left; padding: 12px 14px; color: var(--muted); font-weight: 600;
  background: rgba(0,0,0,.22); border-bottom: 1px solid var(--line);
  white-space: nowrap;
}}
.sum-table td {{
  padding: 12px 14px; border-bottom: 1px solid var(--line); vertical-align: top;
}}
.sum-table tr:last-child td {{ border-bottom: none; }}
.sum-table tr:hover td {{ background: rgba(255,255,255,.02); }}
.sum-table .board {{ font-weight: 600; white-space: nowrap; width: 1%; }}
.sum-table td:nth-child(2) {{ width: 1%; white-space: nowrap; }}
.sum-table .num {{
  text-align: center; font-variant-numeric: tabular-nums; color: var(--muted);
  width: 1%; white-space: nowrap;
}}
.sum-table .stocks {{ width: 18%; min-width: 280px; max-width: 660px; white-space: normal; }}
.sum-table .headline {{
  color: #c5d0dc; width: auto; min-width: 420px; font-size: 13px; line-height: 1.5;
  word-break: break-word;
}}
.stk {{
  display: inline-block; margin: 2px 6px 2px 0; padding: 2px 8px;
  border-radius: 6px; background: rgba(255,255,255,.05);
  border: 1px solid var(--line); color: var(--text); font-size: 12px;
}}
.stk i {{
  font-style: normal; margin-left: 4px; color: var(--muted); font-size: 11px;
}}
.muted {{ color: var(--muted); }}
.grid {{
  display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
  gap: 12px;
}}
.card {{
  background: var(--card); border: 1px solid var(--line);
  border-radius: 14px; padding: 14px 16px 12px;
}}
.card-hd {{
  display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
  margin-bottom: 8px; font-size: 12px; color: var(--muted);
}}
.card h3 {{
  margin: 0 0 10px; font-size: 15px; font-weight: 600; line-height: 1.45;
}}
.card h3 a {{ color: var(--text); text-decoration: none; }}
.card h3 a:hover {{ color: var(--accent); }}
.stance {{
  border-radius: 6px; padding: 2px 8px; font-weight: 600; color: #0b1014;
  display: inline-block;
}}
.stance.bull {{ background: var(--bull); }}
.stance.bear {{ background: var(--bear); color: #062016; }}
.stance.soft-bull {{ background: var(--soft-bull); }}
.stance.soft-bear {{ background: var(--soft-bear); color: #062016; }}
.stance.neutral {{ background: var(--neutral); color: #101820; }}
.boards, .why {{ font-size: 13px; color: var(--muted); margin: 6px 0; }}
.lbl {{
  display: inline-block; min-width: 64px; color: #a9b7c9; margin-right: 6px;
}}
.tag {{
  display: inline-block; margin: 2px 6px 2px 0; padding: 2px 8px;
  border-radius: 999px; background: rgba(61,184,160,.12);
  color: #9fe0d2; border: 1px solid rgba(61,184,160,.28); font-style: normal;
  font-size: 12px;
}}
.tag.muted {{ background: transparent; color: var(--muted); border-color: var(--line); }}
.empty {{
  text-align: center; color: var(--muted); padding: 60px 20px;
  border: 1px dashed var(--line); border-radius: 14px;
}}
.foot {{
  margin-top: 28px; color: var(--muted); font-size: 12px; text-align: center;
}}
</style>
</head>
<body>
<div class="wrap">
  <header class="hero">
    <div class="kicker">Daily Brief · Qbot</div>
    <h1>每日新闻大事</h1>
    <p class="sub">{note}</p>
    <div class="stats">
      <div class="stat"><b>{asof}</b>数据日</div>
      <div class="stat"><b>近{days}天</b>窗口</div>
      <div class="stat"><b>{today_n}</b>条当天</div>
      <div class="stat"><b>{total}</b>条重点</div>
      <div class="stat"><b>{len(sum_rows_html)}</b>个板块结论</div>
      <div class="stat"><b>{updated}</b>更新</div>
    </div>
  </header>
  <nav class="nav">{''.join(nav_bits)}</nav>
  {body}
  <div class="foot">多空为标题层面客观归类；个股为主题种子非荐股名单 · 来源含财联社 / 华尔街见闻 / 东财·新浪</div>
</div>
</body>
</html>
"""
