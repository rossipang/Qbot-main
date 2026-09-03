# -*- coding: utf-8 -*-
"""把主题已走弱但仍标红的票改回绿（不宜买）。"""
import json
import re
from datetime import datetime
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "qbot" / "gui" / "csv" / "forward_watch_latest.json"


def main() -> None:
    p = json.loads(PATH.read_text(encoding="utf-8"))
    theme_status = {
        str(t.get("板块主题") or ""): str(t.get("状态") or "")
        for t in (p.get("themes") or [])
    }
    fixes = []
    for r in p.get("stocks") or []:
        st = theme_status.get(str(r.get("板块主题") or ""), "")
        if "走弱" not in st:
            continue
        if str(r.get("信号色") or "") == "绿":
            continue
        old = r.get("信号色")
        r["信号色"] = "绿"
        r["信号"] = "不宜买"
        r["信号说明"] = "个股或主题走弱"
        r["依据摘要"] = (
            f"绿不宜买；{r.get('当日状态')}；{str(r.get('操作建议') or '')[:24]}"
        )[:100]
        detail = str(r.get("详细依据") or "")
        if "【信号】" in detail:
            r["详细依据"] = re.sub(
                r"【信号】[^\n]*",
                "【信号】绿/不宜买：个股或主题走弱",
                detail,
                count=1,
            )
        fixes.append((r.get("代码"), r.get("名称"), old, "->绿", st))

    rank = {"红": 0, "橙": 1, "黄": 2, "绿": 3}
    p["stocks"] = sorted(
        p.get("stocks") or [],
        key=lambda row: (
            rank.get(str(row.get("信号色") or "绿"), 9),
            0 if row.get("买入候选") == "是" else 1,
            -int(row.get("星级") or 0),
            -int(row.get("连入天数") or 0),
        ),
    )
    p["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    PATH.write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
    print("updated", p["updated_at"])
    for f in fixes:
        print(f)
    for t in p.get("themes") or []:
        if "功率" in str(t.get("板块主题") or ""):
            print(
                "theme",
                t.get("板块主题"),
                t.get("状态"),
                t.get("信号色"),
                "pct",
                t.get("板块涨跌%"),
                "5d",
                t.get("板块5日%"),
                "flow",
                t.get("主力净流入亿"),
            )


if __name__ == "__main__":
    main()
