# -*- coding: utf-8 -*-
"""
个股详情专业版页：同花顺/东财式顶栏 + 标签切换
分时 / 日K / 周K / 月K / 资金 / 财务 / 资讯 —— 单页单滚动，无上下双滚动条。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Union

import pandas as pd
from bokeh.embed import file_html
from bokeh.layouts import column
from bokeh.models import (
    ColumnDataSource,
    Div,
    HoverTool,
    Range1d,
    Span,
    Tabs,
)

try:
    from bokeh.models import TabPanel  # bokeh >= 3
except ImportError:  # pragma: no cover
    from bokeh.models import Panel as TabPanel  # noqa: N813
from bokeh.plotting import figure
from bokeh.resources import INLINE

from qbot.data.quote_charts import (
    DOWN_COLOR,
    INDEX_BAR_WIDTH,
    MA20_COLOR,
    MA5_COLOR,
    UP_COLOR,
    _apply_kline_index_xaxis,
    _build_finance_plots,
    _build_fund_flow_plots,
    _build_stock_news_section,
    _candle_source,
    _fmt_html_num,
    _html_escape,
    _kline_index_ticks,
    _prepare_ohlcv,
    _sign_cls,
    _tools,
    inject_into_bokeh_html,
)


def _empty_note(msg: str) -> Div:
    return Div(
        text=(
            f"<div style='padding:16px;color:#888;background:#fff;"
            f"border:1px solid #eee;'>{_html_escape(msg)}</div>"
        ),
        width=1200,
        height=48,
    )


def _to_float(v) -> Optional[float]:
    try:
        if v is None or v == "" or v == "-":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _is_cn_trade_day(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now()
    return now.weekday() < 5


def _slice_frame_to_today(df: pd.DataFrame, time_col: str = "datetime") -> pd.DataFrame:
    """只保留时间戳落在「今天」的分时行；昨收盘残留的分时一律丢掉。"""
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    today = _today_str()
    if time_col in out.columns:
        ts = pd.to_datetime(out[time_col], errors="coerce")
    elif "time" in out.columns:
        ts = pd.to_datetime(out["time"], errors="coerce")
    elif "date" in out.columns:
        ts = pd.to_datetime(out["date"], errors="coerce")
    else:
        return pd.DataFrame()
    mask = ts.dt.strftime("%Y-%m-%d") == today
    return out.loc[mask].reset_index(drop=True)


def _quote_has_today_session(quote: dict) -> bool:
    """
    盘口是否已有「今日成交」迹象。
    未开盘时常 volume/amount=0、open 为空；此时不能用昨收冒充今日柱。
    """
    quote = quote or {}
    vol = _to_float(quote.get("volume")) or 0.0
    amt = _to_float(quote.get("amount")) or 0.0
    o = _to_float(quote.get("open"))
    if vol <= 0 and amt <= 0:
        return False
    if o is None:
        return False
    return True


def _drop_date_row(df: pd.DataFrame, day: str) -> pd.DataFrame:
    """去掉指定日期的柱（清掉误标成今天的昨日柱）。"""
    if df is None or df.empty or "date" not in df.columns:
        return df if df is not None else pd.DataFrame()
    out = df.copy()
    dates = pd.to_datetime(out["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    return out.loc[dates != day].reset_index(drop=True)


def _should_paint_today_bar(quote: dict, trends_today: pd.DataFrame) -> bool:
    """
    仅当确认有「今天」的实时数据时才画今日柱。
    - 有今日分时 → 画
    - 无今日分时：仅在当日 09:25 之后、且盘口已有成交量时才画（防昨量冒充）
    - 次日开盘前 / 周末 → 不画
    """
    if not _is_cn_trade_day():
        return False
    now = datetime.now()
    # 集合竞价前：即使接口还挂着昨收/昨量，也不画今日柱
    if now.time() < datetime.strptime("09:25", "%H:%M").time():
        return False
    if trends_today is not None and not trends_today.empty:
        return True
    return _quote_has_today_session(quote)


def _build_today_ohlcv(quote: dict, trends: pd.DataFrame) -> Optional[Dict[str, Any]]:
    """用盘口 + 今日分时拼出当日未收盘/已收盘日K柱（东财历史日K常缺当日）。"""
    trends = _slice_frame_to_today(trends)
    if not _should_paint_today_bar(quote, trends):
        return None

    o = _to_float((quote or {}).get("open"))
    h = _to_float((quote or {}).get("high"))
    l = _to_float((quote or {}).get("low"))
    c = _to_float((quote or {}).get("price"))
    v = _to_float((quote or {}).get("volume"))

    if trends is not None and not trends.empty:
        tp = pd.to_numeric(trends["price"], errors="coerce").dropna()
        if not tp.empty:
            if c is None:
                c = float(tp.iloc[-1])
            if o is None:
                if "open" in trends.columns:
                    o = _to_float(trends["open"].iloc[0]) or float(tp.iloc[0])
                else:
                    o = float(tp.iloc[0])
            if h is None:
                h = float(tp.max())
            if l is None:
                l = float(tp.min())
        if (v is None or v <= 0) and "volume" in trends.columns:
            tv = pd.to_numeric(trends["volume"], errors="coerce").fillna(0.0)
            v = float(tv.sum())

    if None in (o, h, l, c):
        return None
    if (v is None or v <= 0) and (trends is None or trends.empty):
        return None
    h = max(float(h), float(o), float(c))
    l = min(float(l), float(o), float(c))
    return {
        "date": _today_str(),
        "open": float(o),
        "high": float(h),
        "low": float(l),
        "close": float(c),
        "volume": float(v or 0.0),
    }


def _ensure_today_daily_bar(
    day: pd.DataFrame, quote: dict, trends: pd.DataFrame
) -> pd.DataFrame:
    """
    日K今日柱：
    - 未开盘 / 无今日实时：不画今日柱，并清掉误把昨天标成今天的柱
    - 开盘后有实时：用盘口+今日分时写入（不整行复制昨天）
    - 收盘后：继续用今日实时/接口数据
    """
    today = _today_str()
    trends_today = _slice_frame_to_today(trends)
    bar = _build_today_ohlcv(quote, trends_today)

    if day is None or day.empty:
        return pd.DataFrame([bar]) if bar else pd.DataFrame()

    out = day.copy()
    if not bar:
        # 无今日实时能力（含开盘前）：去掉伪今日柱
        if not _should_paint_today_bar(quote, trends_today):
            return _drop_date_row(out, today)
        return out

    dates = pd.to_datetime(out["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    if (dates == today).any():
        idx = dates[dates == today].index[-1]
        for k, val in bar.items():
            out.loc[idx, k] = val
    else:
        # 只写今日字段，绝不整行复制昨天
        row = {col: pd.NA for col in out.columns}
        row.update(bar)
        out = pd.concat([out, pd.DataFrame([row])], ignore_index=True)

    out["_sort"] = pd.to_datetime(out["date"], errors="coerce")
    out = out.sort_values("_sort").drop(columns=["_sort"]).reset_index(drop=True)
    return out


def _ensure_today_fund_bar(fund: pd.DataFrame, fflow: pd.DataFrame) -> pd.DataFrame:
    """
    资金今日柱：仅当有「今日」分时资金时写入；未开盘不画、不把昨日资金标成今天。
    """
    today = _today_str()
    fflow_today = _slice_frame_to_today(fflow)

    if fflow_today is None or fflow_today.empty:
        if fund is None or fund.empty:
            return pd.DataFrame()
        return _drop_date_row(fund, today)

    # 开盘前即使误返回了带今日时间戳的空数据也不画
    if not _is_cn_trade_day() or datetime.now().time() < datetime.strptime(
        "09:25", "%H:%M"
    ).time():
        if fund is None or fund.empty:
            return pd.DataFrame()
        return _drop_date_row(fund, today)

    last = fflow_today.iloc[-1]
    today_ts = pd.Timestamp(today)
    row = {
        "date": today_ts,
        "main_net": float(last.get("main_net", 0) or 0),
        "retail_net": float(last.get("retail_net", 0) or 0),
        "mid_net": float(last.get("mid_net", 0) or 0),
        "large_net": float(last.get("large_net", 0) or 0),
        "super_net": float(last.get("super_net", 0) or 0),
    }
    row["main_net_yi"] = row["main_net"] / 1e8
    row["retail_net_yi"] = row["retail_net"] / 1e8
    row["mid_net_yi"] = row["mid_net"] / 1e8
    row["large_net_yi"] = row["large_net"] / 1e8
    row["super_net_yi"] = row["super_net"] / 1e8

    if fund is None or fund.empty:
        out = pd.DataFrame([row])
    else:
        out = fund.copy()
        out["date"] = pd.to_datetime(out["date"], errors="coerce")
        today_mask = out["date"].dt.normalize() == today_ts.normalize()
        if today_mask.any():
            idx = out.index[today_mask][-1]
            for k, val in row.items():
                out.loc[idx, k] = val
        else:
            new_row = {col: pd.NA for col in out.columns}
            new_row.update(row)
            out = pd.concat([out, pd.DataFrame([new_row])], ignore_index=True)

    out = out.sort_values("date").reset_index(drop=True)
    if "main_net_yi" in out.columns:
        out["main_cum_yi"] = (
            pd.to_numeric(out["main_net_yi"], errors="coerce").fillna(0).cumsum()
        )
    if "retail_net_yi" in out.columns:
        out["retail_cum_yi"] = (
            pd.to_numeric(out["retail_net_yi"], errors="coerce").fillna(0).cumsum()
        )
    return out


def _cut_lunch_break(df: pd.DataFrame) -> pd.DataFrame:
    """去掉 11:30~13:00 午休时段。"""
    if df is None or df.empty or "datetime" not in df.columns:
        return df if df is not None else pd.DataFrame()
    out = df.copy()
    ts = pd.to_datetime(out["datetime"])
    minutes = ts.dt.hour * 60 + ts.dt.minute
    # 保留 <=11:30 或 >=13:00
    mask = (minutes <= 11 * 60 + 30) | (minutes >= 13 * 60)
    out = out.loc[mask].reset_index(drop=True)
    return out


_INTRA_TICK_LABELS = ("09:30", "10:00", "11:00", "11:30", "13:00", "14:00", "15:00")


def _session_axis(df: pd.DataFrame):
    """
    用交易序号作横轴，刻度只标关键时刻。
    返回 (df_with_i, tick_idxs, label_overrides)
    """
    out = df.copy()
    out["i"] = list(range(len(out)))
    if "time_label" not in out.columns and "datetime" in out.columns:
        out["time_label"] = pd.to_datetime(out["datetime"]).dt.strftime("%H:%M")
    labels = out["time_label"].astype(str).tolist()
    tick_idxs = []
    overrides = {}
    for i, lab in enumerate(labels):
        if lab in _INTRA_TICK_LABELS:
            tick_idxs.append(i)
            overrides[i] = lab
    return out, tick_idxs, overrides


def _apply_session_xaxis(fig, tick_idxs, overrides):
    from bokeh.models import FixedTicker

    if tick_idxs:
        fig.xaxis.ticker = FixedTicker(ticks=tick_idxs)
        fig.xaxis.major_label_overrides = overrides
    fig.xaxis.axis_label = None


def _build_header_div(quote: dict, name: str, code: str, updated: str, trading: bool) -> Div:
    pct = quote.get("pct")
    main_yi = quote.get("main_net_yi")
    main_pct = quote.get("main_pct")
    html = f"""
    <div class="sd-head">
      <div class="sd-row1">
        <span class="sd-name">{_html_escape(name or code)}</span>
        <span class="sd-code">{_html_escape(code)}</span>
        <span class="sd-badge">{'交易中' if trading else '已收盘'}</span>
        <span class="sd-upd">更新 {_html_escape(updated)}</span>
      </div>
      <div class="sd-row2 {_sign_cls(pct)}">
        <span class="sd-px">{_fmt_html_num(quote.get('price'))}</span>
        <span class="sd-chg">{_fmt_html_num(quote.get('change'))}&nbsp;&nbsp;{_fmt_html_num(pct)}%</span>
      </div>
      <div class="sd-metrics">
        <i>开 {_fmt_html_num(quote.get('open'))}</i>
        <i>高 {_fmt_html_num(quote.get('high'))}</i>
        <i>低 {_fmt_html_num(quote.get('low'))}</i>
        <i>昨收 {_fmt_html_num(quote.get('pre_close'))}</i>
        <i>换手 {_fmt_html_num(quote.get('turnover'))}%</i>
        <i>PE {_fmt_html_num(quote.get('pe_ttm'))}</i>
        <i>PB {_fmt_html_num(quote.get('pb'))}</i>
        <i>市值 {_fmt_html_num(quote.get('mcap'), 0)}亿</i>
        <i class="{_sign_cls(main_yi)}">主力 {_fmt_html_num(main_yi)}亿</i>
        <i class="{_sign_cls(main_pct)}">净占比 {_fmt_html_num(main_pct)}%</i>
      </div>
    </div>
    """
    return Div(text=html, width=1200, height=158, sizing_mode="stretch_width")


def _intraday_tab(trends: pd.DataFrame, fflow: pd.DataFrame, pre_close) -> Any:
    """分时价 + 量 + 主力资金。"""
    from bokeh.models import Range1d

    plots = []
    trends = _cut_lunch_break(trends)
    if trends is None or trends.empty:
        return column(_empty_note("暂无分时数据"), sizing_mode="stretch_width")

    trends, tick_idxs, overrides = _session_axis(trends)
    src = ColumnDataSource(trends)
    p = figure(
        title="分时",
        width=1200,
        height=300,
        tools=_tools(),
        active_drag="pan",
        active_scroll="wheel_zoom",
        toolbar_location="above",
        x_range=Range1d(start=-1, end=max(len(trends) - 1, 1) + 1),
    )
    p.line("i", "price", source=src, line_color="#d4380d", line_width=1.8, legend_label="现价")
    p.line("i", "avg", source=src, line_color="#fa8c16", line_width=1.2, legend_label="均价")
    if pre_close not in (None, ""):
        try:
            p.add_layout(
                Span(
                    location=float(pre_close),
                    dimension="width",
                    line_color="#bfbfbf",
                    line_dash="dashed",
                    line_width=1,
                )
            )
        except (TypeError, ValueError):
            pass
    p.add_tools(
        HoverTool(
            tooltips=[
                ("时间", "@time_label"),
                ("现价", "@price{0.00}"),
                ("均价", "@avg{0.00}"),
                ("涨跌%", "@pct{0.00}"),
                ("量", "@volume{0,0}"),
            ],
            mode="vline",
        )
    )
    _apply_session_xaxis(p, tick_idxs, overrides)
    p.legend.location = "top_left"
    p.legend.click_policy = "hide"
    p.grid.grid_line_alpha = 0.2
    p.yaxis.axis_label = "价格"
    plots.append(p)

    # 成交量纵轴留白
    prices = trends["price"].astype(float).tolist()
    pre = float(pre_close) if pre_close not in (None, "") else None
    colors = []
    for i, v in enumerate(prices):
        if i == 0:
            colors.append(UP_COLOR if (pre is None or v >= pre) else DOWN_COLOR)
        else:
            colors.append(UP_COLOR if v >= prices[i - 1] else DOWN_COLOR)
    vols = trends["volume"].astype(float)
    vmax = float(vols.max()) if len(vols) else 1.0
    if vmax <= 0:
        vmax = 1.0
    src_v = ColumnDataSource(
        dict(i=trends["i"], volume=vols, color=colors, time_label=trends["time_label"])
    )
    p_vol = figure(
        title="成交量",
        width=1200,
        height=160,
        tools="pan,wheel_zoom,reset",
        x_range=p.x_range,
        y_range=Range1d(start=0, end=vmax * 1.35),
        toolbar_location=None,
    )
    p_vol.vbar(x="i", top="volume", width=0.85, source=src_v, color="color", alpha=0.85)
    _apply_session_xaxis(p_vol, tick_idxs, overrides)
    p_vol.yaxis.axis_label = "量"
    p_vol.grid.grid_line_alpha = 0.2
    plots.append(p_vol)

    fflow = _cut_lunch_break(fflow)
    if fflow is not None and not fflow.empty:
        fflow, f_ticks, f_over = _session_axis(fflow)
        src_f = ColumnDataSource(fflow)
        p_f = figure(
            title="主力净流入（当日累计·亿元）",
            width=1200,
            height=220,
            tools=_tools(),
            toolbar_location="above",
            x_range=Range1d(start=-1, end=max(len(fflow) - 1, 1) + 1),
        )
        p_f.line("i", "main_net_yi", source=src_f, line_color="#d4380d", line_width=2, legend_label="主力")
        p_f.line("i", "super_net_yi", source=src_f, line_color="#cf1322", line_width=1.2, legend_label="超大单")
        p_f.line("i", "large_net_yi", source=src_f, line_color="#fa8c16", line_width=1.2, legend_label="大单")
        p_f.line("i", "retail_net_yi", source=src_f, line_color="#389e0d", line_width=1, legend_label="散户")
        p_f.add_layout(Span(location=0, dimension="width", line_color="#999", line_width=1))
        p_f.add_tools(
            HoverTool(
                tooltips=[
                    ("时间", "@time_label"),
                    ("主力", "@main_net_yi{0.00}"),
                    ("超大单", "@super_net_yi{0.00}"),
                    ("大单", "@large_net_yi{0.00}"),
                    ("散户", "@retail_net_yi{0.00}"),
                ],
                mode="vline",
            )
        )
        _apply_session_xaxis(p_f, f_ticks, f_over)
        p_f.legend.location = "top_left"
        p_f.legend.click_policy = "hide"
        p_f.yaxis.axis_label = "亿元"
        p_f.grid.grid_line_alpha = 0.2
        plots.append(p_f)

    return column(*plots, sizing_mode="stretch_width")


def _kline_tab(df: pd.DataFrame, title: str) -> Any:
    """日/周/月K：序号等距横轴，避免未完结周/月在日历轴上挤在一起或把柱宽压瘦。"""
    if df is None or df.empty:
        return column(_empty_note(f"暂无{title}数据"), sizing_mode="stretch_width")
    data = _prepare_ohlcv(df)
    data = data.copy()
    data["i"] = list(range(len(data)))
    source = _candle_source(data)
    width = INDEX_BAR_WIDTH
    n = len(data)
    x_range = Range1d(start=-0.6, end=max(n - 1, 0) + 0.6)
    tick_idxs, overrides = _kline_index_ticks(data["date_str"].tolist())

    p_k = figure(
        title=title,
        width=1200,
        height=360,
        tools=_tools(),
        active_drag="pan",
        active_scroll="wheel_zoom",
        toolbar_location="above",
        x_range=x_range,
    )
    p_k.segment("i", "high", "i", "low", color="color", source=source, line_width=1)
    p_k.vbar("i", width, "open", "close", fill_color="color", line_color="color", source=source)
    p_k.line("i", "ma5", source=source, line_color=MA5_COLOR, line_width=1.4, legend_label="MA5")
    p_k.line("i", "ma20", source=source, line_color=MA20_COLOR, line_width=1.4, legend_label="MA20")
    p_k.add_tools(
        HoverTool(
            tooltips=[
                ("日期", "@date_str"),
                ("开", "@open{0.00}"),
                ("高", "@high{0.00}"),
                ("低", "@low{0.00}"),
                ("收", "@close{0.00}"),
                ("涨跌%", "@ret{0.00}"),
            ],
            mode="vline",
        )
    )
    p_k.legend.location = "top_left"
    p_k.legend.click_policy = "hide"
    p_k.grid.grid_line_alpha = 0.25
    p_k.yaxis.axis_label = "价格"
    _apply_kline_index_xaxis(p_k, tick_idxs, overrides)

    p_vol = figure(
        title="成交量",
        width=1200,
        height=120,
        tools=_tools(),
        x_range=p_k.x_range,
        toolbar_location=None,
    )
    p_vol.vbar(
        "i",
        width,
        0,
        "volume",
        fill_color="color",
        line_color="color",
        source=source,
        alpha=0.85,
    )
    p_vol.grid.grid_line_alpha = 0.25
    _apply_kline_index_xaxis(p_vol, tick_idxs, overrides)
    return column(p_k, p_vol, sizing_mode="stretch_width")


def _page_css() -> str:
    return """
<style>
html, body { margin:0; padding:0; background:#f5f6f8; font-family:"Microsoft YaHei","Segoe UI",Arial,sans-serif; }
.sd-head { background:#fff; border-bottom:1px solid #e8e8e8; padding:12px 18px 14px; margin:0 0 10px 0; box-sizing:border-box; }
.sd-row1 { display:flex; align-items:center; gap:10px; }
.sd-name { font-size:22px; font-weight:700; color:#1f1f1f; }
.sd-code { font-size:13px; color:#8c8c8c; }
.sd-badge { font-size:11px; padding:1px 8px; border-radius:2px; background:#fff1f0; color:#cf1322; border:1px solid #ffa39e; }
.sd-upd { margin-left:auto; font-size:12px; color:#bfbfbf; }
.sd-row2 { margin-top:4px; line-height:1.2; }
.sd-px { font-size:32px; font-weight:700; margin-right:14px; }
.sd-chg { font-size:16px; font-weight:600; }
.sd-row2.up, .up { color:#ef232a; }
.sd-row2.down, .down { color:#14b143; }
.sd-row2.flat, .flat { color:#262626; }
.sd-metrics { display:flex; flex-wrap:wrap; gap:6px 4px; margin-top:10px; }
.sd-metrics i { font-style:normal; font-size:12px; color:#595959; background:#fafafa; border:1px solid #f0f0f0; padding:2px 8px; border-radius:2px; }
/* 顶栏与下方 Tabs 之间留空，避免 K 线区盖住开高低换手 */
.bk-root .bk-Column > .bk-Tabs { margin-top: 6px !important; }
.bk-root .bk-tabs-header { background:#fff !important; border-bottom:1px solid #e8e8e8 !important; }
.bk-root .bk-tab { font-size:14px !important; padding:8px 18px !important; color:#595959 !important; }
.bk-root .bk-tab.bk-active { color:#cf1322 !important; font-weight:600 !important; border-bottom:2px solid #cf1322 !important; }
</style>
"""


def _page_tab_keep_js(code: str) -> str:
    """盘中刷新会换 file:// 文件名，用 localStorage 按代码记住标签，避免跳回分时。"""
    safe = "".join(ch for ch in str(code or "") if ch.isalnum()) or "x"
    return f"""
<script>
(function(){{
  var KEY = "qbot_quote_tab_{safe}";
  function tabNodes(){{
    return Array.prototype.slice.call(
      document.querySelectorAll(".bk-tab, .bk-tabs-header [role='tab']")
    );
  }}
  function restore(){{
    try {{
      var i = parseInt(localStorage.getItem(KEY) || "0", 10);
      if (!i || i < 0) return;
      var ts = tabNodes();
      if (ts[i]) ts[i].click();
    }} catch (e) {{}}
  }}
  document.addEventListener("click", function(e){{
    var t = e.target && e.target.closest ? e.target.closest(".bk-tab, [role='tab']") : null;
    if (!t) return;
    var ts = tabNodes();
    var i = ts.indexOf(t);
    if (i >= 0) {{
      try {{ localStorage.setItem(KEY, String(i)); }} catch (err) {{}}
    }}
  }}, true);
  function boot(){{
    setTimeout(restore, 80);
    setTimeout(restore, 320);
    setTimeout(restore, 900);
  }}
  if (document.readyState === "complete") boot();
  else window.addEventListener("load", boot);
}})();
</script>
"""


def _prefer_longer_frame(old, new):
    """刷新时禁止用更短序列覆盖更长历史（防资金/日K退化成 1 日）。"""
    if new is None or getattr(new, "empty", True):
        return old if old is not None else new
    if old is None or getattr(old, "empty", True):
        return new
    try:
        if len(new) + 2 < len(old):
            return old
    except Exception:
        pass
    return new


def render_stock_detail_page(
    code: str,
    output_path: Union[str, Path],
    name: str = "",
    cache: Optional[Dict[str, Any]] = None,
    refresh_heavy: bool = True,
    active_tab: int = 0,
) -> Path:
    """
    生成专业个股详情 HTML（标签页切换）。
    cache: 可复用日/周/月K、资金、财务，盘中刷新时 refresh_heavy=False 只更新分时。
    active_tab: 初始标签索引（仍会由页面 JS 用 localStorage 覆盖恢复）。
    """
    from qbot.data.eastmoney_quote import fetch_kline
    from qbot.data.fund_flow import DEFAULT_LOOKBACK_DAYS, MIN_USEFUL_KLINES, fetch_fund_flow_bundle
    from qbot.data.intraday import fetch_intraday_bundle
    from qbot.data.stock_finance import fetch_finance_bundle

    cache = cache if cache is not None else {}
    bundle = fetch_intraday_bundle(code)
    quote = bundle.get("quote") or {}
    trends = bundle.get("trends")
    fflow = bundle.get("fflow")
    if trends is None:
        trends = pd.DataFrame()
    if fflow is None:
        fflow = pd.DataFrame()

    name = name or quote.get("name") or ""
    updated = bundle.get("updated_at") or ""
    trading = bool(bundle.get("trading"))

    # ---- 重数据（可缓存）----
    if refresh_heavy or not cache.get("ready"):
        end = datetime.now()
        begin = end - timedelta(days=400)
        b, e = begin.strftime("%Y%m%d"), end.strftime("%Y%m%d")
        prev_day = cache.get("day")
        prev_fund = cache.get("fund")
        try:
            day_new = fetch_kline(code=code, begin=b, end=e, period="日线", adjust="前复权")
        except Exception:
            day_new = pd.DataFrame()
        cache["day"] = _prefer_longer_frame(prev_day, day_new)
        try:
            cache["week"] = fetch_kline(code=code, begin=b, end=e, period="周线", adjust="前复权")
        except Exception:
            cache["week"] = cache.get("week") if cache.get("week") is not None else pd.DataFrame()
        try:
            cache["month"] = fetch_kline(
                code=code,
                begin=(end - timedelta(days=365 * 5)).strftime("%Y%m%d"),
                end=e,
                period="月线",
                adjust="前复权",
            )
        except Exception:
            cache["month"] = cache.get("month") if cache.get("month") is not None else pd.DataFrame()
        try:
            fb = fetch_fund_flow_bundle(code, lookback_days=DEFAULT_LOOKBACK_DAYS)
            fund_new = fb.get("fund_flow") if fb.get("fund_flow") is not None else pd.DataFrame()
            cache["fund"] = _prefer_longer_frame(prev_fund, fund_new)
            if fb.get("northbound") is not None and not fb.get("northbound").empty:
                cache["north"] = fb.get("northbound")
            elif cache.get("north") is None:
                cache["north"] = pd.DataFrame()
        except Exception:
            if cache.get("fund") is None:
                cache["fund"] = pd.DataFrame()
            if cache.get("north") is None:
                cache["north"] = pd.DataFrame()
        try:
            cache["finance"] = fetch_finance_bundle(code)
        except Exception:
            cache["finance"] = cache.get("finance") or {
                "valuation": {"code": code},
                "income": pd.DataFrame(),
                "profile": {"code": code},
            }
        try:
            cache["news_html"] = _build_stock_news_section(code)
        except Exception:
            cache["news_html"] = cache.get("news_html") or (
                '<div style="padding:16px;color:#888">资讯暂不可用</div>'
            )
        cache["ready"] = True

    day = cache.get("day") if cache.get("day") is not None else pd.DataFrame()
    week = cache.get("week") if cache.get("week") is not None else pd.DataFrame()
    month = cache.get("month") if cache.get("month") is not None else pd.DataFrame()
    fund = cache.get("fund") if cache.get("fund") is not None else pd.DataFrame()
    north = cache.get("north") if cache.get("north") is not None else pd.DataFrame()
    finance = cache.get("finance") or {}
    news_html = cache.get("news_html") or ""

    # 历史日K/日资金接口常不含「当天未完结」柱；分时有当天数据时补上
    day = _ensure_today_daily_bar(day, quote, trends)
    fund_src = str(getattr(fund, "attrs", {}).get("source") or "")
    # 新浪日序列不要用东财分时资金覆盖末日，否则柱会跳、长度也可能被搞坏
    if fund_src != "sina" and len(fund) >= MIN_USEFUL_KLINES:
        fund = _ensure_today_fund_bar(fund, fflow)
    elif fund_src != "sina" and (fund is None or fund.empty):
        fund = _ensure_today_fund_bar(fund, fflow)
    # 盘中轻刷新也写回缓存，避免日K/资金停在昨日；但禁止短序列回写覆盖长缓存
    cache["day"] = _prefer_longer_frame(cache.get("day"), day)
    cache["fund"] = _prefer_longer_frame(cache.get("fund"), fund)
    day = cache.get("day") if cache.get("day") is not None else day
    fund = cache.get("fund") if cache.get("fund") is not None else fund

    header = _build_header_div(quote, name, code, updated, trading)
    tab_intra = TabPanel(
        child=_intraday_tab(trends, fflow, quote.get("pre_close")),
        title="分时",
    )
    tab_day = TabPanel(child=_kline_tab(day, "日K / MA"), title="日K")
    tab_week = TabPanel(child=_kline_tab(week, "周K / MA"), title="周K")
    tab_month = TabPanel(child=_kline_tab(month, "月K / MA"), title="月K")

    flow_plots = _build_fund_flow_plots(fund, north, kline=day)
    tab_fund = TabPanel(
        child=column(*flow_plots, sizing_mode="stretch_width")
        if flow_plots
        else column(_empty_note("暂无资金流")),
        title="资金",
    )
    fin_plots = _build_finance_plots(finance)
    tab_fin = TabPanel(
        child=column(*fin_plots, sizing_mode="stretch_width"),
        title="财务",
    )
    tab_news = TabPanel(
        child=column(Div(text=news_html, width=1200), sizing_mode="stretch_width"),
        title="资讯",
    )

    try:
        active = max(0, min(int(active_tab or 0), 6))
    except (TypeError, ValueError):
        active = 0
    tabs = Tabs(
        tabs=[tab_intra, tab_day, tab_week, tab_month, tab_fund, tab_fin, tab_news],
        tabs_location="above",
        width=1200,
        active=active,
    )
    layout = column(header, tabs, spacing=14, sizing_mode="stretch_width")
    title = f"{name} {code}".strip() or str(code)
    html = file_html(layout, INLINE, title=title)
    css = _page_css()
    js = _page_tab_keep_js(code)
    if "<body" in html.lower():
        html = inject_into_bokeh_html(html, body_prepend=css, body_append=js)
    else:
        html = css + html + js

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    return output_path
