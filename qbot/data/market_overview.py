# -*- coding: utf-8 -*-
"""大盘首页（同花顺风格）：四指数横条 + 左分时 + 右涨跌幅排行。"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import pandas as pd
import requests

from qbot.data.intraday import fetch_intraday_bundle, is_cn_trading_session

_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://quote.eastmoney.com/",
}

SH_CODE = "000001.SH"
UP = "#e93030"
DOWN = "#00a000"
FLAT = "#333333"
HALF_LABELS = (
    "09:30",
    "10:00",
    "10:30",
    "11:00",
    "11:30",
    "13:00",
    "13:30",
    "14:00",
    "14:30",
    "15:00",
)


def _session() -> requests.Session:
    s = requests.Session()
    # 绕过系统代理，避免东财被代理掐断（ConnectionReset 10054）
    s.trust_env = False
    s.headers.update(_UA)
    return s


def _fmt_num(v, nd: int = 2, empty: str = "-") -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return empty
    try:
        return f"{float(v):,.{nd}f}"
    except (TypeError, ValueError):
        return empty


def _fmt_pct(v, empty: str = "-") -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return empty
    try:
        x = float(v)
    except (TypeError, ValueError):
        return empty
    s = f"{x:.2f}"
    return f"+{s}%" if x > 0 else f"{s}%"


def _fmt_yi(amount) -> str:
    if amount is None or amount == "" or amount == "-":
        return "-"
    try:
        v = float(amount)
    except (TypeError, ValueError):
        return "-"
    if abs(v) >= 1e12:
        return f"{v / 1e12:.2f}万亿"
    if abs(v) >= 1e8:
        return f"{v / 1e8:.0f}亿"
    return f"{v:,.0f}"


def _sign(v) -> str:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "flat"
    if x > 0:
        return "up"
    if x < 0:
        return "down"
    return "flat"


def _esc(text: Any) -> str:
    s = "" if text is None else str(text)
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def fetch_index_snapshots() -> Dict[str, Dict[str, Any]]:
    """上证 / 深证 / 创业板 / 科创50。"""
    sess = _session()
    r = sess.get(
        "https://push2delay.eastmoney.com/api/qt/ulist.np/get",
        params={
            "fltt": "2",
            "invt": "2",
            "fields": "f12,f14,f2,f3,f6,f104,f105,f106,f15,f16,f17,f18",
            "secids": "1.000001,0.399001,0.399006,1.000688",
        },
        timeout=12,
    )
    out: Dict[str, Dict[str, Any]] = {}
    for item in ((r.json() or {}).get("data") or {}).get("diff") or []:
        code = str(item.get("f12") or "")
        out[code] = {
            "code": code,
            "name": item.get("f14") or "",
            "price": item.get("f2"),
            "pct": item.get("f3"),
            "amount": item.get("f6"),
            "up": item.get("f104"),
            "down": item.get("f105"),
            "flat": item.get("f106"),
            "high": item.get("f15"),
            "low": item.get("f16"),
            "open": item.get("f17"),
            "pre_close": item.get("f18"),
        }
    return out


def fetch_market_breadth() -> Dict[str, Any]:
    sess = _session()
    r = sess.get(
        "https://push2ex.eastmoney.com/getTopicZDFenBu",
        params={
            "ut": "7eea3edcaed734bea9cbfc24409ed989",
            "dpt": "wz.ztzt",
            "_": int(time.time() * 1000),
        },
        timeout=12,
    )
    data = (r.json() or {}).get("data") or {}
    up = down = flat = 0
    for row in data.get("fenbu") or []:
        if not isinstance(row, dict):
            continue
        for k, v in row.items():
            try:
                key, n = int(k), int(v or 0)
            except (TypeError, ValueError):
                continue
            if key > 0:
                up += n
            elif key < 0:
                down += n
            else:
                flat += n
    qdate = data.get("qdate")
    date_s = ""
    if qdate:
        s = str(qdate)
        if len(s) == 8:
            date_s = f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return {"up": up, "down": down, "flat": flat, "date": date_s}


def fetch_industry_movers(n: int = 5) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    行业涨幅/跌幅前 n。
    多镜像重试，避免 ConnectionReset；失败再试概念板块。
    """
    bases = (
        "https://push2delay.eastmoney.com/api/qt/clist/get",
        "https://push2delay.eastmoney.com/api/qt/clist/get",  # 再试一次 delay
        "https://push2.eastmoney.com/api/qt/clist/get",
        "https://82.push2.eastmoney.com/api/qt/clist/get",
    )

    def _parse(diff: list) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for item in diff or []:
            pct = item.get("f3")
            try:
                pct_f = float(pct) if pct not in (None, "-", "") else None
            except (TypeError, ValueError):
                pct_f = None
            if pct_f is None:
                continue
            flow = item.get("f62")
            try:
                flow_yi = float(flow) / 1e8 if flow not in (None, "-", "") else None
            except (TypeError, ValueError):
                flow_yi = None
            rows.append(
                {
                    "name": item.get("f14") or "",
                    "pct": pct_f,
                    "up": item.get("f104"),
                    "down": item.get("f105"),
                    "flow_yi": flow_yi,
                    "leader": item.get("f128") or "",
                }
            )
        return rows

    def _one_page(po: str, fs: str) -> List[Dict[str, Any]]:
        last_err: Optional[Exception] = None
        sess = _session()
        for base in bases:
            try:
                r = sess.get(
                    base,
                    params={
                        "pn": "1",
                        "pz": str(max(n + 10, 30)),
                        "po": po,
                        "np": "1",
                        "fltt": "2",
                        "invt": "2",
                        "fid": "f3",
                        "fs": fs,
                        "fields": "f12,f14,f3,f104,f105,f62,f128,f136",
                        "_": int(time.time() * 1000),
                    },
                    timeout=18,
                )
                r.raise_for_status()
                diff = ((r.json() or {}).get("data") or {}).get("diff") or []
                rows = _parse(diff)
                if rows:
                    return rows
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                time.sleep(0.35)
                continue
        if last_err:
            raise RuntimeError(str(last_err))
        return []

    # 行业优先；不行再概念
    fs_list = ("m:90 t:2 f:!50", "m:90 t:3 f:!50")
    gainers: List[Dict[str, Any]] = []
    losers: List[Dict[str, Any]] = []
    last_exc: Optional[Exception] = None
    for fs in fs_list:
        try:
            gainers = _one_page("1", fs)
            time.sleep(0.2)
            losers = _one_page("0", fs)
            if gainers and losers:
                break
            # 若只拿到一侧，再试下一 fs
            if gainers or losers:
                break
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            continue

    if not gainers and not losers and last_exc:
        raise RuntimeError(str(last_exc))

    # 若只拿到降序一页，本地拆涨跌
    if gainers and not losers:
        all_rows = list(gainers)
        all_rows.sort(key=lambda x: x["pct"], reverse=True)
        gainers = all_rows[:n]
        losers = list(reversed(all_rows[-n:])) if len(all_rows) >= n else list(reversed(all_rows))
        losers.sort(key=lambda x: x["pct"])
    else:
        gainers.sort(key=lambda x: x["pct"], reverse=True)
        losers.sort(key=lambda x: x["pct"])
        gainers, losers = gainers[:n], losers[:n]

    return gainers, losers


def build_market_overview_payload() -> Dict[str, Any]:
    errors: List[str] = []
    indices: Dict[str, Dict[str, Any]] = {}
    breadth: Dict[str, Any] = {}
    top: List[Dict[str, Any]] = []
    bottom: List[Dict[str, Any]] = []
    bundle: Dict[str, Any] = {}

    try:
        indices = fetch_index_snapshots()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"指数: {exc}")
    try:
        breadth = fetch_market_breadth()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"涨跌家数: {exc}")
    try:
        top, bottom = fetch_industry_movers(5)
    except Exception:  # noqa: BLE001
        top, bottom = [], []
    if not top or not bottom:
        # 再补一次概念兜底（fetch 内部已试过，这里防止空列表）
        if not top and not bottom:
            errors.append("行业涨跌幅暂时获取失败，请点「刷新大盘」重试")
    try:
        bundle = fetch_intraday_bundle(SH_CODE)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"分时: {exc}")

    sh = indices.get("000001") or {}
    sz = indices.get("399001") or {}
    try:
        total_amt = float(sh.get("amount") or 0) + float(sz.get("amount") or 0)
        if total_amt == 0:
            total_amt = None
    except (TypeError, ValueError):
        total_amt = None

    up, down, flat = breadth.get("up"), breadth.get("down"), breadth.get("flat")
    if up is None:
        try:
            up = int(sh.get("up") or 0) + int(sz.get("up") or 0)
            down = int(sh.get("down") or 0) + int(sz.get("down") or 0)
            flat = int(sh.get("flat") or 0) + int(sz.get("flat") or 0)
        except (TypeError, ValueError):
            pass

    return {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "trading": is_cn_trading_session(),
        "indices": {
            "sh": sh,
            "sz": sz,
            "cyb": indices.get("399006") or {},
            "kcb": indices.get("000688") or {},
        },
        "amount_sh": sh.get("amount"),
        "amount_sz": sz.get("amount"),
        "amount_total": total_amt,
        "up": up,
        "down": down,
        "flat": flat,
        "breadth_date": breadth.get("date") or "",
        "top_boards": top,
        "bottom_boards": bottom,
        "intraday": bundle,
        "errors": errors,
    }


def _idx_item(label: str, snap: Dict[str, Any]) -> str:
    pct = snap.get("pct")
    cls = _sign(pct)
    return (
        f'<div class="idx {cls}">'
        f'<div class="idx-n">{_esc(label)}</div>'
        f'<div class="idx-p">{_fmt_num(snap.get("price"))}</div>'
        f'<div class="idx-c">{_fmt_pct(pct)}</div>'
        f"</div>"
    )


def _board_bars(rows: List[Dict[str, Any]], title: str, kind: str) -> str:
    """横向柱状图：涨红跌绿。"""
    color = UP if kind == "up" else DOWN
    if not rows:
        body = '<div class="bar-empty">暂无数据，请刷新大盘</div>'
    else:
        max_abs = max(abs(float(r.get("pct") or 0)) for r in rows) or 1.0
        parts = []
        for i, r in enumerate(rows, 1):
            pct = float(r.get("pct") or 0)
            width = max(4.0, min(100.0, abs(pct) / max_abs * 100.0))
            leader = r.get("leader") or "-"
            parts.append(
                f'<div class="bar-row">'
                f'<span class="bar-i">{i}</span>'
                f'<span class="bar-n" title="{_esc(r.get("name"))}">{_esc(r.get("name"))}</span>'
                f'<div class="bar-track">'
                f'<div class="bar-fill" style="width:{width:.1f}%;background:{color}"></div>'
                f"</div>"
                f'<span class="bar-p" style="color:{color}">{_fmt_pct(pct)}</span>'
                f'<span class="bar-l" title="{_esc(leader)}">{_esc(leader)}</span>'
                f"</div>"
            )
        body = "".join(parts)
    return (
        f'<div class="board {kind}">'
        f'<div class="board-h" style="color:{color};border-color:{color}">{_esc(title)}</div>'
        f'{body}</div>'
    )



def _is_half_label(lab: str) -> bool:
    """是否为半点/整点刻度（同花顺分时横轴）。"""
    if lab in HALF_LABELS:
        return True
    if len(lab) >= 5 and lab[2] == ":" and lab[3:5] in ("00", "30"):
        return lab[:2] in ("09", "10", "11", "13", "14", "15")
    return False


def _svg_intraday(trends: pd.DataFrame, pre_close: Optional[float]) -> str:
    """同花顺风格分时+量能 SVG，横轴只标半点。"""
    if trends is None or trends.empty:
        return '<div class="no-chart">暂无分时数据</div>'

    labels = trends["time_label"].astype(str).tolist()
    prices = trends["price"].astype(float).tolist()
    avgs = trends["avg"].astype(float).tolist()
    vols = trends["volume"].astype(float).tolist()
    n = len(prices)
    if n < 2:
        return '<div class="no-chart">分时点过少</div>'

    w, h_price, h_vol, gap = 720, 280, 90, 8
    pad_l, pad_r, pad_t, pad_b = 56, 16, 12, 28
    plot_w = w - pad_l - pad_r
    plot_h = h_price - pad_t - pad_b

    ymin = min(prices + avgs)
    ymax = max(prices + avgs)
    pre: Optional[float] = None
    if pre_close is not None:
        try:
            pre = float(pre_close)
            span = max(abs(ymax - pre), abs(ymin - pre), 1.0)
            ymin, ymax = pre - span * 1.05, pre + span * 1.05
        except (TypeError, ValueError):
            pre = None
    if pre is None:
        pad = (ymax - ymin) * 0.08 or 1.0
        ymin, ymax = ymin - pad, ymax + pad

    def x_at(i: int) -> float:
        return pad_l + (i / (n - 1)) * plot_w

    def y_at(p: float) -> float:
        return pad_t + (ymax - p) / (ymax - ymin) * plot_h

    price_pts = " ".join(f"{x_at(i):.1f},{y_at(p):.1f}" for i, p in enumerate(prices))
    avg_pts = " ".join(f"{x_at(i):.1f},{y_at(a):.1f}" for i, a in enumerate(avgs))

    # 半点刻度：每个半点只取第一次出现，避免密密麻麻
    tick_svg: List[str] = []
    seen = set()
    for i, lab in enumerate(labels):
        if not _is_half_label(lab) or lab in seen:
            continue
        seen.add(lab)
        xx = x_at(i)
        tick_svg.append(
            f'<line x1="{xx:.1f}" y1="{pad_t}" x2="{xx:.1f}" y2="{pad_t + plot_h}" '
            f'stroke="#eee" stroke-width="1"/>'
            f'<text x="{xx:.1f}" y="{h_price - 6}" text-anchor="middle" '
            f'font-size="11" fill="#888">{_esc(lab)}</text>'
        )

    y_labels = []
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        val = ymax - (ymax - ymin) * frac
        yy = pad_t + plot_h * frac
        y_labels.append(
            f'<text x="{pad_l - 6}" y="{yy + 4:.1f}" text-anchor="end" font-size="11" fill="#888">'
            f"{val:.0f}</text>"
            f'<line x1="{pad_l}" y1="{yy:.1f}" x2="{pad_l + plot_w}" y2="{yy:.1f}" '
            f'stroke="#f0f0f0" stroke-width="1"/>'
        )

    pre_line = ""
    if pre is not None:
        yy = y_at(pre)
        pre_line = (
            f'<line x1="{pad_l}" y1="{yy:.1f}" x2="{pad_l + plot_w}" y2="{yy:.1f}" '
            f'stroke="#bbb" stroke-dasharray="4 3" stroke-width="1"/>'
        )

    last = prices[-1]
    line_color = UP if (pre is None or last >= pre) else DOWN

    vmax = max(vols) or 1.0
    vol_top = h_price + gap
    vol_h = h_vol - 18
    vol_bars = []
    bar_w = max(0.8, plot_w / n * 0.85)
    for i, (v, p) in enumerate(zip(vols, prices)):
        bh = (v / vmax) * vol_h
        xx = x_at(i) - bar_w / 2
        yy = vol_top + vol_h - bh
        if i == 0:
            c = UP if (pre is None or p >= pre) else DOWN
        else:
            c = UP if p >= prices[i - 1] else DOWN
        vol_bars.append(
            f'<rect x="{xx:.1f}" y="{yy:.1f}" width="{bar_w:.1f}" height="{max(bh, 0.5):.1f}" '
            f'fill="{c}" opacity="0.75"/>'
        )

    total_h = h_price + gap + h_vol
    return f"""
<svg class="chart-svg" viewBox="0 0 {w} {total_h}" width="100%" preserveAspectRatio="xMidYMid meet">
  <rect x="0" y="0" width="{w}" height="{total_h}" fill="#fff"/>
  {''.join(y_labels)}
  {pre_line}
  {''.join(tick_svg)}
  <polyline fill="none" stroke="{line_color}" stroke-width="1.8" points="{price_pts}"/>
  <polyline fill="none" stroke="#f0a000" stroke-width="1.2" points="{avg_pts}"/>
  <text x="{pad_l}" y="14" font-size="11" fill="#999">现价</text>
  <text x="{pad_l + 36}" y="14" font-size="11" fill="#f0a000">均价</text>
  {''.join(vol_bars)}
  <text x="{pad_l}" y="{vol_top + 12}" font-size="11" fill="#999">成交量</text>
</svg>
"""


def render_market_overview(
    output_path: Union[str, Path],
    payload: Optional[Dict[str, Any]] = None,
) -> Path:
    """同花顺风格纯 HTML 大盘页（不依赖 Bokeh，避免叠层/刻度失效）。"""
    payload = payload or build_market_overview_payload()
    idxs = payload.get("indices") or {}
    sh = idxs.get("sh") or {}
    sz = idxs.get("sz") or {}
    cyb = idxs.get("cyb") or {}
    kcb = idxs.get("kcb") or {}
    bundle = payload.get("intraday") or {}
    quote = bundle.get("quote") or {}
    trends = bundle.get("trends")
    if trends is None:
        trends = pd.DataFrame()

    pre = sh.get("pre_close")
    if pre is None:
        pre = quote.get("pre_close")
    price = sh.get("price") if sh.get("price") is not None else quote.get("price")
    pct = sh.get("pct") if sh.get("pct") is not None else quote.get("pct")
    cls = _sign(pct)

    up, down, flat = payload.get("up"), payload.get("down"), payload.get("flat")
    try:
        total_n = int(up or 0) + int(down or 0) + int(flat or 0)
    except (TypeError, ValueError):
        total_n = None

    err = ""
    if payload.get("errors"):
        err = (
            '<div class="warn">'
            + "；".join(_esc(e) for e in payload["errors"])
            + "</div>"
        )

    chart = _svg_intraday(trends, float(pre) if pre not in (None, "") else None)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>大盘概况</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{
  font-family:"Microsoft YaHei","PingFang SC",sans-serif;
  background:linear-gradient(180deg,#1b2330 0%,#243044 120px,#e9edf3 120px,#e9edf3 100%);
  color:#222; min-height:100vh; padding:0 0 24px;
}}
.topbar{{
  background:linear-gradient(90deg,#1a2230,#243247 40%,#1a2230);
  color:#fff; padding:10px 16px 12px;
  box-shadow:0 2px 8px rgba(0,0,0,.25);
}}
.topbar .ttl{{
  display:flex; align-items:center; gap:10px; margin-bottom:10px;
}}
.topbar .ttl h1{{font-size:16px; font-weight:700; letter-spacing:.5px;}}
.badge{{
  font-size:11px; padding:1px 8px; border-radius:2px;
  background:rgba(233,48,48,.2); color:#ff8a80; border:1px solid rgba(233,48,48,.35);
}}
.sub{{font-size:12px; color:#9aa4b5;}}
.idx-row{{
  display:flex; flex-direction:row; flex-wrap:nowrap; gap:8px; width:100%;
}}
.idx{{
  flex:1 1 0; min-width:0;
  background:rgba(255,255,255,.06);
  border:1px solid rgba(255,255,255,.08);
  border-radius:4px; padding:8px 12px;
  display:flex; flex-direction:row; align-items:baseline; gap:10px;
  white-space:nowrap; overflow:hidden;
}}
.idx-n{{font-size:12px; color:#9aa4b5; flex-shrink:0;}}
.idx-p{{font-size:20px; font-weight:700;}}
.idx-c{{font-size:14px; font-weight:700;}}
.idx.up .idx-p, .idx.up .idx-c{{color:{UP};}}
.idx.down .idx-p, .idx.down .idx-c{{color:#3ddc84;}}
.idx.flat .idx-p, .idx.flat .idx-c{{color:#e8e8e8;}}
.stats{{
  display:flex; gap:8px; margin-top:8px;
}}
.stat{{
  flex:1; background:rgba(255,255,255,.05); border-radius:4px; padding:6px 10px;
}}
.stat .k{{font-size:11px; color:#8b95a8;}}
.stat .v{{font-size:15px; font-weight:700; color:#e8e8e8; margin-top:2px;}}
.stat .v.up{{color:{UP};}}
.stat .v.down{{color:#3ddc84;}}
.stat .s{{font-size:10px; color:#6b7588; margin-top:1px;}}
.warn{{
  margin-top:8px; font-size:12px; color:#ffe58f;
  background:rgba(250,173,20,.15); border:1px solid rgba(250,173,20,.35);
  padding:6px 10px; border-radius:4px;
}}
.boards-wrap{{
  max-width:1280px; margin:12px auto 0; padding:0 12px;
}}
.boards-row{{
  display:flex; flex-direction:row; gap:12px; align-items:stretch;
}}
.board{{
  flex:1 1 50%; min-width:0;
  background:#fff; border-radius:6px; border:1px solid #dfe3ea;
  box-shadow:0 1px 4px rgba(0,0,0,.06); padding:10px 12px 12px;
}}
.board-h{{
  font-size:14px; font-weight:700; padding:2px 2px 8px;
  border-bottom:2px solid; margin-bottom:8px;
}}
.bar-row{{
  display:grid;
  grid-template-columns:22px 92px 1fr 64px 64px;
  gap:8px; align-items:center;
  padding:7px 2px; border-bottom:1px solid #f5f5f5;
}}
.bar-row:last-child{{border-bottom:none;}}
.bar-i{{color:#bbb; text-align:center; font-size:12px;}}
.bar-n{{
  font-size:12px; color:#333; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
}}
.bar-track{{
  height:14px; background:#f3f4f6; border-radius:3px; overflow:hidden;
}}
.bar-fill{{
  height:100%; border-radius:3px; min-width:4px;
  transition:width .25s ease;
}}
.bar-p{{font-size:12px; font-weight:700; text-align:right;}}
.bar-l{{
  font-size:11px; color:#888; text-align:right;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
}}
.bar-empty{{text-align:center; color:#bbb; padding:28px 8px; font-size:13px;}}
.chart-wrap{{
  max-width:1280px; margin:12px auto 0; padding:0 12px;
}}
.chart-card{{
  background:#fff; border-radius:6px; border:1px solid #dfe3ea;
  box-shadow:0 1px 4px rgba(0,0,0,.06); padding:10px 12px 12px;
}}
.chart-cap{{
  display:flex; align-items:baseline; gap:8px; margin-bottom:6px;
  padding-bottom:6px; border-bottom:1px solid #f0f0f0;
}}
.chart-cap .name{{font-size:14px; font-weight:700;}}
.chart-cap .code{{font-size:12px; color:#999;}}
.chart-cap .px{{font-size:18px; font-weight:700;}}
.up{{color:{UP} !important;}}
.down{{color:{DOWN} !important;}}
.chart-svg{{display:block; width:100%; height:auto; background:#fff;}}
.no-chart{{padding:40px; text-align:center; color:#999;}}
@media (max-width:900px){{
  .boards-row{{flex-direction:column;}}
}}
</style>
</head>
<body>
  <div class="topbar">
    <div class="ttl">
      <h1>大盘概况</h1>
      <span class="badge">{'盘中' if payload.get('trading') else '已收盘/休市'}</span>
      <span class="sub">更新 {_esc(payload.get('updated_at'))}</span>
      {f'<span class="sub">行情日 {_esc(payload.get("breadth_date"))}</span>' if payload.get('breadth_date') else ''}
    </div>
    <div class="idx-row">
      {_idx_item('上证指数', sh if sh else {'price': price, 'pct': pct})}
      {_idx_item('深证成指', sz)}
      {_idx_item('创业板指', cyb)}
      {_idx_item('科创50', kcb)}
    </div>
    <div class="stats">
      <div class="stat"><div class="k">两市成交额</div>
        <div class="v">{_esc(_fmt_yi(payload.get('amount_total')))}</div>
        <div class="s">沪 {_esc(_fmt_yi(payload.get('amount_sh')))} · 深 {_esc(_fmt_yi(payload.get('amount_sz')))}</div>
      </div>
      <div class="stat"><div class="k">上涨家数</div>
        <div class="v up">{_esc(up if up is not None else '-')}</div></div>
      <div class="stat"><div class="k">下跌家数</div>
        <div class="v down">{_esc(down if down is not None else '-')}</div></div>
      <div class="stat"><div class="k">平盘 / 合计</div>
        <div class="v">{_esc(flat if flat is not None else '-')}</div>
        <div class="s">合计 {_esc(total_n if total_n is not None else '-')}</div>
      </div>
    </div>
    {err}
  </div>

  <div class="chart-wrap">
    <div class="chart-card">
      <div class="chart-cap">
        <span class="name">上证指数</span>
        <span class="code">000001.SH</span>
        <span class="px {cls}">{_fmt_num(price)}</span>
        <span class="{cls}">{_fmt_pct(pct)}</span>
      </div>
      {chart}
    </div>
  </div>

  <div class="boards-wrap">
    <div class="boards-row">
      {_board_bars(payload.get('top_boards') or [], '涨幅前五', 'up')}
      {_board_bars(payload.get('bottom_boards') or [], '跌幅前五', 'down')}
    </div>
  </div>
</body></html>
"""

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    return output_path


def write_loading_html(output_path: Union[str, Path]) -> Path:
    html = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>加载大盘</title>
<style>
body{margin:0;font-family:"Microsoft YaHei",sans-serif;
background:linear-gradient(180deg,#1b2330,#243044 120px,#e9edf3 120px);color:#fff;padding:40px;}
.box{background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.12);
border-radius:6px;padding:28px 32px;max-width:520px;}
h1{font-size:18px;margin:0 0 8px;} p{color:#9aa4b5;margin:0;font-size:13px;}
</style></head>
<body><div class="box">
<h1>正在加载大盘概况…</h1>
<p>上证/深证/创业板/科创50 · 分时 · 涨跌幅前五</p>
</div></body></html>
"""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return path
