# -*- coding: utf-8 -*-
import json
import re
from datetime import datetime
from pathlib import Path

from qbot.data.forward_watch import _score_stock, _stars_glyph, _stock_worth_in_theme

PATH = Path(__file__).resolve().parents[1] / "qbot" / "gui" / "csv" / "forward_watch_latest.json"


def main() -> None:
    data = json.loads(PATH.read_text(encoding="utf-8"))
    theme_pri = {
        str(t.get("板块主题")): int(t.get("优先级") or 3)
        for t in (data.get("themes") or [])
    }
    out = []
    kick = list(data.get("kicked") or [])
    for r in data.get("stocks") or []:
        theme_name = str(r.get("板块主题") or "")
        tg = "偏好"
        board_flow = None
        for t in data.get("themes") or []:
            if str(t.get("板块主题")) != theme_name:
                continue
            st = str(t.get("状态") or "")
            if "走弱" in st:
                tg = "走弱"
            elif "偏弱" in st:
                tg = "偏弱"
            elif st == "走强":
                tg = "走强"
            else:
                tg = "偏好"
            try:
                board_flow = float(t.get("主力净流入亿"))
            except Exception:
                board_flow = None
            break
        theme = {
            "id": "",
            "name": theme_name,
            "priority": theme_pri.get(theme_name, 3),
        }
        pct = r.get("涨跌幅%")
        pct5 = r.get("5日涨跌%")
        flow = r.get("主力净流入亿")
        grade = str(r.get("当日状态") or "偏好")
        try:
            pct = float(pct) if pct is not None else None
        except Exception:
            pct = None
        try:
            pct5 = float(pct5) if pct5 is not None else None
        except Exception:
            pct5 = None
        try:
            flow = float(flow) if flow is not None else None
        except Exception:
            flow = None
        worth, worth_why = _stock_worth_in_theme(
            stock_pct=pct,
            stock_pct_5d=pct5,
            stock_flow=flow,
            stock_grade=grade,
            theme_ok=tg != "走弱",
        )
        if not worth:
            kick.insert(
                0, f"{r.get('名称')}({r.get('代码')}): 未入选—{worth_why}"
            )
            print("DROP", r.get("名称"), worth_why)
            continue
        stars, rs, ch = _score_stock(
            theme=theme,
            consecutive=int(r.get("连入天数") or 0),
            news_hits=0,
            board_flow=board_flow,
            board_pct=None,
            stock_pct=pct,
            stock_pct_5d=pct5,
            stock_flow=flow,
            theme_ok=tg != "走弱",
            stock_grade=grade,
            theme_grade=tg,
        )
        r["星级"] = stars
        r["星级显示"] = _stars_glyph(stars)
        detail = str(r.get("详细依据") or "")
        score_line = "【评分】" + "；".join(rs)
        if "【评分】" in detail:
            r["详细依据"] = re.sub(
                r"【评分】[^\n]*", score_line, detail, count=1
            )
        out.append(r)
        print(r.get("名称"), "星", stars, "chase", bool(ch))

    rank = {"红": 0, "橙": 1, "黄": 2, "绿": 3}
    out = sorted(
        out,
        key=lambda row: (
            rank.get(str(row.get("信号色") or "绿"), 9),
            0 if row.get("买入候选") == "是" else 1,
            -int(row.get("星级") or 0),
            -int(row.get("连入天数") or 0),
        ),
    )
    data["stocks"] = out
    data["kicked"] = kick[:40]
    data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print("saved stocks", len(out))


if __name__ == "__main__":
    main()
