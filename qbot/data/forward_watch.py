# -*- coding: utf-8 -*-
"""前瞻观察 + 短线周转：新闻/热板 → 主题观察；今日短线跟涨做 1～3 天。

定位：资金要转起来——哪个热板在走跟哪个，短持见好就收；
中军也多是涨几天跌几天，勿空仓干等一两次大回踩而错过其它热板。

固定刷新闭环（每次启动/点刷新/跑 scripts/refresh_forward_watch.py）：
1. 财经30+科技30+医药20，过滤后关联概念；
2. 概念→细分行业（同概念可拆多频道）；THEME_HINTS 只作结构映射/种子；
3. 否决：主题禁区、宏观战略箩筐、情绪板、无催化透支；
4. 细分行业下中军种子 + 活跃票；多形态买点映射买入方法 A～E；假回踩否决；
5. 今日短线独立池最多6只，默认都有「买入方法」+贴价区间；
6. 个股观察仅「买入候选=是」填买入方法，候选=否留空；写入 latest + history。

选股期≠持有期：进池要板块中期（约1～2月）真看好；进了不因个股连跌/走坏踢。
个股走坏止损只用于已买持有。选股对象含：微跌、止跌起稳、上涨回踩、温和上涨。
踢主题：仅中期逻辑破坏才整主题撤；单日/两日走弱不撤。
买点看K线：开高低收+量比，区分真回踩 vs 高开低走/冲高回落；禁止只看收盘涨跌幅。
个股双星：主线星=贴合当前热主线；买点星=是否适合短做。
买入方法：A热板浅回 / B主线微涨横盘 / C热板连涨 / D催化缓涨 / E止跌再起。
买入候选：短线形态主线星≥2，否则≥3；买点星≥3。特变压舱不进短线池。
算电-算必须拆池：国产服务器（紫光/浪潮/锐捷）≠ 海外组装（富联）≠ 液冷散热（英维克等）≠ 算力租赁/智算（协创等），勿混为一谈；
协创是 token/算力租赁偏硬侧，禁止划进短剧/AIGC内容或AI应用软件。
多元主题分散：贵金属/医药/电力/航天/军工/农业/光伏/小金属/AI应用/汽车/科技硬件等 1～2 月看好的都留；
仅地产/教育等禁区与同质金融板不进；科技硬件发现排序有上限，防一跌全跌占满池。
"""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from qbot.data.industry_screener import (
    _fetch_kline_bars,
    _fetch_kline_bars_fast,
    _fetch_ulist_quote_map,
    clear_board_constituents_cache,
    fetch_board_constituents,
    fetch_forward_news,
    fetch_hot_news,
    fetch_industry_boards,
    news_title_is_major_catalyst,
    set_board_fetch_fast,
)

# 观察/短线永久排除：名称易与板块混淆、不当实体中军（300024「机器人」）
_OBSERVE_EXCLUDE_CODES = frozenset({"300024"})

HISTORY_PATH = (
    Path(__file__).resolve().parents[1] / "gui" / "csv" / "forward_watch_history.json"
)
LATEST_PATH = (
    Path(__file__).resolve().parents[1] / "gui" / "csv" / "forward_watch_latest.json"
)

# 管道版本：缓存里可对照是否按新规则刷新
PIPELINE_VERSION = "forward_v7_21_compute_rental"

# 风险分用日K缓存：code:asof → bars（单次刷新内复用）
_RISK_BARS_CACHE: Dict[str, List[Dict[str, Any]]] = {}
_RISK_PREFETCH_WORKERS = 16

# 新进主题/个股星级封顶（连入/主题走好 < 2 日）
NEWCOMER_STAR_CAP = 3

# 概念 → 行业（可一对多）。行业名与东财「行业」板对齐；发现时行业全局唯一。
CONCEPT_TO_INDUSTRIES: Dict[str, List[str]] = {
    "CPO": ["通信设备"],
    "光模块": ["通信设备"],
    "光通信": ["通信设备"],
    "光通信模块": ["通信设备"],
    "光学光电子": ["光学光电子", "通信设备"],
    # 勿映射「元件」：会与被动元件/MLCC 抢同一行业坑，导致 PCB 主题行业列错成被动元件
    "PCB": ["印制电路板"],
    "印制电路板": ["印制电路板"],
    "机器人": ["机器人"],
    "人形机器人": ["机器人", "专用设备"],
    "机器人执行器": ["机器人", "专用设备"],
    "工业机器人": ["机器人", "专用设备"],
    "MLCC": ["被动元件", "元件"],
    "被动元件": ["被动元件"],
    "电子元件": ["元件", "被动元件"],
    "存储": ["半导体", "其他电子Ⅱ"],
    "存储芯片": ["半导体"],
    "半导体": ["半导体"],
    "芯片": ["半导体", "其他电子Ⅱ"],
    "半导体材料": ["半导体材料"],
    "半导体设备": ["半导体设备"],
    "半导体设备概念": ["半导体设备"],
    "电子化学品": ["电子化学品Ⅱ", "电子化学品Ⅲ"],
    "电子特气": ["电子化学品Ⅱ", "半导体材料"],
    "硅片": ["半导体材料"],
    # AI/PCB 高端铜箔并入材料上游观察，不单独开主题
    "铜箔": ["半导体材料"],
    "电子铜箔": ["半导体材料"],
    "HVLP": ["半导体材料"],
    "服务器": ["计算机设备", "通信设备"],
    "国产服务器": ["计算机设备"],
    "海外组装": ["消费电子", "计算机设备"],
    "云计算": ["计算机设备"],
    "人工智能": ["软件开发", "计算机设备"],
    # 短剧/AIGC 内容应用：映射数字媒体/传媒，勿并入软件开发挤掉金融IT
    "短剧": ["数字媒体", "传媒"],
    "短剧互动游戏": ["数字媒体", "传媒"],
    "AIGC": ["数字媒体", "软件开发"],
    "AIGC概念": ["数字媒体", "软件开发"],
    "AI语料": ["数字媒体", "传媒"],
    "数字媒体": ["数字媒体"],
    "影视概念": ["数字媒体", "传媒"],
    # 算力概念共用，行业侧必须拆开（见 THEME_HINTS industry_label）
    "算力": ["计算机设备"],
    "国产算力": ["计算机设备"],
    "AI服务器": ["计算机设备"],
    # 算力租赁/智算：偏硬件基础设施，勿映射数字媒体/AI应用内容
    "算力租赁": ["计算机设备"],
    "智算": ["计算机设备"],
    "智算中心": ["计算机设备"],
    "数据中心": ["计算机设备"],
    "工业富联": ["消费电子", "计算机设备"],
    "软件开发": ["软件开发"],
    "IT服务": ["IT服务Ⅱ"],
    "光伏": ["光伏设备", "光伏辅材"],
    "光伏设备": ["光伏设备"],
    "光伏主材": ["光伏设备"],
    "核电": ["电力设备", "发电设备"],
    "核能核电": ["电力设备", "发电设备"],
    "电网设备": ["电网设备", "电力设备"],
    "电力设备": ["电力设备"],
    "变压器": ["电网设备", "电力设备"],
    "白酒": ["白酒Ⅲ", "酿酒行业"],
    "汽车整车": ["汽车整车", "乘用车", "商用车"],
    "汽车": ["汽车整车", "乘用车", "商用车"],
    # 汽车芯片是半导体/封测概念，禁止映射到整车「汽车」行业
    "汽车芯片": ["半导体"],
    "车规芯片": ["半导体"],
    "汽车电子": ["半导体", "其他电子Ⅱ"],
    "银行": ["银行"],
    "信托": ["信托"],
    "有色金属": ["有色金属"],
    "小金属": ["小金属"],
    "锗": ["小金属"],
    "钨": ["小金属"],
    "光纤": ["通信线缆及配套"],
    "光缆": ["通信线缆及配套"],
    "光纤光缆": ["通信线缆及配套"],
    "通信线缆": ["通信线缆及配套"],
    "在线教育": ["在线教育", "教育"],
    "水利建设": ["水利建设"],
    "液冷": ["其他电源设备Ⅱ", "计算机设备"],
    "液冷服务器": ["其他电源设备Ⅱ", "计算机设备"],
    "消费电子": ["消费电子"],
    "黄金": ["黄金", "贵金属"],
    "贵金属": ["贵金属", "黄金"],
    "培育钻石": ["通用设备", "非金属材料", "磨具磨料"],
    "金刚石": ["通用设备", "非金属材料", "磨具磨料"],
    "商业航天": ["航天装备", "航空装备"],
    "卫星": ["航天装备", "航空装备"],
    "低空经济": ["航空装备", "航天装备"],
    "农业": ["种植业", "农业"],
    "种业": ["种植业"],
    "种植": ["种植业"],
    "饲料": ["饲料"],
    "生猪": ["养殖业"],
    "军工": ["航天装备", "航空装备", "地面兵装", "船舶制造"],
    "国防": ["航天装备", "航空装备", "地面兵装"],
    "国防军工": ["航天装备", "航空装备", "地面兵装", "船舶制造"],
}

# 可选结构佐证库：补种子股/主题名/逻辑；不作为进池白名单。
# 动态发现到的板块若命中此处，可继承种子股与更高优先级。
THEME_HINTS: List[Dict[str, Any]] = [
    {
        "id": "grid_power",
        "name": "算电-电(电网/变压器)",
        "priority": 5,
        "keywords": [
            "特高压",
            "电网",
            "新型电网",
            "变压器",
            "算电",
            "数据中心用电",
            "电力设备",
            "主网",
            "配网",
            "六张网",
        ],
        "board_keys": [
            "电网",
            "电网设备",
            "特高压",
            "输变电",
            "电力设备",
            "能源设备",
            "智能电网",
        ],
        "industries": ["电网设备", "电力设备"],
        "seed_stocks": [
            ("600089", "特变电工"),
            ("601179", "中国西电"),
            ("600312", "平高电气"),
            ("600875", "东方电气"),
        ],
        "thesis": (
            "算电要拆开看：电=供电/变压器/主配网，是算力扩张后的瓶颈延伸。"
            "当国产算力拥挤出清时，电侧往往相对抗跌，更适合作为下一棒观察，"
            "但需等板块连续赚钱效应，不能把「算跌了」直接当成「电必涨」。"
        ),
    },
    {
        "id": "domestic_server",
        "name": "算电-算(国产服务器)",
        "concept_group": "算电-算",
        "industry_label": "国产服务器",
        "split_group": "suanli_suan",
        "priority": 5,
        "keywords": [
            "国产服务器",
            "国产算力",
            "服务器",
            "AI服务器",
            "信创服务器",
            "交换机",
            "ICT",
            "浪潮",
            "紫光",
            "锐捷",
        ],
        "board_keys": [
            "服务器",
            "云计算",
            "计算机设备",
            "网络优化",
            "AI服务器",
            "信创",
        ],
        "industries": ["计算机设备"],
        "seed_stocks": [
            ("000938", "紫光股份"),
            ("000977", "浪潮信息"),
            ("301165", "锐捷网络"),
            ("603019", "中科曙光"),
        ],
        "thesis": (
            "算电-算·国产服务器频道：紫光/浪潮/锐捷/曙光等 ICT/整机交换机。"
            "与海外组装（富联）、液冷散热分池观察，勿混为一谈。"
            "走势多为涨几天跌几天；有短线买点可做1～3天，勿空仓干等大回踩。"
            "杀跌日不抄死底。勿与笼统「计算机设备/电子」混为一谈。"
        ),
    },
    {
        "id": "liquid_cooling",
        "name": "算电-算(液冷服务器)",
        "concept_group": "算电-算",
        "industry_label": "液冷散热",
        "split_group": "suanli_suan",
        "priority": 5,
        "keywords": [
            "液冷",
            "液冷服务器",
            "冷板",
            "浸没式液冷",
            "散热",
            "英维克",
            "申菱",
            "CDU",
        ],
        "news_keys": ["液冷", "液冷服务器", "浸没式", "冷板液冷"],
        "board_keys": ["液冷服务器", "液冷"],
        "industries": ["其他电源设备Ⅱ"],
        "seed_stocks": [
            # 液冷/散热中军；整机浪潮/紫光留在国产服务器池，不重复占坑
            ("002837", "英维克"),
            ("301018", "申菱环境"),
            ("000811", "冰轮环境"),
            ("300442", "润泽科技"),
            ("603757", "大元泵业"),
            ("002418", "康盛股份"),
            ("003018", "金富科技"),
        ],
        "thesis": (
            "算电-算·液冷散热频道：AI机柜功率密度提升后的散热瓶颈；"
            "与整机服务器短线可共振也可分化，须单独盯资金与回踩。"
            "涨停/情绪小票只观察；优先英维克等中军缓涨或回踩，不追尖峰。"
        ),
    },
    {
        "id": "compute_rental",
        "name": "算电-算(算力租赁/智算)",
        "concept_group": "算电-算",
        "industry_label": "算力租赁/智算",
        "split_group": "suanli_suan",
        "priority": 5,
        "keywords": [
            "算力租赁",
            "智算",
            "智算中心",
            "token",
            "算力调度",
            "GPU租赁",
            "协创数据",
        ],
        "news_keys": ["算力租赁", "智算", "智算中心", "GPU租赁", "算力调度"],
        "board_keys": ["数据中心", "算力概念", "云计算"],
        "industries": ["计算机设备"],
        "seed_stocks": [
            # token/算力租赁、智算数据：偏硬件基础设施，≠短剧/AIGC内容，≠金山类AI应用软件
            # 润泽留在液冷池，此处不重复占坑
            ("300857", "协创数据"),
            ("603881", "数据港"),
            ("300383", "光环新网"),
            ("300738", "奥飞数据"),
        ],
        "thesis": (
            "算电-算·算力租赁/智算频道：token算力、机柜/数据中心出租，偏硬件基础设施；"
            "与短剧影视内容、办公AI应用软件不是同一条腿——芒果涨不代表协创涨。"
            "跟算力/数据中心资金，勿用AIGC内容脉冲推断；回踩观察，尖峰不追。"
        ),
    },
    {
        "id": "overseas_odm",
        "name": "算电-算(海外组装)",
        "concept_group": "算电-算",
        "industry_label": "海外组装",
        "split_group": "suanli_suan",
        "priority": 5,
        "keywords": [
            "海外组装",
            "工业富联",
            "富联",
            "代工",
            "ODM",
            "EMS",
            "苹果链",
        ],
        "board_keys": [
            "消费电子",
            "计算机设备",
            "苹果概念",
            "消费电子概念",
        ],
        "industries": ["消费电子", "计算机设备"],
        "seed_stocks": [
            ("601138", "工业富联"),
        ],
        "thesis": (
            "算电-算·海外组装频道：富联等代工/组装大盘，跟全球 AI 资本开支与消费电子β。"
            "不是国产服务器池：7月曾大跌时紫光/浪潮/锐捷仍可涨，回流日也常独自掉队。"
            "只在本频道放量走强时跟，勿用紫光/浪潮/锐捷涨幅推断富联。"
            "可短做，不当「唯一算力中军」空仓死等。"
        ),
    },
    {
        "id": "mlcc",
        "name": "MLCC/被动元件",
        "industry_label": "被动元件/功率电感",
        "priority": 5,
        "keywords": [
            "MLCC",
            "被动元件",
            "多层陶瓷",
            "电容涨价",
            "风华",
            "三环",
            "功率电感",
            "一体成型电感",
            "磁性元件",
            "麦捷",
        ],
        "board_keys": ["MLCC", "被动元件", "电子元件"],
        "industries": ["被动元件"],
        "seed_stocks": [
            ("300408", "三环集团"),
            ("000636", "风华高科"),
            ("003031", "中瓷电子"),
            ("300319", "麦捷科技"),
        ],
        "thesis": (
            "AI服务器高容电容/功率电感缺货+原厂调价景气链；"
            "麦捷等算力电感中军与三环/风华等同池观察，注意股价是否已抢跑。"
        ),
    },
    {
        "id": "memory_storage",
        "name": "存储芯片/模组",
        "priority": 5,
        "keywords": [
            "存储",
            "存储芯片",
            "内存",
            "DRAM",
            "NAND",
            "HBM",
            "闪存",
            "颗粒",
            "模组",
            "长鑫",
            "DDR",
            "NOR",
        ],
        "news_keys": [
            "存储",
            "存储芯片",
            "DRAM",
            "NAND",
            "HBM",
            "闪存",
            "内存涨价",
            "长鑫",
        ],
        "board_keys": ["存储芯片", "高带宽内存", "存储", "内存"],
        "industries": ["半导体"],
        "industry_label": "存储芯片",
        "seed_stocks": [
            ("603986", "兆易创新"),
            ("688008", "澜起科技"),
            ("300223", "北京君正"),
            ("688123", "聚辰股份"),
            ("688525", "佰维存储"),
            ("301308", "江波龙"),
            ("001309", "德明利"),
        ],
        "thesis": (
            "存储中期有涨价/AI/HBM需求逻辑；中军（兆易/澜起/君正等）与模组一起观察。"
            "大起大落后盯缩量起稳/回踩，深跌单日回流不当主升确认。"
        ),
    },
    {
        "id": "power_supply",
        "name": "供电/功率器件",
        "priority": 4,
        "keywords": ["电源", "功率器件", "供电密度", "碳化硅", "氮化镓", "服务器电源"],
        "board_keys": ["电源设备", "功率半导体", "第三代半导体", "碳化硅"],
        "seed_stocks": [
            ("002851", "麦格米特"),
            ("300316", "晶盛机电"),
            ("688396", "华润微"),
        ],
        "thesis": "算力每瓦功耗上升带动机柜供电与功率器件需求，偏订单验证型；介于「算」与「电」之间。",
    },
    {
        "id": "dairy_up",
        "name": "原奶/乳业周期",
        "priority": 3,
        "keywords": ["原奶", "奶价", "存栏", "乳业", "牧场", "生鲜乳"],
        "board_keys": ["乳业", "乳制品"],
        "seed_stocks": [
            ("600887", "伊利股份"),
            ("002946", "新乳业"),
            ("600429", "三元股份"),
        ],
        "thesis": "存栏去化+奶价触底信号；下游连板后看龙头回踩，上游弹性更正。",
    },
    {
        "id": "baijiu",
        "name": "白酒",
        "priority": 4,
        "keywords": ["白酒", "茅台", "五粮液", "批价", "动销", "酒企", "酿酒"],
        "board_keys": ["白酒", "酿酒行业", "白酒概念"],
        "seed_stocks": [
            ("600519", "贵州茅台"),
            ("000858", "五粮液"),
            ("000568", "泸州老窖"),
            ("600809", "山西汾酒"),
        ],
        "thesis": "批价/动销/消费复苏类慢逻辑，偏1-2周观察；单日避险抱团不算景气确认。",
    },
    {
        "id": "innovative_drug",
        "name": "创新药",
        "priority": 5,
        "keywords": [
            "创新药",
            "新药",
            "生物医药",
            "生物药",
            "ADC",
            "GLP-1",
            "减肥药",
            "CXO",
            "临床",
            "获批",
            "医保目录",
            "国谈",
            "恒瑞",
            "百济",
            "药明",
        ],
        "news_keys": [
            "创新药",
            "生物医药",
            "医保",
            "CXO",
            "ADC",
            "GLP-1",
            "新药获批",
            "临床数据",
        ],
        "board_keys": [
            "创新药",
            "化学制药",
            "生物制品",
            "医疗研发外包",
            "医药",
        ],
        # 行业全局唯一：只占一个行业坑，CXO/生物制品靠 board_keys+种子覆盖
        "industries": ["化学制药"],
        "seed_stocks": [
            ("600276", "恒瑞医药"),
            ("688235", "百济神州"),
            ("603259", "药明康德"),
            ("002821", "凯莱英"),
            ("300759", "康龙化成"),
            ("688180", "君实生物"),
            ("300122", "智飞生物"),
        ],
        "thesis": (
            "创新药/CXO 是独立于算力的景气与政策轮动主线；"
            "有医保、获批、临床或资金连续认可时进观察池。"
            "大涨后等回踩，不把一日脉冲当买点；短线仍看形态是否给点。"
        ),
    },
    {
        "id": "precious_metals",
        "name": "黄金/贵金属",
        "priority": 5,
        "keywords": ["黄金", "贵金属", "金价", "金饰", "白银", "避险", "山金", "赤峰"],
        "news_keys": ["黄金", "金价", "贵金属", "金饰", "避险"],
        "board_keys": ["黄金", "贵金属", "白银", "小金属"],
        "industries": ["黄金", "贵金属"],
        "seed_stocks": [
            ("600489", "中金黄金"),
            ("600547", "山东黄金"),
            ("601069", "西部黄金"),
            ("600988", "赤峰黄金"),
            ("000975", "山金国际"),
        ],
        "thesis": (
            "宏观避险/通胀/地缘催化；与科技β低相关，观察池与科技同等保位。"
            "科技杀跌日仍留池盯回踩，不把一日脉冲当买点。"
        ),
    },
    {
        "id": "lab_diamond",
        "name": "培育钻石/金刚石散热",
        "priority": 5,
        "keywords": [
            "培育钻石",
            "人造钻石",
            "金刚石",
            "CVD",
            "HPHT",
            "热沉",
            "散热片",
            "金刚石散热",
            "功能材料",
            "金刚石复合材料",
        ],
        "news_keys": [
            "培育钻石",
            "金刚石",
            "散热",
            "热沉",
            "CVD",
            "算力散热",
            "力量钻石",
            "黄河旋风",
            "四方达",
        ],
        "board_keys": ["培育钻石", "金刚石", "非金属材料"],
        "industries": ["通用设备", "非金属材料", "磨具磨料"],
        "seed_stocks": [
            ("600172", "黄河旋风"),
            ("301071", "力量钻石"),
            ("300179", "四方达"),
            ("002046", "国机精工"),
            ("605580", "恒盛能源"),
            ("688028", "沃尔德"),
        ],
        "thesis": (
            "主线已从珠宝消费切到金刚石热沉/AI散热；产线投产、小批量供货、"
            "募投转向功能材料是实质催化。涨停潮后只盯中军分歧回踩，"
            "珠宝蹭概念不当主线。"
        ),
    },
    {
        "id": "aerospace",
        "name": "商业航天/卫星",
        "priority": 5,
        "keywords": [
            "商业航天",
            "卫星",
            "航天",
            "低空经济",
            "火箭",
            "北斗",
            "星网",
            "卫星互联网",
        ],
        "news_keys": ["商业航天", "卫星", "航天", "星网", "北斗", "低空经济"],
        "board_keys": [
            "商业航天",
            "卫星",
            "航天装备",
            "低空经济",
            "通用航空",
            "航空装备",
        ],
        "industries": ["航天装备", "航空装备"],
        "seed_stocks": [
            ("600879", "航天电子"),
            ("600118", "中国卫星"),
            ("688523", "航天环宇"),
            ("002465", "海格通信"),
        ],
        "thesis": (
            "卫星互联网/商业航天产业政策与订单验证；与纯科技算力分池观察，"
            "科技拥挤出清时常有相对独立走势。"
        ),
    },
    {
        "id": "defense_military",
        "name": "军工/国防",
        "priority": 5,
        "keywords": [
            "军工",
            "国防",
            "导弹",
            "雷达",
            "舰船",
            "航空发动机",
            "军贸",
            "装备",
            "军民融合",
            "中航",
            "航发",
        ],
        "news_keys": ["军工", "国防", "军贸", "装备", "导弹", "雷达", "订单"],
        "board_keys": [
            "军工",
            "国防军工",
            "地面兵装",
            "船舶制造",
            "航空装备",
            "航天装备",
        ],
        "industries": ["地面兵装", "航空装备", "船舶制造"],
        "industry_label": "军工国防",
        "seed_stocks": [
            ("600760", "中航沈飞"),
            ("600893", "航发动力"),
            ("600150", "中国船舶"),
            ("002179", "中航光电"),
            ("600967", "内蒙一机"),
        ],
        "thesis": (
            "军工/国防装备偏政策、订单与军贸周期；与纯科技算力β不同，"
            "与商业航天/卫星分池观察。科技杀跌日仍可独立留池盯回踩。"
        ),
    },
    {
        "id": "agriculture",
        "name": "农业/种植",
        "priority": 5,
        "keywords": ["农业", "种植", "种业", "粮食", "农机", "生猪", "饲料", "北大荒"],
        "news_keys": ["农业", "种业", "粮食", "农机", "生猪", "饲料"],
        "board_keys": ["农业", "种植业", "种子", "饲料", "农林牧渔", "农机", "养殖业"],
        "industries": ["种植业", "农业"],
        "seed_stocks": [
            ("600598", "北大荒"),
            ("000998", "隆平高科"),
            ("002041", "登海种业"),
            ("002714", "牧原股份"),
        ],
        "thesis": (
            "避险/政策/粮价周期；与科技低相关，防守日仍留池观察回踩，"
            "不把一日脉冲当买点。"
        ),
    },
    {
        "id": "auto_oem",
        "name": "汽车整车",
        "priority": 5,
        "keywords": [
            "整车",
            "乘用车",
            "商用车",
            "新能源车",
            "汽车销量",
            "华为汽车",
            "尊界",
            "问界",
        ],
        "board_keys": ["汽车整车", "汽车", "电动乘用车", "商用车", "乘用车"],
        "industries": ["汽车整车", "乘用车", "商用车"],
        "seed_stocks": [
            ("002594", "比亚迪"),
            ("600418", "江淮汽车"),
            ("000625", "长安汽车"),
            ("601127", "赛力斯"),
        ],
        "thesis": (
            "整车有销量/政策/华为智驾等催化时容易独立走强；"
            "优先比亚迪/江淮等中军，勿把汽车零部件/无人问津跟风票当整车主线。"
        ),
    },
    {
        "id": "pv_solar",
        "name": "光伏组件/设备",
        "priority": 5,
        "keywords": ["光伏", "组件", "硅料", "TOPCon", "HJT", "逆变器", "装机", "隆基", "通威"],
        "news_keys": ["光伏", "组件", "硅料", "装机", "逆变器", "TOPCon"],
        "board_keys": ["光伏", "光伏设备", "光伏概念", "光伏主材", "光伏电池"],
        "industries": ["光伏设备"],
        "seed_stocks": [
            ("688472", "阿特斯"),
            ("601012", "隆基绿能"),
            ("600438", "通威股份"),
            ("002459", "晶澳科技"),
        ],
        "thesis": (
            "光伏有自身供需与政策节奏，常与半导体科技杀估值不同步；"
            "相对抗跌时观察，涨幅过大仍等回踩。"
        ),
    },
    {
        "id": "microled_opt",
        "name": "MicroLED光通信",
        "priority": 2,
        "keywords": ["Micro LED", "MicroLED", "光互连", "光引擎", "Palomino"],
        "board_keys": ["MicroLED"],
        "seed_stocks": [
            ("600703", "三安光电"),
            ("300323", "华灿光电"),
            ("002429", "兆驰股份"),
        ],
        "thesis": "前沿送样阶段，2027前后才谈放量；仅作认知储备，难当即期主升。",
    },
    {
        "id": "cpo_optical",
        "name": "CPO/光模块",
        "priority": 4,
        "keywords": [
            "CPO",
            "光模块",
            "光通信",
            "硅光",
            "800G",
            "1.6T",
            "光芯片",
            "光器件",
            "光互联",
            "共封装光学",
            "英伟达",
            "中际旭创",
            "新易盛",
        ],
        "board_keys": ["CPO", "光模块", "光通信", "光通信模块", "光学光电子"],
        "industries": ["通信设备"],
        # 种子只放可交易价位（<800）；中际/源杰一手过贵不进名单，免占位再踢
        "seed_stocks": [
            ("300502", "新易盛"),
            ("300394", "天孚通信"),
            ("002281", "光迅科技"),
            ("300570", "太辰光"),
            ("688048", "长光华芯"),
        ],
        "thesis": (
            "AI算力互联刚需：光模块/CPO是科技主升里的核心产业链，"
            "看资金与龙头回踩，勿把西部大开发/节能环保等箩筐概念当行业归属。"
        ),
    },
    {
        "id": "ai_app_soft",
        "name": "AI应用/金融IT",
        "priority": 5,
        "keywords": [
            "AI应用",
            "智能体",
            "Agent",
            "大模型应用",
            "金融IT",
            "信创",
            "软件落地",
            "办公软件",
            "AI软件",
            "ChatGPT",
        ],
        "board_keys": [
            "软件开发",
            "IT服务",
            "人工智能",
            "ChatGPT概念",
            "AI应用",
            "应用软件",
        ],
        "seed_stocks": [
            ("002410", "广联达"),
            ("600570", "恒生电子"),
            ("603383", "顶点软件"),
            ("688111", "金山办公"),
            ("002230", "科大讯飞"),
            ("300033", "同花顺"),
            ("300085", "银之杰"),
            ("300454", "深信服"),
            ("002405", "四维图新"),
            ("300496", "中科创达"),
            ("300624", "万兴科技"),
        ],
        "thesis": (
            "硬件拥挤后资金常切向兑现型AI应用/金融IT/办公软件；"
            "主升看连续资金与回踩买点，周五冲高回落时先辨真强假强，勿追分时尖峰。"
        ),
    },
    {
        "id": "short_drama_aigc",
        "name": "短剧/AIGC内容",
        "industry_label": "数字媒体/短剧",
        "priority": 5,
        "keywords": [
            "短剧",
            "AIGC",
            "AI语料",
            "微短剧",
            "互动游戏",
            "数字媒体",
            "AI内容",
            "芒果",
            "昆仑万维",
        ],
        "news_keys": ["短剧", "AIGC", "AI语料", "微短剧", "数字媒体"],
        "board_keys": [
            "短剧互动游戏",
            "AIGC概念",
            "AI语料",
            "数字媒体",
            "影视概念",
            "传媒",
        ],
        "industries": ["数字媒体", "传媒"],
        "seed_stocks": [
            # 内容/牌照侧；协创是算力租赁硬侧，已挪到 compute_rental，禁止再塞回本主题
            ("300418", "昆仑万维"),
            ("300017", "网宿科技"),
            ("300413", "芒果超媒"),
            ("300133", "华策影视"),
            ("002517", "恺英网络"),
            ("300182", "捷成股份"),
            ("301262", "海看股份"),
        ],
        "thesis": (
            "短剧/AIGC/语料属AI应用内容侧（芒果/昆仑等）：资金常在硬件拥挤后切向传媒数字媒体；"
            "协创等算力租赁不在本池；只留有盈利口径+资金认可的内容中军，涨停/亏损情绪票不追；"
            "主升看连续流入与回踩，脉冲日只观察。"
        ),
    },
    {
        "id": "humanoid_robot",
        "name": "人形机器人/机器人",
        "priority": 4,
        "keywords": [
            "人形机器人",
            "机器人",
            "具身智能",
            "工业机器人",
            "减速器",
            "谐波减速器",
            "丝杠",
            "灵巧手",
            "机器视觉",
            "3D视觉",
            "执行器",
            "热管理",
        ],
        "board_keys": [
            "机器人",
            "工业机器人",
            "人形机器人",
            "智能机器",
            "机器人执行器",
            "机器视觉",
        ],
        "industries": ["机器人", "专用设备"],
        "seed_stocks": [
            ("601689", "拓普集团"),
            ("002050", "三花智控"),
            ("688322", "奥比中光"),
            ("688017", "绿的谐波"),
            ("002472", "双环传动"),
            ("603728", "鸣志电器"),
        ],
        "thesis": (
            "机器人/具身智能偏产业趋势主题：板块集体走强时优先盯中军回踩，"
            "勿追连板情绪票；链条含减速器/执行器/热管理/3D视觉等。"
            "拓普等执行器中军归机器人观察，勿并入汽车整车。"
            "不观察300024「机器人」（名称与板块混淆、不当实体中军）。"
        ),
    },
    {
        "id": "nuclear_power",
        "name": "核电/电力",
        "priority": 5,
        "keywords": [
            "核电",
            "核能",
            "核电机组",
            "电力",
            "电价",
            "电力现货",
            "现货电",
            "发电",
            "电改",
            "庄河",
            "华龙一号",
            "国和一号",
        ],
        "board_keys": ["核电", "核能核电", "电力", "火力发电", "发电", "清洁能源", "水电"],
        "industries": ["电力设备", "发电设备"],
        "seed_stocks": [
            ("601985", "中国核电"),
            ("003816", "中国广核"),
            ("600900", "长江电力"),
            ("600886", "国投电力"),
        ],
        "thesis": (
            "电力/核电偏政策与装机逻辑：国常会核准机组、电改/现货定价都是前瞻催化；"
            "即便此前偏冷，重大利好后也要把中军纳入观察，再等回踩买点。"
        ),
    },
    {
        "id": "semi_materials",
        "name": "半导体材料",
        "priority": 5,
        "keywords": [
            "半导体材料",
            "电子化学品",
            "电子特气",
            "硅片",
            "大硅片",
            "靶材",
            "光刻胶",
            "抛光液",
            "湿电子",
            "前驱体",
            "芯片材料",
            "材料涨价",
            # AI/高速 PCB 用高端铜箔，并入材料上游同观察
            "铜箔",
            "电子铜箔",
            "HVLP",
            "载体铜箔",
            # 半导体景气/涨价新闻对材料同样是前瞻催化
            "半导体",
            "晶圆",
            "先进制程",
        ],
        "news_keys": [
            "半导体材料",
            "电子化学品",
            "电子特气",
            "硅片",
            "靶材",
            "光刻胶",
            "芯片材料",
            "材料涨价",
            "铜箔",
            "电子铜箔",
            "HVLP",
            "半导体",
            "晶圆",
            "先进制程",
        ],
        "board_keys": [
            "半导体材料",
            "铜箔",
        ],
        "industries": ["半导体材料"],
        "seed_stocks": [
            ("600206", "有研新材"),
            ("688432", "有研硅"),
            ("688126", "沪硅产业"),
            ("300666", "江丰电子"),
            ("688146", "中船特气"),
            ("688233", "神工股份"),
            # 铜箔只盯龙头，不铺开小票
            ("301217", "铜冠铜箔"),
            ("301511", "德福科技"),
            ("600110", "诺德股份"),
        ],
        "thesis": (
            "半导体材料/特气/硅片是晶圆制造上游；AI 服务器 PCB 用 HVLP/电子铜箔"
            "同属材料紧缺链。景气、扩产或涨价新闻对材料侧同向催化。"
            "挖坑观察：调整/走弱日正是买点窗口，跌了仍留池盯回踩，"
            "不能像算力那样一跌就踢出；大涨后等回踩，涨停日不追。"
        ),
    },
    {
        "id": "semi_equipment",
        "name": "半导体设备",
        "priority": 5,
        "keywords": [
            "半导体设备",
            "前道设备",
            "后道设备",
            "刻蚀",
            "薄膜沉积",
            "清洗设备",
            "涂胶显影",
            "CMP",
            "量检测",
            "国产设备",
            "晶圆设备",
        ],
        "news_keys": [
            "半导体设备",
            "刻蚀",
            "薄膜沉积",
            "清洗设备",
            "国产设备",
            "晶圆厂扩产",
            "资本开支",
        ],
        "board_keys": ["半导体设备"],
        "industries": ["半导体设备"],
        "industry_label": "半导体设备",
        "seed_stocks": [
            ("688082", "盛美上海"),
            ("603061", "金海通"),
            ("002371", "北方华创"),
            ("688012", "中微公司"),
            ("688120", "华海清科"),
            ("688072", "拓荆科技"),
            ("688037", "芯源微"),
            ("300604", "长川科技"),
        ],
        "thesis": (
            "晶圆厂资本开支/国产替代下的前道与清洗/量检测设备链；"
            "与材料同属半导体制造上游，走强日跟温，走弱日留池盯缩量回踩/起稳。"
        ),
    },
    {
        "id": "minor_metals",
        "name": "小金属",
        "priority": 5,
        "keywords": [
            "小金属",
            "锗",
            "钨",
            "钼",
            "锑",
            "钽",
            "镓",
            "铟",
            "稀土",
            "金属涨价",
            "战略金属",
        ],
        "news_keys": [
            "小金属",
            "锗",
            "钨",
            "钼",
            "锑",
            "钽",
            "镓",
            "战略金属",
            "金属涨价",
            # 部分小金属（锗等）受益半导体/红外景气
            "半导体",
            "红外",
        ],
        "board_keys": ["小金属", "小金属概念"],
        "industries": ["小金属"],
        "seed_stocks": [
            ("002428", "云南锗业"),
            ("000657", "中钨高新"),
            ("600549", "厦门钨业"),
            ("000962", "东方钽业"),
            ("002182", "宝武镁业"),
        ],
        "thesis": (
            "小金属偏供给约束+战略资源定价；锗等与半导体/红外有交集。"
            "弹性大、情绪重，优先中军回踩，避免一日游小票。"
        ),
    },
    {
        "id": "fiber_cable",
        "name": "光纤光缆",
        "priority": 5,
        "keywords": [
            "光纤",
            "光缆",
            "光纤光缆",
            "预制棒",
            "光棒",
            "通信线缆",
            "裸纤",
            "G.652",
            "光纤涨价",
            # 算力基建间接需求（不与光模块主题混用模块关键词）
            "算力",
            "数据中心",
        ],
        "news_keys": [
            "光纤",
            "光缆",
            "光纤光缆",
            "预制棒",
            "光棒",
            "通信线缆",
            "光纤涨价",
            "裸纤",
            "算力",
            "数据中心",
        ],
        "board_keys": ["光纤", "光缆", "通信线缆", "光纤概念"],
        "industries": ["通信线缆及配套"],
        "seed_stocks": [
            ("600487", "亨通光电"),
            ("601869", "长飞光纤"),
            ("600522", "中天科技"),
            ("600498", "烽火通信"),
        ],
        "thesis": (
            "光纤光缆有独立涨价/供需周期（AI基建、出口、供给偏紧），"
            "与光模块不是同一条腿；杀跌后起稳可观察中军，涨停日不追。"
        ),
    },
    {
        "id": "pcb_ccl",
        "name": "PCB/印制电路板",
        "priority": 5,
        "keywords": [
            "PCB",
            "印制电路板",
            "覆铜板",
            "高频板",
            "HDI",
            "封装基板",
            "AI服务器板",
        ],
        "news_keys": ["PCB", "印制电路板", "覆铜板", "HDI", "封装基板"],
        "board_keys": ["PCB", "印制电路板", "覆铜板"],
        "industries": ["印制电路板"],
        "seed_stocks": [
            ("002463", "沪电股份"),
            ("600183", "生益科技"),
            ("002815", "崇达技术"),
            ("600667", "太极实业"),
            ("001232", "嘉立创"),
        ],
        "thesis": (
            "算力硬件上游PCB/覆铜板：中军看沪电/生益；"
            "嘉立创偏小批量快板/PCBA一站式（非服务器板中军），业绩弹性+新股波动大，回踩观察、高位不追；"
            "活跃旁支（太极等）有资金认可也进；挖坑日仍留池，不能一跌就踢。"
        ),
    },
]

# 兼容旧名：不再当作白名单门禁
THEME_CATALOG = THEME_HINTS

# 情绪/打板/避险脉冲板：不做 1～2 周景气主题
_EMOTION_BOARD_SUBSTR = (
    "两连板",
    "连阳",
    "三连阳",
    "昨日涨停",
    "涨停板",
    "炸板",
    "ST股",
    "ST板",
    "微盘股",
    "人气榜",
    "连续涨停",
    "首板",
    "高标",
    "游资",
    "含B股",
    "昨日触板",
    # 技术统计/情绪筛选项，不是产业景气
    "历史新高",
    "百日新高",
    "半年新高",
    "一年新高",
    "创新高",
    "近期新高",
    "阶段新高",
    "破净股",
    "高市净率",
    "高市盈率",
    "低市盈率",
    "高股息",
    "高振幅",
    "昨日高振幅",
    "振幅榜",
    "百元股",
    "千元股",
    "破板",
    "跌停",
    "昨日跌停",
    "大涨",
    "大跌",
    "高换手",
    "低换手",
    "成交额",
    "量能",
    # 东财杂糅/虚拟标签：名字含产业词但不是真产业板（如「虚拟机器人」）
    "虚拟机器人",
    "虚拟现实概念",
)

# 宽基/通道/财报筛选项：资金体量极大，会挤掉真正的产业主题
_META_BOARD_SUBSTR = (
    "融资融券",
    "深股通",
    "沪股通",
    "港股通",
    "富时罗素",
    "MSCI",
    "标准普尔",
    "标普道琼斯",
    "东方财富热股",
    "证金持股",
    "机构重仓",
    "社保重仓",
    "基金重仓",
    "QFII",
    "沪深300",
    "中证500",
    "中证1000",
    "中证2000",
    "中证A50",
    "上证50",
    "上证180",
    "深成500",
    "深证成指",
    "创业板综",
    "创业板指",
    "科创50",
    "北证50",
    "转债标的",
    "股权转让",
    "次新股",
    "新股与次新股",
    "注册制次新股",
    "核准制次新股",
    "中报扭亏",
    "中报增长",
    "年报增长",
    "业绩预增",
    "业绩预减",
    "预增",
    "预减",
    "扭亏",
    # 宏观战略/政策箩筐概念：成分股跨行业，主题名虚、个股归属失真
    # （如中际旭创被塞进「节能环保/西部大开发」）
    "西部大开发",
    "一带一路",
    "京津冀",
    "粤港澳",
    "大湾区",
    "长江经济带",
    "长三角",
    "雄安",
    "海南自贸",
    "自由贸易港",
    "共同富裕",
    "乡村振兴",
    "新型城镇化",
    "节能环保",
    "碳中和",
    "碳排放权",
    "美丽中国",
    "国企改革",
    "央企改革",
    "地方国资改革",
    "央企红利",
    "专精特新",
    "创投概念",
    "并购重组",
    "股权激励",
    "分拆上市",
    "高送转",
    "中字头",
    "茅指数",
    "宁组合",
    "综合Ⅲ",
)

# 主题禁区：关键词蹭新闻、无产业景气闭环，强制不进观察池（含昨日延续）
_BANNED_THEME_SUBSTR = (
    "物流",
    "房地产",
    "地产",
    "物业管理",
    "园区开发",
)

# 过宽行业：行业直击发现时跳过，避免「电子」一筐装服务器/半导体小票
# 「计算机设备」仍可用于取资金，但展示必须靠 industry_label 拆成国产服务器/海外组装
_TOO_BROAD_INDUSTRIES = {
    "电子",
    "综合",
    "综合Ⅱ",
    "综合Ⅲ",
    "其他电子Ⅱ",
    "其他电子Ⅲ",
}

# 过宽行业名：新闻里单字/两字命中极易误伤（如「欧洲汽车股」打开「汽车」主题）
# 行业直击时必须搭配更具体催化词，不能只靠光秃行业名。
_BROAD_INDUSTRY_NEWS_EXTRA: Dict[str, Tuple[str, ...]] = {
    "汽车": ("整车", "乘用车", "商用车", "新能源车", "汽车销量", "智驾", "车企"),
    "电子": ("消费电子", "电子元件", "电子化学品", "电子制造"),
    "通信": ("光模块", "CPO", "光通信", "5G", "6G", "基站"),
    "机械": ("工程机械", "机床", "机器人"),
}

# 「汽车」等短键用 contains 会误吞「汽车芯片/汽车电子」；这些后缀说明不是整车主线
_BROAD_KEY_BLOCK_SUFFIX: Dict[str, Tuple[str, ...]] = {
    "汽车": ("芯片", "电子", "零部件", "服务", "金融", "租赁", "经销"),
    "电力": ("电子",),
    "通信": ("线缆",),
}


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _to_float(v) -> Optional[float]:
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        if isinstance(v, str):
            v = v.replace("%", "").replace(",", "").strip()
            if v in ("", "-", "None", "nan"):
                return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _stars_glyph(n: int) -> str:
    n = max(1, min(5, int(n)))
    return "★" * n + "☆" * (5 - n)


def _apply_newcomer_star_cap(
    stars: int, consecutive: int, *, label: str = "新进"
) -> Tuple[int, str]:
    """连入/主题走好不足 2 日：星级封顶，避免临时加主题直接五星。"""
    stars = max(1, min(5, int(stars)))
    if int(consecutive or 0) < 2 and stars > NEWCOMER_STAR_CAP:
        return (
            NEWCOMER_STAR_CAP,
            f"{label}不足2日，星级封顶{NEWCOMER_STAR_CAP}星（防临时加主题暴涨星）",
        )
    return stars, ""


def _suggest_buy_range(
    px: Optional[float],
    pct: Optional[float] = None,
    pct5: Optional[float] = None,
) -> str:
    """兼容旧接口：只返回价格区间。"""
    rng, _action = _suggest_buy_plan(px, pct, pct5)
    return rng


def _day_kline_structure(
    *,
    open_: Optional[float] = None,
    high: Optional[float] = None,
    low: Optional[float] = None,
    close: Optional[float] = None,
    prev_close: Optional[float] = None,
    vol_ratio: Optional[float] = None,
) -> Dict[str, Any]:
    """
    用开/高/低/收 + 量比读当日K，不只看「收盘相对昨收」涨跌幅。

    关键区分：
    - 真回踩/起稳：缩量阳、缩量收敛、低开承接，非高开低走
    - 冲高回落/高开低走：即便收盘微涨，也不是回踩买点
    """
    out: Dict[str, Any] = {
        "ok": False,
        "candle": "",
        "gap": "",
        "shape": "",
        "why": "",
        "labels": [],
        "is_yin": False,
        "is_yang": False,
        "gap_up": False,
        "gap_down": False,
        "high_open_low_close": False,
        "upper_reject": False,
        "shrink_vol": False,
        "expand_vol": False,
        "bullish_hold": False,
        "bearish_reject": False,
        "gap_pct": None,
    }
    if open_ is None or high is None or low is None or close is None:
        return out
    try:
        o, h, l, c = float(open_), float(high), float(low), float(close)
    except (TypeError, ValueError):
        return out
    if min(o, h, l, c) <= 0 or h < l:
        return out
    out["ok"] = True
    if c > o * 1.0005:
        out["candle"] = "阳"
        out["is_yang"] = True
    elif c < o * 0.9995:
        out["candle"] = "阴"
        out["is_yin"] = True
    else:
        out["candle"] = "平"

    prev = None
    if prev_close is not None:
        try:
            prev = float(prev_close)
        except (TypeError, ValueError):
            prev = None
    if prev is not None and prev > 0:
        gap_pct = (o / prev - 1.0) * 100.0
        out["gap_pct"] = gap_pct
        if gap_pct >= 0.8:
            out["gap"] = "高开"
            out["gap_up"] = True
        elif gap_pct <= -0.8:
            out["gap"] = "低开"
            out["gap_down"] = True
        else:
            out["gap"] = "平开"
    else:
        out["gap"] = ""

    rng = h - l
    upper = h - max(o, c)
    lower = min(o, c) - l
    # 高开低走：高开且收阴（收盘低于开盘）
    if out["gap_up"] and out["is_yin"]:
        out["high_open_low_close"] = True
        out["labels"].append("高开低走")
    # 冲高回落：上影占比高
    if rng > 0 and upper / rng >= 0.42 and (upper / o) >= 0.012:
        out["upper_reject"] = True
        out["labels"].append("冲高回落")

    vr = None if vol_ratio is None else float(vol_ratio)
    if vr is not None:
        if vr <= 0.95:
            out["shrink_vol"] = True
            out["labels"].append("缩量")
        elif vr >= 1.30:
            out["expand_vol"] = True
            out["labels"].append("放量")

    # 即便收盘相对昨收仍红，高开低走阴、或冲高后收弱/放量，也不当回踩
    close_pos = ((c - l) / rng) if rng > 0 else 0.5
    if out["high_open_low_close"]:
        out["bearish_reject"] = True
    elif out["upper_reject"] and (
        out["is_yin"]
        or out["expand_vol"]
        or (out["gap_up"] and close_pos < 0.40)
    ):
        out["bearish_reject"] = True

    # 起稳/回踩友好K：缩量阳、缩量小实体、低开后收回，且非拒绝形态
    body_pct = abs(c - o) / o * 100.0
    if not out["bearish_reject"] and not out["high_open_low_close"]:
        if out["shrink_vol"] and out["is_yang"]:
            out["bullish_hold"] = True
            out["labels"].append("缩量阳")
        elif out["shrink_vol"] and body_pct <= 1.6:
            out["bullish_hold"] = True
            out["labels"].append("缩量收敛")
        elif out["gap_down"] and out["is_yang"] and (vr is None or vr < 1.35):
            out["bullish_hold"] = True
            out["labels"].append("低开收回")
        elif (
            out["is_yang"]
            and not out["upper_reject"]
            and body_pct >= 0.3
            and (vr is None or vr < 1.25)
        ):
            out["bullish_hold"] = True
            out["labels"].append("温和阳")
        elif (
            out["is_yin"]
            and out["shrink_vol"]
            and not out["gap_up"]
            and body_pct <= 2.0
            and (lower / rng >= 0.25 if rng > 0 else False)
        ):
            # 缩量阴但下影承接，可作为回踩日（非买点确认，供真回踩识别）
            out["labels"].append("缩量阴承接")

    gap_s = out["gap"] or "未知开盘"
    labs = "/".join(out["labels"]) if out["labels"] else out["candle"]
    out["shape"] = labs
    out["why"] = f"{gap_s}{out['candle']}（{labs}）"
    return out


def _quote_ohlc(q: Optional[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    """从行情字典取开高低收/昨收。"""
    q = q or {}
    return {
        "open": _to_float(q.get("开盘")),
        "high": _to_float(q.get("最高")),
        "low": _to_float(q.get("最低")),
        "close": _to_float(q.get("最新价")),
        "prev_close": _to_float(q.get("昨收")),
    }


def _detect_buy_setup(
    *,
    theme_ok: bool,
    theme_grade: str,
    stock_grade: str,
    stock_pct: Optional[float],
    stock_pct_5d: Optional[float],
    stock_flow: Optional[float],
    stock_flow_5d: Optional[float] = None,
    vol_ratio: Optional[float] = None,
    turnover: Optional[float] = None,
    mild_flow_days: int = 0,
    major_catalyst: bool = False,
    chase_reasons: Optional[List[str]] = None,
    open_: Optional[float] = None,
    high: Optional[float] = None,
    low: Optional[float] = None,
    close: Optional[float] = None,
    prev_close: Optional[float] = None,
    bar_struct: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    多形态买点识别（不只回踩一种）。须结合当日K线开高低收，禁止只看收盘涨跌幅。

    kind:
      - true_pullback: 先走高再缩量回踩（真回踩）
      - mild_inflow_run: 连续微涨+资金流入
      - stabilize_up: 止跌起稳 / 企稳后温和上涨
      - dig_watch: 连跌后收敛观察（选股对象，未确认买）
      - catalyst_grind: 重大催化确认下的缓涨
      - fake_pullback: 下跌中继/假回踩
      - reject_bar: 高开低走/冲高回落（收盘或仍红也否决）
      - none: 无明确买点

    buy_ok=True 才允许进「买入候选」。
    """
    chase_reasons = chase_reasons or []
    pct = stock_pct
    pct5 = stock_pct_5d
    flow = stock_flow
    flow5 = stock_flow_5d
    vr = vol_ratio
    close_v = close if close is not None else None
    ks = bar_struct if isinstance(bar_struct, dict) else None
    if not ks or not ks.get("ok"):
        ks = _day_kline_structure(
            open_=open_,
            high=high,
            low=low,
            close=close_v,
            prev_close=prev_close,
            vol_ratio=vr,
        )

    base = {
        "kind": "none",
        "buy_ok": False,
        "label": "无明确买点",
        "why": "",
        "action": "",
        "kline": ks.get("why") or "",
    }
    if chase_reasons or stock_grade == "走弱":
        return {**base, "why": "个股走弱/追高，不做买点（观察可留，持有期再止损）"}
    if not theme_ok:
        return {**base, "why": "主题未在观察池"}
    theme_hard_down = theme_grade == "走弱"

    # —— 先看清今日K：高开低走/冲高回落，即便收盘微涨也不是回踩 ——
    if ks.get("ok") and ks.get("bearish_reject"):
        return {
            "kind": "reject_bar",
            "buy_ok": False,
            "label": "冲高回落",
            "why": f"今日K线{ks.get('why')}，属高开低走/冲高回落，不是真回踩",
            "action": "勿当回踩买：等缩量收敛或真正回踩后再看",
            "kline": ks.get("why") or "",
        }

    k_ok = bool(ks.get("ok"))
    # 有K线数据时，买点须 K 结构友好；无K线则退回价量（并在文案标明）
    k_hold = (not k_ok) or bool(ks.get("bullish_hold"))
    k_pull_friendly = (not k_ok) or (
        bool(ks.get("bullish_hold"))
        or ("缩量阴承接" in (ks.get("labels") or []))
        or (ks.get("is_yin") and ks.get("shrink_vol") and not ks.get("gap_up"))
    )

    # —— 选股期：连跌/阴跌后的微跌·起稳 ≠ 假回踩 ——
    post_decline = (
        pct5 is not None
        and -14.0 <= float(pct5) < 4.0
        and pct is not None
        and -2.2 <= float(pct) <= 2.8
        and stock_grade in ("偏好", "偏弱", "走强")
        and (vr is None or float(vr) <= 1.45)
        and (flow is None or float(flow) >= -1.2)
    )
    if post_decline and float(pct5) < 3.0:
        stabilize_buy = (
            not theme_hard_down
            and k_hold
            and float(pct) >= 0.2
            and flow is not None
            and float(flow) > 0
            and stock_grade in ("走强", "偏好")
            and (
                flow5 is None
                or float(flow5) >= -2.0
                or float(flow) >= 1.0
            )
            and (not k_ok or ks.get("is_yang") or ks.get("bullish_hold"))
        )
        if stabilize_buy:
            return {
                "kind": "stabilize_up",
                "buy_ok": True,
                "label": "止跌起稳",
                "why": (
                    f"连跌/整理后起稳（5日{float(pct5):+.1f}%，今日{float(pct):+.1f}%）"
                    f"且流入{float(flow):.2f}亿"
                    + (f"；K线{ks.get('why')}" if k_ok else "")
                ),
                "action": "可小仓短持1～3天：止跌起稳跟踪，跌破近两日低点走",
                "kline": ks.get("why") or "",
            }
        return {
            "kind": "dig_watch",
            "buy_ok": False,
            "label": "止跌观察",
            "why": (
                f"连跌/整理后收敛（5日{float(pct5):+.1f}%，今日{float(pct):+.1f}%）"
                + (f"；K线{ks.get('why')}" if k_ok else "，等流入/K线确认再买")
            ),
            "action": "先观察起稳：缩量止跌可盯，放量破位放弃",
            "kline": ks.get("why") or "",
        }

    # —— 假回踩否决：仅针对「看起来像涨后回踩」但结构不对 ——
    looks_like_pullback_shape = (
        pct is not None
        and -3.0 <= float(pct) <= 1.5
        and stock_grade in ("偏好", "偏弱")
        and pct5 is not None
        and float(pct5) >= 3.0
    )
    if looks_like_pullback_shape:
        fake_whys: List[str] = []
        if theme_grade in ("偏弱", "走弱"):
            fake_whys.append("主题偏弱，回踩多为下跌中继")
        if pct5 is not None and float(pct5) <= -5.0:
            fake_whys.append(f"近5日仍深跌{float(pct5):.1f}%，反抽不是回踩")
        if flow is not None and float(flow) < -0.35 and (
            flow5 is None or float(flow5) <= 0
        ):
            fake_whys.append("回踩日资金仍在出、近5日无累积")
        if (
            pct is not None
            and float(pct) <= -2.0
            and pct5 is not None
            and float(pct5) < 6.0
        ):
            fake_whys.append("当日仍在跌且此前涨幅不足，像破位不是回踩")
        if k_ok and ks.get("gap_up") and ks.get("is_yin"):
            fake_whys.append(f"K线{ks.get('why')}，高开低走不是回踩")
        if fake_whys:
            return {
                "kind": "fake_pullback",
                "buy_ok": False,
                "label": "假回踩",
                "why": "；".join(fake_whys),
                "action": "勿当回踩买：下跌中继/假回踩，先等真正企稳",
                "kline": ks.get("why") or "",
            }

    if theme_hard_down:
        return {**base, "why": "板块当日走弱，仅观察挖坑，不做追涨买点"}

    # —— 1) 真回踩：先走高再收敛；K线须是缩量阴/收敛，不是高开冲高 ——
    pullback_day = (
        pct is not None
        and -3.2 <= float(pct) <= 1.2
        and stock_grade in ("偏好", "偏弱")
        and k_pull_friendly
        and (not k_ok or not ks.get("gap_up") or ks.get("shrink_vol"))
    )
    pct5_ok_pullback = pct5 is not None and (
        (5.0 <= float(pct5) < 16.0)
        or (pullback_day and 5.0 <= float(pct5) < 28.0 and (vr is None or float(vr) <= 1.2))
    )
    true_pullback = (
        theme_grade in ("走强", "偏好")
        and pct5_ok_pullback
        and pullback_day
        and (flow is None or float(flow) >= -0.35)
        and (flow5 is None or float(flow5) > 0)
        and (vr is None or float(vr) <= 1.25)
        and (turnover is None or float(turnover) < 12.0)
        and (not k_ok or not ks.get("is_yang") or ks.get("shrink_vol"))
    )
    # 真回踩日更常见缩量阴/平；若收阳须明显缩量且非冲高
    if true_pullback and k_ok and ks.get("is_yang") and not ks.get("shrink_vol"):
        true_pullback = False
    if true_pullback:
        return {
            "kind": "true_pullback",
            "buy_ok": True,
            "label": "真回踩",
            "why": (
                f"近5日先涨{float(pct5):.1f}%后今日收敛{float(pct):+.1f}%，"
                "资金未撤离、主题偏好"
                + (f"；K线{ks.get('why')}" if k_ok else "")
                + ("（缩量回踩，允许5日偏热）" if float(pct5) >= 16.0 else "")
            ),
            "action": "可小仓短持1～3天：真回踩波段，破位止损，勿改成长线等回踩",
            "kline": ks.get("why") or "",
        }

    # —— 2) 连续微涨+资金流入 ——
    mild_inflow_run = (
        theme_grade in ("走强", "偏好")
        and mild_flow_days >= 2
        and pct is not None
        and 0.0 <= float(pct) < 3.8
        and flow is not None
        and float(flow) > 0
        and stock_grade in ("走强", "偏好")
        and (pct5 is None or -3.0 <= float(pct5) < 14.0)
        and k_hold
        and (not k_ok or ks.get("is_yang") or ks.get("bullish_hold"))
    )
    if mild_inflow_run:
        return {
            "kind": "mild_inflow_run",
            "buy_ok": True,
            "label": "连涨流入",
            "why": (
                f"连续{mild_flow_days}日微涨且资金流入，缓涨结构"
                + (f"；K线{ks.get('why')}" if k_ok else "")
            ),
            "action": "可小仓短持1～3天：跟温不追尖峰，冲高减、放量长阴走",
            "kline": ks.get("why") or "",
        }

    # —— 3) 企稳后温和上涨 ——
    stabilize_up = (
        theme_grade in ("走强", "偏好")
        and mild_flow_days >= 1
        and pct5 is not None
        and -10.0 <= float(pct5) < 12.0
        and pct is not None
        and 0.3 <= float(pct) < 3.5
        and flow is not None
        and float(flow) > 0
        and (flow5 is None or float(flow5) >= -1.0 or float(flow) >= 1.0)
        and stock_grade in ("走强", "偏好")
        and k_hold
        and (not k_ok or ks.get("is_yang"))
    )
    if stabilize_up:
        return {
            "kind": "stabilize_up",
            "buy_ok": True,
            "label": "企稳缓涨",
            "why": (
                f"企稳后温和上涨（5日{float(pct5):+.1f}%，今日{float(pct):+.1f}%）"
                f"且近{max(mild_flow_days, 1)}日有流入"
                + (f"；K线{ks.get('why')}" if k_ok else "")
            ),
            "action": "可小仓短持1～3天：企稳缓涨跟踪，跌破近两日低点走",
            "kline": ks.get("why") or "",
        }

    # —— 4) 重大催化 + 缓涨确认 ——
    catalyst_grind = (
        major_catalyst
        and theme_grade in ("走强", "偏好")
        and pct is not None
        and 0.6 <= float(pct) < 4.0
        and flow is not None
        and float(flow) > 0
        and (flow5 is None or float(flow5) >= 0)
        and stock_grade in ("走强", "偏好")
        and (pct5 is None or -5.0 <= float(pct5) < 16.0)
        and (vr is None or float(vr) < 2.2)
        and k_hold
        and (not k_ok or not ks.get("is_yin"))
    )
    if catalyst_grind:
        return {
            "kind": "catalyst_grind",
            "buy_ok": True,
            "label": "催化缓涨",
            "why": (
                "重大催化确认下温和上涨且资金流入，允许不等人字形回踩"
                + (f"；K线{ks.get('why')}" if k_ok else "")
            ),
            "action": "可小仓短持1～3天：催化缓涨确认，勿追涨停；冲高减、破位走",
            "kline": ks.get("why") or "",
        }

    if k_ok and not ks.get("bullish_hold") and pct is not None and abs(float(pct)) < 3:
        return {
            **base,
            "why": f"今日K线{ks.get('why')}，结构未到可买（需缩量阳/收敛或真回踩）",
            "kline": ks.get("why") or "",
        }
    return base


def _detect_chase_or_fake(
    *,
    stock_pct: Optional[float],
    stock_pct_5d: Optional[float],
    stock_flow: Optional[float],
    theme_grade: str,
    board_pct: Optional[float],
) -> Tuple[List[str], int]:
    """
    识别追高 / 假强（板块弱个股强、大涨却大幅流出等）。
    返回 (扣分原因列表, 额外扣星数，负数或0)。
    """
    reasons: List[str] = []
    penalty = 0
    pct = stock_pct
    flow = stock_flow

    # 1) 板块弱 + 个股逆势涨：风华典型
    if theme_grade in ("偏弱", "走弱") and pct is not None and pct >= 2.0:
        penalty -= 1
        reasons.append(
            f"板块{theme_grade}而个股涨{pct:.1f}%，逆势抢跑 -1"
        )
        if theme_grade == "走弱" or (board_pct is not None and board_pct <= -2.0):
            penalty -= 1
            reasons.append("板块明显偏弱下的个股独立大涨，跟风风险再 -1")

    # 2) 大涨却主力大幅净流出：冲高回落/炸板出货特征
    if pct is not None and pct >= 3.0 and flow is not None and flow <= -1.0:
        penalty -= 1
        reasons.append(
            f"涨{pct:.1f}%但主力净流出{abs(flow):.2f}亿，疑似冲高回落/出货 -1"
        )
        if flow <= -3.0 or pct >= 5.0:
            penalty -= 1
            reasons.append("大涨+大幅流出共振，追高风险再 -1")

    # 3) 当日涨幅过大：买入风险
    if pct is not None and pct >= 9.5:
        penalty -= 1
        reasons.append(f"接近涨停({pct:.1f}%)，现价买入风险 -1")
    elif pct is not None and pct >= 5.0:
        penalty -= 1
        reasons.append(f"当日大涨{pct:.1f}%，不宜追高 -1")

    # 4) 5日已大幅扩展——只打「还在冲」的；缩量回踩/十字星日不因5日涨幅一票否决
    if stock_pct_5d is not None and float(stock_pct_5d) >= 18:
        if pct is not None and float(pct) >= 2.0:
            penalty -= 1
            reasons.append(
                f"近5日涨{float(stock_pct_5d):.1f}%且今日仍冲{float(pct):+.1f}%，追高风险 -1"
            )
        elif float(stock_pct_5d) >= 40.0 and (
            pct is None or float(pct) >= 0.5
        ):
            # 云南锗业式连板后的极端扩张，横着也不当短线买点
            penalty -= 1
            reasons.append(
                f"近5日涨{float(stock_pct_5d):.1f}%极端扩张，短线风险 -1"
            )
    elif stock_pct_5d is not None and float(stock_pct_5d) >= 12 and (
        pct is not None and float(pct) >= 3.0
    ):
        penalty -= 1
        reasons.append(f"5日已涨{float(stock_pct_5d):.1f}%且今日仍强冲，扩展风险 -1")

    return reasons, penalty


def _suggest_buy_plan(
    px: Optional[float],
    pct: Optional[float] = None,
    pct5: Optional[float] = None,
    *,
    stock_flow: Optional[float] = None,
    theme_grade: str = "偏好",
    stock_grade: str = "偏好",
    stars: int = 2,
    chase_penalties: Optional[List[str]] = None,
    buy_setup: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    """
    返回 (建议买入区间, 操作建议)。
    操作建议按买点形态：真回踩/连涨流入/企稳缓涨/催化缓涨；假回踩明确否决。
    """
    chase_penalties = chase_penalties or []
    setup = buy_setup or {}
    if px is None or px <= 0:
        return "-", "无价格，暂不给出区间"

    pct_v = 0.0 if pct is None else float(pct)
    pct5_v = 0.0 if pct5 is None else float(pct5)
    ref = px / (1.0 + pct5_v / 100.0) if pct5_v > -90 else px

    # 假回踩：绝不给「可盯回踩」
    if setup.get("kind") == "fake_pullback":
        lo, hi = px * 0.97, px * 1.01
        return f"{lo:.2f}~{hi:.2f}", str(
            setup.get("action") or "勿当回踩买：下跌中继/假回踩"
        )

    # 明确买点形态：用形态文案
    if setup.get("buy_ok") and setup.get("action"):
        kind = str(setup.get("kind") or "")
        if kind == "true_pullback":
            lo = min(px * 0.985, ref * 1.01) if ref < px else px * 0.98
            hi = px * 1.005
        elif kind in ("mild_inflow_run", "stabilize_up", "catalyst_grind"):
            lo = px * 0.97
            hi = px * 0.995
        else:
            lo, hi = px * 0.97, px * 1.00
        if lo > hi:
            lo, hi = px * 0.97, px * 1.00
        return f"{lo:.2f}~{hi:.2f}", str(setup["action"])

    hard_chase = bool(chase_penalties) or pct_v >= 5.0 or pct5_v >= 15.0
    fake_outflow = (
        pct_v >= 3.0 and stock_flow is not None and stock_flow <= -1.0
    )
    theme_bad = theme_grade in ("偏弱", "走弱")

    if theme_bad and (pct_v >= 2.0 or fake_outflow):
        lo = min(px * 0.90, ref * 1.00)
        hi = px * 0.94
        if lo > hi:
            lo, hi = px * 0.88, px * 0.93
        action = "观望勿追：板块偏弱+个股抢跑，等板块止跌"
    elif hard_chase or fake_outflow:
        mid = (ref + px) / 2.0
        lo = mid * 0.97
        hi = px * 0.95
        if lo > hi:
            lo, hi = px * 0.90, px * 0.94
        if fake_outflow:
            action = "观望勿追：大涨却资金流出，防冲高回落"
        elif pct_v >= 9.5:
            action = "观望勿追：涨停附近不买，等开板再评估"
        else:
            action = "观望勿追：短线已偏强，不追分时尖峰"
    elif pct5_v <= -10 or pct_v <= -7:
        lo = px * 0.97
        hi = px * 1.02
        action = "先等企稳：大跌后不抄死底，站稳再看"
    elif (
        theme_grade in ("走强", "偏好")
        and stock_grade in ("偏好", "偏弱")
        and stars >= 3
        and pct5_v >= 5.0
    ):
        # 只有近5日先走过一波，才提「回踩」字样
        lo = min(px * 0.96, ref * 1.01) if ref < px else px * 0.97
        hi = px * 0.995
        if lo > hi:
            lo, hi = px * 0.97, px * 1.00
        action = "观察真回踩：需缩量+资金不撤离，假回踩不买"
    elif theme_grade in ("走强", "偏好") and stock_grade == "走强" and pct_v < 4:
        lo = px * 0.96
        hi = px * 0.99
        action = "偏强跟踪：更优等催化缓涨/连涨流入确认，不追尖峰"
    else:
        lo = min(px * 0.96, ref * 1.01) if ref < px else px * 0.97
        hi = px * 1.00
        if lo > hi:
            lo, hi = px * 0.97, px * 1.00
        action = "继续观察：买点未成形（回踩/缓涨/催化均未满足）"

    if lo > hi:
        lo, hi = hi * 0.98, hi
    return f"{lo:.2f}~{hi:.2f}", action


def _load_history() -> Dict[str, Any]:
    if not HISTORY_PATH.exists():
        return {"days": {}}
    try:
        data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("days"), dict):
            return data
    except Exception:
        pass
    return {"days": {}}


def _save_history(data: Dict[str, Any]) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _save_latest(payload: Dict[str, Any]) -> None:
    LATEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    LATEST_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_latest_forward_watch() -> Dict[str, Any]:
    if not LATEST_PATH.exists():
        return {}
    try:
        data = json.loads(LATEST_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _match_news(theme: Dict[str, Any], news_df: pd.DataFrame) -> List[str]:
    hits: List[str] = []
    if news_df is None or news_df.empty:
        return hits
    keys = [str(k).lower() for k in theme.get("keywords") or []]
    for _, r in news_df.iterrows():
        title = str(r.get("title") or "")
        low = title.lower()
        if any(k.lower() in low or k in title for k in theme.get("keywords") or []):
            hits.append(title[:80])
        elif any(k in low for k in keys):
            hits.append(title[:80])
        if len(hits) >= 5:
            break
    return hits


def _blocked_compound_board(board_name: str, key: str) -> bool:
    """短键误吞复合概念：如 key=汽车 且 board=汽车芯片 → True。"""
    bn = str(board_name or "")
    k = str(key or "")
    if not k or k not in bn or bn == k:
        return False
    for stem, suffixes in _BROAD_KEY_BLOCK_SUFFIX.items():
        if k != stem and not k.startswith(stem):
            continue
        rest = bn.split(k, 1)[-1]
        if any(rest.startswith(sfx) for sfx in suffixes):
            return True
    return False


def _match_boards(theme: Dict[str, Any], boards: pd.DataFrame) -> pd.DataFrame:
    if boards is None or boards.empty or "板块名称" not in boards.columns:
        return pd.DataFrame()
    keys = [str(k) for k in (theme.get("board_keys") or []) if k]
    # 延续主题若 board_keys 被收成单板名，补回 hint 的产业关键词，避免锁死坏板
    tid = str(theme.get("id") or theme.get("_hint_id") or "")
    tname = str(theme.get("name") or "")
    for h in THEME_HINTS:
        if h.get("id") == tid or h.get("name") == tname:
            for k in h.get("board_keys") or []:
                ks = str(k)
                if ks and ks not in keys:
                    keys.append(ks)
            break

    mask = False
    for k in keys:
        hit = boards["板块名称"].astype(str).str.contains(str(k), na=False)
        if any(k == stem or k.startswith(stem) for stem in _BROAD_KEY_BLOCK_SUFFIX):
            names = boards["板块名称"].astype(str)
            hit = hit & ~names.map(lambda bn, kk=k: _blocked_compound_board(bn, kk))
        mask = mask | hit
    if isinstance(mask, bool):
        return pd.DataFrame()
    sub = boards.loc[mask].copy()
    if sub.empty:
        return sub
    # 去掉情绪/虚拟杂糅板，避免「虚拟机器人」靠5日涨幅抢走真·机器人行业板
    if "板块名称" in sub.columns:
        keep = []
        for _, row in sub.iterrows():
            bn = str(row.get("板块名称") or "")
            bt = str(row.get("类型") or "")
            keep.append(not _is_emotion_board(bn, bt))
        sub = sub.loc[keep].copy()
    if sub.empty:
        return sub

    # 排序：行业优先 + 资金态度 + 走势；大额流出重罚（防虚涨概念板占坑）
    pct5 = (
        pd.to_numeric(sub["涨跌幅_5日"], errors="coerce")
        if "涨跌幅_5日" in sub.columns
        else pd.Series(0, index=sub.index)
    )
    flow = (
        pd.to_numeric(sub["主力净流入_亿"], errors="coerce")
        if "主力净流入_亿" in sub.columns
        else pd.Series(0, index=sub.index)
    )
    pct = (
        pd.to_numeric(sub["涨跌幅"], errors="coerce")
        if "涨跌幅" in sub.columns
        else pd.Series(0, index=sub.index)
    )
    flow5 = (
        pd.to_numeric(sub["主力净流入_5日_亿"], errors="coerce")
        if "主力净流入_5日_亿" in sub.columns
        else pd.Series(0, index=sub.index)
    )
    btypes = (
        sub["类型"].astype(str)
        if "类型" in sub.columns
        else pd.Series("", index=sub.index)
    )
    names = sub["板块名称"].astype(str)

    def _name_fit(bn: str) -> float:
        # 精确/短名匹配加分：「机器人」>「XX机器人概念」
        best = 0.0
        for k in keys:
            if _blocked_compound_board(bn, k):
                continue
            if bn == k:
                best = max(best, 12.0)
            elif bn.startswith(k) and len(bn) <= len(k) + 2:
                best = max(best, 8.0)
            elif k in bn:
                # 前缀杂质（虚拟/其他）降权
                if bn.startswith(k):
                    best = max(best, 4.0)
                else:
                    best = max(best, 1.0)
        return best

    type_bonus = btypes.map(
        lambda t: 10.0 if t == "行业" else (2.0 if t == "概念" else 0.0)
    )
    fit_bonus = names.map(_name_fit)
    flow_v = flow.fillna(0)
    # 流入正常加权；大额流出重罚
    flow_term = flow_v.where(flow_v >= 0, flow_v * 1.2)
    score = (
        pct5.fillna(0) * 1.2
        + flow_term * 0.45
        + flow5.fillna(0) * 0.08
        + pct.fillna(0) * 0.6
        + type_bonus
        + fit_bonus
    )
    sub = sub.assign(_score=score).sort_values(
        "_score", ascending=False, na_position="last"
    )
    return sub.head(5)


def _is_banned_theme(name: str) -> bool:
    n = str(name or "")
    if any(s in n for s in _BANNED_THEME_SUBSTR):
        return True
    # 笼统「电子/综合」等不作独立观察主题
    key = _normalize_concept_key(n) or n
    if n in _TOO_BROAD_INDUSTRIES or key in _TOO_BROAD_INDUSTRIES:
        return True
    return False


def _is_emotion_board(name: str, btype: str = "") -> bool:
    n = str(name or "")
    if _is_banned_theme(n):
        return True
    if any(s in n for s in _EMOTION_BOARD_SUBSTR):
        return True
    if any(s in n for s in _META_BOARD_SUBSTR):
        return True
    # 虚拟打板池代码常见前缀
    if str(btype or "") == "市场" and ("综" in n or "指" in n):
        return True
    return False


def _hint_for_board(board_name: str) -> Optional[Dict[str, Any]]:
    """按 board_keys / industries / 细分行业标签 / 主题名 匹配结构佐证。"""
    name = str(board_name or "").strip()
    if not name:
        return None
    best = None
    best_len = 0

    def _consider(h: Dict[str, Any], score: int) -> None:
        nonlocal best, best_len
        if score > best_len:
            best = h
            best_len = score

    def _key_hit(k: str) -> int:
        if not k:
            return 0
        if name == k or name == k + "概念":
            return len(k) + 20
        for suf in ("Ⅰ", "Ⅱ", "Ⅲ", "I", "II", "III"):
            if name == k + suf:
                return len(k) + 15
        # 允许「通信线缆」命中「通信线缆及配套」；「电网」命中「电网设备」
        if name.startswith(k) and len(name) > len(k) and len(k) >= 2:
            nxt = name[len(k) : len(k) + 1]
            if nxt in ("及", "/", "(", "（", " ", "设", "整", "零", "Ⅰ", "Ⅱ", "Ⅲ"):
                return len(k) + 8
        return 0

    for h in THEME_HINTS:
        hname = str(h.get("name") or "")
        hid = str(h.get("id") or "")
        label = str(h.get("industry_label") or "")
        group = str(h.get("concept_group") or "")
        if name == hname:
            _consider(h, 100 + len(hname))
            continue
        if label and name == label:
            _consider(h, 98 + len(label))
            continue
        if name == hid:
            _consider(h, 90)
            continue
        if group and name == group:
            # 仅概念组名：列表在前的频道优先，姊妹池靠 sibling 注入
            _consider(h, 50 + int(h.get("priority") or 0))
            continue
        for k in (
            list(h.get("board_keys") or [])
            + list(h.get("industries") or [])
            + list(h.get("keywords") or [])
        ):
            sc = _key_hit(str(k))
            if sc:
                _consider(h, sc)
    return best


def _split_sibling_hints(hint: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """同概念拆频道：国产服务器 / 海外组装 等成对注入。"""
    if not hint:
        return []
    group = str(hint.get("split_group") or "").strip()
    if not group:
        return []
    hid = str(hint.get("id") or "")
    return [
        h
        for h in THEME_HINTS
        if str(h.get("split_group") or "") == group and str(h.get("id") or "") != hid
    ]


def _apply_structure_labels(
    theme: Dict[str, Any],
    hint: Optional[Dict[str, Any]],
    em_industry: str,
) -> Dict[str, Any]:
    """概念可共用；行业列用细分标签；资金仍盯东财行业/概念板。"""
    em = str(em_industry or theme.get("_matched_board") or "").strip()
    if hint:
        label = str(hint.get("industry_label") or "").strip()
        concept = str(hint.get("concept_group") or "").strip()
        theme["id"] = str(hint.get("id") or theme.get("id") or "")
        theme["name"] = str(hint.get("name") or theme.get("name") or "")
        theme["priority"] = int(hint.get("priority") or theme.get("priority") or 3)
        if hint.get("seed_stocks"):
            theme["seed_stocks"] = list(hint.get("seed_stocks") or [])
        if hint.get("board_keys"):
            theme["board_keys"] = list(hint.get("board_keys") or [])
        keys = list(theme.get("keywords") or [])
        for k in hint.get("keywords") or []:
            ks = str(k)
            if ks and ks not in keys:
                keys.append(ks)
        theme["keywords"] = keys
        if hint.get("thesis"):
            thesis = str(hint.get("thesis") or "")
            if theme.get("_carry") and thesis and not thesis.startswith("延续"):
                thesis = "延续前瞻：" + thesis
            theme["thesis"] = thesis
        theme["_concept"] = concept or str(theme.get("name") or theme.get("_concept") or "")
        theme["_industry"] = label or em or str(theme.get("_industry") or "-")
        theme["_hint_id"] = str(hint.get("id") or "")
        theme["_split_group"] = str(hint.get("split_group") or "")
    else:
        theme.setdefault("_concept", theme.get("name"))
        theme.setdefault("_industry", em or "-")
    theme["_em_industry"] = em or str(theme.get("_em_industry") or "")
    if em:
        theme["_matched_board"] = em
    return theme


# 旧主题 id → 新拆池（历史延续用）
_LEGACY_THEME_ID_MAP: Dict[str, List[str]] = {
    "domestic_compute": [
        "domestic_server",
        "overseas_odm",
        "liquid_cooling",
        "compute_rental",
    ],
}


# 结构主题种子股代码 → hint_id：成分股补齐时禁止跨主题抢走（如拓普归机器人）
_RESERVED_SEED_TO_HINT: Dict[str, str] = {}
for _h in THEME_HINTS:
    _hid = str(_h.get("id") or "")
    for _c, _n in list(_h.get("seed_stocks") or []):
        _code = str(_c or "").zfill(6)[-6:]
        if _code.isdigit() and len(_code) == 6 and _hid:
            # 先登记的主题保留（列表顺序：电网/算力优先于后写主题）
            _RESERVED_SEED_TO_HINT.setdefault(_code, _hid)


def _is_st_stock_name(name: str) -> bool:
    n = str(name or "").upper()
    return "ST" in n


# 市值排名/复盘类标题：不算「未来1～2周」实质催化
_WEAK_FORWARD_NEWS_SUBSTR = (
    "市值排名",
    "市值跻身",
    "一图速览",
    "热搜个股",
    "龙虎榜",
    "涨停复盘",
    "涨停板复盘",
)


def _is_weak_forward_news(title: str) -> bool:
    t = str(title or "")
    return any(s in t for s in _WEAK_FORWARD_NEWS_SUBSTR)


def _news_hits_for_keys(keys: List[str], news_df: pd.DataFrame) -> List[str]:
    hits: List[str] = []
    if news_df is None or news_df.empty or not keys:
        return hits
    clean = [str(k).strip() for k in keys if str(k).strip()]
    # 重大政策稿优先收集，再补普通命中；过滤市值排名等弱新闻
    majors: List[str] = []
    normals: List[str] = []
    for _, r in news_df.iterrows():
        title = str(r.get("title") or "")
        if _is_weak_forward_news(title):
            continue
        low = title.lower()
        if not any(k.lower() in low or k in title for k in clean):
            continue
        (majors if news_title_is_major_catalyst(title) else normals).append(title[:80])
        if len(majors) + len(normals) >= 8:
            break
    for title in majors + normals:
        if title not in hits:
            hits.append(title)
        if len(hits) >= 5:
            break
    return hits


def _major_catalyst_in_hits(news_hits: List[str]) -> bool:
    return any(
        news_title_is_major_catalyst(t) and not _is_weak_forward_news(t)
        for t in (news_hits or [])
    )


def _board_quality_score(
    *,
    board_name: str,
    board_pct: Optional[float],
    board_flow: Optional[float],
    board_pct5: Optional[float],
    board_flow5: Optional[float],
    news_hits: List[str],
    hint: Optional[Dict[str, Any]] = None,
) -> Tuple[int, float, bool, List[str]]:
    """
    前瞻打分（预测未来1～2周，不是表彰过去涨幅）。
    返回 (quality, rank_score, is_pulse, reasons)。
    quality>=3 才具备前瞻入围资格；pulse 一日游直接否决。
    """
    reasons: List[str] = []
    quality = 0
    pulse = False
    # 无催化的单日脉冲：已经发生的情绪，不是前瞻
    if (
        board_pct is not None
        and board_pct >= 2.5
        and not news_hits
        and (board_pct5 is None or board_pct5 < 4.0)
        and (board_pct5 is None or board_pct5 <= board_pct * 1.15 + 0.5)
    ):
        pulse = True

    # —— 1) 新闻/事件：最主要的前瞻催化 ——
    major = _major_catalyst_in_hits(news_hits)
    if news_hits:
        if major:
            quality += 4
            reasons.append(f"重大政策/投资利好：新闻命中{len(news_hits)}条（突发催化，允许冷板块启动）")
        else:
            quality += 3 if len(news_hits) >= 2 else 2
            reasons.append(f"前瞻催化：新闻命中{len(news_hits)}条")

    # —— 2) 资金萌芽：涨幅还没透支时的流入，偏「将走」——
    # 超大净流入多为宽基通道板，产业主题一般远低于此，打分时截断
    flow_cap = min(float(board_flow), 25.0) if board_flow is not None else None
    flow5_cap = min(max(float(board_flow5), 0.0), 60.0) if board_flow5 is not None else None
    # 近5日大跌后的单日回流：超跌反抽，不是「未透支的前瞻萌芽」
    deep_drawdown = board_pct5 is not None and float(board_pct5) <= -5.0
    early_flow = False
    if flow_cap is not None and flow_cap >= 0.8:
        if deep_drawdown:
            reasons.append(
                f"今日回流{board_flow:.2f}亿，但近5日跌{board_pct5:.1f}%属超跌反抽，"
                "不做资金萌芽加分"
            )
        elif board_pct5 is None or board_pct5 < 10.0:
            quality += 2
            early_flow = True
            reasons.append(
                f"资金萌芽：今日流入{board_flow:.2f}亿且近5日涨幅未透支"
                f"（5日{board_pct5 if board_pct5 is not None else '-'}%）"
            )
        else:
            reasons.append(
                f"今日仍流入{board_flow:.2f}亿，但5日已涨{board_pct5:.1f}%偏透支，前瞻降权"
            )
    if (
        not early_flow
        and not deep_drawdown
        and flow5_cap is not None
        and flow5_cap >= 2.0
        and (board_pct5 is None or board_pct5 < 8.0)
    ):
        quality += 1
        reasons.append(f"近5日资金温和累积约{board_flow5:.1f}亿（启动期特征）")
    elif deep_drawdown and flow5_cap is not None and flow5_cap >= 2.0:
        reasons.append(
            f"近5日资金口径仍流入约{board_flow5:.1f}亿，但价格深跌，需先确认企稳再谈前瞻"
        )

    # —— 3) 结构佐证（电网/算力/机器人/核电等）只加分，不单独保送 ——
    if hint is not None and int(hint.get("priority") or 0) >= 5:
        quality += 1
        reasons.append(f"结构主题佐证（{hint.get('name') or board_name}）+1")
        rank_boost = float(hint.get("priority") or 3) * 0.35
    elif hint is not None:
        rank_boost = float(hint.get("priority") or 3) * 0.25
    else:
        rank_boost = 0.0

    # —— 4) 过去涨幅只作「是否已透支」辅助，不当主入池分 ——
    sector_run = (
        board_pct5 is not None
        and board_pct5 >= 4.0
        and board_pct is not None
        and board_pct >= 1.2
    )
    if board_pct5 is not None and board_pct5 >= 15.0 and not news_hits:
        # 已大涨且无新催化：回看票，不是前瞻
        quality = min(quality, 1)
        reasons.append(f"近5日已涨{board_pct5:.1f}%且无新催化，偏回看透支，不做前瞻新开")
    elif board_pct5 is not None and 2.0 <= board_pct5 < 10.0 and news_hits:
        quality += 1
        reasons.append(f"辅助：5日已有启动{board_pct5:.1f}%且仍有新闻，后续1-2周可延续观察")
    elif board_pct5 is not None and board_pct5 >= 10.0 and news_hits:
        reasons.append(f"辅助：5日涨{board_pct5:.1f}%偏高，需靠新催化才继续前瞻")
    elif sector_run and (early_flow or (flow5_cap is not None and flow5_cap >= 1.5)):
        quality += 1
        reasons.append(
            f"板块集体走强：近5日{board_pct5:.1f}% / 今日{board_pct:+.2f}%，具备赚钱效应"
        )

    # 今日整理/温和：比已经大涨更适合「未来」窗口
    if (
        board_pct is not None
        and -1.5 <= board_pct <= 3.0
        and news_hits
        and (board_pct5 is None or board_pct5 < 12.0)
    ):
        quality += 1
        reasons.append(f"价格未失控（今日{board_pct:+.2f}%），留出未来1-2周空间")

    # 排名优先新闻/结构主题，资金流截断，避免宽基通道板靠体量霸榜
    rank = (
        len(news_hits) * 5.0
        + (12.0 if major else 0.0)
        + (flow_cap or 0) * 0.45
        + (flow5_cap or 0) * 0.2
        + (2.5 if sector_run else 0.0)
        # 涨幅过高扣分：越涨透支，前瞻排名越低
        - max(0.0, (board_pct5 or 0) - 8.0) * 0.8
        + (0.5 if (board_pct is not None and -1.0 <= board_pct <= 2.5) else 0.0)
        + rank_boost
    )
    return quality, rank, pulse, reasons


def _is_soft_homogeneous_theme(hint: Optional[Dict[str, Any]], board_name: str = "") -> bool:
    """银行/证券/煤炭等同质软主题：进池门槛更高（观察不过来）。"""
    blob = " ".join(
        [
            str((hint or {}).get("name") or ""),
            str((hint or {}).get("concept_group") or ""),
            str(board_name or ""),
        ]
    )
    return any(s in blob for s in _HOMOGENEOUS_THEME_SUBSTR)


def _forward_outlook_ok(
    *,
    quality: int,
    pulse: bool,
    news_hits: List[str],
    board_pct: Optional[float],
    board_flow: Optional[float],
    board_pct5: Optional[float],
    board_flow5: Optional[float] = None,
    hint: Optional[Dict[str, Any]] = None,
    board_name: str = "",
) -> Tuple[bool, str]:
    """
    新进主题门槛：未来约1～2个月是否值得盯（观察不过来，宁缺毋滥）。
    交叉确认 + 明显否决；同质软主题更严。已在池去留见 _theme_thesis_broken。
    """
    major = _major_catalyst_in_hits(news_hits)
    real_news = [t for t in (news_hits or []) if not _is_weak_forward_news(t)]
    soft = _is_soft_homogeneous_theme(hint, board_name)
    flow_pos = board_flow is not None and board_flow > 0
    flow5_pos = board_flow5 is not None and board_flow5 > 1.0

    # —— 明显否决 ——
    if pulse and not major:
        return False, "无催化单日脉冲，情绪一日游，不符1～2月前瞻"
    if (
        board_pct5 is not None
        and float(board_pct5) <= -5.0
        and not major
        and len(real_news) < 2
    ):
        return (
            False,
            f"近5日跌{float(board_pct5):.1f}%且缺实质前瞻新闻，单日回流属超跌反抽",
        )
    if board_pct is not None and board_pct <= -3.0 and not major:
        return False, "当日大跌走弱，新主题不进池"
    if (
        board_flow is not None
        and board_flow <= -3.0
        and (board_pct is None or board_pct < 0)
        and not major
    ):
        return False, "下跌且资金大幅流出，资金态度与中期前瞻相反"
    if (
        board_flow5 is not None
        and board_flow5 <= -8.0
        and (board_pct5 is None or board_pct5 < 0)
        and not news_hits
    ):
        return False, "近5日资金撤离且走弱、无新催化，不适合1～2月观察"
    if (
        board_pct5 is not None
        and board_pct5 >= 15.0
        and not news_hits
        and (board_flow is None or board_flow < 0.5)
    ):
        return False, "涨幅已透支且无新催化/资金，属回看不是前瞻"
    if soft and not major:
        if len(real_news) < 1 or not (
            (board_flow is not None and board_flow > 0)
            or (board_flow5 is not None and board_flow5 > 1.0)
        ):
            return False, "同质软主题须新闻+资金双确认才进（观察不过来）"

    # 重大突发利好：即便板块此前偏冷/资金一般，也允许进观察池
    if major and (board_pct is None or board_pct > -3.5):
        return True, "重大政策/投资利好催化，纳入1～2月前瞻观察"

    # 近1～2周已走强且资金正：不强制当日新闻，避免漏掉正热主线
    if (
        not soft
        and board_pct5 is not None
        and 3.5 <= float(board_pct5) < 16.0
        and (board_pct is None or float(board_pct) > -1.5)
        and (flow_pos or flow5_pos)
    ):
        return True, f"近1～2周走强{float(board_pct5):.1f}%+资金正，纳入中期观察"

    # —— 多维证据：新进至少 2 维；软主题至少含新闻 ——
    dims: List[str] = []
    if news_hits:
        dims.append(f"新闻×{len(news_hits)}")
    if flow_pos or flow5_pos:
        dims.append("资金正态度")
    if hint is not None and int(hint.get("priority") or 0) >= 4:
        dims.append("行业结构")
    sector_run = (
        board_pct5 is not None
        and float(board_pct5) >= 4.0
        and board_pct is not None
        and float(board_pct) >= 1.2
        and (flow_pos or flow5_pos or (hint is not None and int(hint.get("priority") or 0) >= 4))
    )
    if sector_run:
        dims.append("板块集体走强")
    trend_ok = (
        board_pct is not None
        and -2.0 <= float(board_pct) <= 4.5
        and (board_pct5 is None or float(board_pct5) < 14.0)
    )
    if trend_ok and (
        flow_pos
        or news_hits
        or sector_run
        or (hint and int(hint.get("priority") or 0) >= 5)
    ):
        dims.append("走势健康")

    n_dim = len(dims)
    if soft and "新闻" not in "".join(dims) and not major:
        return False, "同质软主题缺新闻维度，不进1～2月观察"
    if n_dim >= 2:
        return True, f"中期多因子确认：{' + '.join(dims)}"
    if len(real_news) >= 2 and (board_pct is None or board_pct > -2.0):
        return True, "强新闻催化（≥2条实质），允许1～2月前瞻"
    if (
        hint is not None
        and int(hint.get("priority") or 0) >= 5
        and flow_pos
        and (board_pct is None or board_pct > -2.5)
    ):
        return True, f"高优行业结构+资金正态度（{hint.get('name')}）"
    if (
        hint is not None
        and int(hint.get("priority") or 0) >= 4
        and len(news_hits) >= 1
        and (board_pct is None or board_pct > -2.0)
    ):
        return True, f"结构主题+新闻催化（{hint.get('name')}）"
    if quality >= 5 and trend_ok and (flow_pos or news_hits):
        return True, f"综合质量分{quality}+资金/新闻且走势未坏"
    if n_dim == 1:
        return False, f"仅单因子（{dims[0]}），中期确认不足，暂不进观察"
    return False, "新闻/资金/行业/走势均未见1～2月前瞻线索"


def _build_weekly_news_view(
    news: pd.DataFrame,
    boards: pd.DataFrame,
    *,
    limit: int = 18,
) -> List[Dict[str, Any]]:
    """近一周新闻大事 → 每条映射可能相关的板块（展示层）。"""
    if news is None or news.empty:
        return []
    board_names: List[str] = []
    if boards is not None and not boards.empty and "板块名称" in boards.columns:
        board_names = [
            str(x)
            for x in boards["板块名称"].astype(str).tolist()
            if x and x != "nan"
        ]
    # 额外用佐证库关键词扩大映射
    hint_keys: List[Tuple[str, str]] = []  # (key, theme_name)
    for h in THEME_HINTS:
        tname = str(h.get("name") or "")
        for k in list(h.get("keywords") or []) + list(h.get("board_keys") or []):
            k = str(k).strip()
            if len(k) >= 2:
                hint_keys.append((k, tname))

    out: List[Dict[str, Any]] = []
    for _, r in news.head(limit * 2).iterrows():
        title = str(r.get("title") or "").strip()
        if not title:
            continue
        related: List[str] = []
        seen = set()
        for bn in board_names:
            if _is_banned_theme(bn):
                continue
            short = bn.replace("概念", "").replace("Ⅲ", "").replace("Ⅱ", "").replace("Ⅰ", "")
            if len(short) >= 2 and (short in title or bn in title):
                if bn not in seen:
                    seen.add(bn)
                    related.append(bn)
            if len(related) >= 4:
                break
        for k, tname in hint_keys:
            if _is_banned_theme(tname) or _is_banned_theme(k):
                continue
            if k in title and tname and tname not in seen:
                seen.add(tname)
                related.append(tname)
            if len(related) >= 5:
                break
        out.append(
            {
                "时间": str(r.get("time") or "")[:16],
                "频道": str(r.get("channel") or "-"),
                "来源": str(r.get("source") or ""),
                "标题": title[:100],
                "相关板块": "、".join(related[:5]) if related else "-",
                "相关概念/行业": "、".join(related[:5]) if related else "-",
                "url": str(r.get("url") or ""),
            }
        )
        if len(out) >= limit:
            break
    return out


def _seed_stocks_for_board(
    board_name: str, hint: Optional[Dict[str, Any]], limit: int = 8, *, fast: bool = False
) -> List[Tuple[str, str]]:
    """
    佐证库中军种子 + 板块里「相对活跃、资金认可」的票。
    fast=True（前瞻刷新）：全量刷配置种子，不截断「前几只」；不扫成分以免卡死。
    股价≥800 在入池阶段跳过且不占主题名额，故此处不因名额先砍种子。
    """
    out: List[Tuple[str, str]] = []
    seen: set = set()
    hint_id = str((hint or {}).get("id") or "")
    # 非 fast 时活跃票补齐上限；种子本身全量保留
    active_target = max(limit, 6)

    def _add(code: str, name: str, *, from_seed: bool = False) -> None:
        code = str(code or "").zfill(6)[-6:]
        if not (code.isdigit() and len(code) == 6):
            return
        if code in seen:
            return
        if _is_st_stock_name(name):
            return
        if code in _OBSERVE_EXCLUDE_CODES:
            return
        # 非本主题种子：禁止抢走已登记给其他结构主题的中军
        if not from_seed:
            owner = _RESERVED_SEED_TO_HINT.get(code)
            if owner and owner != hint_id:
                return
        seen.add(code)
        out.append((code, str(name or code)))

    seed_list = list((hint or {}).get("seed_stocks") or [])
    for c, n in seed_list:
        _add(c, n, from_seed=True)
    n_seed_added = len(out)

    if fast:
        return out

    names_to_try: List[str] = []
    raw = str(board_name or "").strip()
    if raw and not _is_banned_theme(raw) and raw not in _TOO_BROAD_INDUSTRIES:
        names_to_try.append(raw)
        if "概念" in raw:
            alt = raw.replace("概念", "").strip()
            if alt and alt not in names_to_try:
                names_to_try.append(alt)
    for ind in list((hint or {}).get("industries") or []):
        ind = str(ind or "").strip()
        if (
            ind
            and ind not in names_to_try
            and "概念" not in ind
            and not _is_banned_theme(ind)
            and ind not in _TOO_BROAD_INDUSTRIES
        ):
            names_to_try.append(ind)
    # board_keys 里也可试行业名（如印制电路板），概念板仍跳过全量扫描
    for bk in list((hint or {}).get("board_keys") or []):
        bk = str(bk or "").strip()
        if (
            bk
            and bk not in names_to_try
            and "概念" not in bk
            and not _is_banned_theme(bk)
            and bk not in _TOO_BROAD_INDUSTRIES
            and len(bk) >= 2
        ):
            names_to_try.append(bk)

    def _is_futian_cold(row: pd.Series) -> bool:
        """资金差 + 近5日无表现：福田类安静票。"""
        flow = _to_float(row.get("主力净流入_亿"))
        flow5 = _to_float(row.get("主力净流入_5日_亿"))
        pct5 = _to_float(row.get("涨跌幅_5日"))
        pct = _to_float(row.get("涨跌幅"))
        cold_flow = (flow is None or float(flow) < -0.15) and (
            flow5 is None or float(flow5) <= 0
        )
        no_move = pct5 is None or abs(float(pct5)) < 2.5
        # 持续大出且无明显趋势
        heavy_out = flow is not None and float(flow) < -1.0 and (
            flow5 is None or float(flow5) <= 0
        )
        if heavy_out and (pct5 is None or float(pct5) < 4.0):
            return True
        if cold_flow and no_move:
            return True
        # 当日大跌且资金出：不进观察
        if pct is not None and float(pct) <= -4.0 and (
            flow is None or float(flow) < 0
        ):
            return True
        return False

    def _is_active_ok(row: pd.Series) -> bool:
        """相对活跃且有资金/走势认可。"""
        flow = _to_float(row.get("主力净流入_亿"))
        flow5 = _to_float(row.get("主力净流入_5日_亿"))
        pct5 = _to_float(row.get("涨跌幅_5日"))
        pct = _to_float(row.get("涨跌幅"))
        turn = _to_float(row.get("换手率"))
        amt = _to_float(row.get("成交额"))
        mkt = _to_float(row.get("总市值_亿"))
        if mkt is not None and mkt < 35:
            return False
        capital_ok = (flow is not None and float(flow) > 0.08) or (
            flow5 is not None and float(flow5) > 0.5
        )
        moved = pct5 is not None and float(pct5) >= 3.0
        # 缩量回踩候选：5日有趋势、今日收敛、资金未大出
        pullbackish = (
            pct5 is not None
            and 4.0 <= float(pct5) < 28.0
            and pct is not None
            and -3.0 <= float(pct) <= 2.0
            and (flow is None or float(flow) >= -0.6)
        )
        active = (turn is not None and float(turn) >= 1.2) or (
            amt is not None and float(amt) >= 8e7
        )
        # 至少：资金认可，或有走势+略活跃，或明确回踩结构
        if capital_ok and (moved or active or (pct is not None and float(pct) > -2)):
            return True
        if pullbackish and (capital_ok or active):
            return True
        if moved and active and (flow is None or float(flow) >= -0.35):
            return True
        return False

    for fetch_name in names_to_try:
        if not fetch_name or "概念" in fetch_name:
            continue
        if len(out) >= active_target:
            break
        try:
            cons = fetch_board_constituents(fetch_name)
            if cons is None or cons.empty:
                continue
            df = cons.copy()
            for col in (
                "主力净流入_亿",
                "主力净流入_5日_亿",
                "总市值_亿",
                "涨跌幅",
                "涨跌幅_5日",
                "换手率",
                "成交额",
                "量比",
            ):
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            # 先滤福田类
            df = df[~df.apply(_is_futian_cold, axis=1)]
            if df.empty:
                continue
            df = df[df.apply(_is_active_ok, axis=1)]
            if df.empty:
                continue
            # 活跃分：资金 > 走势 > 换手/成交；市值只轻微加权（避免只剩超大盘中军）
            flow_s = (
                df["主力净流入_亿"].fillna(0)
                if "主力净流入_亿" in df.columns
                else 0
            )
            flow5_s = (
                df["主力净流入_5日_亿"].fillna(0)
                if "主力净流入_5日_亿" in df.columns
                else 0
            )
            pct5_s = (
                df["涨跌幅_5日"].fillna(0).clip(lower=-5, upper=18)
                if "涨跌幅_5日" in df.columns
                else 0
            )
            turn_s = (
                df["换手率"].fillna(0).clip(upper=20)
                if "换手率" in df.columns
                else 0
            )
            amt_s = (
                (df["成交额"].fillna(0) / 1e8).clip(upper=40)
                if "成交额" in df.columns
                else 0
            )
            mkt_s = (
                df["总市值_亿"].fillna(0).clip(upper=2500) * 0.0012
                if "总市值_亿" in df.columns
                else 0
            )
            pct_s = (
                df["涨跌幅"].fillna(0)
                if "涨跌幅" in df.columns
                else 0
            )
            df = df.assign(
                _sc=(
                    flow_s * 1.6
                    + flow5_s.clip(lower=0) * 0.25
                    + pct5_s * 0.55
                    + turn_s * 0.35
                    + amt_s * 0.2
                    + mkt_s
                    # 涨停尖峰略降权（观察池要可盯，不是只收涨停）
                    - pct_s.clip(lower=5, upper=11) * 0.35
                )
            ).sort_values("_sc", ascending=False, na_position="last")
            for _, r in df.head(active_target + 8).iterrows():
                _add(
                    str(r.get("代码") or r.get("code") or ""),
                    str(r.get("名称") or r.get("name") or ""),
                    from_seed=False,
                )
                if len(out) >= active_target:
                    break
            if len(out) >= active_target:
                break
        except Exception:
            continue
    # 配置种子全保留；活跃票最多补到 active_target
    return out[: max(n_seed_added, active_target)]


def _normalize_concept_key(name: str) -> str:
    n = (
        str(name or "")
        .replace("概念", "")
        .replace("Ⅱ", "")
        .replace("Ⅰ", "")
        .replace("Ⅲ", "")
        .replace("行业", "")
        .strip()
    )
    return n


def _industries_for_concept(concept_name: str) -> List[str]:
    """概念 → 候选行业名列表（仅显式映射，不用宽泛 board_keys 炸开）。"""
    raw = str(concept_name or "")
    key = _normalize_concept_key(raw)
    out: List[str] = []

    def _add_all(inds: List[str]) -> None:
        for ind in inds:
            ind = str(ind or "").strip()
            if ind and ind not in out and "概念" not in ind:
                out.append(ind)

    # 精确优先，再子串（只允许「映射键 ⊆ 概念名」，禁止「元件」命中「被动元件」这种反向包含）
    for ck, inds in CONCEPT_TO_INDUSTRIES.items():
        if ck == key:
            _add_all(inds)
    if not out:
        for ck, inds in CONCEPT_TO_INDUSTRIES.items():
            if len(ck) >= 2 and (ck in key or ck in raw):
                _add_all(inds)
    hint = _hint_for_board(raw) or _hint_for_board(key)
    if hint:
        _add_all([str(x) for x in (hint.get("industries") or [])])
    if not out and key:
        out.append(key)
    return out[:3]


def _find_industry_row(
    boards: pd.DataFrame, industry_names: List[str]
) -> Optional[Dict[str, Any]]:
    if boards is None or boards.empty or not industry_names:
        return None
    _skip = {"通信", "综合", "综合Ⅱ", "综合Ⅲ", "其他", "其他Ⅱ", "其他Ⅲ"}
    ind = boards[boards["类型"].astype(str) == "行业"].copy()
    if ind.empty:
        return None
    names = ind["板块名称"].astype(str)
    for want in industry_names:
        want = str(want or "").strip()
        if not want or want in _skip or _normalize_concept_key(want) in _skip:
            continue
        if _is_emotion_board(want, "行业"):
            continue
        exact = ind.loc[names == want]
        if not exact.empty:
            return exact.iloc[0].to_dict()
    for want in industry_names:
        want = str(want or "").strip()
        if len(want) < 2 or want in _skip:
            continue
        if _is_emotion_board(want, "行业"):
            continue
        sub = ind.loc[names.str.contains(re.escape(want), na=False)]
        if sub.empty:
            continue
        # 丢掉过于宽泛的命中
        sub = sub[
            ~sub["板块名称"].astype(str).isin(_skip)
            & ~sub["板块名称"].astype(str).map(lambda x: _is_emotion_board(x, "行业"))
        ]
        if sub.empty:
            continue
        sub = sub.assign(_nlen=names.loc[sub.index].str.len()).sort_values(
            "_nlen", ascending=True
        )
        return sub.iloc[0].to_dict()
    return None


def _theme_from_concept_industry(
    *,
    concept_name: str,
    industry_row: Dict[str, Any],
    news_hits: List[str],
    quality_why: List[str],
    carry: bool = False,
    force_hint: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    industry_name = str(industry_row.get("板块名称") or "")
    hint = (
        force_hint
        or _hint_for_board(concept_name)
        or _hint_for_board(industry_name)
    )
    if hint:
        tid = str(hint["id"])
        cname = str(hint.get("name") or concept_name)
        thesis = str(hint.get("thesis") or "")
        seeds = list(hint.get("seed_stocks") or [])
        priority = int(hint.get("priority") or 3)
        keys = list(hint.get("keywords") or []) + [concept_name, industry_name]
        board_keys = list(hint.get("board_keys") or []) or [industry_name]
    else:
        slug = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", industry_name)[:24]
        tid = f"ind_{slug}"
        cname = _normalize_concept_key(concept_name) or concept_name
        thesis = "新闻/资金佐证的产业概念，映射到细分行业做前瞻观察。"
        seeds = []
        priority = 3
        keys = [concept_name, industry_name, _normalize_concept_key(concept_name)]
        board_keys = [industry_name]
    if carry:
        thesis = ("延续前瞻：" + thesis) if thesis else "昨日在池，今日做前瞻生命周期判定。"
    theme = {
        "id": tid,
        "name": cname,
        "priority": priority,
        "keywords": [k for k in keys if k],
        "board_keys": board_keys,
        "seed_stocks": seeds,
        "thesis": thesis,
        "_matched_board": industry_name,
        "_concept": cname,
        "_industry": industry_name,
        "_em_industry": industry_name,
        "_discover_why": quality_why,
        "_news_hits": news_hits,
        "_carry": carry,
        "_hint_id": hint.get("id") if hint else "",
        "_board_pct": _to_float(industry_row.get("涨跌幅")),
        "_board_flow": _to_float(industry_row.get("主力净流入_亿")),
        "_board_pct5": _to_float(industry_row.get("涨跌幅_5日")),
        "_board_flow5": _to_float(industry_row.get("主力净流入_5日_亿")),
    }
    return _apply_structure_labels(theme, hint, industry_name)


def _theme_from_board_row(
    row: Dict[str, Any],
    *,
    news_hits: List[str],
    quality_why: List[str],
    carry: bool = False,
    force_hint: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    board_name = str(row.get("板块名称") or "")
    btype = str(row.get("类型") or "")
    if btype == "行业":
        return _theme_from_concept_industry(
            concept_name=_normalize_concept_key(board_name) or board_name,
            industry_row=row,
            news_hits=news_hits,
            quality_why=quality_why,
            carry=carry,
            force_hint=force_hint,
        )
    inds = _industries_for_concept(board_name)
    board_code = str(row.get("板块代码") or board_name)
    hint = force_hint or _hint_for_board(board_name)
    if hint:
        tid = str(hint["id"])
        name = str(hint["name"])
    else:
        slug = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", board_code)[:24]
        tid = f"dyn_{slug}"
        name = _normalize_concept_key(board_name) or board_name
    keys = list(hint.get("keywords") or []) if hint else []
    if board_name and board_name not in keys:
        keys = [board_name] + keys
    short = _normalize_concept_key(board_name)
    if short and short not in keys:
        keys.append(short)
    seeds = list(hint.get("seed_stocks") or []) if hint else []
    thesis = (
        str(hint.get("thesis") or "")
        if hint
        else "由新闻/资金萌芽动态发现的产业概念；需映射到细分行业。"
    )
    if carry:
        thesis = ("延续前瞻：" + thesis) if thesis else "昨日在池，今日做前瞻生命周期判定。"
    em_ind = inds[0] if inds else board_name
    theme = {
        "id": tid,
        "name": name,
        "priority": int(hint.get("priority") or 3) if hint else 3,
        "keywords": keys,
        "board_keys": list(hint.get("board_keys") or []) if hint else (inds or [board_name]),
        "seed_stocks": seeds,
        "thesis": thesis,
        "_matched_board": board_name,
        "_concept": name,
        "_industry": em_ind,
        "_em_industry": em_ind,
        "_discover_why": quality_why,
        "_news_hits": news_hits,
        "_carry": carry,
        "_hint_id": hint.get("id") if hint else "",
        "_board_pct": _to_float(row.get("涨跌幅")),
        "_board_flow": _to_float(row.get("主力净流入_亿")),
        "_board_pct5": _to_float(row.get("涨跌幅_5日")),
        "_board_flow5": _to_float(row.get("主力净流入_5日_亿")),
    }
    return _apply_structure_labels(theme, hint, em_ind)


def discover_theme_universe(
    boards: pd.DataFrame,
    news: pd.DataFrame,
    history: Dict[str, Any],
    asof: str,
    *,
    max_new: int = 24,
) -> List[Dict[str, Any]]:
    """
    概念←新闻佐证 → 映射细分行业（同概念可拆多频道）：
    1) 财经+科技新闻命中产业概念；
    2) 概念映射到东财行业板取资金，展示用 industry_label 细分；
    3) 昨日在池细分行业强制带入做生命周期。
    """
    themes: List[Dict[str, Any]] = []
    seen_ids: set = set()
    seen_industries: set = set()

    def _add(theme: Dict[str, Any]) -> None:
        tid = str(theme.get("id") or "")
        ind = str(theme.get("_industry") or theme.get("_matched_board") or "")
        ind_key = _normalize_concept_key(ind)
        if ind_key and ind_key in seen_industries:
            return
        if tid:
            seen_ids.add(tid)
        if ind_key:
            seen_industries.add(ind_key)
        themes.append(theme)

    best_by_industry: Dict[str, Tuple[float, Dict[str, Any]]] = {}

    def _consider(rank: float, theme: Dict[str, Any]) -> None:
        ind = str(theme.get("_industry") or "")
        name = str(theme.get("name") or "")
        if _is_banned_theme(ind) or _is_banned_theme(name):
            return
        ind_key = _normalize_concept_key(ind)
        if not ind_key:
            return
        old = best_by_industry.get(ind_key)
        if old is None or rank > old[0]:
            best_by_industry[ind_key] = (rank, theme)

    def _consider_with_siblings(
        rank: float,
        theme: Dict[str, Any],
        news_hits: List[str],
        quality_why: List[str],
        *,
        carry: bool = False,
    ) -> None:
        _consider(rank, theme)
        tid = str(theme.get("id") or theme.get("_hint_id") or "")
        hint = next((h for h in THEME_HINTS if h.get("id") == tid), None)
        base_em = str(theme.get("_em_industry") or theme.get("_matched_board") or "")
        for sib in _split_sibling_hints(hint):
            ind_names = list(sib.get("industries") or [])
            sib_row = _find_industry_row(boards, ind_names)
            if not sib_row and base_em:
                sib_row = {"板块名称": base_em, "类型": "行业"}
            if not sib_row:
                continue
            t2 = _theme_from_concept_industry(
                concept_name=str(sib.get("concept_group") or sib.get("name")),
                industry_row=sib_row,
                news_hits=news_hits,
                quality_why=list(quality_why or [])
                + [f"同概念拆频道:{sib.get('industry_label')}"],
                carry=carry,
                force_hint=sib,
            )
            _consider(rank - 0.05, t2)

    if boards is not None and not boards.empty:
        for _, r in boards.iterrows():
            bname = str(r.get("板块名称") or "")
            btype = str(r.get("类型") or "")
            if not bname or _is_emotion_board(bname, btype):
                continue
            if btype != "概念":
                continue
            hint = _hint_for_board(bname)
            keys = [bname, _normalize_concept_key(bname)]
            if hint:
                keys = (
                    list(hint.get("keywords") or [])
                    + list(hint.get("board_keys") or [])
                    + keys
                )
            news_hits = _news_hits_for_keys([k for k in keys if k], news)
            pct = _to_float(r.get("涨跌幅"))
            flow = _to_float(r.get("主力净流入_亿"))
            # 无新闻：概念不主动开新坑（防今天CPO明天PCB追涨）；
            # 例外：近1～2周已走强且资金正，可进中期观察池
            if not news_hits and hint is None:
                continue
            # 硬主题优先用自身 industries，避免 PCB 误映射到被动元件等旁支行业
            ind_names: List[str] = []
            if hint:
                ind_names = [str(x) for x in (hint.get("industries") or []) if x]
            for x in _industries_for_concept(bname):
                if x and x not in ind_names:
                    ind_names.append(x)
            ind_row = _find_industry_row(boards, ind_names)
            if not ind_row:
                continue
            industry_name = str(ind_row.get("板块名称") or "")
            ipct = _to_float(ind_row.get("涨跌幅"))
            iflow = _to_float(ind_row.get("主力净流入_亿"))
            ipct5 = _to_float(ind_row.get("涨跌幅_5日"))
            iflow5 = _to_float(ind_row.get("主力净流入_5日_亿"))
            week_hot = (
                ipct5 is not None
                and float(ipct5) >= 3.0
                and (ipct is None or float(ipct) > -2.0)
                and (
                    (iflow is not None and float(iflow) > 0)
                    or (iflow5 is not None and float(iflow5) > 0)
                )
            )
            if not news_hits and not week_hot and (
                flow is None or flow < 3.0 or pct is None or pct < 1.0
            ):
                continue
            quality, rank, pulse, why = _board_quality_score(
                board_name=industry_name,
                board_pct=ipct,
                board_flow=iflow,
                board_pct5=ipct5,
                board_flow5=iflow5,
                news_hits=news_hits,
                hint=hint,
            )
            forward_ok, _fwd = _forward_outlook_ok(
                quality=quality,
                pulse=pulse,
                news_hits=news_hits,
                board_pct=ipct,
                board_flow=iflow,
                board_pct5=ipct5,
                board_flow5=iflow5,
                hint=hint,
            )
            if not forward_ok:
                continue
            if news_hits:
                rank += 8.0 + min(6.0, len(news_hits) * 2.0)
            elif week_hot:
                rank += 6.0
                why = list(why or []) + ["近1～2周走强+资金正"]
            if _major_catalyst_in_hits(news_hits):
                rank += 15.0
            if hint is not None:
                rank += 2.0
                # 结构高优（光纤/材料等）无新闻时仍给排序权重，避免被挤出 max_new
                if not news_hits and int(hint.get("priority") or 0) >= 5:
                    rank += 12.0
            if pct is not None and pct >= 3.0 and flow is not None and flow > 0:
                rank += 1.5
            concept_label = (
                str(hint["name"]) if hint else (_normalize_concept_key(bname) or bname)
            )
            theme = _theme_from_concept_industry(
                concept_name=concept_label,
                industry_row=ind_row,
                news_hits=news_hits,
                quality_why=why
                + [f"概念佐证:{concept_label}", f"映射行业:{industry_name}"],
                carry=False,
                force_hint=hint,
            )
            _consider_with_siblings(
                rank,
                theme,
                news_hits,
                why + [f"概念佐证:{concept_label}", f"映射行业:{industry_name}"],
                carry=False,
            )

        for _, r in boards.iterrows():
            bname = str(r.get("板块名称") or "")
            btype = str(r.get("类型") or "")
            if btype != "行业" or not bname or _is_emotion_board(bname, btype):
                continue
            if _is_banned_theme(bname) or bname in _TOO_BROAD_INDUSTRIES:
                continue
            hint = _hint_for_board(bname)
            # 行业直击默认只用行业名；若 hint 声明了 news_keys，则用其承接关联新闻
            # （如「半导体」新闻同时催化「半导体材料」），避免电力类宽词误伤。
            keys = [bname, _normalize_concept_key(bname)]
            if hint is not None:
                news_keys = list(hint.get("news_keys") or [])
                if news_keys:
                    keys = news_keys + keys
            news_hits = _news_hits_for_keys([k for k in keys if k and len(k) >= 2], news)
            # 过宽行业名：标题仅有「汽车」不够，须同时出现整车/乘用车等具体词
            broad_extra = _BROAD_INDUSTRY_NEWS_EXTRA.get(
                _normalize_concept_key(bname) or bname
            ) or _BROAD_INDUSTRY_NEWS_EXTRA.get(bname)
            if broad_extra and news_hits:
                news_hits = [
                    t
                    for t in news_hits
                    if any(x in t for x in broad_extra)
                ]
            pct = _to_float(r.get("涨跌幅"))
            flow = _to_float(r.get("主力净流入_亿"))
            pct5 = _to_float(r.get("涨跌幅_5日"))
            flow5 = _to_float(r.get("主力净流入_5日_亿"))
            # 无新闻：纯行业名不刷屏；近1～2周走强+资金正可进中期观察
            week_hot = (
                pct5 is not None
                and float(pct5) >= 3.0
                and (pct is None or float(pct) > -2.0)
                and (
                    (flow is not None and float(flow) > 0)
                    or (flow5 is not None and float(flow5) > 0)
                )
            )
            if not news_hits and hint is None and not week_hot:
                continue
            if not news_hits and not week_hot:
                continue
            quality, rank, pulse, why = _board_quality_score(
                board_name=bname,
                board_pct=pct,
                board_flow=flow,
                board_pct5=pct5,
                board_flow5=flow5,
                news_hits=news_hits,
                hint=hint,
            )
            forward_ok, _fwd = _forward_outlook_ok(
                quality=quality,
                pulse=pulse,
                news_hits=news_hits,
                board_pct=pct,
                board_flow=flow,
                board_pct5=pct5,
                board_flow5=flow5,
                hint=hint,
            )
            if not forward_ok:
                continue
            rank += 1.5
            if news_hits:
                rank += 6.0
            elif week_hot:
                rank += 5.0
                why = list(why or []) + ["近1～2周走强+资金正"]
            if _major_catalyst_in_hits(news_hits):
                rank += 15.0
            concept_label = (
                str(hint["name"]) if hint else (_normalize_concept_key(bname) or bname)
            )
            theme = _theme_from_concept_industry(
                concept_name=concept_label,
                industry_row=r.to_dict(),
                news_hits=news_hits,
                quality_why=why + [f"行业直击:{bname}"],
                carry=False,
                force_hint=hint,
            )
            _consider_with_siblings(
                rank,
                theme,
                news_hits,
                why + [f"行业直击:{bname}"],
                carry=False,
            )

    ranked = sorted(best_by_industry.values(), key=lambda x: x[0], reverse=True)
    # 同质通道板（银行/证券/煤炭等）降权；结构硬主题优先占坑
    reranked: List[Tuple[float, Dict[str, Any]]] = []
    for rank, theme in ranked:
        adj = float(rank)
        if _is_homogeneous_theme(theme):
            adj -= 22.0
        reranked.append((adj, theme))
    reranked.sort(key=lambda x: x[0], reverse=True)

    structure_first: List[Tuple[float, Dict[str, Any]]] = []
    rest_themes: List[Tuple[float, Dict[str, Any]]] = []
    tech_hw_structure_count = 0
    for rank, theme in reranked:
        tid = str(theme.get("id") or theme.get("_hint_id") or "")
        is_structure = tid in _STRUCTURE_KEEP_HINT_IDS or int(theme.get("priority") or 0) >= 5
        if is_structure:
            if tid in _TECH_HARDWARE_HINT_IDS and tech_hw_structure_count >= _MAX_TECH_HARDWARE_STRUCTURE_FIRST:
                rest_themes.append((rank - 3.0, theme))
                continue
            if tid in _TECH_HARDWARE_HINT_IDS:
                tech_hw_structure_count += 1
            structure_first.append((rank, theme))
        else:
            rest_themes.append((rank, theme))

    new_count = 0
    for _rank, theme in structure_first + rest_themes:
        before = len(themes)
        _add(theme)
        if len(themes) > before:
            new_count += 1
            if new_count >= max_new:
                break

    days = sorted(d for d in (history.get("days") or {}) if d < asof)
    if days and len(themes) < max_new + 8:
        yday = history["days"].get(days[-1]) or {}
        y_names = [str(x) for x in (yday.get("theme_names") or [])]
        y_ids = [str(x) for x in (yday.get("theme_ids") or [])]
        y_status = yday.get("theme_status") or {}
        carry_items: List[Tuple[str, str]] = []
        for i, name in enumerate(y_names):
            tid = y_ids[i] if i < len(y_ids) else ""
            carry_items.append((tid, name))
        for tid, st in y_status.items():
            if isinstance(st, dict) and st.get("name"):
                pair = (str(tid), str(st["name"]))
                if pair not in carry_items and not any(
                    p[0] == pair[0] or p[1] == pair[1] for p in carry_items
                ):
                    carry_items.append(pair)

        for tid, name in carry_items:
            if _is_banned_theme(name):
                continue
            hint_ids = _LEGACY_THEME_ID_MAP.get(str(tid) or "", [str(tid)] if tid else [])
            if not hint_ids and tid:
                hint_ids = [str(tid)]
            hints_to_carry: List[Optional[Dict[str, Any]]] = []
            for hid in hint_ids:
                h = next((x for x in THEME_HINTS if x.get("id") == hid), None)
                if h is not None:
                    hints_to_carry.append(h)
            if not hints_to_carry:
                h = next((x for x in THEME_HINTS if x.get("name") == name), None)
                hints_to_carry.append(h)
            # 旧「国产算力/服务器」名称也拆成两池延续
            if name in (
                "算电-算(国产算力/服务器)",
                "算电-算(国产服务器)",
                "算电-算(海外组装)",
                "算电-算(液冷服务器)",
                "算电-算",
            ):
                for h in THEME_HINTS:
                    if str(h.get("split_group") or "") == "suanli_suan" and h not in hints_to_carry:
                        hints_to_carry.append(h)
            for hint in hints_to_carry:
                cname = str((hint or {}).get("name") or name)
                if any(t.get("name") == cname for t in themes):
                    continue
                label = str((hint or {}).get("industry_label") or "")
                if label and _normalize_concept_key(label) in seen_industries:
                    continue
                ind_names = list((hint or {}).get("industries") or [])
                if not ind_names:
                    ind_names = _industries_for_concept(name)
                if label:
                    ind_names = [label] + ind_names
                ind_names = [name] + ind_names
                ind_row = _find_industry_row(boards, ind_names)
                if not ind_row:
                    continue
                industry_name = str(ind_row.get("板块名称") or "")
                if _is_banned_theme(industry_name):
                    continue
                # 去重键用细分行业标签，避免两池抢同一个「计算机设备」
                dedupe_key = _normalize_concept_key(label or industry_name)
                if dedupe_key and dedupe_key in seen_industries:
                    continue
                keys = list((hint or {}).get("keywords") or []) + [name, industry_name]
                news_hits = _news_hits_for_keys(keys, news)
                theme = _theme_from_concept_industry(
                    concept_name=cname,
                    industry_row=ind_row,
                    news_hits=news_hits,
                    quality_why=["昨日在池延续判定"],
                    carry=True,
                    force_hint=hint,
                )
                _add(theme)

    # 多元主题保位：1～2月看好的都尽量留；科技硬件有上限防一跌全跌占满池
    # 硬上限略大于 max_new：正热板不因腾位被踢；只挤同质/偏冷主题
    hard_cap = max_new + 6
    have_ids = {str(t.get("id") or "") for t in themes}

    def _theme_is_hot_active(t: Dict[str, Any]) -> bool:
        """正热/近1～2周走强主题：保位注入时禁止随意挤掉。"""
        tid = str(t.get("id") or "")
        if tid in _STRUCTURE_KEEP_HINT_IDS:
            return True
        pct = _to_float(t.get("_board_pct"))
        flow = _to_float(t.get("_board_flow"))
        pct5 = _to_float(t.get("_board_pct5"))
        flow5 = _to_float(t.get("_board_flow5"))
        news_n = len(t.get("_news_hits") or [])
        if pct is not None and float(pct) >= 1.0 and flow is not None and float(flow) > 0:
            return True
        if (
            pct5 is not None
            and float(pct5) >= 3.0
            and (pct is None or float(pct) > -1.5)
            and (
                (flow is not None and float(flow) > 0)
                or (flow5 is not None and float(flow5) > 0)
                or news_n > 0
            )
        ):
            return True
        return False

    def _eviction_cold_score(t: Dict[str, Any]) -> float:
        """越高越优先腾位（越冷/越同质）。"""
        score = 0.0
        if _is_homogeneous_theme(t):
            score += 40.0
        pct = _to_float(t.get("_board_pct"))
        flow = _to_float(t.get("_board_flow"))
        pct5 = _to_float(t.get("_board_pct5"))
        flow5 = _to_float(t.get("_board_flow5"))
        news_n = len(t.get("_news_hits") or [])
        if flow is not None and float(flow) < 0:
            score += min(12.0, abs(float(flow)))
        if flow5 is not None and float(flow5) < 0:
            score += min(10.0, abs(float(flow5)) * 0.5)
        if pct is not None and float(pct) < 0:
            score += abs(float(pct)) * 2.0
        if pct5 is not None and float(pct5) < 0:
            score += abs(float(pct5)) * 1.5
        if news_n <= 0:
            score += 4.0
        if int(t.get("priority") or 0) <= 2:
            score += 3.0
        return score

    def _pop_theme_at(i: int) -> None:
        t = themes.pop(i)
        ind = str(t.get("_industry") or t.get("_matched_board") or "")
        ind_key = _normalize_concept_key(ind)
        lab_key = _normalize_concept_key(str(t.get("industry_label") or ""))
        tid = str(t.get("id") or "")
        if ind_key:
            seen_industries.discard(ind_key)
        if lab_key:
            seen_industries.discard(lab_key)
        if tid:
            seen_ids.discard(tid)
            have_ids.discard(tid)

    def _evict_one_homogeneous() -> bool:
        for i in range(len(themes) - 1, -1, -1):
            t = themes[i]
            if not _is_homogeneous_theme(t):
                continue
            _pop_theme_at(i)
            return True
        return False

    def _evict_one_cold_non_structure() -> bool:
        """只踢偏冷非结构主题；正热主线不腾位。"""
        best_i = -1
        best_score = -1.0
        for i, t in enumerate(themes):
            tid = str(t.get("id") or "")
            if tid in _STRUCTURE_KEEP_HINT_IDS:
                continue
            if _theme_is_hot_active(t):
                continue
            sc = _eviction_cold_score(t)
            if sc > best_score:
                best_score = sc
                best_i = i
        if best_i < 0 or best_score < 3.0:
            return False
        _pop_theme_at(best_i)
        return True

    if boards is not None and not boards.empty:
        for h in THEME_HINTS:
            hid = str(h.get("id") or "")
            if hid not in _STRUCTURE_KEEP_HINT_IDS or hid in have_ids:
                continue
            label = str(h.get("industry_label") or "")
            ind_names = list(h.get("industries") or []) + list(h.get("board_keys") or [])
            ind_row = _find_industry_row(boards, ind_names)
            if not ind_row:
                continue
            industry_name = str(ind_row.get("板块名称") or "")
            if _is_banned_theme(industry_name):
                continue
            dedupe_key = _normalize_concept_key(label or industry_name)
            ind_key = _normalize_concept_key(industry_name)
            ipct = _to_float(ind_row.get("涨跌幅"))
            iflow = _to_float(ind_row.get("主力净流入_亿"))
            dig_wait = hid in _DIG_WAIT_HINT_IDS
            user_pin = hid in _USER_PIN_HINT_IDS
            if (
                not dig_wait
                and not user_pin
                and ipct is not None
                and float(ipct) <= -4.0
                and (iflow is None or float(iflow) < 0)
            ):
                continue
            keys = (
                list(h.get("news_keys") or [])
                + list(h.get("keywords") or [])
                + [str(h.get("name") or ""), industry_name]
            )
            news_hits = _news_hits_for_keys([k for k in keys if k], news)
            # 先挤同质/偏冷；绝不挤正热。腾不出则扩到 hard_cap 直接追加。
            while len(themes) >= max_new and (
                _evict_one_homogeneous() or _evict_one_cold_non_structure()
            ):
                pass
            if len(themes) >= hard_cap and not user_pin:
                continue
            why_keep = (
                "用户点名强保：强制留池观察"
                if user_pin
                else (
                    "材料/设备/存储/贵金属/农业挖坑保位：走弱日正是观察买点，不踢出"
                    if dig_wait
                    else "多元主题保位：防科技硬件独占挤掉医药/电力/贵金属/光伏/军工/小金属等"
                )
            )
            theme = _theme_from_concept_industry(
                concept_name=str(h.get("concept_group") or h.get("name")),
                industry_row=ind_row,
                news_hits=news_hits,
                quality_why=[why_keep],
                carry=False,
                force_hint=h,
            )
            # 同行业已被动态空壳占位：用带种子硬主题替换（不踩掉其它结构硬主题）
            replaced = False
            for i in range(len(themes) - 1, -1, -1):
                t = themes[i]
                t_em = _normalize_concept_key(
                    str(t.get("_em_industry") or t.get("_matched_board") or "")
                )
                t_ind = _normalize_concept_key(
                    str(t.get("_industry") or t.get("_matched_board") or "")
                )
                t_lab = _normalize_concept_key(str(t.get("industry_label") or ""))
                if not (
                    t_em == ind_key
                    or t_ind == ind_key
                    or (dedupe_key and t_lab == dedupe_key)
                ):
                    continue
                old_tid = str(t.get("id") or "")
                if old_tid == hid:
                    replaced = True
                    break
                # 其它结构硬主题已占真·行业：不替换，靠 industry_label 分池并存
                if old_tid in _STRUCTURE_KEEP_HINT_IDS:
                    continue
                themes.pop(i)
                if old_tid:
                    seen_ids.discard(old_tid)
                    have_ids.discard(old_tid)
                if t_ind:
                    seen_industries.discard(t_ind)
                if t_em:
                    seen_industries.discard(t_em)
                if t_lab:
                    seen_industries.discard(t_lab)
                themes.append(theme)
                if hid:
                    seen_ids.add(hid)
                    have_ids.add(hid)
                if ind_key:
                    seen_industries.add(ind_key)
                if dedupe_key:
                    seen_industries.add(dedupe_key)
                replaced = True
                break
            if replaced:
                continue
            if dedupe_key and dedupe_key in seen_industries and not user_pin:
                continue
            if label and _normalize_concept_key(label) in seen_industries and not user_pin:
                continue
            before = len(themes)
            _add(theme)
            if len(themes) > before:
                have_ids.add(hid)
            elif user_pin and hid not in have_ids:
                # 用户点名：即便行业坑被占也强制追加（靠 industry_label 区分展示）
                themes.append(theme)
                have_ids.add(hid)
                if hid:
                    seen_ids.add(hid)
                if dedupe_key:
                    seen_industries.add(dedupe_key)

    return themes



def _consecutive_good_days(history: Dict[str, Any], code: str, asof: str) -> int:
    """asof 之前连续「走强」天数（仅走强计连入；旧数据 ok=True 视为走强）。"""
    days = sorted(d for d in (history.get("days") or {}) if d < asof)
    if not days:
        return 0
    streak = 0
    for d in reversed(days):
        day = history["days"].get(d) or {}
        codes = set(day.get("codes") or [])
        if code not in codes:
            break
        st = (day.get("status") or {}).get(code)
        if st is None:
            streak += 1
            continue
        grade = str(st.get("grade") or "")
        if grade:
            if grade != "走强":
                break
        elif not bool(st.get("ok", True)):
            break
        streak += 1
    return streak


def _consecutive_bad_days(history: Dict[str, Any], code: str, asof: str) -> int:
    """asof 之前连续「走弱」天数（仅跌超3%的走弱计踢出；偏弱/偏好打断）。"""
    days = sorted(d for d in (history.get("days") or {}) if d < asof)
    if not days:
        return 0
    streak = 0
    for d in reversed(days):
        day = history["days"].get(d) or {}
        codes = set(day.get("codes") or [])
        if code not in codes:
            break
        st = (day.get("status") or {}).get(code)
        if st is None:
            break
        grade = str(st.get("grade") or "")
        if grade:
            if grade != "走弱":
                break
        elif st.get("bad") is True:
            pass
        elif bool(st.get("ok", True)):
            break
        else:
            # 旧数据 ok=False：当作走弱
            pass
        streak += 1
    return streak


# —— 个股当日分档（禁止把微跌写成「走好」）——
# 走强：涨幅 ≥ 2% → 可 +连入
# 偏好：0% ≤ 涨幅 < 2% → 不加不减星、不加连入
# 偏弱：-3% < 涨跌 < 0 → 不加不减星、不加连入（不能标走好）
# 走弱：涨跌 ≤ -3% → 减星、不加连入
# 踢出：看量能——缩量阴跌（浪潮式）留观察；放量大跌（兆易式）才踢
STOCK_STRONG_PCT = 2.0
STOCK_WEAK_PCT = -3.0
STOCK_HARD_DROP_PCT = -5.0  # 证据文案用：更深跌幅说明
STOCK_FLOW_BAD_YI = -0.8
STOCK_5D_BROKEN_PCT = -12.0
BOARD_HARD_DROP_PCT = -4.0
BOARD_FLOW_BAD_YI = -5.0
# 量能裁决踢出
VOL_SHRINK_MAX = 0.95  # 量比≤此视为缩量，阴跌不踢
VOL_DUMP_MIN = 1.30  # 量比≥此 + 跌超3% → 放量砸
VOL_HARD_DUMP_MIN = 1.50  # 量比≥此 + 跌超5% → 偏硬砸
VOL_EXTREME_DUMP_MIN = 1.80
PCT_EXTREME_DUMP = -6.0


def _is_volume_dump(
    *,
    stock_pct: Optional[float],
    vol_ratio: Optional[float],
    stock_flow: Optional[float] = None,
) -> Tuple[bool, str]:
    """
    是否「放量大跌」。
    缩量阴跌返回 False（哪怕跌超3%）；放量砸盘返回 True。
    """
    pct = None if stock_pct is None else float(stock_pct)
    if pct is None or pct > STOCK_WEAK_PCT:
        return False, ""
    vr = None if vol_ratio is None else float(vol_ratio)
    flow = None if stock_flow is None else float(stock_flow)

    if vr is not None and vr <= VOL_SHRINK_MAX:
        return False, f"缩量阴跌(量比{vr:.2f}/跌{pct:.1f}%)，消化观察"

    if vr is None:
        # 无量比：仅极端硬砸+大幅流出才当放量砸，避免误踢缩量票
        if pct <= STOCK_HARD_DROP_PCT and flow is not None and flow <= -1.5:
            return True, f"无量比但硬砸{pct:.1f}%+流出{abs(flow):.1f}亿，按放量砸"
        return False, "无量比且非极端流出，不按放量砸踢"

    if vr >= VOL_EXTREME_DUMP_MIN and pct <= PCT_EXTREME_DUMP:
        return True, f"极端放量崩盘(量比{vr:.2f}/跌{pct:.1f}%)"
    if vr >= VOL_HARD_DUMP_MIN and pct <= STOCK_HARD_DROP_PCT:
        return True, f"放量硬砸(量比{vr:.2f}/跌{pct:.1f}%)"
    if vr >= VOL_DUMP_MIN and pct <= STOCK_WEAK_PCT:
        return True, f"放量大跌(量比{vr:.2f}/跌{pct:.1f}%)"
    if (
        vr >= 1.20
        and pct <= STOCK_WEAK_PCT
        and flow is not None
        and flow <= -1.5
    ):
        return True, f"放量流出砸(量比{vr:.2f}/流出{abs(flow):.1f}亿)"
    return False, f"走弱但量能未放(量比{vr:.2f}/跌{pct:.1f}%)，留观察"


def _prev_status_dump(
    history: Dict[str, Any], code: str, asof: str
) -> Tuple[bool, str]:
    """上一交易日是否已记录为放量砸。"""
    days = sorted(d for d in (history.get("days") or {}) if d < asof)
    if not days:
        return False, ""
    st = ((history["days"].get(days[-1]) or {}).get("status") or {}).get(code) or {}
    grade = str(st.get("grade") or "")
    pct = _to_float(st.get("pct"))
    vr = _to_float(st.get("vol_ratio"))
    flow = _to_float(st.get("flow"))
    if grade and grade != "走弱" and (pct is None or float(pct) > STOCK_WEAK_PCT):
        return False, ""
    # 旧缓存无量比：若明确走弱且跌很深+流出，保守当砸过
    if vr is None and grade == "走弱":
        if pct is not None and float(pct) <= STOCK_HARD_DROP_PCT and (
            flow is not None and float(flow) <= -1.0
        ):
            return True, f"昨走弱无量比但硬砸{float(pct):.1f}%"
        return False, "昨走弱但无量比，不记放量砸"
    dump, why = _is_volume_dump(stock_pct=pct, vol_ratio=vr, stock_flow=flow)
    return dump, why


def _should_kick_stock_on_weak(
    *,
    dig_wait: bool,
    stock_pct: Optional[float],
    stock_pct_5d: Optional[float],
    vol_ratio: Optional[float],
    stock_flow: Optional[float],
    history: Dict[str, Any],
    code: str,
    asof: str,
    grade_why: str,
) -> Tuple[bool, str]:
    """
    选股/观察期：个股走弱一律不踢出池。
    微跌、连跌、放量砸都是选股要盯的位置；走坏止损只用于已买持有期。
    主题中期逻辑坏了才整主题撤（见 _theme_thesis_broken），不在个股层踢。
    """
    _ = (
        dig_wait,
        stock_pct,
        stock_pct_5d,
        vol_ratio,
        stock_flow,
        history,
        code,
        asof,
        grade_why,
    )
    return False, "选股期个股走弱不踢，留观察微跌/起稳/回踩（止损属持有期）"


def _theme_thesis_broken(
    *,
    hint: Optional[Dict[str, Any]],
    news_hits: List[str],
    board_pct: Optional[float],
    board_flow: Optional[float],
    board_pct5: Optional[float],
    board_flow5: Optional[float],
    bad_before: int,
    theme_grade: str,
) -> Tuple[bool, str]:
    """
    已在池主题：仅中期逻辑破坏才整主题撤。
    单日/两日走弱不撤——那是选股盯买点的窗口，不是踢出理由。
    """
    hid = str((hint or {}).get("id") or "")
    dig_wait = hid in _DIG_WAIT_HINT_IDS
    structure_keep = hid in _STRUCTURE_KEEP_HINT_IDS
    major = _major_catalyst_in_hits(news_hits)
    real_news = [t for t in (news_hits or []) if not _is_weak_forward_news(t)]

    if major:
        return False, ""

    if dig_wait:
        if (
            board_pct5 is not None
            and float(board_pct5) <= -15.0
            and board_flow5 is not None
            and float(board_flow5) <= -10.0
            and len(real_news) < 1
        ):
            return True, "材料主题近5日深度破位且资金撤离、无催化，中期逻辑破坏"
        return False, ""

    today_weakish = theme_grade in ("偏弱", "走弱")
    # 连续偏弱/走弱≥3日（昨已≥2 + 今仍弱）且近5日仍跌 → 中期转坏
    if today_weakish and int(bad_before) >= 2:
        if board_pct5 is not None and float(board_pct5) <= -2.0:
            if structure_keep:
                if float(board_pct5) <= -8.0 and (
                    board_flow5 is None or float(board_flow5) < 0
                ):
                    return (
                        True,
                        f"硬主题已连续走坏{int(bad_before)+1}日且近5日"
                        f"{float(board_pct5):+.1f}%，中期逻辑破坏",
                    )
            else:
                return (
                    True,
                    f"主题已连续偏弱/走弱{int(bad_before)+1}日且近5日"
                    f"{float(board_pct5):+.1f}%，中期逻辑破坏",
                )

    # 单日深崩 + 5日已坏（非硬结构保位）
    if (
        not structure_keep
        and theme_grade == "走弱"
        and board_pct is not None
        and float(board_pct) <= -5.0
        and board_pct5 is not None
        and float(board_pct5) <= -5.0
        and (board_flow5 is None or float(board_flow5) < 0)
    ):
        return True, "板块单日深跌且近5日走坏，主题中期逻辑破坏"

    if (
        board_pct5 is not None
        and float(board_pct5) <= -8.0
        and board_flow5 is not None
        and float(board_flow5) <= -6.0
        and len(real_news) < 1
        and not structure_keep
    ):
        return True, "近5日深跌且资金撤离、无催化，主题中期逻辑破坏"

    _ = board_flow
    return False, ""


def _board_trend_broken(
    board_pct: Optional[float], board_flow: Optional[float]
) -> Tuple[bool, str]:
    """板块是否「明显崩坏」（仅作走弱证据补充，不单独决定分档）。"""
    if board_pct is not None and board_pct <= BOARD_HARD_DROP_PCT:
        if board_flow is not None and board_flow <= BOARD_FLOW_BAD_YI:
            return True, (
                f"板块大跌{board_pct:.2f}%且主力净流出{abs(board_flow):.2f}亿"
            )
        if board_pct <= BOARD_HARD_DROP_PCT - 1.0:  # ≤ -5%
            return True, f"板块单日大跌{board_pct:.2f}%"
    return False, ""


def _stock_day_grade(
    *,
    stock_pct: Optional[float],
    stock_pct_5d: Optional[float] = None,
    stock_flow: Optional[float] = None,
    board_pct: Optional[float] = None,
    board_flow: Optional[float] = None,
) -> Tuple[str, int, str]:
    """
    个股当日分档。
    返回 (状态标签, 星级增减, 说明)。
    星级增减：仅「走弱」为 -1；偏好/偏弱为 0；走强本身不加星（连入加成在评分里）。
    """
    pct = stock_pct
    if pct is None:
        return "偏好", 0, "无涨跌幅，按偏好处理（不加不减星）"

    if pct >= STOCK_STRONG_PCT:
        return "走强", 0, f"涨幅{pct:.1f}%≥{STOCK_STRONG_PCT:.0f}%，走强可计连入"

    if pct >= 0:
        return "偏好", 0, f"涨幅{pct:.1f}%<{STOCK_STRONG_PCT:.0f}%，偏好：不加星不减星、不计连入"

    if pct > STOCK_WEAK_PCT:
        return "偏弱", 0, f"跌幅{abs(pct):.1f}%未超3%，偏弱：不加星不减星、不计连入"

    # 跌超 3%：走弱，必减星
    why = f"跌超3%（{pct:.1f}%），走弱减星"
    if pct <= STOCK_HARD_DROP_PCT:
        why = f"个股硬砸{pct:.1f}%，走弱减星"
    elif stock_flow is not None and stock_flow <= STOCK_FLOW_BAD_YI:
        why += f"；主力净流出{abs(stock_flow):.2f}亿"
    else:
        board_bad, board_why = _board_trend_broken(board_pct, board_flow)
        if board_bad:
            why += f"；{board_why}"
        elif stock_pct_5d is not None and stock_pct_5d <= STOCK_5D_BROKEN_PCT:
            why += f"；近5日回撤{stock_pct_5d:.1f}%"
    return "走弱", -1, why


def _day_quality_ok(
    *,
    stock_pct: Optional[float],
    stock_pct_5d: Optional[float],
    stock_flow: Optional[float],
    board_pct: Optional[float],
    board_flow: Optional[float],
) -> Tuple[bool, str]:
    """兼容旧接口：仅「走强」视为可 +连入的 ok。"""
    grade, _delta, why = _stock_day_grade(
        stock_pct=stock_pct,
        stock_pct_5d=stock_pct_5d,
        stock_flow=stock_flow,
        board_pct=board_pct,
        board_flow=board_flow,
    )
    return grade == "走强", why


def _theme_in_day(day: Dict[str, Any], theme: Dict[str, Any]) -> bool:
    name = str(theme.get("name") or "")
    tid = str(theme.get("id") or "")
    names = [str(x) for x in (day.get("theme_names") or [])]
    ids = [str(x) for x in (day.get("theme_ids") or [])]
    hit = (name and name in names) or (tid and tid in ids)
    if not hit and name:
        hit = any(name[:4] in n or n[:4] in name for n in names if n)
    return bool(hit)


def _theme_status_for_day(
    day: Dict[str, Any], theme: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    st_map = day.get("theme_status") or {}
    tid = str(theme.get("id") or "")
    name = str(theme.get("name") or "")
    if tid and tid in st_map:
        return st_map.get(tid)
    if name and name in st_map:
        return st_map.get(name)
    return None


def _theme_recent_streak(
    history: Dict[str, Any], theme: Dict[str, Any], asof: str, lookback: int = 5
) -> int:
    """asof 之前连续多少天该主题已在观察池（按名称或 id）。"""
    days = sorted(d for d in (history.get("days") or {}) if d < asof)[-lookback:]
    if not days:
        return 0
    streak = 0
    for d in reversed(days):
        day = history["days"].get(d) or {}
        if _theme_in_day(day, theme):
            streak += 1
        else:
            break
    return streak


def _theme_consecutive_bad_days(
    history: Dict[str, Any], theme: Dict[str, Any], asof: str
) -> int:
    """asof 之前连续「偏弱/走弱」天数。偏好/走强打断。旧数据无标记不计入。"""
    days = sorted(d for d in (history.get("days") or {}) if d < asof)
    if not days:
        return 0
    streak = 0
    for d in reversed(days):
        day = history["days"].get(d) or {}
        if not _theme_in_day(day, theme):
            break
        st = _theme_status_for_day(day, theme)
        if st is None:
            break
        grade = str(st.get("grade") or "")
        if grade:
            if grade not in ("偏弱", "走弱"):
                break
        elif bool(st.get("ok", True)):
            break
        streak += 1
    return streak


def _theme_consecutive_good_days(
    history: Dict[str, Any], theme: Dict[str, Any], asof: str
) -> int:
    """asof 之前连续「走强/偏好」天数。旧数据无标记视为走强。"""
    days = sorted(d for d in (history.get("days") or {}) if d < asof)
    if not days:
        return 0
    streak = 0
    for d in reversed(days):
        day = history["days"].get(d) or {}
        if not _theme_in_day(day, theme):
            break
        st = _theme_status_for_day(day, theme)
        if st is None:
            streak += 1
            continue
        grade = str(st.get("grade") or "")
        if grade:
            if grade not in ("走强", "偏好"):
                break
        elif not bool(st.get("ok", True)):
            break
        streak += 1
    return streak


# 主题分档与个股对齐（文案用走强/偏好/偏弱/走弱）
THEME_STRONG_PCT = 2.0
THEME_WEAK_PCT = -3.0


def _theme_day_grade(
    *,
    board_pct: Optional[float],
    board_flow: Optional[float] = None,
    board_pct5: Optional[float] = None,
    board_flow5: Optional[float] = None,
) -> Tuple[str, int, str]:
    """
    主题当日分档。返回 (标签, 星级增减, 说明)。
    仅「走弱」(跌超3%) 减星；偏弱/偏好不加不减。
    """
    if board_pct is None:
        return "偏好", 0, "无板块涨跌，按偏好"
    if board_pct >= THEME_STRONG_PCT:
        return "走强", 0, f"板块涨{board_pct:.2f}%≥{THEME_STRONG_PCT:.0f}%"
    if board_pct >= 0:
        return "偏好", 0, f"板块涨{board_pct:.2f}%不足2%，偏好不加不减星"
    if board_pct > THEME_WEAK_PCT:
        extra = ""
        if board_pct5 is not None:
            extra = f"，5日{board_pct5:+.1f}%"
        return "偏弱", 0, f"板块跌{abs(board_pct):.2f}%未超3%{extra}，偏弱不加不减星"
    why = f"板块跌超3%（{board_pct:+.2f}%），走弱减星"
    if board_flow is not None and board_flow < 0:
        why += f"，主力净流出{abs(board_flow):.2f}亿"
    if board_flow5 is not None and board_flow5 < 0:
        why += f"，5日资金{board_flow5:.1f}亿"
    return "走弱", -1, why


def _theme_day_ok(
    *,
    board_pct: Optional[float],
    board_flow: Optional[float],
    board_pct5: Optional[float],
    board_flow5: Optional[float] = None,
) -> Tuple[bool, str]:
    """兼容：走强/偏好视为主题日偏正（不计入偏弱踢出计数）。"""
    grade, _d, why = _theme_day_grade(
        board_pct=board_pct,
        board_flow=board_flow,
        board_pct5=board_pct5,
        board_flow5=board_flow5,
    )
    return grade in ("走强", "偏好"), why


def _evaluate_theme_entry(
    *,
    theme: Dict[str, Any],
    news_hits: List[str],
    board_name: str,
    board_pct: Optional[float],
    board_flow: Optional[float],
    board_pct5: Optional[float],
    board_flow5: Optional[float],
    recent_streak: int,
    bad_before: int,
) -> Tuple[bool, bool, bool, str, List[str], bool, str, int]:
    """
    主题是否进入当日观察。
    返回 (enter, weak_board, theme_ok, weak_why, reasons, kicked, grade, star_delta)。

    原则：
    - 新进：板块须具备约1～2月前瞻，宁缺毋滥；
    - 已在池：不因单日/两日走弱踢；仅中期逻辑破坏才整主题撤；
    - 主题日弱时仍可列个股，盯微跌/起稳/回踩（选股对象）。
    """
    reasons: List[str] = []
    theme_grade, theme_star_delta, weak_why = _theme_day_grade(
        board_pct=board_pct,
        board_flow=board_flow,
        board_pct5=board_pct5,
        board_flow5=board_flow5,
    )

    hint = None
    tid = str(theme.get("id") or "")
    tname = str(theme.get("name") or "")
    for h in THEME_HINTS:
        if h.get("id") == tid or h.get("name") == tname:
            hint = h
            break

    dig_wait = bool(hint) and str(hint.get("id") or "") in _DIG_WAIT_HINT_IDS
    structure_keep = bool(hint) and str(hint.get("id") or "") in _STRUCTURE_KEEP_HINT_IDS

    quality, _rank, pulse, q_why = _board_quality_score(
        board_name=board_name or tname,
        board_pct=board_pct,
        board_flow=board_flow,
        board_pct5=board_pct5,
        board_flow5=board_flow5,
        news_hits=news_hits,
        hint=hint,
    )
    reasons.extend(q_why)

    forward_ok, forward_why = _forward_outlook_ok(
        quality=quality,
        pulse=pulse,
        news_hits=news_hits,
        board_pct=board_pct,
        board_flow=board_flow,
        board_pct5=board_pct5,
        board_flow5=board_flow5,
        hint=hint,
        board_name=board_name or tname,
    )

    thesis_broken, thesis_why = _theme_thesis_broken(
        hint=hint,
        news_hits=news_hits,
        board_pct=board_pct,
        board_flow=board_flow,
        board_pct5=board_pct5,
        board_flow5=board_flow5,
        bad_before=bad_before,
        theme_grade=theme_grade,
    )

    enter_new = False
    enter_sticky = False
    kicked = False

    if recent_streak >= 1:
        # —— 已在池：粘性；仅中期逻辑坏了才撤 ——
        if thesis_broken:
            kicked = True
            reasons.append(f"主题中期逻辑破坏，整主题撤出：{thesis_why}")
        else:
            enter_sticky = True
            if theme_grade == "走弱":
                reasons.append(
                    f"已在池：板块当日走弱不撤（{weak_why}）；"
                    "个股盯微跌/起稳/回踩，止损属持有期"
                )
            elif forward_ok:
                reasons.append(
                    f"延续前瞻：已连续{recent_streak}日在池，今日{theme_grade}；{forward_why}"
                )
            else:
                reasons.append(
                    f"已在池暂留（中期未破）：今日{theme_grade}；{forward_why}"
                )
    else:
        # —— 新进：门槛严 ——
        if thesis_broken and not dig_wait:
            reasons.append(f"新主题中期已坏，不进：{thesis_why}")
        elif theme_grade == "走弱" and not dig_wait:
            reasons.append(f"新主题当日走弱，不进观察：{weak_why}")
        elif dig_wait and theme_grade == "走弱":
            enter_new = True
            reasons.append(
                f"材料挖坑新进：今日走弱（{weak_why}），正是观察买点窗口"
            )
        elif forward_ok:
            enter_new = True
            reasons.append(f"新进前瞻：{forward_why}（看未来1～2月，宁缺毋滥）")
        else:
            reasons.append(forward_why)

    # 结构硬主题：新进被挡时仍可保位注入（材料/PCB）；已在池已由粘性处理
    if structure_keep and recent_streak < 1 and not enter_new and not kicked:
        if theme_grade != "走弱" or dig_wait:
            enter_new = True
            reasons.append(
                "材料挖坑保位观察（跌了才有买点；不代表现价可买）"
                if dig_wait
                else "结构硬主题保位观察（防同质板挤出；不代表现价可买）"
            )

    enter = (enter_new or enter_sticky) and not kicked
    # 选股期：主题在池即允许列个股与买点识别（含当日偏弱/走弱的挖坑窗口）
    theme_ok = bool(enter)
    weak_board = theme_grade in ("偏弱", "走弱") and enter

    if not enter and not reasons:
        reasons.append("多因子前瞻线索不足")

    return (
        enter,
        weak_board,
        theme_ok,
        weak_why,
        reasons,
        kicked,
        theme_grade,
        theme_star_delta,
    )


def _has_matched_board(board_name: str) -> bool:
    return bool(board_name and board_name != "-")


def _score_theme(
    *,
    theme: Dict[str, Any],
    consecutive_good: int,
    news_hits: int,
    board_pct: Optional[float],
    board_flow: Optional[float],
    board_pct5: Optional[float],
    theme_ok: bool,
    bad_before: int,
    theme_grade: str = "偏好",
    star_delta: int = 0,
) -> Tuple[int, List[str]]:
    """主题 1～5 星：仅走弱减星；偏弱/偏好不加不减；走强后连日/好转可加。"""
    reasons: List[str] = []
    score = 2
    reasons.append("主题在观察池（起步2星）")
    pri = int(theme.get("priority") or 3)
    if pri >= 5:
        score += 1
        reasons.append("结构高优 +1")
    if consecutive_good >= 2:
        score += 1
        reasons.append(f"连续{consecutive_good}日主题走强/偏好 +1")
    if consecutive_good >= 3:
        score += 1
        reasons.append("连续≥3日走强/偏好 +1")
    if news_hits >= 1:
        score += 1
        reasons.append(f"新闻命中{news_hits}条 +1")
    if board_flow is not None and board_flow > 0.5:
        score += 1
        reasons.append(f"板块流入{board_flow:.2f}亿 +1")
    if board_pct5 is not None and board_pct5 >= 5.0 and theme_grade == "走强":
        score += 1
        reasons.append(f"5日涨{board_pct5:.1f}%且今日走强 +1")
    if star_delta < 0:
        score += star_delta
        reasons.append(f"今日{theme_grade}减星 {star_delta}")
    elif theme_grade in ("偏弱", "偏好"):
        reasons.append(f"今日{theme_grade}：不加星不减星")
    elif theme_ok and bad_before >= 1:
        score += 1
        reasons.append("偏弱/走弱后今日转偏好或走强加星 +1")
    if board_pct is not None and board_pct <= -3.0 and star_delta >= 0:
        # 已在 star_delta 处理；避免重复
        pass
    # 首日：结构高优不加那一星，避免佐证库挂靠就抬星
    if pri >= 5 and consecutive_good < 2 and score > 2:
        score -= 1
        reasons.append("主题新进：结构高优首日不加星")
    score = max(1, min(5, score))
    capped, cap_why = _apply_newcomer_star_cap(
        score, consecutive_good, label="主题走好连日"
    )
    if cap_why:
        reasons.append(cap_why)
    return capped, reasons


def _stock_worth_in_theme(
    *,
    stock_pct: Optional[float],
    stock_pct_5d: Optional[float],
    stock_flow: Optional[float],
    stock_flow_5d: Optional[float] = None,
    stock_grade: str,
    theme_ok: bool,
    major_catalyst: bool = False,
    is_seed: bool = False,
    dig_wait_theme: bool = False,
) -> Tuple[bool, str]:
    """
    主题在池时，中军/种子是否列入观察。
    选股对象含微跌、走弱、回踩——不因个股当日走坏拒看（止损属持有期）。
    """
    pct = stock_pct
    pct5 = stock_pct_5d
    flow = stock_flow
    flow5 = stock_flow_5d

    # 主题中军种子：只要主题还在池，一律留观察（跌了才有买点）
    if is_seed and theme_ok:
        return True, "主题中军种子留观察（微跌/起稳/回踩也是选股对象）"
    if dig_wait_theme and is_seed:
        return True, "材料挖坑观察：走弱日仍留中军（不代表现价可买）"
    if major_catalyst and is_seed:
        return True, "重大利好催化下的主题中军，纳入观察"

    capital_hot = (flow is not None and float(flow) > 0.15) or (
        flow5 is not None and float(flow5) > 0.8
    )
    had_move = pct5 is not None and float(pct5) >= 3.0
    # 真正回踩候选：近5日有过表现，今日在收敛
    real_pullback = (
        pct5 is not None
        and 4.0 <= float(pct5) < 16.0
        and pct is not None
        and -2.5 <= float(pct) <= 1.5
        and stock_grade in ("偏好", "偏弱")
        and (flow is None or float(flow) >= -0.8)
    )
    # 连跌后起稳/微跌：也是选股对象
    dig_hold = (
        pct is not None
        and -3.0 <= float(pct) <= 2.5
        and pct5 is not None
        and -12.0 <= float(pct5) < 5.0
        and stock_grade in ("偏好", "偏弱", "走强", "走弱")
    )

    if (
        not capital_hot
        and (pct5 is None or abs(float(pct5)) < 2.5)
        and stock_grade in ("偏弱", "偏好", "走弱")
        and not dig_hold
        and not (major_catalyst and is_seed)
    ):
        return False, "资金冷淡且近5日无表现，缺乏板块代表投资价值"

    if (
        flow is not None
        and float(flow) < -0.5
        and (flow5 is None or float(flow5) <= 0)
        and not real_pullback
        and not dig_hold
        and not (had_move and capital_hot)
    ):
        return False, "个股资金持续冷淡/流出，不优先观察"

    if capital_hot or had_move or real_pullback or dig_hold:
        why = []
        if capital_hot:
            why.append("资金有认可")
        if real_pullback:
            why.append("走高后回踩结构")
        elif dig_hold:
            why.append("连跌/整理后微跌起稳窗口")
        elif had_move:
            why.append("近5日有表现")
        return True, "；".join(why)

    if not theme_ok and not capital_hot:
        return False, "主题一般且个股无资金认可"

    return False, "未见资金认可或有效走势结构"


# 软主题：主线星封顶，避免地产/教育/金融通道刷成「当下最硬主升浪」
# 黄金/贵金属等已纳入多元保位，不再封顶
_SOFT_WAVE_THEME_SUBSTR = (
    "房地产",
    "租赁",
    "在线教育",
    "教育",
    "白酒",
    "乳业",
    "银行",
    "证券",
    "保险",
    "信托",
    "煤炭",
    "航运",
)

# 同质通道板：个股高度同涨同跌，观察池只留 2～3 只中军即可
_HOMOGENEOUS_THEME_SUBSTR = (
    "银行",
    "证券",
    "保险",
    "信托",
    "煤炭",
    "航运",
    "白酒",
    "期货",
)

# 多元主题保位：1～2月看好的细分主题都尽量留池；科技硬件单独设上限
_STRUCTURE_KEEP_HINT_IDS = frozenset(
    {
        # 科技硬件（发现排序有上限）
        "cpo_optical",
        "domestic_server",
        "liquid_cooling",
        "compute_rental",
        "pcb_ccl",
        "semi_materials",
        "semi_equipment",
        "mlcc",
        "memory_storage",
        # 科技应用/独立景气（与硬件分池，同等保位）
        "ai_app_soft",
        "short_drama_aigc",
        "humanoid_robot",
        "fiber_cable",
        # 非科技多元
        "precious_metals",
        "innovative_drug",
        "grid_power",
        "nuclear_power",
        "aerospace",
        "defense_military",
        "agriculture",
        "auto_oem",
        "pv_solar",
        "minor_metals",
        "lab_diamond",
    }
)

# 科技硬件簇：CPO/服务器/PCB/材料/设备/MLCC/存储等同涨同跌，发现阶段最多先入池 N 个
_TECH_HARDWARE_HINT_IDS = frozenset(
    {
        "cpo_optical",
        "domestic_server",
        "overseas_odm",
        "liquid_cooling",
        "compute_rental",
        "pcb_ccl",
        "semi_materials",
        "semi_equipment",
        "mlcc",
        "memory_storage",
        "power_supply",
    }
)

_MAX_TECH_HARDWARE_STRUCTURE_FIRST = 5

# 挖坑观察：跌了才有买点窗口——走弱日也不踢
_DIG_WAIT_HINT_IDS = frozenset(
    {
        "semi_materials",
        "semi_equipment",
        "pcb_ccl",
        "mlcc",
        "memory_storage",
        "precious_metals",
        "agriculture",
        "pv_solar",
        "minor_metals",
    }
)

# 用户点名强保：主题未热时仍强制保位注入（hint_id 必须在 THEME_HINTS）
_USER_PIN_HINT_IDS = frozenset({"mlcc"})

_HOMOGENEOUS_STOCK_CAP = 3
_DEFAULT_STOCK_CAP = 8
_OVERSEAS_ODM_STOCK_CAP = 3


def _theme_text_blob(theme: Optional[Dict[str, Any]]) -> str:
    if not theme:
        return ""
    return " ".join(
        str(theme.get(k) or "")
        for k in ("name", "_concept", "_industry", "id", "_hint_id")
    )


def _is_homogeneous_theme(theme: Optional[Dict[str, Any]] = None, *names: str) -> bool:
    blob = _theme_text_blob(theme) + " " + " ".join(str(x or "") for x in names)
    return any(s in blob for s in _HOMOGENEOUS_THEME_SUBSTR)


def _stock_limit_for_theme(theme: Optional[Dict[str, Any]]) -> int:
    """银行/证券等同质板最多 3 只；海外组装只盯富联频道。"""
    if not theme:
        return _DEFAULT_STOCK_CAP
    tid = str(theme.get("id") or theme.get("_hint_id") or "")
    if tid == "overseas_odm":
        return _OVERSEAS_ODM_STOCK_CAP
    if _is_homogeneous_theme(theme):
        return _HOMOGENEOUS_STOCK_CAP
    return _DEFAULT_STOCK_CAP


def _score_wave_fit_stars(
    *,
    theme: Dict[str, Any],
    theme_ok: bool,
    theme_grade: str,
    news_hits: int,
    board_flow: Optional[float],
    board_pct: Optional[float],
    board_pct5: Optional[float],
    consecutive: int,
) -> Tuple[int, List[str]]:
    """主线星：与当前最硬主升浪的贴合度（越高越像当下主线）。"""
    reasons: List[str] = ["主线星起步2星（贴合当前主升浪硬度）"]
    score = 2
    pri = int(theme.get("priority") or 3)
    tname = str(theme.get("name") or theme.get("_concept") or "")
    soft = any(s in tname for s in _SOFT_WAVE_THEME_SUBSTR)

    if pri >= 5:
        score += 1
        reasons.append(f"结构高优主题(priority={pri}) +1")
    elif pri <= 2:
        score -= 1
        reasons.append("主题偏题材/远期 -1")

    if theme_grade == "走强" and theme_ok:
        score += 1
        reasons.append("主题当日走强 +1")
    elif theme_grade == "偏好" and theme_ok:
        reasons.append("主题偏好：主线星不加不减")
    elif theme_grade in ("偏弱", "走弱"):
        score -= 1
        reasons.append(f"主题{theme_grade}，主升浪贴合降权 -1")

    if board_pct5 is not None and float(board_pct5) >= 12.0 and theme_ok:
        score += 1
        reasons.append(f"板块5日+{float(board_pct5):.1f}%处主升段 +1")
    elif board_pct5 is not None and float(board_pct5) <= -5.0:
        score -= 1
        reasons.append(f"板块5日{float(board_pct5):.1f}%，非主升 -1")

    if board_flow is not None and float(board_flow) >= 3.0 and theme_ok:
        score += 1
        reasons.append(f"板块流入{float(board_flow):.1f}亿，资金认主线 +1")
    elif board_flow is not None and float(board_flow) <= -5.0:
        score -= 1
        reasons.append(f"板块流出{abs(float(board_flow)):.1f}亿 -1")

    if news_hits >= 1 and theme_ok and not soft:
        score += 1
        reasons.append(f"新闻催化×{news_hits}支撑主线 +1")

    if consecutive >= 2 and theme_ok and theme_grade in ("走强", "偏好"):
        score += 1
        reasons.append(f"主题侧连入/走好{consecutive}日 +1")

    if soft:
        score = min(score, 2)
        reasons.append("软主题（地产/教育等）主线星封顶2星")

    if board_pct is not None and float(board_pct) >= 4.5 and theme_ok:
        # 当日过热不降主线星（主线可以很热），只备注
        reasons.append(f"板块今日+{float(board_pct):.1f}%偏热，主线仍可高星")

    score = max(1, min(5, score))
    capped, cap_why = _apply_newcomer_star_cap(score, consecutive, label="主线连入")
    if cap_why:
        reasons.append(cap_why)
    return capped, reasons


def _score_buy_timing_stars(
    *,
    theme: Dict[str, Any],
    consecutive: int,
    board_flow: Optional[float],
    board_pct: Optional[float],
    stock_pct: Optional[float],
    stock_pct_5d: Optional[float],
    stock_flow: Optional[float] = None,
    stock_flow_5d: Optional[float] = None,
    theme_ok: bool = True,
    stock_grade: str = "偏好",
    star_delta: int = 0,
    theme_grade: str = "偏好",
    theme_star_delta: int = 0,
    mild_flow_days: int = 0,
    buy_setup: Optional[Dict[str, Any]] = None,
    chase_reasons: Optional[List[str]] = None,
) -> Tuple[int, List[str], List[str]]:
    """
    买点星：当前位置是否适合买（越高越接近上车）。
    含：真回踩 / 起稳微涨 / 连涨流入；追高与涨停附近大降权。
    """
    reasons: List[str] = ["买点星起步2星（时机好坏，不是涨幅榜）"]
    score = 2
    buy_setup = buy_setup or {}
    chase_reasons = list(chase_reasons or [])
    if not chase_reasons:
        chase_reasons, chase_pen = _detect_chase_or_fake(
            stock_pct=stock_pct,
            stock_pct_5d=stock_pct_5d,
            stock_flow=stock_flow,
            theme_grade=theme_grade,
            board_pct=board_pct,
        )
    else:
        _, chase_pen = _detect_chase_or_fake(
            stock_pct=stock_pct,
            stock_pct_5d=stock_pct_5d,
            stock_flow=stock_flow,
            theme_grade=theme_grade,
            board_pct=board_pct,
        )
    is_chase = bool(chase_reasons)
    deep_stock_dd = stock_pct_5d is not None and float(stock_pct_5d) <= -8.0

    kind = str(buy_setup.get("kind") or "none")
    if kind in ("true_pullback", "stabilize_up") and buy_setup.get("buy_ok"):
        score += 2
        reasons.append(f"买点形态「{buy_setup.get('label')}」+2")
    elif kind in ("mild_inflow_run", "catalyst_grind") and buy_setup.get("buy_ok"):
        score += 2
        reasons.append(f"买点形态「{buy_setup.get('label')}」+2")
    elif kind == "dig_watch":
        score += 1
        reasons.append("止跌观察窗口（选股对象）+1")
    elif kind == "reject_bar":
        score -= 1
        reasons.append("高开低走/冲高回落，买点星 -1")
    elif kind == "fake_pullback":
        score -= 1
        reasons.append("假回踩，买点星 -1")

    # 起稳微涨：2～3日温和走强，尚未大涨、未贴阶段高；或连跌后今日翻红
    early_stabilize = (
        theme_ok
        and not is_chase
        and theme_grade in ("走强", "偏好", "偏弱")
        and stock_grade in ("走强", "偏好")
        and stock_pct is not None
        and 0.3 <= float(stock_pct) < 3.5
        and stock_pct_5d is not None
        and (
            (3.0 <= float(stock_pct_5d) < 10.0 and not deep_stock_dd)
            or (-10.0 <= float(stock_pct_5d) < 4.0)
        )
        and (stock_flow is None or float(stock_flow) >= -0.2)
        and (
            mild_flow_days >= 1
            or (
                stock_flow is not None
                and float(stock_flow) > 0
                and float(stock_pct) < 3.5
            )
        )
    )
    if early_stabilize and kind not in (
        "true_pullback",
        "mild_inflow_run",
        "stabilize_up",
        "catalyst_grind",
        "dig_watch",
    ):
        score += 2
        reasons.append(
            f"起稳微涨（5日{float(stock_pct_5d):.1f}%/今{float(stock_pct):+.1f}%）"
            "适合早上车 +2"
        )
    elif early_stabilize:
        score += 1
        reasons.append("起稳微涨与已有买点形态叠加 +1")
    elif (
        stock_pct_5d is not None
        and 10.0 <= float(stock_pct_5d) < 14.0
        and stock_pct is not None
        and float(stock_pct) >= 1.5
        and not is_chase
    ):
        reasons.append(
            f"5日已+{float(stock_pct_5d):.1f}%且仍在涨，偏中段非刚起稳，起稳不加分"
        )

    real_pullback = (
        theme_ok
        and not is_chase
        and theme_grade in ("走强", "偏好")
        and stock_pct_5d is not None
        and (
            5.0 <= float(stock_pct_5d) < 16.0
            or (
                5.0 <= float(stock_pct_5d) < 28.0
                and stock_pct is not None
                and -3.2 <= float(stock_pct) <= 1.2
            )
        )
        and stock_pct is not None
        and -3.2 <= float(stock_pct) <= 1.2
        and stock_grade in ("偏好", "偏弱")
        and (stock_flow is None or float(stock_flow) >= -0.35)
        and (stock_flow_5d is None or float(stock_flow_5d) > 0)
    )
    if real_pullback and kind != "true_pullback":
        score += 1
        reasons.append("走高后收敛回踩结构 +1")

    if stock_flow is not None and float(stock_flow) > 0.2 and not deep_stock_dd:
        score += 1
        reasons.append(f"个股流入{float(stock_flow):.2f}亿支撑买点 +1")
    elif stock_flow is not None and float(stock_flow) < -0.35:
        score -= 1
        reasons.append("个股资金流出，买点降权 -1")

    if deep_stock_dd and kind not in ("stabilize_up", "dig_watch", "true_pullback"):
        score -= 1
        reasons.append("近5日深跌未企稳，买点降权 -1")
    elif deep_stock_dd and kind in ("stabilize_up", "dig_watch"):
        reasons.append("近5日回撤但已处止跌/起稳窗口，深跌不额外降权")

    # 已大涨/涨停附近：买点星必须下来——只打「还在冲」；回踩日不因5日涨幅重罚
    if stock_pct is not None and float(stock_pct) >= 4.5:
        score -= 2
        reasons.append(f"今日{float(stock_pct):+.1f}%偏强/近涨停，买点 -2")
    if stock_pct_5d is not None and float(stock_pct_5d) >= 18.0:
        if stock_pct is not None and float(stock_pct) >= 2.0:
            score -= 2
            reasons.append(
                f"近5日已+{float(stock_pct_5d):.1f}%且今日仍冲，买点过热 -2"
            )
        elif stock_pct_5d is not None and float(stock_pct_5d) >= 40.0:
            score -= 2
            reasons.append(f"近5日+{float(stock_pct_5d):.1f}%极端连板扩张，买点 -2")
        # 回踩/十字星日：5日偏热不扣或轻扣
    elif stock_pct_5d is not None and float(stock_pct_5d) >= 14.0:
        if stock_pct is not None and float(stock_pct) >= 2.5:
            score -= 1
            reasons.append(f"近5日+{float(stock_pct_5d):.1f}%偏透支且仍冲，买点 -1")

    if chase_pen:
        score += chase_pen
        reasons.extend(chase_reasons)

    if star_delta < 0:
        score += star_delta
        reasons.append(f"今日{stock_grade}减星 {star_delta}")
    if theme_star_delta < 0:
        score += theme_star_delta
        reasons.append(f"主题走弱传导买点 {theme_star_delta}")

    if (
        str(theme.get("id") or "") in {"domestic_server", "overseas_odm", "domestic_compute"}
        and stock_pct_5d is not None
        and stock_pct_5d <= -12
    ):
        score -= 1
        reasons.append("算电-算近5日回撤大，买点 -1")

    if (
        score >= 4
        and stock_flow is not None
        and float(stock_flow) < 0
        and (stock_flow_5d is None or float(stock_flow_5d) <= 0)
    ):
        score = min(score, 3)
        reasons.append("个股资金负向，买点星封顶3")

    score = max(1, min(5, score))
    capped, cap_why = _apply_newcomer_star_cap(score, consecutive, label="买点连入")
    if cap_why:
        reasons.append(cap_why)
    return capped, reasons, chase_reasons


def _score_stock(
    *,
    theme: Dict[str, Any],
    consecutive: int,
    news_hits: int,
    board_flow: Optional[float],
    board_pct: Optional[float],
    board_pct5: Optional[float] = None,
    stock_pct: Optional[float],
    stock_pct_5d: Optional[float],
    stock_flow: Optional[float] = None,
    stock_flow_5d: Optional[float] = None,
    day_ok: bool = True,
    theme_ok: bool = True,
    stock_grade: str = "偏好",
    star_delta: int = 0,
    theme_grade: str = "偏好",
    theme_star_delta: int = 0,
    mild_flow_days: int = 0,
    buy_setup: Optional[Dict[str, Any]] = None,
) -> Tuple[int, int, int, List[str], List[str], List[str]]:
    """
    返回 (主线星, 买点星, 兼容星级=买点星, 主线理由, 买点理由, chase_reasons)。
    """
    wave, wave_why = _score_wave_fit_stars(
        theme=theme,
        theme_ok=theme_ok,
        theme_grade=theme_grade,
        news_hits=news_hits,
        board_flow=board_flow,
        board_pct=board_pct,
        board_pct5=board_pct5,
        consecutive=consecutive,
    )
    buy, buy_why, chase_reasons = _score_buy_timing_stars(
        theme=theme,
        consecutive=consecutive,
        board_flow=board_flow,
        board_pct=board_pct,
        stock_pct=stock_pct,
        stock_pct_5d=stock_pct_5d,
        stock_flow=stock_flow,
        stock_flow_5d=stock_flow_5d,
        theme_ok=theme_ok,
        stock_grade=stock_grade,
        star_delta=star_delta,
        theme_grade=theme_grade,
        theme_star_delta=theme_star_delta,
        mild_flow_days=mild_flow_days,
        buy_setup=buy_setup,
    )
    # 兼容旧「星级」字段：以买点星为主（决定能不能买）
    return wave, buy, buy, wave_why, buy_why, chase_reasons


def _next_move_horizon_label() -> str:
    """盘中偏「下一时段/次日」，收盘后偏「次日」。"""
    hour = datetime.now().hour
    if hour >= 15:
        return "次日"
    if hour < 9:
        return "次日"
    return "下一时段/次日"


def _score_next_move_bias(
    *,
    news_hits: int,
    major_catalyst: bool,
    theme_ok: bool,
    theme_grade: str,
    board_flow: Optional[float],
    board_pct: Optional[float],
    board_pct5: Optional[float] = None,
    stock_pct: Optional[float],
    stock_pct_5d: Optional[float],
    stock_flow: Optional[float],
    stock_flow_5d: Optional[float],
) -> Tuple[float, str, List[str]]:
    """
    下一时段/次日涨跌偏向，区间 [-1, 1]（客观方向分，不指导买卖）。

    只看：新闻、板块热度、资金流向、价与资金/热度是否背离。
    不看：主线星/买点星/买点形态/追高否决——那些是另一套「能不能买」。

    原则：
    - 大涨本身不扣分；大涨+超大流入+板块仍热 → 仍可偏涨（趋势延续）
    - 大涨+资金流出/板块退潮 → 偏跌（出货/情绪退坡）
    - 板块走弱、资金持续负、个股自己也不认 → 偏续跌
    """
    score = 0.0
    reasons: List[str] = []
    horizon = _next_move_horizon_label()

    p = float(stock_pct) if stock_pct is not None else None
    p5 = float(stock_pct_5d) if stock_pct_5d is not None else None
    sf = float(stock_flow) if stock_flow is not None else None
    sf5 = float(stock_flow_5d) if stock_flow_5d is not None else None
    bf = float(board_flow) if board_flow is not None else None
    bp = float(board_pct) if board_pct is not None else None
    bp5 = float(board_pct5) if board_pct5 is not None else None
    tg = str(theme_grade or "")

    # —— 1) 新闻（催化是否还在）——
    nh = int(news_hits or 0)
    if nh > 0:
        add = 0.06 * min(3, nh)
        score += add
        reasons.append(f"新闻命中{nh}条 +{add:.2f}")
    if major_catalyst:
        score += 0.12
        reasons.append("重大催化仍在 +0.12")

    # —— 2) 板块热度（状态 + 今日强弱 + 5日热度）——
    if not theme_ok:
        score -= 0.08
        reasons.append("主题结构弱 -0.08")
    if tg == "走强":
        score += 0.14
        reasons.append("板块主题走强 +0.14")
    elif tg == "偏好":
        score += 0.05
        reasons.append("板块主题偏好 +0.05")
    elif tg == "偏弱":
        score -= 0.12
        reasons.append("板块主题偏弱 -0.12")
    elif tg == "走弱":
        score -= 0.22
        reasons.append("板块主题走弱 -0.22")

    if bp is not None:
        if bp >= 4.0:
            score += 0.12
            reasons.append(f"板块今日很热+{bp:.1f}% +0.12")
        elif bp >= 2.0:
            score += 0.07
            reasons.append(f"板块今日偏热+{bp:.1f}% +0.07")
        elif bp <= -3.0:
            score -= 0.14
            reasons.append(f"板块今日很冷{bp:.1f}% -0.14")
        elif bp <= -1.0:
            score -= 0.07
            reasons.append(f"板块今日偏冷{bp:.1f}% -0.07")

    if bp5 is not None:
        if bp5 >= 12.0:
            score += 0.08
            reasons.append(f"板块5日热(+{bp5:.1f}%) +0.08")
        elif bp5 >= 6.0:
            score += 0.04
            reasons.append(f"板块5日偏热(+{bp5:.1f}%) +0.04")
        elif bp5 <= -6.0:
            score -= 0.10
            reasons.append(f"板块5日冷({bp5:.1f}%) -0.10")
        elif bp5 <= -3.0:
            score -= 0.05
            reasons.append(f"板块5日偏冷({bp5:.1f}%) -0.05")

    # —— 3) 资金流向（板块 + 个股，核心权重）——
    if bf is not None:
        if bf >= 20:
            score += 0.18
            reasons.append(f"板块超大流入{bf:.1f}亿 +0.18")
        elif bf >= 8:
            score += 0.12
            reasons.append(f"板块大流入{bf:.1f}亿 +0.12")
        elif bf >= 2:
            score += 0.06
            reasons.append(f"板块流入{bf:.1f}亿 +0.06")
        elif bf <= -15:
            score -= 0.18
            reasons.append(f"板块超大流出{bf:.1f}亿 -0.18")
        elif bf <= -5:
            score -= 0.12
            reasons.append(f"板块大流出{bf:.1f}亿 -0.12")
        elif bf <= -1:
            score -= 0.06
            reasons.append(f"板块流出{bf:.1f}亿 -0.06")

    if sf is not None:
        if sf >= 3.0:
            score += 0.16
            reasons.append(f"个股超大流入{sf:.2f}亿 +0.16")
        elif sf >= 1.0:
            score += 0.10
            reasons.append(f"个股大流入{sf:.2f}亿 +0.10")
        elif sf >= 0.15:
            score += 0.05
            reasons.append(f"个股流入{sf:.2f}亿 +0.05")
        elif sf <= -2.0:
            score -= 0.18
            reasons.append(f"个股超大流出{sf:.2f}亿 -0.18")
        elif sf <= -0.5:
            score -= 0.12
            reasons.append(f"个股大流出{sf:.2f}亿 -0.12")
        elif sf <= -0.1:
            score -= 0.06
            reasons.append(f"个股流出{sf:.2f}亿 -0.06")

    if sf5 is not None:
        if sf5 >= 2.0:
            score += 0.06
            reasons.append("5日个股资金偏正 +0.06")
        elif sf5 <= -2.0:
            score -= 0.08
            reasons.append("5日个股资金偏负 -0.08")

    # —— 4) 价×资金×热度背离（客观，不是「涨了就抑」）——
    board_hot = (bf is not None and bf >= 5) or (bp is not None and bp >= 2.5) or tg == "走强"
    board_cold = (
        (bf is not None and bf <= -3)
        or (bp is not None and bp <= -1.5)
        or tg in ("偏弱", "走弱")
    )
    board_fading = False
    if bp5 is not None and bp5 >= 8.0 and bf is not None and bf < 0:
        board_fading = True
        score -= 0.12
        reasons.append("板块5日曾热但今日资金转负(退潮) -0.12")
    elif bp5 is not None and bp5 >= 8.0 and bp is not None and bp < 0 and (
        bf is None or bf <= 1
    ):
        board_fading = True
        score -= 0.08
        reasons.append("板块5日热、今日翻绿且资金不撑(退潮) -0.08")

    stock_in = sf is not None and sf > 0.15
    stock_out = sf is not None and sf < -0.1

    if p is not None:
        if p >= 4.0:
            # 大涨：看资金与板块是否还认，不默认打压
            if stock_out and (board_cold or board_fading or (bf is not None and bf < 0)):
                score -= 0.28
                reasons.append(
                    f"大涨{p:.1f}%但资金流出且板块退/冷 → 次日偏跌 -0.28"
                )
            elif stock_out:
                score -= 0.20
                reasons.append(f"大涨{p:.1f}%但个股资金流出(背离) -0.20")
            elif stock_in and board_hot and not board_fading:
                score += 0.14
                reasons.append(
                    f"大涨{p:.1f}%且流入+板块仍热 → 趋势可延续 +0.14"
                )
            elif stock_in and not board_cold:
                score += 0.06
                reasons.append(f"大涨{p:.1f}%且个股仍流入 +0.06")
            elif board_cold or board_fading:
                score -= 0.12
                reasons.append(f"大涨{p:.1f}%但板块已冷/退潮 -0.12")
            # 其余：大涨本身不加不减
        elif p <= -3.0:
            # 大跌：板块/资金不认则续跌；板块仍热且流入则反抽
            if board_cold and (stock_out or sf is None):
                score -= 0.16
                reasons.append(
                    f"大跌{p:.1f}%且板块冷/资金不认 → 偏续跌 -0.16"
                )
            elif stock_out and board_cold:
                score -= 0.20
                reasons.append(f"大跌{p:.1f}%+流出+板块冷 → 续跌 -0.20")
            elif stock_in and board_hot and not board_fading:
                score += 0.10
                reasons.append(
                    f"大跌{p:.1f}%但流入+板块仍热 → 偏反抽 +0.10"
                )
            elif board_hot and stock_in:
                score += 0.06
                reasons.append(f"大跌{p:.1f}%但板块热且流入 +0.06")
        elif -1.0 <= p <= 2.5:
            if stock_in and board_hot:
                score += 0.07
                reasons.append("横盘/微涨+流入+板块热 +0.07")
            elif stock_out and board_cold:
                score -= 0.08
                reasons.append("横盘/微动+流出+板块冷 -0.08")

    # 5日位置只作「高潮退潮」辅助：仅当资金/板块已退时加压，不因涨多本身扣分
    if p5 is not None and p5 >= 15.0:
        if stock_out or board_fading or (board_cold and (sf is None or sf <= 0)):
            score -= 0.10
            reasons.append(
                f"5日已+{p5:.1f}%且资金/板块退坡 → 消化压力 -0.10"
            )
        elif stock_in and board_hot and not board_fading:
            reasons.append(f"5日+{p5:.1f}%但资金板块仍认，不因涨多扣分")

    if p5 is not None and p5 <= -8.0 and board_cold and (
        stock_out or (sf is not None and sf <= 0)
    ):
        score -= 0.08
        reasons.append(f"5日{p5:.1f}%弱势+板块冷+资金不认 → 偏续跌 -0.08")

    score = max(-1.0, min(1.0, round(float(score), 2)))
    if score >= 0.15:
        tag = "偏涨"
    elif score <= -0.15:
        tag = "偏跌"
    else:
        tag = "中性"
    sign = f"+{score:.2f}" if score >= 0 else f"{score:.2f}"
    display = f"{sign} {tag}"
    reasons.insert(0, f"窗口={horizon}；客观方向={display}（非买卖建议）")
    return score, display, reasons


def _mild_up_flow_streak(
    history: Dict[str, Any],
    code: str,
    asof: str,
    today_pct: Optional[float],
    today_flow: Optional[float],
) -> int:
    """
    连续「微涨 + 资金净流入」天数（含今日）。
    微涨：0% ≤ 涨跌 < 3.5%；资金：主力净流入 > 0。
    """
    if today_pct is None or today_flow is None:
        return 0
    if not (0.0 <= float(today_pct) < 3.5 and float(today_flow) > 0):
        return 0
    streak = 1
    days = [d for d in sorted(history.get("days") or {}) if d < asof]
    for d in reversed(days):
        st = ((history["days"].get(d) or {}).get("status") or {}).get(code) or {}
        p = st.get("pct")
        f = st.get("flow")
        if p is None or f is None:
            break
        try:
            pv, fv = float(p), float(f)
        except (TypeError, ValueError):
            break
        if 0.0 <= pv < 3.5 and fv > 0:
            streak += 1
        else:
            break
    return streak


def _buy_signal_tier(
    *,
    theme_ok: bool,
    theme_grade: str,
    stock_grade: str,
    stars: int,
    consecutive: int,
    stock_pct: Optional[float],
    stock_pct_5d: Optional[float],
    stock_flow: Optional[float],
    stock_flow_5d: Optional[float] = None,
    vol_ratio: Optional[float] = None,
    turnover: Optional[float] = None,
    mild_flow_days: int = 0,
    chase_reasons: Optional[List[str]],
    buy_ready: bool,
    buy_action: str = "",
    buy_setup: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str, str]:
    """
    可买信号色 —— 按买卖形态分档（多形态，不只回踩）。

    红：真回踩 / 连涨流入 / 企稳缓涨 / 催化缓涨
    橙：涨未回踩、资金刚进等过渡态
    黄：待观察
    绿：走弱/追高/假回踩/勿追
    """
    chase_reasons = chase_reasons or []
    setup = buy_setup or {}
    action = str(buy_action or "")
    pct = stock_pct
    pct5 = stock_pct_5d
    flow = stock_flow
    flow5 = stock_flow_5d
    vr = vol_ratio
    kind = str(setup.get("kind") or "")

    # —— 绿：明确不宜买 ——
    if stock_grade == "走弱":
        return "绿", "不宜买", "个股走弱（观察可留，持有期止损）"
    if theme_grade == "走弱" and kind not in ("dig_watch", "stabilize_up"):
        return "绿", "不宜买", "主题走弱且无止跌/挖坑形态"
    if kind == "fake_pullback":
        return "绿", "假回踩", setup.get("why") or "下跌中继，不当回踩买"
    if kind == "reject_bar":
        return "绿", "冲高回落", setup.get("why") or "高开低走/冲高回落，不是真回踩"
    if chase_reasons:
        return "绿", "不宜买", "追高或假强（大涨资金背离等）"
    if action.startswith("观望勿追") or action.startswith("勿当回踩"):
        return "绿", "不宜买", "操作建议为勿追/假回踩"
    if pct is not None and float(pct) >= 5.0:
        return "绿", "不宜买", "当日涨幅过大，不是买点"
    # 5日偏热：仅「仍在冲」时标不宜买；回踩消化不因5日一刀切
    if pct5 is not None and float(pct5) >= 28.0 and (
        pct is not None and float(pct) >= 3.0
    ):
        return "绿", "不宜买", "近5日涨幅过大且仍在强冲"
    if theme_grade == "偏弱" and pct is not None and float(pct) >= 3.0:
        return "绿", "不宜买", "主题偏弱个股抢跑"

    # —— 红：完整买点形态 ——
    if kind == "true_pullback" and setup.get("buy_ok"):
        return "红", "真回踩", str(setup.get("why") or "走高后回踩")
    if kind == "mild_inflow_run" and setup.get("buy_ok"):
        return "红", "连涨流入", str(setup.get("why") or f"连续{mild_flow_days}日微涨流入")
    if kind == "stabilize_up" and setup.get("buy_ok"):
        return "红", "止跌起稳", str(setup.get("why") or "止跌起稳")
    if kind == "catalyst_grind" and setup.get("buy_ok"):
        return "红", "催化缓涨", str(setup.get("why") or "催化确认缓涨")
    if kind == "dig_watch":
        return "橙", "止跌观察", str(setup.get("why") or "连跌后收敛，等确认")

    # 兼容：未传入 setup 时的旧形态兜底
    pullback_shrink = (
        theme_ok
        and theme_grade in ("走强", "偏好")
        and pct5 is not None
        and 5.0 <= float(pct5) < 16.0
        and pct is not None
        and -2.0 <= float(pct) <= 1.2
        and stock_grade in ("偏好", "偏弱")
        and (vr is None or float(vr) <= 1.15)
        and (flow is None or float(flow) >= -0.25)
        and (flow5 is None or float(flow5) > 0)
        and (turnover is None or float(turnover) < 12.0)
    )
    mild_inflow_run = (
        theme_ok
        and mild_flow_days >= 2
        and pct is not None
        and 0.0 <= float(pct) < 3.5
        and flow is not None
        and float(flow) > 0
        and stock_grade in ("走强", "偏好")
    )
    if pullback_shrink:
        return "红", "真回踩", "走高后缩量回踩，像买点（仍看建议区间）"
    if mild_inflow_run:
        return "红", "连涨流入", f"连续{mild_flow_days}日微涨且资金流入"

    risen_no_pullback = (
        theme_ok
        and stock_grade in ("走强", "偏好")
        and (
            (pct is not None and 2.0 <= float(pct) < 5.0)
            or (
                pct5 is not None
                and 8.0 <= float(pct5) < 18.0
                and pct is not None
                and float(pct) >= 0.8
            )
        )
    )
    flow_just_in = (
        theme_ok
        and flow is not None
        and float(flow) > 0.25
        and (pct is None or float(pct) < 4.0)
        and stock_grade in ("走强", "偏好", "偏弱")
        and mild_flow_days < 2
        and not pullback_shrink
    )
    if (
        not flow_just_in
        and theme_ok
        and flow5 is not None
        and float(flow5) > 1.0
        and flow is not None
        and float(flow) > 0
        and (pct is None or float(pct) < 3.5)
        and mild_flow_days < 2
    ):
        flow_just_in = True

    if risen_no_pullback:
        return "橙", "涨未确认", "已走强但买点未完成（可等缓涨/回踩/催化）"
    if flow_just_in:
        return "橙", "资金刚进", "资金流入看好，买点未完全成形"

    if stock_grade != "走弱" and theme_grade != "走弱":
        return "黄", "待观察", "可买可不买，先放池子"

    return "绿", "不宜买", "综合形态偏弱"


def _theme_signal_tier(
    *,
    theme_grade: str,
    theme_stars: int,
    board_pct: Optional[float],
    board_pct5: Optional[float],
    board_flow: Optional[float],
) -> Tuple[str, str]:
    """主题行信号色：跟板块强弱/资金，不按主题星级硬套。"""
    if theme_grade == "走弱" or (board_pct is not None and board_pct <= -3.0):
        return "绿", "主题走弱"
    if theme_grade == "偏弱":
        return "黄", "主题偏弱观察"
    if theme_grade == "走强" and (
        board_flow is None or board_flow > 0
    ) and (board_pct is None or board_pct < 4.5):
        return "橙", "主题走强盯回踩股"
    if theme_grade == "走强" and board_pct is not None and board_pct >= 4.5:
        return "黄", "主题偏热先等等"
    if theme_grade == "偏好" and (board_flow is None or board_flow > 0):
        return "橙", "主题偏好可跟踪"
    return "黄", "主题一般"


# 今日短线：新闻热点板常用种子（主线池会压软主题，短线池单独放行）
_DAILY_SHORT_BOARD_SEEDS: Dict[str, List[Tuple[str, str]]] = {
    "黄金": [
        ("601069", "西部黄金"),
        ("600489", "中金黄金"),
        ("600547", "山东黄金"),
        ("000975", "山金国际"),
        ("002155", "湖南黄金"),
    ],
    "贵金属": [
        ("601069", "西部黄金"),
        ("600489", "中金黄金"),
        ("600988", "赤峰黄金"),
    ],
    "白酒": [
        ("600519", "贵州茅台"),
        ("000858", "五粮液"),
        ("000568", "泸州老窖"),
    ],
    "证券": [
        ("601211", "国泰君安"),
        ("000776", "广发证券"),
    ],
    "创新药": [
        ("600276", "恒瑞医药"),
        ("688235", "百济神州"),
        ("603259", "药明康德"),
        ("002821", "凯莱英"),
    ],
    "化学制药": [
        ("600276", "恒瑞医药"),
        ("000963", "华东医药"),
    ],
    "银行": [
        ("601166", "兴业银行"),
        ("600036", "招商银行"),
        ("601398", "工商银行"),
    ],
    "电网": [
        ("600089", "特变电工"),
        ("601179", "中国西电"),
        ("600312", "平高电气"),
    ],
    "电力": [
        ("600089", "特变电工"),
        ("600900", "长江电力"),
    ],
    "核电": [
        ("601985", "中国核电"),
        ("003816", "中国广核"),
    ],
    "商业航天": [
        ("600879", "航天电子"),
        ("600118", "中国卫星"),
    ],
    "卫星": [
        ("600879", "航天电子"),
        ("600118", "中国卫星"),
    ],
    "农业": [
        ("600598", "北大荒"),
        ("000998", "隆平高科"),
    ],
    "种业": [
        ("000998", "隆平高科"),
        ("002041", "登海种业"),
    ],
    "汽车": [
        ("002594", "比亚迪"),
        ("600418", "江淮汽车"),
        ("601127", "赛力斯"),
    ],
    "乘用车": [
        ("002594", "比亚迪"),
        ("000625", "长安汽车"),
    ],
    "软件开发": [
        ("002410", "广联达"),
        ("600570", "恒生电子"),
        ("688111", "金山办公"),
        ("300085", "银之杰"),
    ],
    "人工智能": [
        ("002230", "科大讯飞"),
        ("300033", "同花顺"),
        ("300085", "银之杰"),
        ("300418", "昆仑万维"),
    ],
    "液冷": [
        ("002837", "英维克"),
        ("301018", "申菱环境"),
        ("000811", "冰轮环境"),
        ("300442", "润泽科技"),
    ],
    "液冷服务器": [
        ("002837", "英维克"),
        ("301018", "申菱环境"),
        ("000811", "冰轮环境"),
        ("300442", "润泽科技"),
    ],
    "PCB": [
        ("002463", "沪电股份"),
        ("600183", "生益科技"),
        ("001232", "嘉立创"),
        ("002815", "崇达技术"),
    ],
    "印制电路板": [
        ("002463", "沪电股份"),
        ("600183", "生益科技"),
        ("001232", "嘉立创"),
    ],
    "覆铜板": [
        ("600183", "生益科技"),
        ("002463", "沪电股份"),
    ],
    "传媒": [
        ("300418", "昆仑万维"),
        ("300413", "芒果超媒"),
        ("300133", "华策影视"),
        ("002517", "恺英网络"),
    ],
    "数字媒体": [
        ("300418", "昆仑万维"),
        ("300413", "芒果超媒"),
        ("300017", "网宿科技"),
    ],
    "短剧": [
        ("300418", "昆仑万维"),
        ("300413", "芒果超媒"),
        ("300133", "华策影视"),
        ("002517", "恺英网络"),
    ],
    "算力租赁": [
        ("300857", "协创数据"),
        ("300442", "润泽科技"),
        ("603881", "数据港"),
    ],
    "智算": [
        ("300857", "协创数据"),
        ("300442", "润泽科技"),
        ("000977", "浪潮信息"),
    ],
    "数据中心": [
        ("300442", "润泽科技"),
        ("300857", "协创数据"),
        ("603881", "数据港"),
        ("300383", "光环新网"),
    ],
    "光伏": [
        ("601012", "隆基绿能"),
        ("600438", "通威股份"),
        ("688472", "阿特斯"),
    ],
    "小金属": [
        ("002428", "云南锗业"),
        ("600549", "厦门钨业"),
        ("000657", "中钨高新"),
    ],
    "军工": [
        ("600760", "中航沈飞"),
        ("600893", "航发动力"),
        ("002179", "中航光电"),
    ],
    "国防军工": [
        ("600760", "中航沈飞"),
        ("600893", "航发动力"),
        ("600150", "中国船舶"),
    ],
}

# 新闻词 → 短线种子（不依赖东财板块名是否刚好叫「麒麟电池」）
_DAILY_SHORT_NEWS_SEEDS: List[Tuple[Tuple[str, ...], str, List[Tuple[str, str]]]] = [
    (
        ("黄金", "金价", "贵金属", "金饰"),
        "黄金",
        [
            ("601069", "西部黄金"),
            ("600489", "中金黄金"),
            ("600547", "山东黄金"),
            ("000975", "山金国际"),
        ],
    ),
    (
        ("电池", "麒麟电池", "储能", "固态电池", "动力电池"),
        "电池",
        [
            ("002126", "银轮股份"),
            ("300750", "宁德时代"),
            ("002074", "国轩高科"),
            ("300014", "亿纬锂能"),
        ],
    ),
    (
        ("光模块", "CPO", "光通信", "光纤"),
        "光通信",
        [
            ("300502", "新易盛"),
            ("300394", "天孚通信"),
            ("002281", "光迅科技"),
            ("300570", "太辰光"),
            ("688048", "长光华芯"),
            ("601869", "长飞光纤"),
            ("600487", "亨通光电"),
        ],
    ),
    (
        ("国产服务器", "AI服务器", "信创服务器", "浪潮", "紫光股份", "锐捷"),
        "国产服务器",
        [
            ("000938", "紫光股份"),
            ("000977", "浪潮信息"),
            ("301165", "锐捷网络"),
            ("603019", "中科曙光"),
        ],
    ),
    (
        ("液冷", "液冷服务器", "浸没式", "英维克", "冷板"),
        "液冷服务器",
        [
            ("002837", "英维克"),
            ("301018", "申菱环境"),
            ("000811", "冰轮环境"),
            ("300442", "润泽科技"),
        ],
    ),
    (
        ("算力租赁", "智算", "智算中心", "GPU租赁", "token算力", "协创"),
        "算力租赁",
        [
            ("300857", "协创数据"),
            ("300442", "润泽科技"),
            ("603881", "数据港"),
            ("300383", "光环新网"),
        ],
    ),
    (
        ("工业富联", "海外组装", "服务器代工", "ODM"),
        "海外组装",
        [
            ("601138", "工业富联"),
        ],
    ),
    (
        ("华为", "鸿蒙", "昇腾"),
        "华为",
        [
            ("301236", "软通动力"),
            ("002230", "科大讯飞"),
            ("000938", "紫光股份"),
        ],
    ),
    (
        ("半导体设备", "前道设备", "刻蚀", "清洗设备", "国产设备"),
        "半导体设备",
        [
            ("688082", "盛美上海"),
            ("603061", "金海通"),
            ("002371", "北方华创"),
            ("688012", "中微公司"),
            ("688120", "华海清科"),
            ("688072", "拓荆科技"),
            ("688037", "芯源微"),
            ("300604", "长川科技"),
        ],
    ),
    (
        ("半导体", "芯片", "存储", "晶圆"),
        "半导体",
        [
            ("688525", "佰维存储"),
            ("603986", "兆易创新"),
            ("688008", "澜起科技"),
            ("300223", "北京君正"),
            ("002049", "紫光国微"),
        ],
    ),
    (
        ("创新药", "生物医药", "医保", "CXO", "新药", "ADC", "GLP-1"),
        "创新药",
        [
            ("600276", "恒瑞医药"),
            ("688235", "百济神州"),
            ("603259", "药明康德"),
            ("002821", "凯莱英"),
            ("300759", "康龙化成"),
        ],
    ),
    (
        ("白酒", "茅台", "五粮液", "批价", "动销"),
        "白酒",
        [
            ("600519", "贵州茅台"),
            ("000858", "五粮液"),
            ("000568", "泸州老窖"),
            ("600809", "山西汾酒"),
        ],
    ),
    (
        ("银行", "净息差", "社融", "信贷"),
        "银行",
        [
            ("600036", "招商银行"),
            ("601166", "兴业银行"),
            ("601398", "工商银行"),
        ],
    ),
    (
        ("电网", "特高压", "变压器", "算电"),
        "电网",
        [
            ("600089", "特变电工"),
            ("601179", "中国西电"),
            ("600312", "平高电气"),
        ],
    ),
]


def _news_text_blob(news: pd.DataFrame) -> str:
    if news is None or news.empty:
        return ""
    title_col = "title" if "title" in news.columns else (
        "标题" if "标题" in news.columns else None
    )
    if not title_col:
        return ""
    return " ".join(str(x) for x in news[title_col].astype(str).tolist()[:60])


def _news_hit_count_for_text(text: str, news: pd.DataFrame) -> Tuple[int, List[str]]:
    """标题命中关键词条数 + 样例。"""
    keys = [str(text or "").strip()]
    short = (
        str(text or "")
        .replace("概念", "")
        .replace("Ⅱ", "")
        .replace("Ⅰ", "")
        .replace("行业", "")
        .strip()
    )
    if short and short not in keys:
        keys.append(short)
    if len(short) >= 2:
        keys.append(short[:2])
    hits: List[str] = []
    if news is None or news.empty:
        return 0, hits
    title_col = "title" if "title" in news.columns else (
        "标题" if "标题" in news.columns else None
    )
    if not title_col:
        return 0, hits
    for _, r in news.iterrows():
        title = str(r.get(title_col) or "")
        if any(k and k in title for k in keys):
            hits.append(title[:72])
        if len(hits) >= 5:
            break
    return len(hits), hits


def _parse_buy_band(rng: str) -> Tuple[Optional[float], Optional[float]]:
    s = str(rng or "").strip()
    if "~" not in s:
        return None, None
    try:
        a, b = s.split("~", 1)
        return float(a), float(b)
    except (TypeError, ValueError):
        return None, None


# 压舱长持票：不进短线周转池（与特变这类压舱仓位分开）
_SHORT_BALLAST_EXCLUDE = {"600089"}
# 股价过高一手成本太大，短线/观察池先滤掉（如源杰级）
_MAX_TRADE_PRICE = 800.0


def _detect_t1_short_setup(
    *,
    news_hits: int,
    board_pct: Optional[float],
    pct: Optional[float],
    pct5: Optional[float],
    flow: Optional[float],
    flow5: Optional[float],
    vol_ratio: Optional[float],
    open_: Optional[float] = None,
    high: Optional[float] = None,
    low: Optional[float] = None,
    close: Optional[float] = None,
    prev_close: Optional[float] = None,
    bar_struct: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    短线周转买点（持有预期 1～3 天）：
    跟正在走的热板——催化轻涨 / 热板连涨 / 回调续涨 / 轻踩承接。
    须看当日开高低收：高开低走/冲高回落即便收盘微涨也不当轻涨买点。
    """
    base = {
        "kind": "none",
        "buy_ok": False,
        "label": "无短线买点",
        "why": "",
        "action": "",
        "kline": "",
    }
    ks = bar_struct if isinstance(bar_struct, dict) else None
    if not ks or not ks.get("ok"):
        ks = _day_kline_structure(
            open_=open_,
            high=high,
            low=low,
            close=close,
            prev_close=prev_close,
            vol_ratio=vol_ratio,
        )
    base["kline"] = str(ks.get("why") or "")

    board_hot = board_pct is not None and float(board_pct) >= 0.8
    if news_hits <= 0 and not board_hot:
        return {**base, "why": "无新闻且板块不热，不做短线跟进"}
    if pct is None or flow is None:
        return {**base, "why": "缺行情/资金"}
    if float(pct) >= 9.5:
        return {**base, "why": "接近涨停，短线不追尖峰"}
    if float(pct) <= -5.0:
        return {**base, "why": "当日大跌过深，先等止跌"}
    if float(flow) < 0:
        return {**base, "why": "当日资金净流出，短线承接不足"}

    board_ok = board_pct is None or float(board_pct) >= -1.2
    if not board_ok:
        return {**base, "why": "板块当日偏弱"}

    # 高开低走/冲高回落：先否决，不看收盘涨跌幅微红
    if ks.get("ok") and ks.get("bearish_reject"):
        return {
            **base,
            "kind": "reject_bar",
            "label": "冲高回落",
            "why": f"今日K线{ks.get('why')}，高开低走/冲高回落，不做热板轻涨",
            "action": "勿追：等缩量收敛或真回踩",
        }

    hold = "短持1～3天见好就收，勿改成长线等回踩"
    pct5_follow_ok = pct5 is None or float(pct5) < 22.0
    pct5_pull_ok = pct5 is not None and 2.0 <= float(pct5) < 35.0
    k_hold = (not ks.get("ok")) or bool(ks.get("bullish_hold"))
    k_yang_ok = (not ks.get("ok")) or bool(ks.get("is_yang")) or bool(ks.get("bullish_hold"))

    # A) 催化/跟风轻涨：须K线偏多（阳/缩量持稳），禁止阴线微涨冒充
    if (
        k_hold
        and k_yang_ok
        and 0.3 <= float(pct) <= 3.5
        and float(flow) > 0
    ):
        if flow5 is None or float(flow5) >= -1.5:
            if vol_ratio is None or float(vol_ratio) < 2.8:
                if pct5_follow_ok or (pct5 is not None and float(pct) <= 2.2):
                    lab = "催化轻涨" if news_hits > 0 else "热板轻涨"
                    return {
                        "kind": "t1_catalyst_up",
                        "buy_ok": True,
                        "label": lab,
                        "why": (
                            f"今日{float(pct):+.1f}%且流入{float(flow):.1f}亿，"
                            f"{'新闻+' if news_hits > 0 else ''}短线可执行区"
                            + (f"；K线{ks.get('why')}" if ks.get("ok") else "")
                        ),
                        "action": f"可小仓：现价附近分批，冲高减、跌破今日低点走；{hold}",
                        "kline": str(ks.get("why") or ""),
                    }

    # B) 热板连涨跟一段
    if (
        k_hold
        and k_yang_ok
        and board_hot
        and 0.5 <= float(pct) <= 3.8
        and float(flow) > 0
        and pct5_follow_ok
        and pct5 is not None
        and 2.0 <= float(pct5)
        and (vol_ratio is None or float(vol_ratio) < 3.0)
    ):
        return {
            "kind": "t1_hot_continue",
            "buy_ok": True,
            "label": "热板连涨",
            "why": (
                f"板块热(+{float(board_pct):.1f}%)，个股5日{float(pct5):+.1f}%/"
                f"今{float(pct):+.1f}%仍流入，跟一段不恋战"
                + (f"；K线{ks.get('why')}" if ks.get("ok") else "")
            ),
            "action": f"可小仓跟温：不追尖峰；连涨末段减仓；{hold}",
            "kline": str(ks.get("why") or ""),
        }

    # C) 回调续涨：允许缩量阳续上；拒绝高开阴
    if (
        k_hold
        and pct5_pull_ok
        and -0.3 <= float(pct) <= 2.8
        and float(flow) > 0
        and (flow5 is None or float(flow5) > 0)
        and (vol_ratio is None or float(vol_ratio) <= 2.2)
        and (not ks.get("ok") or not ks.get("is_yin") or ks.get("shrink_vol"))
    ):
        return {
            "kind": "t1_resume",
            "buy_ok": True,
            "label": "回调续涨",
            "why": (
                f"5日先有趋势({float(pct5):+.1f}%)，今日{float(pct):+.1f}%续上且流入，"
                "短线再做一两天"
                + (f"；K线{ks.get('why')}" if ks.get("ok") else "")
            ),
            "action": f"可小仓：回调后续涨试错，放量跌破不恋战；{hold}",
            "kline": str(ks.get("why") or ""),
        }

    # D) 轻踩承接（缩量阴/收敛更像真回踩）
    pull_k_ok = (not ks.get("ok")) or (
        (ks.get("is_yin") or ks.get("shrink_vol") or "缩量阴承接" in (ks.get("labels") or []))
        and not ks.get("gap_up")
    )
    if (
        pull_k_ok
        and pct5_pull_ok
        and -3.2 <= float(pct) <= 0.6
        and float(flow) > 0
        and (vol_ratio is None or float(vol_ratio) <= 1.35)
        and (flow5 is None or float(flow5) > 0)
    ):
        return {
            "kind": "t1_dip_hold",
            "buy_ok": True,
            "label": "轻踩承接",
            "why": (
                f"5日先有趋势({float(pct5):+.1f}%)，今日轻踩{float(pct):+.1f}%仍流入，"
                "缩量回踩可试"
                + (f"；K线{ks.get('why')}" if ks.get("ok") else "")
            ),
            "action": f"可小仓：现价附近试，放量跌破不恋战；{hold}",
            "kline": str(ks.get("why") or ""),
        }

    if ks.get("ok") and not ks.get("bullish_hold"):
        return {
            **base,
            "why": f"今日K线{ks.get('why')}，结构不支持短线轻涨/回踩",
        }
    return base


def _resolve_buy_method(
    setup: Optional[Dict[str, Any]],
    *,
    pct: Optional[float] = None,
    news_hits: int = 0,
) -> str:
    """
    将买点形态映射为可执行「买入方法」（多条件任一即可，禁止只推深坑最低点）。

    A热板浅回 / B主线微涨横盘 / C热板连涨 / D催化缓涨 / E止跌再起
    """
    setup = setup or {}
    kind = str(setup.get("kind") or "")
    if not setup.get("buy_ok") or kind in ("", "none", "fake_pullback"):
        return ""

    pct_v = None if pct is None else float(pct)
    mild_sideways = pct_v is not None and -0.5 <= pct_v <= 2.0

    if kind in ("true_pullback", "t1_dip_hold"):
        return "A热板浅回"
    if kind == "t1_resume":
        return "A热板浅回·回调续涨"
    if kind == "stabilize_up":
        return "E止跌再起"
    if kind in ("catalyst_grind",):
        return "D催化缓涨"
    if kind == "t1_catalyst_up":
        if int(news_hits) > 0:
            return "D催化缓涨"
        if mild_sideways:
            return "B主线微涨横盘"
        return "C热板连涨"
    if kind in ("t1_hot_continue", "mild_inflow_run"):
        if mild_sideways and int(news_hits) <= 0:
            return "B主线微涨横盘"
        return "C热板连涨"

    # 兜底：用已有 label，避免空白
    lab = str(setup.get("label") or "").strip()
    return lab or "C热板连涨"


def _near_price_buy_band(px: float) -> str:
    """短线可成交挂单：贴现价约 −1.2%～+1.2%，禁止深坑假挂。"""
    lo, hi = float(px) * 0.988, float(px) * 1.012
    return f"{lo:.2f}~{hi:.2f}"


def _clamp(x: float, lo: float = -100.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, float(x)))


def _soft_penalty(x: float, start: float, full: float, max_pen: float) -> float:
    """超过 start 开始扣，到 full 扣满 max_pen（连续光滑，非一刀切）。"""
    if x is None or x <= start:
        return 0.0
    if x >= full:
        return -abs(max_pen)
    t = (float(x) - start) / max(full - start, 1e-9)
    return -abs(max_pen) * t


def _news_risk_delta(news_hits: Optional[List[str]]) -> Tuple[float, List[str]]:
    """
    新闻对买入风险分的轻量修正。
    主题级新闻已体现在主题星/入池，个股风险值以价量/CFA为主；
    此处仅接受「点名该股」的新闻，且总修正封顶，避免利好叠到 60+。
    """
    why: List[str] = []
    if not news_hits:
        return 0.0, why
    bull_major = bull_norm = bear_major = bear_norm = 0
    bear_kw = (
        "澄清",
        "否认",
        "减持",
        "亏损",
        "下滑",
        "立案",
        "处罚",
        "问询",
        "违约",
        "爆雷",
        "造假",
        "退市",
        "风险警示",
        "业绩预减",
        "预亏",
    )
    for t in news_hits[:6]:
        title = str(t or "")
        if _is_weak_forward_news(title):
            continue
        bearish = any(k in title for k in bear_kw)
        major = news_title_is_major_catalyst(title)
        if bearish:
            if major or any(k in title for k in ("立案", "造假", "退市", "爆雷")):
                bear_major += 1
            else:
                bear_norm += 1
        else:
            if major:
                bull_major += 1
            else:
                bull_norm += 1
    delta = 0.0
    if bull_major:
        add = min(8.0, 5.0 + 3.0 * (bull_major - 1))
        delta += add
        why.append(f"个股利好×{bull_major} +{add:.0f}")
    if bull_norm:
        add = min(4.0, 2.0 * bull_norm)
        delta += add
        why.append(f"一般利好×{bull_norm} +{add:.0f}")
    if bear_norm:
        sub = min(8.0, 3.0 * bear_norm)
        delta -= sub
        why.append(f"一般利空×{bear_norm} -{sub:.0f}")
    if bear_major:
        sub = min(18.0, 10.0 + 4.0 * (bear_major - 1))
        delta -= sub
        why.append(f"重大利空×{bear_major} -{sub:.0f}")
    # 硬封顶：风险值不被新闻主导
    if delta > 8.0:
        delta = 8.0
    if delta < -18.0:
        delta = -18.0
    return delta, why


def _news_hits_for_stock(
    code: str,
    name: str,
    news_hits: Optional[List[str]],
) -> List[str]:
    """只保留标题里点名该股代码/简称的新闻，供个股风险分使用。"""
    code6 = str(code or "").zfill(6)[-6:]
    nm = str(name or "").strip()
    # 常见简称：药明康德→药明；光库科技→光库
    aliases = {nm, code6}
    if nm.endswith("股份") and len(nm) > 2:
        aliases.add(nm[:-2])
    if nm.endswith("科技") and len(nm) > 2:
        aliases.add(nm[:-2])
    if nm.endswith("有限公司"):
        aliases.add(nm.replace("有限公司", ""))
    short = nm
    for suf in ("股份", "科技", "集团", "控股", "有限公司", "股份有限公司"):
        if short.endswith(suf) and len(short) > len(suf) + 1:
            short = short[: -len(suf)]
            aliases.add(short)
            break
    out: List[str] = []
    for t in news_hits or []:
        s = str(t or "")
        if any(a and len(a) >= 2 and a in s for a in aliases):
            out.append(s)
    return out


def _clear_risk_bars_cache() -> None:
    _RISK_BARS_CACHE.clear()


def _prefetch_risk_bars(
    codes: List[str],
    asof: str,
    *,
    limit: int = 28,
    max_workers: int = _RISK_PREFETCH_WORKERS,
) -> int:
    """并行预拉日K，避免评分阶段逐股串行等网络（刷新慢的主因）。"""
    end = _asof_yyyymmdd(asof)
    if len(end) != 8:
        return 0
    todo: List[str] = []
    seen: set = set()
    for c in codes:
        code = str(c or "").zfill(6)[-6:]
        if len(code) != 6 or not code.isdigit() or code in seen:
            continue
        key = f"{code}:{end}:{int(limit)}"
        if key in _RISK_BARS_CACHE:
            continue
        seen.add(code)
        todo.append(code)
    if not todo:
        return 0

    workers = min(max_workers, max(4, (len(todo) + 7) // 8))

    def _one(code: str) -> None:
        _get_risk_bars(code, asof, limit=limit, fast_fetch=True)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(_one, todo, chunksize=max(1, len(todo) // workers)))
    return len(todo)


def _asof_yyyymmdd(asof: str) -> str:
    return str(asof or "").replace("-", "")[:8]


def _get_risk_bars(
    code: str, asof: str, limit: int = 28, *, fast_fetch: bool = False
) -> List[Dict[str, Any]]:
    """取截止 asof 的日K（含开收），供阳线连阳与 CFA 波动/回撤。"""
    code = str(code or "").zfill(6)
    end = _asof_yyyymmdd(asof)
    if len(code) != 6 or len(end) != 8:
        return []
    key = f"{code}:{end}:{int(limit)}"
    if key in _RISK_BARS_CACHE:
        return _RISK_BARS_CACHE[key]
    try:
        if fast_fetch:
            bars = _fetch_kline_bars_fast(code, end, limit=int(limit))
        else:
            bars = _fetch_kline_bars(code, end, limit=int(limit))
    except Exception:
        bars = []
    # 只要 <= asof
    out = [b for b in (bars or []) if str(b.get("date") or "")[:8] <= end]
    _RISK_BARS_CACHE[key] = out
    return out


def _is_user_yang(bar: Dict[str, Any], prev: Optional[Dict[str, Any]]) -> bool:
    """
    用户阳线：收盘>开盘，且今日开盘>昨日开盘。
    （不是「收盘>昨收」；收盘略跌但仍收阳且开盘抬高，仍算连阳。）
    """
    if not bar or not prev:
        return False
    try:
        o, c = float(bar["open"]), float(bar["close"])
        po = float(prev["open"])
    except (TypeError, ValueError, KeyError):
        return False
    return c > o and o > po


def _is_user_yin(bar: Dict[str, Any], prev: Optional[Dict[str, Any]]) -> bool:
    """对称阴线：收盘<开盘，且今日开盘<昨日开盘。"""
    if not bar or not prev:
        return False
    try:
        o, c = float(bar["open"]), float(bar["close"])
        po = float(prev["open"])
    except (TypeError, ValueError, KeyError):
        return False
    return c < o and o < po


def _yang_yin_streak_from_bars(bars: List[Dict[str, Any]]) -> Tuple[int, int]:
    """从序列末端往前数用户定义连阳/连阴天数（含今日）。"""
    if not bars or len(bars) < 2:
        return 0, 0
    yang = yin = 0
    last = bars[-1]
    prev = bars[-2]
    if _is_user_yang(last, prev):
        yang = 1
        for i in range(len(bars) - 2, 0, -1):
            if _is_user_yang(bars[i], bars[i - 1]):
                yang += 1
            else:
                break
    elif _is_user_yin(last, prev):
        yin = 1
        for i in range(len(bars) - 2, 0, -1):
            if _is_user_yin(bars[i], bars[i - 1]):
                yin += 1
            else:
                break
    return yang, yin


def _prior_yang_streak(bars: List[Dict[str, Any]]) -> int:
    """不含今日的连阳天数（用于「连阳后首日消化」）。"""
    if not bars or len(bars) < 3:
        return 0
    return _yang_yin_streak_from_bars(bars[:-1])[0]


def _cfa_metrics_from_bars(bars: List[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    """
    CFA 口径常用风险度量（日频近似，短窗）：
    - σ：收益标准差（历史波动）
    - downside_dev：目标半偏差（B=0，只惩罚跌）
    - max_dd：窗口最大回撤
    - var95：参数法 1 日 95% VaR 幅度 ≈ 1.65σ − μ（损失为正）
    - pct_from_high：相对窗口最高收盘的回撤幅度（负=低于高点）
    """
    empty = {
        "hist_vol": None,
        "downside_dev": None,
        "max_dd": None,
        "var95": None,
        "pct_from_high": None,
        "mean_ret": None,
    }
    if not bars or len(bars) < 6:
        return empty
    # 波动用更长窗；回撤/距高点用近 12 日，避免把更早暴跌旧伤反复重罚
    rets: List[float] = []
    closes_all: List[float] = []
    for b in bars:
        try:
            rets.append(float(b.get("pct") or 0.0))
            closes_all.append(float(b["close"]))
        except (TypeError, ValueError, KeyError):
            continue
    if len(rets) < 6 or len(closes_all) < 6:
        return empty
    n = len(rets)
    mean_r = sum(rets) / n
    var = sum((r - mean_r) ** 2 for r in rets) / max(n - 1, 1)
    sigma = var ** 0.5
    below = [r for r in rets if r < 0.0]
    if below:
        dvar = sum((r - 0.0) ** 2 for r in below) / max(n - 1, 1)
        ddev = dvar ** 0.5
    else:
        ddev = 0.0
    closes = closes_all[-12:] if len(closes_all) >= 12 else closes_all
    peak = closes[0]
    max_dd = 0.0
    for c in closes:
        if c > peak:
            peak = c
        dd = c / peak - 1.0
        if dd < max_dd:
            max_dd = dd
    var95 = max(0.0, 1.65 * sigma - mean_r)
    hi = max(closes)
    pct_from_high = closes[-1] / hi - 1.0 if hi > 0 else 0.0
    return {
        "hist_vol": round(sigma, 4),
        "downside_dev": round(ddev, 4),
        "max_dd": round(max_dd * 100.0, 3),
        "var95": round(var95, 4),
        "pct_from_high": round(pct_from_high * 100.0, 3),
        "mean_ret": round(mean_r, 4),
    }


def _vol_ratio_from_bars(bars: List[Dict[str, Any]]) -> Optional[float]:
    """无行情量比时，用近5日均量近似：需 bars 带 volume；否则 None。"""
    if not bars or "volume" not in (bars[-1] or {}):
        return None
    try:
        vols = [float(b["volume"]) for b in bars if b.get("volume") is not None]
    except (TypeError, ValueError):
        return None
    if len(vols) < 6:
        return None
    today = vols[-1]
    base = sum(vols[-6:-1]) / 5.0
    if base <= 0:
        return None
    return today / base


def _build_risk_context(
    code: str,
    asof: str,
    *,
    history: Optional[Dict[str, Any]] = None,
    today_pct: Optional[float] = None,
) -> Dict[str, Any]:
    """组装风险分所需：阳/阴连线 + CFA 度量。K 线失败则回退收盘涨跌连涨。"""
    bars = _get_risk_bars(code, asof, limit=28)
    yang_s, yin_s = _yang_yin_streak_from_bars(bars)
    prior_yang = _prior_yang_streak(bars)
    cfa = _cfa_metrics_from_bars(bars)
    # 回退：无开盘价时仍用收盘涨跌（旧逻辑）
    if (not bars or len(bars) < 2) and history is not None:
        yang_s, yin_s = _up_down_streak_from_history_pct(
            history, code, asof, today_pct
        )
        prior_yang = 0
    return {
        "yang_streak": yang_s,
        "yin_streak": yin_s,
        "prior_yang_streak": prior_yang,
        **cfa,
        "bars_n": len(bars),
    }


def _up_down_streak_from_history_pct(
    history: Dict[str, Any],
    code: str,
    asof: str,
    today_pct: Optional[float],
) -> Tuple[int, int]:
    """兜底：仅用收盘涨跌幅估算连涨/连跌（无K线开盘时）。"""
    up = down = 0
    if today_pct is not None:
        if float(today_pct) > 0.15:
            up = 1
        elif float(today_pct) < -0.15:
            down = 1
    days = [d for d in sorted(history.get("days") or {}) if d < asof]
    for d in reversed(days):
        st = ((history["days"].get(d) or {}).get("status") or {}).get(code) or {}
        p = st.get("pct")
        if p is None:
            break
        try:
            pv = float(p)
        except (TypeError, ValueError):
            break
        if up > 0:
            if pv > 0.15:
                up += 1
            else:
                break
        elif down > 0:
            if pv < -0.15:
                down += 1
            else:
                break
        else:
            break
    return up, down


# 兼容旧名
def _up_down_streak_from_history(
    history: Dict[str, Any],
    code: str,
    asof: str,
    today_pct: Optional[float],
) -> Tuple[int, int]:
    return _up_down_streak_from_history_pct(history, code, asof, today_pct)


def _score_buy_risk(
    *,
    pct: Optional[float],
    pct5: Optional[float],
    flow: Optional[float] = None,
    flow5: Optional[float] = None,
    vol_ratio: Optional[float] = None,
    mild_up_days: int = 0,
    up_streak: int = 0,
    down_streak: int = 0,
    yang_streak: Optional[int] = None,
    yin_streak: Optional[int] = None,
    prior_yang_streak: int = 0,
    hist_vol: Optional[float] = None,
    downside_dev: Optional[float] = None,
    max_dd: Optional[float] = None,
    var95: Optional[float] = None,
    pct_from_high: Optional[float] = None,
    news_hits: Optional[List[str]] = None,
    theme_grade: str = "偏好",
) -> Dict[str, Any]:
    """
    短线买入风险适配分 ∈ [-100, 100]。

    专业层（CFA 常用度量，日频短窗近似）：
    - 历史波动 σ、下行偏差（target=0）、最大回撤、参数法 95% VaR
    - 相对近期高点位置（类 drawdown from peak）

    结构层（A股短线实务）：
    - 连阳/连阴用用户定义：C>O 且 O>昨O（阳）；对称为阴——不是收盘>昨收
    - 缩量回踩必须有量比证据；连阳后首跌加分小于第二日消化
    - 资金/主题/新闻修正

    越负越不宜买；连续加减，非单规则一刀切。
    """
    parts: List[str] = []
    score = 8.0

    pct_v = None if pct is None else float(pct)
    pct5_v = None if pct5 is None else float(pct5)
    flow_v = None if flow is None else float(flow)
    flow5_v = None if flow5 is None else float(flow5)
    vr_v = None if vol_ratio is None else float(vol_ratio)
    mild = int(mild_up_days or 0)
    # 优先阳/阴线连数；未传则回退收盘连涨跌
    yangs = int(yang_streak if yang_streak is not None else (up_streak or 0))
    yins = int(yin_streak if yin_streak is not None else (down_streak or 0))
    prior_yang = int(prior_yang_streak or 0)
    ups = yangs
    downs = yins
    shrink_ok = vr_v is not None and vr_v <= 1.15
    hard_shrink = vr_v is not None and vr_v <= 1.0

    # —— 0) CFA 度量（高度相关，合并为一项压力，避免 σ/下行/VaR 三重扣）——
    # 日频短窗：A股题材股日波动 3～6% 常见，阈值高于股市教科书月频口径
    stress_bits: List[float] = []
    if hist_vol is not None:
        # 0 at 3.5%，1 at 8%
        v = float(hist_vol)
        stress_bits.append(max(0.0, min(1.0, (v - 3.5) / 4.5)))
    if downside_dev is not None:
        d = float(downside_dev)
        stress_bits.append(max(0.0, min(1.0, (d - 2.0) / 4.0)) * 0.85)
    if var95 is not None:
        vv = float(var95)
        stress_bits.append(max(0.0, min(1.0, (vv - 5.0) / 6.0)) * 0.7)
    if stress_bits:
        stress = max(stress_bits)  # 取最紧的一项，不累加
        pen = -16.0 * stress
        if abs(pen) >= 0.5:
            score += pen
            bits = []
            if hist_vol is not None:
                bits.append(f"σ{float(hist_vol):.1f}")
            if downside_dev is not None:
                bits.append(f"下行{float(downside_dev):.1f}")
            if var95 is not None:
                bits.append(f"VaR{float(var95):.1f}")
            parts.append(f"波动压力({'/'.join(bits)}) {pen:+.0f}")
    if max_dd is not None and float(max_dd) <= -18.0:
        depth = -float(max_dd)
        pen = _soft_penalty(depth, start=18.0, full=35.0, max_pen=8.0)
        if abs(pen) >= 0.5:
            score += pen
            parts.append(f"窗内回撤{float(max_dd):.0f}% {pen:+.0f}")
    if pct_from_high is not None:
        # 贴着高点还冲：拥挤；离开高点 2～6% 且未崩：消化加分
        pfh = float(pct_from_high)
        if pfh >= -1.0 and pct_v is not None and pct_v >= 2.0:
            score -= 8.0
            parts.append("贴近高点仍冲 -8")
        elif -6.0 <= pfh <= -1.5 and pct5_v is not None and pct5_v >= 8.0:
            score += 8.0
            parts.append(f"距高点{pfh:.1f}%消化 +8")
        elif pfh <= -15.0 and pct5_v is not None and pct5_v < -5.0 and (
            pct_v is None or pct_v < 0.5
        ):
            # 仍处下跌段的深回撤，不是「已反弹但窗内还看得到旧高」
            score -= 6.0
            parts.append(f"深回撤未修复 {pfh:.0f}% -6")

    # —— 1) 理想结构：温和涨 1～2 天（仍要防已连阳过长）——
    if mild >= 1 and mild <= 2 and pct_v is not None and 0.3 <= pct_v < 3.0 and yangs <= 2:
        add = 22.0 if mild == 2 else 14.0
        score += add
        parts.append(f"温和连涨{mild}日 +{add:.0f}")
    elif (
        mild < 1
        and pct_v is not None
        and 0.3 <= pct_v < 2.5
        and yangs <= 1
        and hard_shrink
        and (pct_from_high is None or float(pct_from_high) <= -1.0)
    ):
        # 无资金字段时：缩量微涨离开高点，仍给一截结构分（光迅12号型）
        score += 12.0
        parts.append("缩量微涨离高点 +12")
    elif mild >= 3:
        pen = min(28.0, 6.0 * (mild - 2))
        score -= pen
        parts.append(f"温和连涨已{mild}日，拥挤 -{pen:.0f}")

    # 连阳拥挤：用阳线天数，收盘略跌但仍阳也算连阳
    if yangs >= 3:
        pen = min(28.0, 6.0 * (yangs - 2))
        score -= pen
        parts.append(f"连阳约{yangs}日 -{pen:.0f}")
    elif prior_yang >= 3 and yangs == 0:
        # 连阳刚断的第一天：拥挤尚未消完，继续带罚（避免奥比6号被打成最佳）
        pen = min(18.0, 4.0 * (prior_yang - 2))
        score -= pen
        parts.append(f"连阳{prior_yang}日后首日仍拥挤 -{pen:.0f}")

    # —— 2) 5日涨幅 ——
    if pct5_v is not None:
        base_pen = _soft_penalty(pct5_v, start=12.0, full=35.0, max_pen=28.0)
        if pct_v is not None and pct_v >= 2.0:
            base_pen *= 1.35
        elif (
            pct_v is not None
            and pct_v <= 0.5
            and vr_v is not None
            and vr_v <= 1.0
            and (yins >= 2 or (prior_yang >= 2 and yins >= 1))
        ):
            # 仅「真消化」（缩量+至少第2日阴/或连阴）才大幅减拥挤扣分
            base_pen *= 0.40
        elif pct_v is not None and pct_v <= 0.5:
            # 首跌/无量比：少减一点，避免大连涨后首跌分虚高
            base_pen *= 0.85
        if abs(base_pen) >= 0.5:
            score += base_pen
            parts.append(f"5日{pct5_v:+.1f}%拥挤 {base_pen:+.0f}")

    # —— 3) 当日极端 ——
    if pct_v is not None:
        if pct_v >= 7.0:
            pen = 18.0 + min(22.0, (pct_v - 7.0) * 3.0)
            score -= pen
            parts.append(f"当日大涨{pct_v:+.1f}%追高 -{pen:.0f}")
        elif pct_v >= 4.5:
            score -= 10.0
            parts.append(f"当日偏强{pct_v:+.1f}% -10")
        elif pct_v <= -7.0:
            pen = 16.0 + min(20.0, (-pct_v - 7.0) * 2.5)
            score -= pen
            parts.append(f"当日大跌{pct_v:+.1f}%飞刀 -{pen:.0f}")
        elif pct_v <= -3.5:
            score -= 10.0
            parts.append(f"当日偏弱{pct_v:+.1f}% -10")

    # —— 4) 连阴风险；止跌后再温和阳 ——
    if yins >= 2:
        pen = min(30.0, 10.0 + 8.0 * (yins - 2))
        if pct_v is not None and pct_v <= -3.0:
            pen += 8.0
        score -= pen
        parts.append(f"连阴约{yins}日 -{pen:.0f}")
    if yins >= 2 and pct_v is not None and 0.4 <= pct_v < 3.0 and mild >= 1 and yangs <= 1:
        add = 16.0 if mild >= 2 else 10.0
        score += add
        parts.append(f"连阴后温和修复 +{add:.0f}")

    # —— 5) 热后缩量消化（必须有量比；不能仍在连阳中；区分首跌 vs 第二日）——
    if (
        pct5_v is not None
        and pct5_v >= 10.0
        and pct_v is not None
        and -3.2 <= pct_v <= 1.2
        and shrink_ok
        and yangs == 0  # 仍连阳不算回踩消化
    ):
        # 基础分：第二日阴/连阴消化 > 首日
        if yins >= 2 or (prior_yang >= 2 and yins >= 1 and hard_shrink):
            add = 22.0
            tag = "热后缩量连阴消化"
        elif prior_yang >= 3 and yins == 0:
            # 连阳后首根非阳非阴：加分很克制
            add = 6.0
            tag = "连阳后首日观望"
        elif prior_yang >= 2 and yins == 1:
            add = 12.0
            tag = "热后首阴缩量"
        else:
            add = 10.0
            tag = "热后缩量回踩"
        if pct5_v >= 18.0 and add >= 12:
            add += 4.0
        if flow_v is not None and flow_v > 0 and add >= 12:
            add += 5.0
        score += add
        parts.append(f"{tag} +{add:.0f}")
    elif (
        pct5_v is not None
        and pct5_v >= 12.0
        and pct_v is not None
        and -3.0 <= pct_v < 0
        and hard_shrink
        and yins >= 1
        and yangs == 0
    ):
        add = 8.0 if yins >= 2 else 4.0
        score += add
        parts.append(f"涨后缩量回撤 +{add:.0f}")

    # —— 6) 资金 ——
    if flow_v is not None:
        if pct_v is not None and pct_v >= 2.0 and flow_v <= -0.8:
            score -= 16.0
            parts.append("涨时大出（背离） -16")
        elif pct_v is not None and pct_v <= 0.8 and flow_v > 0.3 and yangs <= 1:
            score += 12.0
            parts.append("回踩/横盘仍流入 +12")
        elif flow_v > 0.5:
            # 连阳中流入只说明分歧买，不把风险分抬成好买点
            add = 2.0 if yangs >= 3 else 6.0
            score += add
            parts.append(f"流入{flow_v:.1f}亿 +{add:.0f}")
        elif flow_v < -1.5:
            score -= 10.0
            parts.append(f"流出{flow_v:.1f}亿 -10")
    if flow5_v is not None and flow5_v > 1.0 and (pct_v is None or pct_v < 4.0) and yangs <= 2:
        score += 5.0
        parts.append("5日资金累积 +5")
    elif flow5_v is not None and flow5_v < -3.0:
        score -= 8.0
        parts.append("5日资金撤离 -8")

    # —— 7) 主题 ——
    if theme_grade == "走强":
        score += 6.0
        parts.append("主题走强 +6")
    elif theme_grade == "走弱":
        score -= 18.0
        parts.append("主题走弱 -18")
    elif theme_grade == "偏弱":
        score -= 8.0
        parts.append("主题偏弱 -8")

    # —— 8) 新闻 ——
    nd, nw = _news_risk_delta(news_hits)
    score += nd
    parts.extend(nw)

    score = _clamp(score)
    if score >= 35:
        tier = "低风险"
    elif score >= 10:
        tier = "偏低风险"
    elif score >= -15:
        tier = "中性"
    elif score >= -40:
        tier = "偏高风险"
    else:
        tier = "高风险"

    return {
        "风险值": round(score, 1),
        "风险档": tier,
        "风险说明": "；".join(parts[:8]) if parts else "样本不足，中性",
        "可短做": score >= -25.0,
        "宜谨慎": -40.0 <= score < -25.0,
        "宜拒绝": score < -55.0,
        "连阳": yangs,
        "连阴": yins,
        "波动σ": hist_vol,
        "VaR95": var95,
    }


# 短线池/候选：风险分低于此不作为可买（仍可观察）
_RISK_BUY_FLOOR = -25.0
_RISK_HARD_REJECT = -55.0


def _score_buy_risk_for_code(
    code: str,
    asof: str,
    *,
    history: Optional[Dict[str, Any]] = None,
    pct: Optional[float] = None,
    pct5: Optional[float] = None,
    flow: Optional[float] = None,
    flow5: Optional[float] = None,
    vol_ratio: Optional[float] = None,
    mild_up_days: int = 0,
    news_hits: Optional[List[str]] = None,
    theme_grade: str = "偏好",
) -> Dict[str, Any]:
    """拉取日K阳线/CFA度量后打风险分。"""
    ctx = _build_risk_context(code, asof, history=history, today_pct=pct)
    return _score_buy_risk(
        pct=pct,
        pct5=pct5,
        flow=flow,
        flow5=flow5,
        vol_ratio=vol_ratio,
        mild_up_days=mild_up_days,
        up_streak=int(ctx.get("yang_streak") or 0),
        down_streak=int(ctx.get("yin_streak") or 0),
        yang_streak=int(ctx.get("yang_streak") or 0),
        yin_streak=int(ctx.get("yin_streak") or 0),
        prior_yang_streak=int(ctx.get("prior_yang_streak") or 0),
        hist_vol=ctx.get("hist_vol"),
        downside_dev=ctx.get("downside_dev"),
        max_dd=ctx.get("max_dd"),
        var95=ctx.get("var95"),
        pct_from_high=ctx.get("pct_from_high"),
        news_hits=news_hits,
        theme_grade=theme_grade,
    )


def build_daily_short_picks(
    boards: pd.DataFrame,
    news: pd.DataFrame,
    *,
    limit: int = 6,
    exclude_codes: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """
    今日短线周转池：哪个热板在走就跟哪个，持有预期 1～3 天。
    形态含催化轻涨/热板连涨/回调续涨/轻踩承接；不是干等中军大回踩。
    """
    exclude_codes = {str(c).zfill(6)[-6:] for c in (exclude_codes or set())}
    exclude_codes |= set(_SHORT_BALLAST_EXCLUDE)
    exclude_codes |= set(_OBSERVE_EXCLUDE_CODES)
    if news is None:
        news = pd.DataFrame()
    if boards is None:
        boards = pd.DataFrame()

    _clear_risk_bars_cache()
    history = _load_history()
    asof = _today()

    ranked_boards: List[Tuple[float, Dict[str, Any], int, List[str]]] = []
    if not boards.empty:
        for _, r in boards.iterrows():
            bname = str(r.get("板块名称") or "")
            btype = str(r.get("类型") or "")
            if not bname or _is_emotion_board(bname, btype):
                continue
            if btype == "市场":
                continue
            pct = _to_float(r.get("涨跌幅"))
            flow = _to_float(r.get("主力净流入_亿"))
            pct5 = _to_float(r.get("涨跌幅_5日"))
            flow5 = _to_float(r.get("主力净流入_5日_亿"))
            n_hits, hit_titles = _news_hit_count_for_text(bname, news)
            seed_hit = any(k in bname for k in _DAILY_SHORT_BOARD_SEEDS)
            if any(k in bname for k in ("黄金", "贵金属")):
                nh2, titles2 = _news_hit_count_for_text("黄金", news)
                nh3, titles3 = _news_hit_count_for_text("金价", news)
                n_hits = max(n_hits, nh2, nh3)
                if not hit_titles:
                    hit_titles = titles2 or titles3
            # 跟热：新闻 / 短线种子板 / 当日热板资金，任一即可
            heat_ok = (
                (pct is not None and float(pct) >= 0.8)
                and (flow is not None and float(flow) >= 0.8)
            ) or (
                (pct is not None and float(pct) >= 1.5)
                and (flow is not None and float(flow) >= 0.2)
            )
            if n_hits <= 0 and not seed_hit and not heat_ok:
                continue
            if pct is not None and pct <= -2.5:
                continue
            # 已大幅透支且无新闻：少跟末段
            if (
                n_hits <= 0
                and pct5 is not None
                and float(pct5) >= 14.0
                and pct is not None
                and float(pct) >= 3.0
            ):
                continue
            score = (
                n_hits * 4.0
                + max(flow or 0.0, 0.0) * 0.55
                + max(flow5 or 0.0, 0.0) * 0.15
                + max(pct or 0.0, 0.0) * 0.85
                + (2.0 if 2.0 <= (pct5 or 0) < 12.0 else 0.0)
                + (3.0 if seed_hit and n_hits > 0 else (1.2 if seed_hit else 0.0))
                + (2.5 if heat_ok and n_hits <= 0 else 0.0)
            )
            if n_hits <= 0 and seed_hit and not heat_ok:
                if not (
                    (flow is not None and flow >= 2.5)
                    or (flow5 is not None and flow5 >= 6)
                ):
                    continue
            if score < 2.5:
                continue
            ranked_boards.append((score, r.to_dict(), n_hits, hit_titles))

    ranked_boards.sort(key=lambda x: x[0], reverse=True)
    top_boards = ranked_boards[:10]

    cand: List[Tuple[str, str, str, float, int, List[str]]] = []
    seen_c: set = set()

    def _add_stock(
        code: str,
        name: str,
        board: str,
        bscore: float,
        nh: int,
        titles: List[str],
    ) -> None:
        code = str(code or "").zfill(6)[-6:]
        if not (code.isdigit() and len(code) == 6):
            return
        if code in seen_c or code in exclude_codes:
            return
        if _is_st_stock_name(name):
            return
        seen_c.add(code)
        cand.append((code, str(name or code), board, bscore, nh, titles))

    for bi, (bscore, brow, nh, titles) in enumerate(top_boards):
        bname = str(brow.get("板块名称") or "")
        hint = _hint_for_board(bname)
        for key, seeds in _DAILY_SHORT_BOARD_SEEDS.items():
            if key in bname:
                for c, n in seeds:
                    _add_stock(c, n, bname, bscore, nh, titles)
        try:
            seeds = list((hint or {}).get("seed_stocks") or [])
            for item in seeds:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    _add_stock(str(item[0]), str(item[1]), bname, bscore, nh, titles)
        except Exception:
            pass
        # 前 6 个热板拉活跃成分：任意主线里正在动的票，不只中军种子
        if bi < 6:
            try:
                for c, n in _seed_stocks_for_board(bname, hint, limit=8, fast=True):
                    _add_stock(c, n, bname, bscore + 0.5, nh, titles)
            except Exception:
                pass

    # 新闻别名直达种子：避免板块名对不上导致空窗（如新闻有电池、板名叫麒麟电池）
    blob = _news_text_blob(news)
    for aliases, label, seeds in _DAILY_SHORT_NEWS_SEEDS:
        hit_n = sum(1 for a in aliases if a and a in blob)
        if hit_n <= 0:
            continue
        titles: List[str] = []
        if news is not None and not news.empty:
            tcol = "title" if "title" in news.columns else (
                "标题" if "标题" in news.columns else None
            )
            if tcol:
                for _, nr in news.iterrows():
                    t = str(nr.get(tcol) or "")
                    if any(a in t for a in aliases):
                        titles.append(t[:72])
                    if len(titles) >= 2:
                        break
        bscore = 4.0 + hit_n * 2.0
        for c, n in seeds:
            _add_stock(c, n, label, bscore, max(hit_n, 1), titles)

    if not cand:
        return []

    codes = [c[0] for c in cand]
    try:
        qmap = _fetch_ulist_quote_map(codes)
    except Exception:
        qmap = {}

    try:
        _prefetch_risk_bars(codes, asof)
    except Exception:
        pass

    scored: List[Tuple[float, Dict[str, Any]]] = []
    for code, name, board, bscore, nh, titles in cand:
        q = qmap.get(code) or {}
        px = _to_float(q.get("最新价"))
        pct = _to_float(q.get("涨跌幅"))
        pct5 = _to_float(q.get("涨跌幅_5日"))
        flow = _to_float(q.get("主力净流入_亿"))
        flow5 = _to_float(q.get("主力净流入_5日_亿"))
        mkt = _to_float(q.get("总市值_亿"))
        vol_ratio = _to_float(q.get("量比"))
        turnover = _to_float(q.get("换手率"))
        disp_name = str(q.get("名称") or name)

        # 短线：避开过小票与超大压舱；股价过高一手成本太大
        if mkt is not None and (mkt < 35 or mkt > 4500):
            continue
        if code in {"300750", "600519", "601318", "601398"}:
            continue
        if px is not None and float(px) >= _MAX_TRADE_PRICE:
            continue
        if pct is not None and (pct >= 5.0 or pct <= -3.5):
            continue
        if pct5 is not None and pct5 >= 22:
            continue
        if flow is None or flow <= 0:
            continue

        board_pct = None
        board_flow = None
        try:
            hit = boards.loc[boards["板块名称"].astype(str) == board]
            if not hit.empty:
                board_pct = _to_float(hit.iloc[0].get("涨跌幅"))
                board_flow = _to_float(hit.iloc[0].get("主力净流入_亿"))
        except Exception:
            pass
        # 新闻别名板可能对不上东财名：用热度分兜底，不因此整票否决
        if board_pct is None and bscore >= 5:
            board_pct = 1.0

        theme_grade = "走强" if (board_pct or 0) >= 1.0 or (board_flow or 0) >= 2 else "偏好"
        if (board_pct or 0) <= -1.8 and (board_flow or 0) < 0:
            continue

        if pct is not None and pct <= -2.0:
            stock_grade = "偏弱"
        elif pct is not None and pct >= 1.0 and (flow or 0) > 0:
            stock_grade = "走强"
        else:
            stock_grade = "偏好"

        mild_days = _mild_up_flow_streak(history, code, asof, pct, flow)
        major = nh >= 1
        ohlc = _quote_ohlc(q)
        bar_ks = _day_kline_structure(
            open_=ohlc["open"],
            high=ohlc["high"],
            low=ohlc["low"],
            close=ohlc["close"] or px,
            prev_close=ohlc["prev_close"],
            vol_ratio=vol_ratio,
        )
        # 短线优先用周转形态（含热板连涨/回调续涨），再回落通用买点
        setup = _detect_t1_short_setup(
            news_hits=nh,
            board_pct=board_pct,
            pct=pct,
            pct5=pct5,
            flow=flow,
            flow5=flow5,
            vol_ratio=vol_ratio,
            open_=ohlc["open"],
            high=ohlc["high"],
            low=ohlc["low"],
            close=ohlc["close"] or px,
            prev_close=ohlc["prev_close"],
            bar_struct=bar_ks,
        )
        if not setup.get("buy_ok"):
            setup = _detect_buy_setup(
                theme_ok=True,
                theme_grade=theme_grade,
                stock_grade=stock_grade,
                stock_pct=pct,
                stock_pct_5d=pct5,
                stock_flow=flow,
                stock_flow_5d=flow5,
                vol_ratio=vol_ratio,
                turnover=turnover,
                mild_flow_days=mild_days,
                major_catalyst=major,
                open_=ohlc["open"],
                high=ohlc["high"],
                low=ohlc["low"],
                close=ohlc["close"] or px,
                prev_close=ohlc["prev_close"],
                bar_struct=bar_ks,
            )
        if not setup.get("buy_ok"):
            continue

        kind = str(setup.get("kind") or "")
        short_kinds = (
            "t1_catalyst_up",
            "t1_hot_continue",
            "t1_resume",
            "t1_dip_hold",
            "catalyst_grind",
            "mild_inflow_run",
            "stabilize_up",
            "true_pullback",
        )
        if kind in short_kinds and px is not None and px > 0:
            rng = _near_price_buy_band(float(px))
            action = str(setup.get("action") or "")
            lo, hi = _parse_buy_band(rng)
        else:
            rng, action = _suggest_buy_plan(
                px,
                pct,
                pct5,
                stock_flow=flow,
                theme_grade=theme_grade,
                stock_grade=stock_grade,
                stars=3,
                buy_setup=setup,
            )
            lo, hi = _parse_buy_band(rng)
        if px is None or lo is None or hi is None:
            continue
        if not (lo * 0.995 <= float(px) <= hi * 1.005):
            continue
        bad_words = ("观望", "观察真回踩", "继续观察", "先等", "勿当回踩", "假回踩")
        if any(w in str(action) for w in bad_words):
            continue
        if "短持" not in str(action):
            action = f"{action}；短持1～3天见好就收"

        buy_method = _resolve_buy_method(setup, pct=pct, news_hits=nh)
        if not buy_method:
            buy_method = str(setup.get("label") or "C热板连涨")

        risk = _score_buy_risk_for_code(
            code,
            asof,
            history=history,
            pct=pct,
            pct5=pct5,
            flow=flow,
            flow5=flow5,
            vol_ratio=vol_ratio,
            mild_up_days=mild_days,
            news_hits=_news_hits_for_stock(code, disp_name, titles),
            theme_grade=theme_grade,
        )
        risk_score = float(risk.get("风险值") or 0)
        if risk_score < _RISK_HARD_REJECT:
            continue
        if risk_score < _RISK_BUY_FLOOR and kind not in (
            "t1_dip_hold",
            "true_pullback",
            "t1_resume",
        ):
            # 偏高风险：仅回踩类仍可进池标橙；连涨追高类直接跳过
            continue

        total = (
            bscore * 0.28
            + (4.0 if setup.get("buy_ok") else 0.0)
            + min(max(flow or 0.0, 0.0), 6.0) * 0.45
            + (2.2 if 0.3 <= (pct or -99) <= 3.2 else 0.0)
            + (1.2 if kind in ("t1_hot_continue", "t1_resume", "mild_inflow_run") else 0.0)
            + (1.5 if nh >= 2 else (0.8 if nh >= 1 else 0.0))
            + min(mild_days, 3) * 0.5
            + (1.0 if (board_pct or 0) >= 1.0 else 0.0)
            + max(risk_score, -40.0) * 0.04
        )
        sig, sig_lab = "红", str(setup.get("label") or "短线可买")
        if (
            total < 5.2
            or risk_score < 0
            or (pct is not None and pct < 0 and (flow or 0) < 0.25)
        ):
            sig, sig_lab = "橙", str(setup.get("label") or "贴近买点")
        if risk_score < _RISK_BUY_FLOOR:
            sig, sig_lab = "橙", f"高风险·{setup.get('label') or '回踩'}"

        why_bits = [
            str(setup.get("why") or ""),
            f"风险值{risk_score:.0f}({risk.get('风险档')})",
            f"现价{px:.2f}落在买点带{rng}",
            "持有预期1～3天",
        ]
        if nh > 0:
            why_bits.insert(0, f"新闻命中{nh}条")
        elif board_pct is not None and board_pct >= 0.8:
            why_bits.insert(0, f"热板跟进(板{board_pct:+.1f}%)")
        if flow is not None and flow > 0:
            why_bits.append(f"流入{flow:.1f}亿")

        row = {
            "信号色": sig,
            "信号": sig_lab,
            "代码": code,
            "名称": disp_name,
            "所属板块": board,
            "最新价": px,
            "涨跌幅%": pct,
            "5日涨跌%": pct5,
            "主力净流入亿": flow,
            "建议买入": rng,
            "买入方法": buy_method,
            "风险值": risk_score,
            "风险档": risk.get("风险档"),
            "风险说明": risk.get("风险说明"),
            "操作建议": str(action),
            "入选原因": f"{board}；" + "；".join([x for x in why_bits if x][:4]),
            "新闻摘录": "；".join(titles[:2]) if titles else "",
            "短线分": round(total, 2),
            "持有预期": "1～3天",
            "详细依据": (
                f"【今日短线·周转】跟正在走的热板，持仓预期1～3天见好就收；"
                f"国产服务器与海外组装分池，不是空仓等单一大盘中军回踩，"
                f"也不是特变式压舱长持。\n"
                f"【买入方法】{buy_method}（多条件任一即可，贴价可成交）\n"
                f"【风险值】{risk_score:.1f}（{risk.get('风险档')}）："
                f"{risk.get('风险说明')}\n"
                f"【形态】{setup.get('label')} / {setup.get('kind')}\n"
                f"【板块】{board}\n"
                f"【原因】{'；'.join([x for x in why_bits if x])}\n"
                f"【新闻】{'；'.join(titles[:3]) if titles else '热板/资金'}\n"
                f"【建议】{rng} | {action}"
            ),
        }
        scored.append((total, row))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored[:limit]]


def build_forward_watch(
    *,
    news_limit: int = 50,
    persist: bool = True,
    progress_cb: Optional[Any] = None,
) -> Dict[str, Any]:
    """生成当日前瞻观察结果。

    progress_cb: 可选回调 (pct: int 0~100, msg: str) -> None，供 UI 进度条。
    """

    def _prog(pct: int, msg: str) -> None:
        if progress_cb is None:
            return
        try:
            progress_cb(int(max(0, min(100, pct))), str(msg or ""))
        except Exception:  # noqa: BLE001
            pass

    asof = _today()
    err_parts: List[str] = []
    news = pd.DataFrame()
    boards = pd.DataFrame()
    _clear_risk_bars_cache()
    clear_board_constituents_cache()
    set_board_fetch_fast(True)
    _prog(5, "并行拉取新闻与板块行情…")

    def _load_news() -> pd.DataFrame:
        try:
            return fetch_forward_news(
                finance_limit=30, tech_limit=40, pharma_limit=20, fast=True
            )
        except Exception as exc:  # noqa: BLE001
            err_parts.append(f"新闻:{exc}")
        try:
            return fetch_hot_news(news_limit)
        except Exception as exc2:  # noqa: BLE001
            err_parts.append(f"新闻兜底:{exc2}")
            return pd.DataFrame()

    def _load_boards() -> pd.DataFrame:
        try:
            return fetch_industry_boards(fast=True)
        except Exception as exc:  # noqa: BLE001
            err_parts.append(f"板块:{exc}")
            return pd.DataFrame()

    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_news = pool.submit(_load_news)
        fut_boards = pool.submit(_load_boards)
        try:
            news = fut_news.result(timeout=120)
        except Exception as exc:  # noqa: BLE001
            err_parts.append(f"新闻超时:{exc}")
        try:
            boards = fut_boards.result(timeout=150)
        except Exception as exc:  # noqa: BLE001
            err_parts.append(f"板块超时:{exc}")
    _prog(22, "新闻与板块就绪…")

    history = _load_history()
    theme_rows: List[Dict[str, Any]] = []
    stock_rows: List[Dict[str, Any]] = []
    codes_today: List[str] = []
    _prog(28, "整理新闻并发现主题…")
    weekly_news = _build_weekly_news_view(news, boards, limit=18)

    # 每日动态主题宇宙（非写死白名单）+ 昨日在池延续
    theme_universe = discover_theme_universe(
        boards, news, history, asof, max_new=26
    )

    # 先收集候选代码，批量取行情
    candidate_codes: List[str] = []
    theme_pack: List[Dict[str, Any]] = []
    kicked_theme_notes: List[str] = []

    _prog(40, "匹配主题入池条件…")
    for theme in theme_universe:
        news_hits = list(theme.get("_news_hits") or []) or _match_news(theme, news)
        # 始终按产业匹配重排，不锁死昨日/发现时的坏板名（如虚拟机器人）
        board_sub = _match_boards(theme, boards)
        recent_streak = _theme_recent_streak(history, theme, asof)
        bad_before = _theme_consecutive_bad_days(history, theme, asof)
        good_before = _theme_consecutive_good_days(history, theme, asof)

        # 多候选板试入池：顶板若只是杂糅概念失败，换真·行业板再评
        enter = False
        weak_board = False
        theme_ok = False
        weak_why = ""
        enter_why: List[str] = []
        kicked = False
        theme_grade = "偏好"
        theme_star_delta = 0
        top_board: Dict[str, Any] = {}
        board_pct = board_flow = board_pct5 = board_flow5 = None
        board_name = ""
        candidates = (
            [board_sub.iloc[i].to_dict() for i in range(len(board_sub))]
            if not board_sub.empty
            else [{}]
        )
        best_fail: Optional[Tuple] = None
        for cand in candidates:
            board_pct = _to_float(cand.get("涨跌幅"))
            board_flow = _to_float(cand.get("主力净流入_亿"))
            board_pct5 = _to_float(cand.get("涨跌幅_5日"))
            board_flow5 = _to_float(cand.get("主力净流入_5日_亿"))
            board_name = str(cand.get("板块名称") or "")
            (
                enter,
                weak_board,
                theme_ok,
                weak_why,
                enter_why,
                kicked,
                theme_grade,
                theme_star_delta,
            ) = _evaluate_theme_entry(
                theme=theme,
                news_hits=news_hits,
                board_name=board_name,
                board_pct=board_pct,
                board_flow=board_flow,
                board_pct5=board_pct5,
                board_flow5=board_flow5,
                recent_streak=recent_streak,
                bad_before=bad_before,
            )
            if enter:
                top_board = cand
                theme["_matched_board"] = board_name
                break
            if best_fail is None or (kicked and recent_streak >= 1):
                best_fail = (
                    enter,
                    weak_board,
                    theme_ok,
                    weak_why,
                    enter_why,
                    kicked,
                    theme_grade,
                    theme_star_delta,
                    cand,
                    board_pct,
                    board_flow,
                    board_pct5,
                    board_flow5,
                    board_name,
                )
        if not enter and best_fail is not None:
            (
                enter,
                weak_board,
                theme_ok,
                weak_why,
                enter_why,
                kicked,
                theme_grade,
                theme_star_delta,
                top_board,
                board_pct,
                board_flow,
                board_pct5,
                board_flow5,
                board_name,
            ) = best_fail

        # 拼上动态发现依据
        disc = [str(x) for x in (theme.get("_discover_why") or []) if x]
        if disc and enter and not theme.get("_carry"):
            enter_why = [f"动态发现:{'；'.join(disc[:3])}"] + enter_why
        if kicked:
            kicked_theme_notes.append(
                f"{theme.get('name')}: {'；'.join(enter_why[-2:])}"
            )
        if not enter:
            continue

        # 种子股：佐证库 + 行业成分龙头（优先东财行业名取成分，展示用细分标签）
        industry_for_seed = str(
            theme.get("_em_industry") or theme.get("_industry") or ""
        )
        matched = str(board_name or theme.get("_matched_board") or "")
        # 整车主题若顶板误匹配到「汽车芯片」，仍按行业成分取种，勿用芯片概念成分
        seed_board = (
            industry_for_seed
            if industry_for_seed
            and "概念" not in industry_for_seed
            and industry_for_seed
            not in {"国产服务器", "海外组装", "液冷散热"}
            else matched
        )
        if any(
            _blocked_compound_board(matched, k)
            for k in (
                list(theme.get("board_keys") or [])
                + [industry_for_seed, str(theme.get("name") or "")]
            )
            if k
        ):
            if industry_for_seed and "概念" not in industry_for_seed:
                seed_board = industry_for_seed
        theme["seed_stocks"] = _seed_stocks_for_board(
            seed_board,
            {
                "id": str(theme.get("_hint_id") or theme.get("id") or ""),
                "seed_stocks": list(theme.get("seed_stocks") or []),
                "industries": list(theme.get("board_keys") or [])
                + ([industry_for_seed] if industry_for_seed else []),
                "board_keys": list(theme.get("board_keys") or []),
            },
            limit=_stock_limit_for_theme(theme),
            fast=True,
        )

        theme_good_streak = (
            good_before + 1 if theme_grade in ("走强", "偏好") else good_before
        )
        theme_stars, theme_score_why = _score_theme(
            theme=theme,
            consecutive_good=theme_good_streak,
            news_hits=len(news_hits),
            board_pct=board_pct,
            board_flow=board_flow,
            board_pct5=board_pct5,
            theme_ok=theme_ok,
            bad_before=bad_before,
            theme_grade=theme_grade,
            star_delta=theme_star_delta,
        )

        theme_pack.append(
            {
                "theme": theme,
                "news_hits": news_hits,
                "board_sub": board_sub,
                "top_board": top_board,
                "board_pct": board_pct,
                "board_flow": board_flow,
                "board_pct5": board_pct5,
                "board_flow5": board_flow5,
                "board_name": board_name,
                "enter_why": enter_why,
                "weak_board": weak_board,
                "theme_ok": theme_ok,
                "weak_why": weak_why,
                "theme_grade": theme_grade,
                "theme_star_delta": theme_star_delta,
                "theme_stars": theme_stars,
                "theme_score_why": theme_score_why,
                "theme_good_streak": theme_good_streak,
                "bad_before": bad_before,
            }
        )
        for code, _name in theme.get("seed_stocks") or []:
            candidate_codes.append(str(code))

    quote_map = {}
    uniq_codes = sorted(set(candidate_codes))
    _prog(52, f"拉取个股行情（约{len(uniq_codes)}只）…")
    if uniq_codes:
        try:
            quote_map = _fetch_ulist_quote_map(uniq_codes)
        except Exception as exc:  # noqa: BLE001
            err_parts.append(f"行情:{exc}")
            quote_map = {}

    _prog(58, f"并行预拉日K（约{len(uniq_codes)}只，算风险值）…")
    try:
        n_pref = _prefetch_risk_bars(uniq_codes, asof)
        if n_pref:
            _prog(62, f"日K预拉完成 {n_pref} 只")
    except Exception as exc:  # noqa: BLE001
        err_parts.append(f"日K预拉:{exc}")

    kicked_notes: List[str] = list(kicked_theme_notes)
    frozen_notes: List[str] = []
    status_today: Dict[str, Dict[str, Any]] = {}
    theme_status_today: Dict[str, Dict[str, Any]] = {}

    _prog(68, "评分主题与个股买点/风险值…")
    for pack in theme_pack:
        theme = pack["theme"]
        news_hits: List[str] = pack["news_hits"]
        board_pct = pack["board_pct"]
        board_flow = pack["board_flow"]
        board_name = pack["board_name"]
        enter_why = pack["enter_why"]
        weak_board = bool(pack.get("weak_board"))
        theme_ok = bool(pack.get("theme_ok", True))
        weak_why = str(pack.get("weak_why") or "")
        theme_grade = str(pack.get("theme_grade") or ("偏好" if theme_ok else "偏弱"))
        theme_star_delta = int(pack.get("theme_star_delta") or 0)
        theme_stars = int(pack.get("theme_stars") or 2)
        theme_score_why = pack.get("theme_score_why") or []
        theme_good_streak = int(pack.get("theme_good_streak") or 0)
        bad_before = int(pack.get("bad_before") or 0)

        board_pct5 = pack.get("board_pct5")
        board_flow5 = pack.get("board_flow5")
        if theme_grade == "走强":
            theme_status = "走强"
        elif theme_grade == "偏好":
            theme_status = "偏好"
        elif theme_grade == "偏弱":
            theme_status = "偏弱回踩"
        else:
            theme_status = "偏好"
        theme_sig, theme_sig_why = _theme_signal_tier(
            theme_grade=theme_grade,
            theme_stars=theme_stars,
            board_pct=board_pct,
            board_pct5=board_pct5,
            board_flow=board_flow,
        )
        theme_rows.append(
            {
                "概念": str(theme.get("_concept") or theme["name"]),
                "行业": str(theme.get("_industry") or board_name or "-"),
                "板块主题": str(theme.get("_concept") or theme["name"]),  # 兼容旧列
                "主题ID": str(theme.get("id") or ""),
                "优先级": int(theme.get("priority") or 3),
                "星级": theme_stars,
                "星级显示": _stars_glyph(theme_stars),
                "信号色": theme_sig,
                "信号": theme_sig_why,
                "匹配板块": str(
                    theme.get("_em_industry")
                    or theme.get("_matched_board")
                    or board_name
                    or "-"
                ),
                "板块涨跌%": board_pct,
                "板块5日%": board_pct5,
                "主力净流入亿": board_flow,
                "5日主力净流入亿": board_flow5,
                "新闻条数": len(news_hits),
                "入池原因": "；".join(enter_why),
                "主题逻辑": theme.get("thesis") or "",
                "新闻摘录": " | ".join(news_hits[:3]),
                "状态": theme_status,
                "主题走好连日": theme_good_streak,
            }
        )
        theme_status_today[str(theme.get("id") or theme["name"])] = {
            "ok": bool(theme_ok),
            "grade": theme_grade,
            "bad": theme_grade in ("偏弱", "走弱"),
            "why": "" if theme_ok else weak_why,
            "stars": theme_stars,
            "consecutive_good": theme_good_streak,
            "name": theme["name"],
        }

        # 弱主题：材料挖坑或已在池粘性主题仍列个股（选股盯下跌/起稳）
        dig_wait = str(theme.get("id") or theme.get("_hint_id") or "") in _DIG_WAIT_HINT_IDS
        if not theme_ok and not dig_wait:
            continue

        seed_items = list(theme.get("seed_stocks") or [])
        # 主题名额按「实际入池」计；≥800 跳过不占位。种子全量尝试，至少保 6 席意图。
        theme_cap = max(_stock_limit_for_theme(theme), len(seed_items), 6)
        added_here = 0
        for code, name in seed_items:
            code = str(code or "").zfill(6)[-6:]
            # 行业下个股全局不重复：先到先得（按主题排序）
            if code in codes_today:
                continue
            if code in _OBSERVE_EXCLUDE_CODES:
                kicked_notes.append(
                    f"{name}({code}): 永久排除（名称/板块混淆票，不进观察池）"
                )
                continue
            if _is_st_stock_name(str(name)):
                kicked_notes.append(f"{name}({code}): ST/*ST 不进观察池")
                continue
            q = quote_map.get(code) or {}
            px = _to_float(q.get("最新价"))
            if px is not None and float(px) >= _MAX_TRADE_PRICE:
                kicked_notes.append(
                    f"{name}({code}): 股价{float(px):.0f}≥{_MAX_TRADE_PRICE:.0f}，一手成本过高不进池（不占主题名额）"
                )
                continue
            if added_here >= theme_cap:
                continue
            pct = _to_float(q.get("涨跌幅"))
            pct5 = _to_float(q.get("涨跌幅_5日"))
            stock_flow = _to_float(q.get("主力净流入_亿"))
            stock_flow_5d = _to_float(q.get("主力净流入_5日_亿"))
            vol_ratio = _to_float(q.get("量比"))
            turnover = _to_float(q.get("换手率"))

            stock_grade, stock_star_delta, grade_why = _stock_day_grade(
                stock_pct=pct,
                stock_pct_5d=pct5,
                stock_flow=stock_flow,
                board_pct=board_pct,
                board_flow=board_flow,
            )
            day_ok = stock_grade == "走强"
            is_hard_weak = stock_grade == "走弱"
            bad_before_s = _consecutive_bad_days(history, code, asof)
            good_before = _consecutive_good_days(history, code, asof)

            # 选股期：个股走弱不踢（止损属持有期）；主题中期坏了才整主题撤
            kick_weak, kick_why = _should_kick_stock_on_weak(
                dig_wait=dig_wait,
                stock_pct=pct,
                stock_pct_5d=pct5,
                vol_ratio=vol_ratio,
                stock_flow=stock_flow,
                history=history,
                code=code,
                asof=asof,
                grade_why=grade_why,
            )
            if kick_weak:
                kicked_notes.append(f"{name}({code}): {kick_why}")
                continue

            worth, worth_why = _stock_worth_in_theme(
                stock_pct=pct,
                stock_pct_5d=pct5,
                stock_flow=stock_flow,
                stock_flow_5d=stock_flow_5d,
                stock_grade=stock_grade,
                theme_ok=theme_ok,
                major_catalyst=_major_catalyst_in_hits(news_hits),
                is_seed=True,
                dig_wait_theme=dig_wait,
            )
            if not worth:
                kicked_notes.append(f"{name}({code}): 未入选—{worth_why}")
                continue

            # 连入仅走强 +1；偏好/偏弱/走弱均冻结
            if day_ok:
                consecutive = good_before + 1
            else:
                consecutive = good_before
                if stock_grade in ("偏好", "偏弱"):
                    frozen_notes.append(
                        f"{name}({code}): 今日{stock_grade}不计连入、不加不减星（{grade_why}）"
                    )
                else:
                    frozen_notes.append(
                        f"{name}({code}): 今日走弱减星、不计连入（{grade_why}）"
                    )

            status_today[code] = {
                "ok": bool(day_ok),
                "grade": stock_grade,
                "bad": bool(is_hard_weak),
                "why": grade_why,
                "consecutive_good": consecutive,
                "pct": pct,
                "flow": stock_flow,
                "flow5": stock_flow_5d,
                "vol_ratio": vol_ratio,
                "turnover": turnover,
            }

            mild_flow_days = _mild_up_flow_streak(
                history, code, asof, pct, stock_flow
            )
            chase_reasons, _chase_pen = _detect_chase_or_fake(
                stock_pct=pct,
                stock_pct_5d=pct5,
                stock_flow=stock_flow,
                theme_grade=theme_grade,
                board_pct=board_pct,
            )
            major_cat = _major_catalyst_in_hits(news_hits)
            ohlc = _quote_ohlc(q)
            bar_ks = _day_kline_structure(
                open_=ohlc["open"],
                high=ohlc["high"],
                low=ohlc["low"],
                close=ohlc["close"] or px,
                prev_close=ohlc["prev_close"],
                vol_ratio=vol_ratio,
            )
            buy_setup = _detect_buy_setup(
                theme_ok=theme_ok,
                theme_grade=theme_grade,
                stock_grade=stock_grade,
                stock_pct=pct,
                stock_pct_5d=pct5,
                stock_flow=stock_flow,
                stock_flow_5d=stock_flow_5d,
                vol_ratio=vol_ratio,
                turnover=turnover,
                mild_flow_days=mild_flow_days,
                major_catalyst=major_cat,
                chase_reasons=chase_reasons,
                open_=ohlc["open"],
                high=ohlc["high"],
                low=ohlc["low"],
                close=ohlc["close"] or px,
                prev_close=ohlc["prev_close"],
                bar_struct=bar_ks,
            )
            # 热板时再扫短线形态，用于「买入方法」展示（评分仍用 buy_setup）
            method_setup = buy_setup
            t1_setup = _detect_t1_short_setup(
                news_hits=len(news_hits),
                board_pct=board_pct,
                pct=pct,
                pct5=pct5,
                flow=stock_flow,
                flow5=stock_flow_5d,
                vol_ratio=vol_ratio,
                open_=ohlc["open"],
                high=ohlc["high"],
                low=ohlc["low"],
                close=ohlc["close"] or px,
                prev_close=ohlc["prev_close"],
                bar_struct=bar_ks,
            )
            if t1_setup.get("buy_ok"):
                method_setup = t1_setup
            (
                wave_stars,
                buy_stars,
                stars,
                wave_reasons,
                buy_reasons,
                chase_reasons,
            ) = _score_stock(
                theme=theme,
                consecutive=consecutive,
                news_hits=len(news_hits),
                board_flow=board_flow,
                board_pct=board_pct,
                board_pct5=board_pct5,
                stock_pct=pct,
                stock_pct_5d=pct5,
                stock_flow=stock_flow,
                stock_flow_5d=stock_flow_5d,
                day_ok=day_ok,
                theme_ok=theme_ok,
                stock_grade=stock_grade,
                star_delta=stock_star_delta,
                theme_grade=theme_grade,
                theme_star_delta=theme_star_delta,
                mild_flow_days=mild_flow_days,
                buy_setup=buy_setup,
            )
            score_reasons = [
                "【主线星】" + "；".join(wave_reasons),
                "【买点星】" + "；".join(buy_reasons),
            ]
            next_bias, next_bias_disp, next_bias_why = _score_next_move_bias(
                news_hits=len(news_hits),
                major_catalyst=major_cat,
                theme_ok=theme_ok,
                theme_grade=theme_grade,
                board_flow=board_flow,
                board_pct=board_pct,
                board_pct5=board_pct5,
                stock_pct=pct,
                stock_pct_5d=pct5,
                stock_flow=stock_flow,
                stock_flow_5d=stock_flow_5d,
            )
            # 买入候选：短线波段优先——有明确买点即可；主线星≥2（勿死等中军5星回踩）
            _short_kinds = {
                "mild_inflow_run",
                "stabilize_up",
                "catalyst_grind",
                "true_pullback",
                "t1_catalyst_up",
                "t1_hot_continue",
                "t1_resume",
                "t1_dip_hold",
            }
            _is_short_setup = str(buy_setup.get("kind") or "") in _short_kinds or str(
                method_setup.get("kind") or ""
            ) in _short_kinds
            risk = _score_buy_risk_for_code(
                code,
                asof,
                history=history,
                pct=pct,
                pct5=pct5,
                flow=stock_flow,
                flow5=stock_flow_5d,
                vol_ratio=vol_ratio,
                mild_up_days=mild_flow_days,
                news_hits=_news_hits_for_stock(code, name, news_hits),
                theme_grade=theme_grade,
            )
            risk_score = float(risk.get("风险值") or 0)
            buy_ready = (
                theme_ok
                and not chase_reasons
                and int(wave_stars) >= (2 if _is_short_setup else 3)
                and int(buy_stars) >= 3
                and (
                    bool(buy_setup.get("buy_ok")) or bool(method_setup.get("buy_ok"))
                )
                and stock_grade in ("走强", "偏好", "偏弱")
                and (pct is None or float(pct) < 7.0)
                and risk_score >= _RISK_BUY_FLOOR
                and (stock_flow is None or float(stock_flow) >= -0.25)
            )
            # 硬风险：仍可留在观察池，但候选必须否、方法留空
            if risk_score < _RISK_HARD_REJECT:
                buy_ready = False

            buy_range, buy_action = _suggest_buy_plan(
                px,
                pct,
                pct5,
                stock_flow=stock_flow,
                theme_grade=theme_grade,
                stock_grade=stock_grade,
                stars=buy_stars,
                chase_penalties=chase_reasons,
                buy_setup=buy_setup if buy_setup.get("buy_ok") else method_setup,
            )
            # 候选=是：强制贴价可成交区间；候选=否：买入方法留空
            buy_method = ""
            if buy_ready:
                if px is not None and float(px) > 0:
                    buy_range = _near_price_buy_band(float(px))
                buy_method = _resolve_buy_method(
                    method_setup if method_setup.get("buy_ok") else buy_setup,
                    pct=pct,
                    news_hits=len(news_hits),
                )
                if not buy_method:
                    buy_method = str(
                        (method_setup or buy_setup).get("label") or "C热板连涨"
                    )
                if "短持" not in str(buy_action):
                    buy_action = f"{buy_action}；短持1～3天见好就收"
                if risk_score < 0:
                    buy_action = f"风险{risk_score:.0f}偏高，仓位更小；{buy_action}"
            evidence = []
            evidence.append(f"【主题】{theme['name']}：{theme.get('thesis')}")
            evidence.append(f"【入池】{'；'.join(enter_why)}")
            evidence.append(
                f"【主题星级】{_stars_glyph(theme_stars)}（{'；'.join(theme_score_why)}）"
            )
            if board_name:
                evidence.append(
                    f"【板块】{board_name} 涨跌{board_pct if board_pct is not None else '-'}% "
                    f"主力净流入{board_flow if board_flow is not None else '-'}亿"
                )
            if stock_flow is not None:
                evidence.append(f"【个股资金】主力净流入{stock_flow:.2f}亿")
            evidence.append(
                f"【风险值】{risk_score:.1f}（{risk.get('风险档')}）："
                f"{risk.get('风险说明')}"
                )
            if news_hits:
                evidence.append("【新闻】" + "；".join(news_hits[:3]))
            if theme_grade in ("偏弱", "走弱"):
                evidence.append(
                    f"【主题{theme_grade}】{weak_why}"
                    f"（已连续不好{bad_before}日；仅中期逻辑破坏才整主题撤，"
                    f"单日/两日走弱不撤）"
                )
            if stock_grade == "走强":
                evidence.append(
                    f"【走强】连入天数={consecutive}（较前日走强{good_before}日 +1）"
                )
            elif stock_grade in ("偏好", "偏弱"):
                evidence.append(
                    f"【{stock_grade}】{grade_why}；连入冻结为{consecutive}；不加星不减星"
                )
            else:
                evidence.append(
                    f"【走弱】{grade_why}；连入冻结为{consecutive}；"
                    f"{kick_why or '选股期不踢，留观察起稳/回踩'}"
                )
            if chase_reasons:
                evidence.append("【追高/假强】" + "；".join(chase_reasons))
            if buy_setup.get("kind") and buy_setup.get("kind") != "none":
                evidence.append(
                    f"【买点形态】{buy_setup.get('label')}："
                    f"{buy_setup.get('why') or buy_setup.get('kind')}"
                )
            if buy_setup.get("kline"):
                evidence.append(f"【今日K线】{buy_setup.get('kline')}")
            if buy_method:
                evidence.append(
                    f"【买入方法】{buy_method}（候选=是才给；贴价可成交，多方法任一即可）"
                )
            evidence.append("【评分】" + "；".join(score_reasons))
            evidence.append(
                f"【双星】主线{_stars_glyph(wave_stars)} / 买点{_stars_glyph(buy_stars)}"
            )
            evidence.append(
                f"【涨跌概率】{next_bias_disp}（{'；'.join(next_bias_why[:8])}）"
            )
            evidence.append(f"【建议买入】{buy_range}")
            evidence.append(f"【操作建议】{buy_action}")
            sig_color, sig_label, sig_why = _buy_signal_tier(
                theme_ok=theme_ok,
                theme_grade=theme_grade,
                stock_grade=stock_grade,
                stars=buy_stars,
                consecutive=consecutive,
                stock_pct=pct,
                stock_pct_5d=pct5,
                stock_flow=stock_flow,
                stock_flow_5d=stock_flow_5d,
                vol_ratio=vol_ratio,
                turnover=turnover,
                mild_flow_days=mild_flow_days,
                chase_reasons=chase_reasons,
                buy_ready=buy_ready,
                buy_action=buy_action,
                buy_setup=buy_setup if buy_setup.get("buy_ok") else method_setup,
            )
            evidence.append(f"【信号】{sig_color}/{sig_label}：{sig_why}")
            if buy_ready:
                evidence.append(
                    "【升级】满足买入候选（短线形态主线星≥2/否则≥3，且买点星≥3；"
                    "短持1～3天，勿改压舱长持）"
                )

            stock_rows.append(
                {
                    "主线星": wave_stars,
                    "主线星显示": _stars_glyph(wave_stars),
                    "买点星": buy_stars,
                    "买点星显示": _stars_glyph(buy_stars),
                    "涨跌概率": next_bias,
                    "涨跌概率显示": next_bias_disp,
                    "星级": buy_stars,
                    "星级显示": _stars_glyph(buy_stars),
                    "连入天数": consecutive,
                    "当日状态": stock_grade,
                    "买入候选": "是" if buy_ready else "否",
                    "信号色": sig_color,
                    "信号": sig_label,
                    "信号说明": sig_why,
                    "操作建议": buy_action,
                    "买点形态": str(
                        (method_setup if method_setup.get("buy_ok") else buy_setup).get(
                            "label"
                        )
                        or buy_setup.get("label")
                        or ""
                    ),
                    "买入方法": buy_method,
                    "风险值": risk_score,
                    "风险档": risk.get("风险档"),
                    "风险说明": risk.get("风险说明"),
                    "概念": str(theme.get("_concept") or theme["name"]),
                    "行业": str(theme.get("_industry") or board_name or "-"),
                    "板块主题": str(theme.get("_concept") or theme["name"]),
                    "代码": code,
                    "名称": name or str(q.get("名称") or code),
                    "最新价": px,
                    "建议买入": buy_range,
                    "涨跌幅%": pct,
                    "5日涨跌%": pct5,
                    "主力净流入亿": stock_flow,
                    "匹配板块": str(
                        theme.get("_em_industry")
                        or theme.get("_matched_board")
                        or board_name
                        or "-"
                    ),
                    "依据摘要": (
                        f"主线{_stars_glyph(wave_stars)}买点{_stars_glyph(buy_stars)}；"
                        f"概率{next_bias_disp}；{sig_color}{sig_label}；{buy_action[:16]}"
                    )[:100],
                    "详细依据": "\n".join(evidence),
                }
            )
            codes_today.append(code)
            added_here += 1

    # 同股多主题：保留更优信号，其次更高买点星/主线星
    if stock_rows:
        _sig_rank = {"红": 0, "橙": 1, "黄": 2, "绿": 3}
        best: Dict[str, Dict[str, Any]] = {}
        for row in stock_rows:
            c = row["代码"]
            if c not in best:
                best[c] = row
                continue
            old = best[c]
            nr = _sig_rank.get(str(row.get("信号色") or "绿"), 9)
            or_ = _sig_rank.get(str(old.get("信号色") or "绿"), 9)
            better = nr < or_ or (
                nr == or_
                and (
                    int(row.get("买点星") or row["星级"])
                    > int(old.get("买点星") or old["星级"])
                    or (
                        int(row.get("买点星") or 0) == int(old.get("买点星") or 0)
                        and int(row.get("主线星") or 0)
                        > int(old.get("主线星") or 0)
                    )
                )
            )
            if better:
                best[c] = row
        stock_rows = sorted(
            best.values(),
            key=lambda r: (
                _sig_rank.get(str(r.get("信号色") or "绿"), 9),
                0 if r["买入候选"] == "是" else 1,
                -int(r.get("主线星") or 0),
                -int(r.get("买点星") or r.get("星级") or 0),
                -int(r["连入天数"]),
            ),
        )
        # 硬滤永久排除票（防旧种子/成分扫描漏网）
        stock_rows = [
            r
            for r in stock_rows
            if str(r.get("代码") or "").zfill(6)[-6:] not in _OBSERVE_EXCLUDE_CODES
        ]
        codes_today = [r["代码"] for r in stock_rows]
        status_today = {
            r["代码"]: {
                "ok": r.get("当日状态") == "走强",
                "grade": str(r.get("当日状态") or ""),
                "bad": r.get("当日状态") == "走弱",
                "why": str(r.get("依据摘要") or ""),
                "consecutive_good": int(r.get("连入天数") or 0),
                "signal": str(r.get("信号色") or ""),
                "pct": r.get("涨跌幅%"),
                "flow": r.get("主力净流入亿"),
            }
            for r in stock_rows
        }

    # 今日短线：新闻+资金，填主线空窗（与主线池独立）
    _prog(88, "生成今日短线周转池…")
    daily_shorts: List[Dict[str, Any]] = []
    try:
        daily_shorts = build_daily_short_picks(
            boards,
            news,
            limit=6,
            exclude_codes=set(),
        )
    except Exception as exc:  # noqa: BLE001
        err_parts.append(f"今日短线:{exc}")

    payload = {
        "asof": asof,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "pipeline": PIPELINE_VERSION,
        "errors": "; ".join(err_parts),
        "weekly_news": weekly_news,
        "themes": theme_rows,
        "stocks": stock_rows,
        "daily_shorts": daily_shorts,
        "kicked": kicked_notes[:40],
        "frozen": frozen_notes[:40],
        "note": (
            "定位：短线周转赚钱——哪个热板在走跟哪个，持有1～3天见好就收；"
            "中军也多是涨几天跌几天，勿空仓干等大回踩。"
            "进池严：板块须约1～2月前瞻看好（观察不过来，宁缺毋滥）；"
            "在池粘：不因个股连跌/单日走坏踢，仅主题中期逻辑破坏才整主题撤。"
            "选股对象：微跌、止跌起稳、上涨回踩、温和上涨；个股走坏止损属持有期。"
            "买点看K线：开高低收+量比，区分真回踩 vs 高开低走/冲高回落，禁止只看收盘涨跌幅。"
            "股价≥800不进观察/短线池（一手成本过高）；种子名单不列高价票，跳过时不占主题名额；配置种子全量刷、不截前几只。"
            "算电-算拆池：国产服务器（紫光/浪潮/锐捷）≠海外组装（富联）≠液冷散热（英维克等）。"
            "多元主题分散：贵金属/医药/电力/航天/军工/光伏/小金属/AI应用/汽车/科技等1～2月看好的都留；"
            "科技硬件发现排序有上限；地产/教育等禁区与同质金融板不进。"
            "银行/证券/煤炭等同质板须新闻+资金才进，观察最多2～3只；"
            "PCB/材料/贵金属等保位主题不被同质板挤出。"
            "双星：主线星=贴合热主线；买点星=时机。"
            "买入候选：短线形态主线星≥2且买点星≥3；否则主线星≥3。新进封顶3星。"
            "风险值∈[-100,100]：越负越不宜买。特变压舱不进短线池。"
        ),
    }

    if persist:
        history.setdefault("days", {})
        history["days"][asof] = {
            "codes": codes_today,
            "status": status_today,
            "theme_status": theme_status_today,
            "updated_at": payload["updated_at"],
            "theme_names": [t.get("概念") or t["板块主题"] for t in theme_rows],
            "theme_industries": [t.get("行业") or t.get("匹配板块") for t in theme_rows],
            "theme_ids": [str(t.get("主题ID") or "") for t in theme_rows],
        }
        keep_days = sorted(history["days"].keys())[-60:]
        history["days"] = {d: history["days"][d] for d in keep_days}
        _save_history(history)
        _save_latest(payload)

    _prog(100, "前瞻分析完成")
    set_board_fetch_fast(False)
    return payload


def forward_watch_to_frames(payload: Optional[Dict[str, Any]] = None) -> Tuple[pd.DataFrame, pd.DataFrame]:
    payload = payload or load_latest_forward_watch()
    themes = pd.DataFrame(payload.get("themes") or [])
    stocks = pd.DataFrame(payload.get("stocks") or [])
    return themes, stocks
