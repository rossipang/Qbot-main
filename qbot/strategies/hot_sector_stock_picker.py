#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
热点 + 政策相关选股（过滤亏损）

当前默认主题（2026-07 市况）：
- 稳市/国家队：银行、央企能源
- 迎峰度夏：电力
- 增持催化：煤炭、石油、铝（有色慎追高）
- 医药回暖：创新药龙头（回避纯题材连板）

用法：
  python -m qbot.strategies.hot_sector_stock_picker
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

# 手工维护的「板块热点 × 政策/新闻」候选（可每周改）
HOT_THEMES = {
    "电力_迎峰度夏": ["600011", "600900", "600236", "600795", "601985"],
    "能源_央企增持": ["600938", "601898", "601088", "601857"],
    "银行_稳市权重": ["601398", "601939", "601288", "601166", "600036"],
    "医药_创新药": ["002422", "600276", "688235", "000661"],
}


@dataclass
class PickResult:
    code: str
    name: str
    theme: str
    net_profit: Optional[float]  # 最近报告期归母净利（万元或元，取决于接口）
    ok: bool
    reason: str


def _safe_float(x) -> Optional[float]:
    try:
        if x is None or x == "" or str(x) in ("-", "None", "nan"):
            return None
        return float(str(x).replace(",", ""))
    except Exception:
        return None


def fetch_basic_profit(code: str) -> tuple[Optional[str], Optional[float], str]:
    """
    尝试用 akshare 拉最新业绩快报/年报摘要，判断是否亏损。
    失败时返回 (None, None, 原因)，调用方可仅用主题池手工确认。
    """
    try:
        import akshare as ak
    except ImportError:
        return None, None, "未安装 akshare"

    name = code
    try:
        # 个股信息
        info = ak.stock_individual_info_em(symbol=code)
        if info is not None and not info.empty:
            m = dict(zip(info.iloc[:, 0].astype(str), info.iloc[:, 1]))
            name = str(m.get("股票简称", name))
    except Exception:
        pass

    # 优先：业绩快报
    profit = None
    try:
        df = ak.stock_yjkb_em(date="")  # 部分版本需要具体日期
    except Exception:
        df = None

    # 退而用财务分析指标：净利润
    try:
        fin = ak.stock_financial_analysis_indicator(symbol=code)
        if fin is not None and not fin.empty:
            # 常见列名：净利润
            col = None
            for c in fin.columns:
                if "净利润" in str(c) and "扣非" not in str(c):
                    col = c
                    break
            if col:
                profit = _safe_float(fin.iloc[0][col])
    except Exception as e:
        return name, None, f"财务拉取失败:{e}"

    if profit is None:
        return name, None, "未取到净利润，请人工确认是否盈利"
    if profit < 0:
        return name, profit, "净利润为负，过滤"
    return name, profit, "净利润为正"


def screen_pool(
    themes: Optional[dict] = None,
    max_per_theme: int = 3,
    require_profit: bool = True,
) -> List[PickResult]:
    themes = themes or HOT_THEMES
    out: List[PickResult] = []
    for theme, codes in themes.items():
        n = 0
        for code in codes:
            if n >= max_per_theme:
                break
            name, profit, reason = fetch_basic_profit(code)
            ok = True
            if require_profit:
                if profit is None:
                    # 数据不全时默认保留但标记，避免空仓；也可改成 ok=False
                    ok = True
                    reason = reason + "（未严格过滤）"
                else:
                    ok = profit >= 0
            out.append(
                PickResult(
                    code=code,
                    name=name or code,
                    theme=theme,
                    net_profit=profit,
                    ok=ok,
                    reason=reason,
                )
            )
            if ok:
                n += 1
    return out


def print_picks(results: List[PickResult]) -> None:
    print("=" * 72)
    print("热点选股结果（请再结合新闻/公告确认）")
    print("=" * 72)
    for r in results:
        flag = "✓" if r.ok else "✗"
        profit_s = f"{r.net_profit:.2f}" if r.net_profit is not None else "N/A"
        print(
            f"{flag} {r.code} {r.name:8s} | {r.theme:12s} | "
            f"净利={profit_s:>12s} | {r.reason}"
        )
    buyable = [r for r in results if r.ok]
    print("-" * 72)
    print("建议关注代码:", ", ".join(r.code for r in buyable) or "(无)")


if __name__ == "__main__":
    print_picks(screen_pool())
