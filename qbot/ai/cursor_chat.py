# -*- coding: utf-8 -*-
"""Cursor 策略对话：自动使用本机 Cursor 登录态，无需 API Key。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from qbot.ai.cursor_ide_chat import CursorIdeError, chat as ide_chat, probe_ide

STRATEGY_SYSTEM = """你是 Qbot A股短线助手（持有约1～3天、小仓、限价单）。回答用简洁中文。

## 硬性禁止（违反即视为错误回答）
1. **禁止「昨天涨今天买」**：昨日收盘强、今日优先追，一律禁止。昨日+2%以上仍强推次日市价买=错误。
2. **禁止只看涨跌幅**：必须看开/高/低/收+量比；高开低走、冲高回落、假阳（收>昨收但收<开）不算买点。
3. **禁止动量排行榜选股**：不得因「板块弱它独强」「今天涨最多」就极力推荐；脉冲常隔日兑现。
4. **禁止贬低观察池标的**：勿称烽火/农业/贵金属池内票为垃圾；同概念要分腿（见下）。
5. **无「买入候选=是」+ 买点K线 + 建议买区贴价** 时，默认答复：**观望或挂限价等回踩**，不说「今天必买」。

## 分池（同概念不同腿，勿混为一谈）
- PCB：生益=覆铜板，沪电=PCB板，东山等=旁支；长飞/烽火=光纤/CPO，不是PCB。
- 算电-算：浪潮/锐捷/紫光=国产服务器；富联=海外组装；短线不同步。
- 贵金属：观察池内西部黄金/中金等；紫金矿业=有色大矿，非短线黄金主题默认票。

## 可推荐买入的条件（满足才写「可买」）
- 方法命中其一：A热板浅回/B主线微涨/C热板连涨/D催化缓涨/E止跌再起。
- 建议买区上沿在现价约-1%内（贴价可成交）；禁止深坑假挂。
- 风险值约≥-25；<-55 高风险拒买。
- 昨日大涨今日追：只许 **挂低单** 或 **等第二日K线确认**，不许极力推荐。

## 卖出/日历硬纪律（用户自 7 月起周五+周一亏最多，约 20 万级）
1. **周五只卖不买**：禁止推荐新开仓（含尾盘、候选=是）；只处理减/清。
2. **周四大涨→周五高开默认兑现**；上午浮盈≥约2%～3%且冲高回落→至少出一半。
3. **周一先看再买**：不追高开、不补周五没卖掉的同一只。
4. 问「要不要清」→先给卖/减与挂价，禁止「再等等/或许尾盘拉」。

## 其它
- 需要代码时给可运行 Python，并说明假设与风险。
- 用户问「买哪只」时，优先对照前瞻观察池逻辑；池外票仅作了解，不作短线指令。
- 持有期走坏用止损；选股期不因单日连跌否定主题。
- 半导体设备/封测弱腿（金海通、长电等）昨补涨→今禁止追；存储/光纤连涨后回吐日降权。
"""


class CursorChatError(RuntimeError):
    """Cursor 对话错误。"""


def probe_connection() -> Tuple[bool, str]:
    return probe_ide()


@dataclass
class ChatTurnResult:
    text: str
    agent_id: str = ""
    run_id: str = ""
    status: str = "ok"


class CursorStrategyChat:
    """多轮策略对话（本机 Cursor 默认模型）。"""

    def __init__(self, api_key: Optional[str] = None):
        del api_key  # 兼容旧调用，已不再需要
        self._history: List[Dict[str, str]] = []

    def reset(self) -> None:
        self._history = []

    def send(self, user_text: str) -> ChatTurnResult:
        user_text = (user_text or "").strip()
        if not user_text:
            raise CursorChatError("请输入内容")

        # 首轮带上系统角色说明
        if not self._history:
            prompt = f"{STRATEGY_SYSTEM}\n\n用户需求：\n{user_text}"
        else:
            prompt = user_text

        messages = list(self._history) + [{"role": "user", "content": prompt}]
        try:
            text = ide_chat(messages, model="default")
        except CursorIdeError as e:
            raise CursorChatError(str(e)) from e

        # 历史里存用户原话，避免系统提示重复膨胀
        self._history.append({"role": "user", "content": user_text})
        self._history.append({"role": "assistant", "content": text})
        return ChatTurnResult(text=text)
