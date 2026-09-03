# -*- coding: utf-8 -*-
"""按新规则清洗缓存：弱主题（偏弱/走弱）及其个股移出观察池。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "qbot" / "gui" / "csv" / "forward_watch_latest.json"


def _is_weak_theme(t: dict) -> bool:
    st = str(t.get("状态") or "")
    sig = str(t.get("信号色") or "")
    grade_hint = str(t.get("信号") or "") + st
    if any(x in st for x in ("偏弱", "走弱")):
        return True
    if sig == "绿" and ("走弱" in grade_hint or "偏弱" in grade_hint):
        return True
    pct = t.get("板块涨跌%")
    try:
        if pct is not None and float(pct) < 0:
            return True
    except Exception:
        pass
    return False


def main() -> None:
    p = json.loads(PATH.read_text(encoding="utf-8"))
    themes = list(p.get("themes") or [])
    stocks = list(p.get("stocks") or [])
    keep_themes = []
    kick = []
    weak_names = set()
    for t in themes:
        name = str(t.get("板块主题") or "")
        if _is_weak_theme(t):
            weak_names.add(name)
            kick.append(f"{name}: 弱板块不进观察（状态={t.get('状态')} 涨跌={t.get('板块涨跌%')}）")
        else:
            keep_themes.append(t)

    keep_stocks = []
    dropped_stocks = []
    for r in stocks:
        theme = str(r.get("板块主题") or "")
        if theme in weak_names:
            dropped_stocks.append(f"{r.get('名称')}({r.get('代码')}) <- {theme}")
            continue
        keep_stocks.append(r)

    p["themes"] = keep_themes
    p["stocks"] = keep_stocks
    old_kick = list(p.get("kicked") or [])
    p["kicked"] = (kick + old_kick)[:40]
    p["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    p["note"] = (
        "流程：先筛近1～2周大概率走强的板块（偏弱/走弱不进不留），"
        "再从中挑代表股，再看星级与信号色。"
        "红=缩量回踩或连日微涨+资金流入；橙=已涨未回踩或资金刚进；"
        "黄=待观察；绿=不宜买。颜色不等于星级。"
    )
    PATH.write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
    print("updated", p["updated_at"])
    print("themes", len(themes), "->", len(keep_themes))
    print("stocks", len(stocks), "->", len(keep_stocks))
    print("kicked themes:")
    for x in kick:
        print(" ", x)
    print("dropped stocks:")
    for x in dropped_stocks:
        print(" ", x)
    print("remain themes:")
    for t in keep_themes:
        print(
            " ",
            t.get("信号色"),
            t.get("状态"),
            t.get("板块主题"),
            "pct",
            t.get("板块涨跌%"),
            "5d",
            t.get("板块5日%"),
        )


if __name__ == "__main__":
    main()
