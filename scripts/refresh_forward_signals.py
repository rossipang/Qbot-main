# -*- coding: utf-8 -*-
"""用最新行情重算前瞻观察信号色，写回 forward_watch_latest.json（不全量拉新闻）。"""
from __future__ import annotations

import re
from datetime import datetime

from qbot.data.forward_watch import (
    LATEST_PATH,
    _buy_signal_tier,
    _load_history,
    _mild_up_flow_streak,
    _save_latest,
    _today,
    load_latest_forward_watch,
)
from qbot.data.industry_screener import _fetch_ulist_quote_map


def _f(x):
    try:
        if x is None or str(x) in ("", "nan", "None"):
            return None
        return float(x)
    except Exception:
        return None


def main() -> None:
    p = load_latest_forward_watch() or {}
    stocks = list(p.get("stocks") or [])
    themes = list(p.get("themes") or [])
    theme_by_name = {str(t.get("板块主题") or ""): t for t in themes}
    codes = [str(r.get("代码") or "") for r in stocks if r.get("代码")]
    qm = _fetch_ulist_quote_map(codes)
    hist = _load_history()
    asof = _today()

    changed = []
    for r in stocks:
        code = str(r.get("代码") or "")
        q = qm.get(code) or {}
        pct = _f(q.get("涨跌幅"))
        if pct is None:
            pct = _f(r.get("涨跌幅%"))
        pct5 = _f(q.get("涨跌幅_5日"))
        if pct5 is None:
            pct5 = _f(r.get("5日涨跌%"))
        flow = _f(q.get("主力净流入_亿"))
        if flow is None:
            flow = _f(r.get("主力净流入亿"))
        flow5 = _f(q.get("主力净流入_5日_亿"))
        vr = _f(q.get("量比"))
        turn = _f(q.get("换手率"))
        px = _f(q.get("最新价"))
        if px is None:
            px = _f(r.get("最新价"))

        th = theme_by_name.get(str(r.get("板块主题") or ""), {})
        tg_raw = str(th.get("状态") or "")
        # 界面状态可能是「走弱观察」，映射回分档
        if "走弱" in tg_raw:
            tg = "走弱"
        elif "偏弱" in tg_raw:
            tg = "偏弱"
        elif "走强" in tg_raw:
            tg = "走强"
        elif "偏好" in tg_raw or "观察" in tg_raw:
            tg = "偏好"
        else:
            tg = "偏好"
        theme_ok = tg in ("走强", "偏好")
        stock_grade = str(r.get("当日状态") or "偏弱")
        mild = _mild_up_flow_streak(hist, code, asof, pct or 0.0, flow or 0.0)
        detail = str(r.get("详细依据") or "")
        # 仅认证据段【追高/假强】，避免「未追高」「别追连板」误杀
        chase = []
        for line in detail.splitlines():
            if line.startswith("【追高") or line.startswith("【假强"):
                chase = [line]
                break

        sig_c, sig_l, sig_w = _buy_signal_tier(
            theme_ok=theme_ok,
            theme_grade=tg,
            stock_grade=stock_grade,
            stars=int(r.get("星级") or 0),
            consecutive=int(r.get("连入天数") or 0),
            stock_pct=pct,
            stock_pct_5d=pct5,
            stock_flow=flow,
            stock_flow_5d=flow5,
            vol_ratio=vr,
            turnover=turn,
            mild_flow_days=mild,
            chase_reasons=chase,
            buy_ready=str(r.get("买入候选")) == "是",
            buy_action=str(r.get("操作建议") or ""),
        )
        old = str(r.get("信号色") or "")
        r["信号色"] = sig_c
        r["信号"] = sig_l
        r["信号说明"] = sig_w
        if pct is not None:
            r["涨跌幅%"] = pct
        if pct5 is not None:
            r["5日涨跌%"] = pct5
        if flow is not None:
            r["主力净流入亿"] = flow
        if px is not None:
            r["最新价"] = px
        r["依据摘要"] = (
            f"{sig_c}{sig_l}；{stock_grade}；{str(r.get('操作建议') or '')[:24]}"
        )[:100]
        if "【信号】" in detail:
            r["详细依据"] = re.sub(
                r"【信号】[^\n]*",
                f"【信号】{sig_c}/{sig_l}：{sig_w}",
                detail,
                count=1,
            )
        if old != sig_c or code in ("002594", "600166"):
            changed.append(
                (code, r.get("名称"), old, sig_c, sig_l, pct, pct5, flow, vr, mild)
            )

    _sig_rank = {"红": 0, "橙": 1, "黄": 2, "绿": 3}
    stocks = sorted(
        stocks,
        key=lambda row: (
            _sig_rank.get(str(row.get("信号色") or "绿"), 9),
            0 if row.get("买入候选") == "是" else 1,
            -int(row.get("星级") or 0),
            -int(row.get("连入天数") or 0),
        ),
    )
    p["stocks"] = stocks
    p["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    p["note"] = (
        "颜色按可买形态：红=缩量回踩或连日微涨+资金流入；"
        "橙=已涨未回踩或资金刚进；黄=待观察；绿=不宜买。"
        "颜色不等于星级；信号色才是买卖提示。仅信号/星级列着色。"
    )
    _save_latest(p)
    print("saved", LATEST_PATH)
    print("updated_at", p["updated_at"], "stocks", len(stocks))
    for u in changed:
        if u[0] in ("002594", "600166"):
            print("KEY", u)
    print("changed", len(changed))
    for u in changed[:25]:
        print(u)


if __name__ == "__main__":
    main()
