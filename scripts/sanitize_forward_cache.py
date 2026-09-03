# -*- coding: utf-8 -*-
"""按多因子否决规则清洗旧缓存：走弱/资金撤离/无催化透支的主题及个股移出。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "qbot" / "gui" / "csv" / "forward_watch_latest.json"


def _should_drop_theme(t: dict) -> str:
    name = str(t.get("板块主题") or "")
    st = str(t.get("状态") or "")
    try:
        pct = float(t["板块涨跌%"]) if t.get("板块涨跌%") is not None else None
    except Exception:
        pct = None
    try:
        flow = float(t["主力净流入亿"]) if t.get("主力净流入亿") is not None else None
    except Exception:
        flow = None
    try:
        flow5 = float(t["5日主力净流入亿"]) if t.get("5日主力净流入亿") is not None else None
    except Exception:
        flow5 = None
    news_n = int(t.get("新闻条数") or 0)

    if "走弱" in st:
        return "走弱"
    if pct is not None and pct <= -3:
        return f"当日大跌{pct}"
    if flow is not None and flow <= -3 and (pct is None or pct < 0):
        return f"下跌且资金流出{flow:.1f}亿"
    if flow5 is not None and flow5 <= -8 and (pct is None or pct < 0) and news_n <= 0:
        return f"5日资金撤离{flow5:.1f}亿且无新闻"
    if "功率" in name or name.startswith("供电"):
        if (flow is not None and flow < 0) or (pct is not None and pct < 0):
            return "供电/功率器件走弱资金负"
    return ""


def main() -> None:
    p = json.loads(PATH.read_text(encoding="utf-8"))
    themes = list(p.get("themes") or [])
    stocks = list(p.get("stocks") or [])
    keep_t, kick, weak_names = [], [], set()
    for t in themes:
        why = _should_drop_theme(t)
        if why:
            weak_names.add(str(t.get("板块主题") or ""))
            kick.append(f"{t.get('板块主题')}: {why}")
        else:
            keep_t.append(t)
    keep_s, drop_s = [], []
    for r in stocks:
        th = str(r.get("板块主题") or "")
        if th in weak_names:
            drop_s.append(f"{r.get('名称')}({r.get('代码')}) <- {th}")
        else:
            keep_s.append(r)
    p["themes"] = keep_t
    p["stocks"] = keep_s
    p["kicked"] = (kick + list(p.get("kicked") or []))[:40]
    p["weekly_news"] = p.get("weekly_news") or []
    p["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    p["note"] = (
        "前瞻=多因子预测未来1～2周（新闻/资金/行业/走势交叉确认），"
        "不是涨幅回看榜；明显走弱或资金撤离的不进。"
    )
    PATH.write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
    print("updated", p["updated_at"])
    print("themes", len(themes), "->", len(keep_t))
    print("stocks", len(stocks), "->", len(keep_s))
    for x in kick:
        print(" kick", x)
    for x in drop_s[:15]:
        print(" drop", x)
    print("remain:")
    for t in keep_t:
        print(
            " ",
            t.get("板块主题"),
            t.get("状态"),
            "pct",
            t.get("板块涨跌%"),
            "flow",
            t.get("主力净流入亿"),
            "news",
            t.get("新闻条数"),
        )


if __name__ == "__main__":
    main()
