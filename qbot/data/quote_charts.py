"""行情可视化：Bokeh 交互图表（与默认 bkt_result 同类：悬停、缩放、拖动）。"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

import numpy as np
import pandas as pd
from bokeh.embed import file_html
from bokeh.layouts import column
from bokeh.models import (
    ColumnDataSource,
    CrosshairTool,
    Div,
    FactorRange,
    HoverTool,
    Legend,
    LegendItem,
    LinearAxis,
    NumeralTickFormatter,
    Range1d,
    Span,
)
from bokeh.plotting import figure
from bokeh.resources import INLINE
from bokeh.transform import dodge

UP_COLOR = "#ef232a"
DOWN_COLOR = "#14b143"
LINE_COLOR = "#1f77b4"
CUM_COLOR = "#ff7f0e"
MA5_COLOR = "#e67e22"
MA20_COLOR = "#8e44ad"
VOLUME_UP = "#f7a1a4"
VOLUME_DOWN = "#8fd19e"


def _prepare_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    data = df.copy()
    rename = {}
    for src, dst in [
        ("日期", "date"),
        ("开盘", "open"),
        ("收盘", "close"),
        ("最高", "high"),
        ("最低", "low"),
        ("成交量", "volume"),
    ]:
        if src in data.columns and dst not in data.columns:
            rename[src] = dst
    if rename:
        data = data.rename(columns=rename)

    required = {"date", "open", "close", "high", "low"}
    missing = required - set(data.columns)
    if missing:
        raise ValueError(f"行情数据缺少字段: {sorted(missing)}")

    data = data.dropna(subset=["date", "open", "close", "high", "low"]).copy()
    data["date"] = pd.to_datetime(data["date"])
    for col in ["open", "close", "high", "low", "volume"]:
        if col in data.columns:
            data[col] = pd.to_numeric(data[col], errors="coerce")
    data = data.sort_values("date").reset_index(drop=True)

    data["ma5"] = data["close"].rolling(5, min_periods=1).mean()
    data["ma20"] = data["close"].rolling(20, min_periods=1).mean()
    data["ret"] = data["close"].pct_change() * 100.0
    data["cum_ret"] = (
        (1.0 + data["close"].pct_change().fillna(0.0)).cumprod() - 1.0
    ) * 100.0
    data["date_str"] = data["date"].dt.strftime("%Y-%m-%d %H:%M")
    # 无分时则去掉多余时分显示
    if data["date"].dt.normalize().equals(data["date"]):
        data["date_str"] = data["date"].dt.strftime("%Y-%m-%d")
    return data


def _monthly_returns(data: pd.DataFrame) -> pd.DataFrame:
    try:
        s = data.set_index("date")["close"].resample("ME").last().dropna()
    except ValueError:
        s = data.set_index("date")["close"].resample("M").last().dropna()
    if len(s) < 2:
        return pd.DataFrame(columns=["period", "ret", "date"])
    ret = s.pct_change().dropna() * 100.0
    out = ret.reset_index()
    out.columns = ["date", "ret"]
    out["period"] = out["date"].dt.strftime("%Y-%m")
    return out


def _yearly_returns(data: pd.DataFrame) -> pd.DataFrame:
    try:
        s = data.set_index("date")["close"].resample("YE").last().dropna()
    except ValueError:
        s = data.set_index("date")["close"].resample("Y").last().dropna()
    if len(s) < 2:
        return pd.DataFrame(columns=["period", "ret", "date"])
    ret = s.pct_change().dropna() * 100.0
    out = ret.reset_index()
    out.columns = ["date", "ret"]
    out["period"] = out["date"].dt.strftime("%Y")
    return out


def _tools():
    return "pan,wheel_zoom,box_zoom,reset,save"


def _add_crosshair(plots):
    cross = CrosshairTool(dimensions="both", line_color="#888888", line_alpha=0.6)
    for p in plots:
        p.add_tools(cross)


def _candle_source(data: pd.DataFrame) -> ColumnDataSource:
    inc = data["close"] >= data["open"]
    colors = np.where(inc, UP_COLOR, DOWN_COLOR)
    vol = data["volume"] if "volume" in data.columns else pd.Series(np.nan, index=data.index)
    vol_colors = np.where(inc, VOLUME_UP, VOLUME_DOWN)
    ret = data["ret"].fillna(0.0)
    ret_colors = np.where(ret >= 0, UP_COLOR, DOWN_COLOR)
    return ColumnDataSource(
        data=dict(
            date=data["date"],
            date_str=data["date_str"],
            open=data["open"],
            high=data["high"],
            low=data["low"],
            close=data["close"],
            ma5=data["ma5"],
            ma20=data["ma20"],
            volume=vol.fillna(0.0),
            ret=ret,
            cum_ret=data["cum_ret"],
            color=colors,
            vol_color=vol_colors,
            ret_color=ret_colors,
        )
    )


def _bar_width(data: pd.DataFrame) -> float:
    """按时间间隔估算蜡烛宽度（毫秒）。"""
    if len(data) < 2:
        return 12 * 60 * 60 * 1000  # 半天
    deltas = data["date"].diff().dropna().dt.total_seconds() * 1000
    med = float(deltas.median()) if len(deltas) else 24 * 60 * 60 * 1000
    return max(med * 0.7, 60 * 60 * 1000 * 0.2)


FLOW_Y_CAP_YI = 100.0  # 单日资金流向纵轴上限（亿元）


def _yi_range(*series, cap: float = FLOW_Y_CAP_YI, pad: float = 0.12) -> Range1d:
    """按序列自适应纵轴，但限制在 ±cap 亿元内。"""
    vals = []
    for s in series:
        if s is None:
            continue
        vals.extend(pd.to_numeric(pd.Series(s), errors="coerce").dropna().tolist())
    if not vals:
        return Range1d(start=-min(20.0, cap), end=min(20.0, cap))
    lo = float(min(vals))
    hi = float(max(vals))
    span = max(hi - lo, 1.0)
    lo -= span * pad
    hi += span * pad
    if lo > 0:
        lo = 0.0
    if hi < 0:
        hi = 0.0
    lo = max(lo, -cap)
    hi = min(hi, cap)
    if abs(hi - lo) < 1e-6:
        lo, hi = -min(5.0, cap), min(5.0, cap)
    return Range1d(start=lo, end=hi)


def _flow_figure(title, x_range, height=200, y_range=None):
    """资金流专用图：分类横轴=交易日列表。"""
    kwargs = dict(
        title=title,
        x_range=x_range,
        width=1100,
        height=height,
        tools="pan,box_zoom,reset,save",
        toolbar_location="right",
        active_drag="pan",
    )
    if y_range is not None:
        kwargs["y_range"] = y_range
    p = figure(**kwargs)
    p.xaxis.major_label_orientation = 0.9
    p.xaxis.axis_label = "交易日"
    p.yaxis.axis_label = "亿元"
    p.grid.grid_line_alpha = 0.25
    return p


def _align_kline_to_factors(kline, factors) -> Optional[pd.DataFrame]:
    """把日K对齐到资金流交易日横轴。"""
    if kline is None or getattr(kline, "empty", True) or not factors:
        return None
    data = _prepare_ohlcv(kline)
    if data.empty:
        return None
    data = data.copy()
    data["date_str"] = pd.to_datetime(data["date"]).dt.strftime("%Y-%m-%d")
    by = data.drop_duplicates("date_str", keep="last").set_index("date_str")
    rows = []
    for d in factors:
        if d not in by.index:
            continue
        r = by.loc[d]
        o = float(r["open"])
        c = float(r["close"])
        rows.append(
            {
                "date_str": d,
                "open": o,
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": c,
                "volume": float(r["volume"]) if "volume" in r and pd.notna(r["volume"]) else 0.0,
                "color": UP_COLOR if c >= o else DOWN_COLOR,
            }
        )
    if not rows:
        return None
    return pd.DataFrame(rows)


def _fund_kline_plot(kline, factors, x_range):
    """资金栏顶部：近30日K线，横轴与资金流向一致。"""
    aligned = _align_kline_to_factors(kline, factors)
    if aligned is None or aligned.empty:
        return None
    src = ColumnDataSource(aligned)
    p = figure(
        title="近30日K线",
        x_range=x_range,
        width=1100,
        height=240,
        tools="pan,box_zoom,reset,save",
        toolbar_location="right",
        active_drag="pan",
    )
    p.segment("date_str", "high", "date_str", "low", color="color", source=src, line_width=1)
    p.vbar(
        "date_str",
        0.6,
        "open",
        "close",
        fill_color="color",
        line_color="color",
        source=src,
    )
    p.add_tools(
        HoverTool(
            tooltips=[
                ("日期", "@date_str"),
                ("开", "@open{0.00}"),
                ("高", "@high{0.00}"),
                ("低", "@low{0.00}"),
                ("收", "@close{0.00}"),
            ],
            mode="vline",
        )
    )
    p.xaxis.major_label_orientation = 0.9
    p.yaxis.axis_label = "价格"
    p.grid.grid_line_alpha = 0.25
    return p


def _attach_cum_axis(p, src, x_col, y_col, color, label):
    """累计线放到右轴，避免把单日净流入纵轴撑开。"""
    cum = pd.to_numeric(pd.Series(src.data.get(y_col, [])), errors="coerce").dropna()
    if cum.empty:
        return
    lo = float(cum.min())
    hi = float(cum.max())
    span = max(hi - lo, 1.0)
    p.extra_y_ranges = {
        "cum": Range1d(start=lo - span * 0.1, end=hi + span * 0.1)
    }
    p.add_layout(LinearAxis(y_range_name="cum", axis_label="累计(亿)"), "right")
    p.line(
        x=x_col,
        y=y_col,
        source=src,
        line_color=color,
        line_width=2,
        legend_label=label,
        y_range_name="cum",
    )


def _build_fund_flow_plots(
    fund_flow,
    northbound,
    x_range=None,
    width=0.7,
    kline=None,
):
    """资金流向：近30交易日分类横轴；顶部可叠加对齐日K；纵轴±100亿内。"""
    del x_range, width
    plots = []

    ff = fund_flow if fund_flow is not None else pd.DataFrame()
    if not ff.empty:
        ff = ff.copy()
        ff["date"] = pd.to_datetime(ff["date"])
        ff = ff.sort_values("date").reset_index(drop=True)
        ff["date_str"] = ff["date"].dt.strftime("%Y-%m-%d")
        ff["main_color"] = np.where(ff["main_net_yi"] >= 0, UP_COLOR, DOWN_COLOR)
        ff["retail_color"] = np.where(ff["retail_net_yi"] >= 0, "#3498db", "#9b59b6")
        factors = ff["date_str"].tolist()
        shared_x = FactorRange(factors=list(factors))
        src = ColumnDataSource(ff)

        p_k = _fund_kline_plot(kline, factors, shared_x)
        if p_k is not None:
            plots.append(p_k)

        p_main = _flow_figure(
            "资金流向分析（近30日）- 主力净流入(亿元)（超大单+大单）",
            shared_x,
            200,
            y_range=_yi_range(ff["main_net_yi"]),
        )
        p_main.vbar(
            x="date_str",
            width=0.7,
            top="main_net_yi",
            fill_color="main_color",
            line_color="main_color",
            source=src,
            legend_label="主力净流入",
        )
        _attach_cum_axis(p_main, src, "date_str", "main_cum_yi", "#e74c3c", "主力累计")
        p_main.add_layout(Span(location=0, dimension="width", line_color="#666", line_width=1))
        p_main.add_tools(
            HoverTool(
                tooltips=[
                    ("日期", "@date_str"),
                    ("主力净流入(亿)", "@main_net_yi{0.00}"),
                    ("主力累计(亿)", "@main_cum_yi{0.00}"),
                    ("主力净占比%", "@main_pct{0.00}"),
                ]
            )
        )
        p_main.legend.location = "top_left"
        p_main.legend.click_policy = "hide"
        plots.append(p_main)

        part_cols = ["super_net_yi", "large_net_yi", "mid_net_yi", "retail_net_yi"]
        p_parts = _flow_figure(
            "资金流向细分（近30日）- 超大单 / 大单 / 中单 / 散户(小单) 净流入(亿元)",
            shared_x,
            240,
            y_range=_yi_range(*[ff[c] for c in part_cols if c in ff.columns]),
        )
        for col, color, label in [
            ("super_net_yi", "#c0392b", "超大单"),
            ("large_net_yi", "#e67e22", "大单"),
            ("mid_net_yi", "#27ae60", "中单"),
            ("retail_net_yi", "#2980b9", "散户(小单)"),
        ]:
            p_parts.line(
                x="date_str",
                y=col,
                source=src,
                line_color=color,
                line_width=1.8 if col != "retail_net_yi" else 2.0,
                legend_label=label,
            )
        p_parts.add_layout(Span(location=0, dimension="width", line_color="#666", line_width=1))
        p_parts.add_tools(
            HoverTool(
                tooltips=[
                    ("日期", "@date_str"),
                    ("超大单(亿)", "@super_net_yi{0.00}"),
                    ("大单(亿)", "@large_net_yi{0.00}"),
                    ("中单(亿)", "@mid_net_yi{0.00}"),
                    ("散户小单(亿)", "@retail_net_yi{0.00}"),
                ]
            )
        )
        p_parts.legend.location = "top_left"
        p_parts.legend.click_policy = "hide"
        plots.append(p_parts)

        p_retail = _flow_figure(
            "散户资金（近30日）- 小单净流入(亿元) 与累计",
            shared_x,
            180,
            y_range=_yi_range(ff["retail_net_yi"]),
        )
        p_retail.vbar(
            x="date_str",
            width=0.7,
            top="retail_net_yi",
            fill_color="retail_color",
            line_color="retail_color",
            source=src,
            legend_label="散户净流入",
        )
        _attach_cum_axis(p_retail, src, "date_str", "retail_cum_yi", "#8e44ad", "散户累计")
        p_retail.add_layout(Span(location=0, dimension="width", line_color="#666", line_width=1))
        p_retail.add_tools(
            HoverTool(
                tooltips=[
                    ("日期", "@date_str"),
                    ("散户净流入(亿)", "@retail_net_yi{0.00}"),
                    ("散户累计(亿)", "@retail_cum_yi{0.00}"),
                ]
            )
        )
        p_retail.legend.location = "top_left"
        p_retail.legend.click_policy = "hide"
        plots.append(p_retail)
    else:
        # 无资金流时仍尽量展示近30日K
        if kline is not None and not getattr(kline, "empty", True):
            try:
                kd = _prepare_ohlcv(kline).tail(30)
                factors = pd.to_datetime(kd["date"]).dt.strftime("%Y-%m-%d").tolist()
                shared_x = FactorRange(factors=list(factors))
                p_k = _fund_kline_plot(kd, factors, shared_x)
                if p_k is not None:
                    plots.append(p_k)
            except Exception:
                pass
        plots.append(
            Div(
                text=(
                    "<div style='padding:10px;color:#a94442;background:#fff3f3;"
                    "border:1px solid #f0c0c0;'>"
                    "资金流向（近30日·主力/散户）暂无数据。请重新加载行情。</div>"
                ),
                width=1100,
                height=50,
            )
        )

    nb = northbound if northbound is not None else pd.DataFrame()
    if not nb.empty:
        nb = nb.copy()
        nb["date"] = pd.to_datetime(nb["date"])
        nb = nb.sort_values("date").reset_index(drop=True)
        nb["date_str"] = nb["date"].dt.strftime("%Y-%m-%d")
        nb["north_color"] = np.where(nb["north_net_yi"] >= 0, "#16a085", "#c0392b")
        nsrc = ColumnDataSource(nb)
        nb_factors = nb["date_str"].tolist()
        nb_x = FactorRange(factors=list(nb_factors))
        p_nb = _flow_figure(
            "北向资金（近30日）- 个股净流入/增持资金(亿元) 与累计",
            nb_x,
            200,
            y_range=_yi_range(nb["north_net_yi"]),
        )
        p_nb.vbar(
            x="date_str",
            width=0.7,
            top="north_net_yi",
            fill_color="north_color",
            line_color="north_color",
            source=nsrc,
            legend_label="北向净流入",
        )
        if "north_cum_yi" in nb.columns:
            _attach_cum_axis(p_nb, nsrc, "date_str", "north_cum_yi", "#1abc9c", "北向累计")
        p_nb.add_layout(Span(location=0, dimension="width", line_color="#666", line_width=1))
        p_nb.add_tools(
            HoverTool(
                tooltips=[
                    ("日期", "@date_str"),
                    ("北向净流入(亿)", "@north_net_yi{0.00}"),
                    ("北向累计(亿)", "@north_cum_yi{0.00}"),
                    ("持股占比%", "@hold_ratio{0.00}"),
                ]
            )
        )
        p_nb.legend.location = "top_left"
        p_nb.legend.click_policy = "hide"
        plots.append(p_nb)
    else:
        plots.append(
            Div(
                text=(
                    "<div style='padding:10px;color:#666;background:#fff;"
                    "border:1px solid #eee;'>"
                    "北向资金：暂无数据（非沪深港通标的，或接口暂不可用）</div>"
                ),
                width=1100,
                height=40,
            )
        )
    return plots


def build_interactive_layout(
    data: pd.DataFrame,
    title: str,
    meta_line: str,
    fund_flow: Optional[pd.DataFrame] = None,
    northbound: Optional[pd.DataFrame] = None,
    finance: Optional[dict] = None,
):
    source = _candle_source(data)
    width = _bar_width(data)
    x_range = Range1d(start=data["date"].iloc[0], end=data["date"].iloc[-1])

    # ---- K 线 ----
    p_k = figure(
        title=f"{title} - K线 / MA（滚轮缩放，拖动平移，悬停看明细）",
        x_axis_type="datetime",
        width=1100,
        height=360,
        tools=_tools(),
        active_drag="pan",
        active_scroll="wheel_zoom",
        x_range=x_range,
        toolbar_location="above",
    )
    p_k.segment("date", "high", "date", "low", color="color", source=source, line_width=1)
    p_k.vbar(
        "date",
        width,
        "open",
        "close",
        fill_color="color",
        line_color="color",
        source=source,
    )
    p_k.line("date", "ma5", source=source, line_color=MA5_COLOR, line_width=1.5, legend_label="MA5")
    p_k.line("date", "ma20", source=source, line_color=MA20_COLOR, line_width=1.5, legend_label="MA20")
    p_k.add_tools(
        HoverTool(
            tooltips=[
                ("日期", "@date_str"),
                ("开盘", "@open{0.00}"),
                ("最高", "@high{0.00}"),
                ("最低", "@low{0.00}"),
                ("收盘", "@close{0.00}"),
                ("成交量", "@volume{0,0}"),
                ("日收益率%", "@ret{0.00}"),
                ("累计收益%", "@cum_ret{0.00}"),
            ],
            mode="vline",
            formatters={"@date": "datetime"},
        )
    )
    p_k.legend.location = "top_left"
    p_k.legend.click_policy = "hide"
    p_k.yaxis.axis_label = "价格"
    p_k.grid.grid_line_alpha = 0.25

    # ---- 成交量（共享 x）----
    p_vol = figure(
        title="成交量",
        x_axis_type="datetime",
        width=1100,
        height=140,
        tools=_tools(),
        active_drag="pan",
        active_scroll="wheel_zoom",
        x_range=p_k.x_range,
        toolbar_location="right",
    )
    p_vol.vbar(
        "date",
        width,
        top="volume",
        fill_color="vol_color",
        line_color="vol_color",
        source=source,
    )
    p_vol.add_tools(
        HoverTool(
            tooltips=[("日期", "@date_str"), ("成交量", "@volume{0,0}")],
            mode="vline",
        )
    )
    p_vol.yaxis.formatter = NumeralTickFormatter(format="0.0a")
    p_vol.grid.grid_line_alpha = 0.25

    # ---- 收盘价 + 累计收益 ----
    p_trend = figure(
        title="收盘价走势 & 累计收益率(%)",
        x_axis_type="datetime",
        width=1100,
        height=220,
        tools=_tools(),
        active_drag="pan",
        active_scroll="wheel_zoom",
        x_range=p_k.x_range,
        toolbar_location="right",
    )
    p_trend.extra_y_ranges = {
        "cum": Range1d(
            start=float(data["cum_ret"].min()) - 1,
            end=float(data["cum_ret"].max()) + 1,
        )
    }
    p_trend.add_layout(LinearAxis(y_range_name="cum", axis_label="累计收益率(%)"), "right")
    p_trend.line(
        "date",
        "close",
        source=source,
        line_color=LINE_COLOR,
        line_width=2,
        legend_label="收盘价",
    )
    p_trend.line(
        "date",
        "cum_ret",
        source=source,
        y_range_name="cum",
        line_color=CUM_COLOR,
        line_width=2,
        legend_label="累计收益%",
    )
    p_trend.add_tools(
        HoverTool(
            tooltips=[
                ("日期", "@date_str"),
                ("收盘价", "@close{0.00}"),
                ("累计收益%", "@cum_ret{0.00}"),
            ],
            mode="vline",
        )
    )
    p_trend.legend.location = "top_left"
    p_trend.legend.click_policy = "hide"
    p_trend.grid.grid_line_alpha = 0.25

    # ---- 日收益率 ----
    p_ret = figure(
        title="日收益率(%)",
        x_axis_type="datetime",
        width=1100,
        height=180,
        tools=_tools(),
        active_drag="pan",
        active_scroll="wheel_zoom",
        x_range=p_k.x_range,
        toolbar_location="right",
    )
    p_ret.vbar(
        "date",
        width,
        top="ret",
        fill_color="ret_color",
        line_color="ret_color",
        source=source,
    )
    p_ret.add_layout(Span(location=0, dimension="width", line_color="#666", line_width=1))
    p_ret.add_tools(
        HoverTool(
            tooltips=[("日期", "@date_str"), ("日收益率%", "@ret{0.00}")],
            mode="vline",
        )
    )
    p_ret.grid.grid_line_alpha = 0.25

    # ---- 月/年度收益 ----
    yearly = _yearly_returns(data)
    period_df = yearly if len(yearly) >= 2 else _monthly_returns(data)
    period_title = "年度收益率(%)" if len(yearly) >= 2 else "月度收益率(%)"
    if not period_df.empty:
        p_colors = np.where(period_df["ret"].to_numpy() >= 0, UP_COLOR, DOWN_COLOR)
        p_src = ColumnDataSource(
            data=dict(
                period=period_df["period"].tolist(),
                ret=period_df["ret"].to_numpy(),
                color=p_colors,
            )
        )
        p_period = figure(
            title=period_title + "（与默认页同类柱状收益）",
            x_range=period_df["period"].tolist(),
            width=1100,
            height=220,
            tools="pan,wheel_zoom,box_zoom,reset,save,hover",
            toolbar_location="right",
        )
        p_period.vbar(
            x="period",
            top="ret",
            width=0.6,
            fill_color="color",
            line_color="color",
            source=p_src,
        )
        p_period.add_layout(
            Span(location=0, dimension="width", line_color="#666", line_width=1)
        )
        p_period.add_tools(
            HoverTool(tooltips=[("周期", "@period"), ("收益率%", "@ret{0.00}")])
        )
        p_period.xaxis.major_label_orientation = 0.8
        p_period.grid.grid_line_alpha = 0.25
    else:
        p_period = figure(title=period_title + "（样本不足）", width=1100, height=120, tools="")

    flow_plots = _build_fund_flow_plots(fund_flow, northbound, kline=data)
    # 仅 K 线组共用十字线；资金流为分类轴，绝不与 datetime 轴联动
    _add_crosshair([p_k, p_vol, p_trend, p_ret])

    finance_plots = _build_finance_plots(finance)
    k_block = column(p_k, p_vol, p_trend, p_ret, sizing_mode="stretch_width")
    flow_block = column(*flow_plots, sizing_mode="stretch_width")
    finance_block = column(*finance_plots, sizing_mode="stretch_width")
    layout = column(
        k_block, flow_block, finance_block, p_period, sizing_mode="stretch_width"
    )
    # 用标题注释 meta
    p_k.title.text = f"{title} - K线 / MA\n{meta_line}"
    p_k.title.text_font_size = "11pt"
    return layout


def _fmt_num(v, nd=2, suffix=""):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "-"
    try:
        return f"{float(v):.{nd}f}{suffix}"
    except (TypeError, ValueError):
        return str(v)


def _segment_table_html(title: str, items: list, report_date: str = "") -> str:
    if not items:
        return ""
    rows_html = []
    for it in items:
        name = _html_escape(it.get("name") or "")
        ratio = it.get("ratio")
        income = it.get("income_yi")
        gross = it.get("gross_margin")
        ratio_s = _fmt_num(ratio, 1, "%") if ratio is not None else "-"
        income_s = _fmt_num(income, 2) if income is not None else "-"
        gross_s = _fmt_num(gross, 1, "%") if gross is not None else "-"
        bar_w = 0
        try:
            bar_w = max(0, min(100, float(ratio or 0)))
        except (TypeError, ValueError):
            bar_w = 0
        rows_html.append(
            f"<tr>"
            f"<td style='padding:4px 8px;white-space:nowrap'>{name}</td>"
            f"<td style='padding:4px 8px;text-align:right;font-weight:600'>{ratio_s}</td>"
            f"<td style='padding:4px 8px;min-width:120px'>"
            f"<div style='background:#e8eef5;border-radius:2px;height:8px'>"
            f"<div style='width:{bar_w:.1f}%;background:#3b6ea5;height:8px;border-radius:2px'></div>"
            f"</div></td>"
            f"<td style='padding:4px 8px;text-align:right'>{income_s}</td>"
            f"<td style='padding:4px 8px;text-align:right'>{gross_s}</td>"
            f"</tr>"
        )
    date_tip = f"（报告期 {_html_escape(report_date)}）" if report_date else ""
    return f"""
    <div style="margin-top:10px">
      <div style="font-weight:700;color:#222;margin-bottom:4px">{_html_escape(title)}{date_tip}</div>
      <table style="width:100%;border-collapse:collapse;font-size:12.5px;color:#333">
        <thead>
          <tr style="background:#f3f6fa;color:#555">
            <th style="text-align:left;padding:4px 8px;font-weight:600">业务/项目</th>
            <th style="text-align:right;padding:4px 8px;font-weight:600">收入占比</th>
            <th style="padding:4px 8px"></th>
            <th style="text-align:right;padding:4px 8px;font-weight:600">收入(亿)</th>
            <th style="text-align:right;padding:4px 8px;font-weight:600">毛利率</th>
          </tr>
        </thead>
        <tbody>
          {''.join(rows_html)}
        </tbody>
      </table>
    </div>
    """


def _company_profile_inner_html(profile: Optional[dict]) -> str:
    """公司介绍 HTML 片段（不含外层卡片）。"""
    p = profile or {}
    full_name = p.get("full_name") or p.get("name") or ""
    code = p.get("code") or ""
    industry = p.get("industry") or ""
    csrc = p.get("csrc_industry") or ""
    intro = (p.get("intro") or "").strip()
    scope = (p.get("business_scope") or "").strip()
    main_biz = (p.get("main_business") or "").strip()
    review = (p.get("business_review") or "").strip()
    seg_date = p.get("segment_date") or ""

    meta_bits = []
    if industry:
        meta_bits.append(f"所属行业 <b>{_html_escape(industry)}</b>")
    if csrc:
        meta_bits.append(f"证监会行业 <b>{_html_escape(csrc)}</b>")
    if p.get("registered_capital"):
        meta_bits.append(f"注册资本 <b>{_html_escape(p.get('registered_capital'))}</b>")
    if p.get("employees"):
        meta_bits.append(f"员工 <b>{_html_escape(p.get('employees'))}</b> 人")
    if p.get("found_date"):
        meta_bits.append(f"成立 <b>{_html_escape(p.get('found_date'))}</b>")
    if p.get("list_date"):
        meta_bits.append(f"上市 <b>{_html_escape(p.get('list_date'))}</b>")
    if p.get("website"):
        host = str(p.get("website") or "").replace("http://", "").replace("https://", "")
        meta_bits.append(
            f'官网 <a href="http://{_html_escape(host)}" target="_blank" rel="noopener">'
            f"{_html_escape(p.get('website'))}</a>"
        )

    def _para(label: str, text: str, max_len: int = 900) -> str:
        if not text:
            return ""
        t = text if len(text) <= max_len else text[:max_len].rstrip() + "…"
        return (
            f"<div style='margin-top:8px'>"
            f"<div style='font-weight:700;color:#222;margin-bottom:2px'>{_html_escape(label)}</div>"
            f"<div style='color:#444;line-height:1.65;text-align:justify'>{_html_escape(t)}</div>"
            f"</div>"
        )

    segs_prod = p.get("segments_product") or []
    segs_ind = p.get("segments_industry") or []
    segs_reg = p.get("segments_region") or []
    primary = segs_prod or segs_ind
    primary_title = "主营构成 · 按产品" if segs_prod else "主营构成 · 按行业"
    seg_html = _segment_table_html(primary_title, primary, seg_date)
    if segs_reg:
        seg_html += _segment_table_html("主营构成 · 按地区", segs_reg, seg_date)
    if segs_prod and segs_ind:
        seg_html += _segment_table_html("主营构成 · 按行业", segs_ind, seg_date)

    lead_line = ""
    if primary:
        tops = [x for x in primary if (x.get("ratio") or 0) >= 5][:3]
        if not tops:
            tops = primary[:2]
        parts = []
        for x in tops:
            r = x.get("ratio")
            parts.append(f"{_html_escape(x.get('name'))}（{_fmt_num(r, 1)}%）")
        if parts:
            lead_line = (
                f"<div style='margin-top:8px;padding:6px 8px;background:#f7fafc;"
                f"border-left:3px solid #3b6ea5;color:#333'>"
                f"<b>主要收入来源：</b>{'、'.join(parts)}"
                f"</div>"
            )

    title = _html_escape(full_name or "公司介绍")
    code_s = f"（{_html_escape(code)}）" if code else ""
    if not any([intro, scope, main_biz, primary, industry]):
        return (
            '<div style="padding:4px 0;color:#888">暂无公司介绍数据</div>'
        )

    return f"""
      <div style="font-size:15px;font-weight:700;color:#1a1a1a;margin-bottom:4px">
        公司介绍 · {title}{code_s}
      </div>
      <div style="color:#555;line-height:1.8">{' &nbsp;|&nbsp; '.join(meta_bits) if meta_bits else ''}</div>
      {lead_line}
      {_para('公司简介', intro, 1200)}
      {_para('主营业务', main_biz, 400)}
      {_para('经营范围', scope, 500)}
      {seg_html}
      {_para('经营评述', review, 600)}
    """


def _estimate_profile_height(profile: Optional[dict], val_extra: int = 0) -> int:
    """按内容估算 Div 占位高度，避免与下方组件重叠。"""
    p = profile or {}
    intro = (p.get("intro") or "")[:1200]
    scope = (p.get("business_scope") or "")[:500]
    review = (p.get("business_review") or "")[:600]
    n_seg = (
        len(p.get("segments_product") or [])
        + len(p.get("segments_region") or [])
        + len(p.get("segments_industry") or [])
    )
    # 约每 42 字一行、行高 ~20；表格行 ~26
    text_h = 20 * (1 + (len(intro) + len(scope) + len(review)) // 42)
    h = 90 + text_h + 26 * max(n_seg, 0) + 80 + val_extra
    return int(min(max(h, 120), 960))


def _build_finance_head_div(finance: Optional[dict]) -> Div:
    """
    公司介绍 + 财务估值：合并进同一个 Div，中间用分隔线，
    避免两个 Bokeh Div 高度估算不准导致文字叠在一起。
    """
    finance = finance or {}
    profile = finance.get("profile") or {}
    val = finance.get("valuation") or {}

    name = val.get("name") or profile.get("name") or ""
    code = val.get("code") or profile.get("code") or ""
    head = f"{name}({code})" if name else (code or "个股")
    profile_html = _company_profile_inner_html(profile)
    val_html = f"""
      <div style="margin-top:14px;padding-top:12px;border-top:1px solid #dce3ea">
        <div style="font-size:14px;font-weight:700;color:#1a1a1a;margin-bottom:4px">
          财务估值 · {_html_escape(head)}
        </div>
        <div style="line-height:1.75;color:#333">
          市盈率(TTM) <b>{_fmt_num(val.get('pe_ttm'))}</b>
          &nbsp;|&nbsp; 市盈率(静) <b>{_fmt_num(val.get('pe_static'))}</b>
          &nbsp;|&nbsp; 市净率 <b>{_fmt_num(val.get('pb'))}</b>
          &nbsp;|&nbsp; 市销率 <b>{_fmt_num(val.get('ps'))}</b>
          &nbsp;|&nbsp; PEG <b>{_fmt_num(val.get('peg'))}</b>
          &nbsp;|&nbsp; 市现率 <b>{_fmt_num(val.get('pcf'))}</b>
          <br/>
          总市值 <b>{_fmt_num(val.get('mcap'), 2)} 亿</b>
          &nbsp;|&nbsp; 流通市值 <b>{_fmt_num(val.get('float_mcap'), 2)} 亿</b>
          &nbsp;|&nbsp; 最新价 <b>{_fmt_num(val.get('price'))}</b>
          &nbsp;|&nbsp; 涨跌幅 <b>{_fmt_num(val.get('pct'), 2)}%</b>
          &nbsp;|&nbsp; ROE <b>{_fmt_num(val.get('roe'), 2)}%</b>
          &nbsp;|&nbsp; 估值日 {_html_escape(val.get('date') or '-')}
        </div>
      </div>
    """
    html = f"""
    <div style="padding:12px 14px;margin:0;background:#fafbfc;border:1px solid #e6ebf0;
                border-radius:4px;font-family:Microsoft YaHei,Arial,sans-serif;font-size:13px;color:#333;
                box-sizing:border-box;height:100%;overflow-y:auto">
      {profile_html}
      {val_html}
    </div>
    """
    h = _estimate_profile_height(profile, val_extra=72)
    # 略留余量，内容过长在卡片内滚动，绝不盖住下方图表
    return Div(text=html, width=1100, height=min(h + 24, 980))


def _build_finance_plots(finance: Optional[dict]):
    """公司介绍 + 估值 + 同花顺风格营收/净利柱图（年报+当年季度，红涨绿跌，中位数趋势）。"""
    plots = []
    finance = finance or {}
    income = finance.get("income")
    if income is None:
        income = pd.DataFrame()

    # 公司介绍与估值同一卡片，避免重叠
    plots.append(_build_finance_head_div(finance))

    if income is None or income.empty:
        plots.append(
            Div(
                text='<div style="color:#888;padding:8px">暂无财务报告期数据（营收/净利润）</div>',
                width=1100,
                height=36,
            )
        )
        return plots

    from qbot.data.stock_finance import prepare_ths_style_income

    chart_df = prepare_ths_style_income(income)
    if chart_df is None or chart_df.empty:
        plots.append(
            Div(
                text='<div style="color:#888;padding:8px">暂无可用年报/季度财务数据</div>',
                width=1100,
                height=36,
            )
        )
        return plots

    labels = [str(x) for x in chart_df["报告期标签"].tolist()]
    kinds = [str(x) for x in chart_df["口径"].tolist()]
    rev = pd.to_numeric(chart_df["营业收入_亿"], errors="coerce")
    profit = pd.to_numeric(chart_df["净利润_亿"], errors="coerce")
    rev_yoy = pd.to_numeric(chart_df["营收同比"], errors="coerce")
    p_yoy = pd.to_numeric(chart_df["净利同比"], errors="coerce")

    def _signed_bar_parts(series: pd.Series):
        """正值向上红柱，负值向下绿柱。"""
        tops, bottoms, colors, values = [], [], [], []
        for v in series:
            if pd.isna(v):
                tops.append(0.0)
                bottoms.append(0.0)
                colors.append("#bbbbbb")
                values.append(None)
                continue
            x = float(v)
            values.append(x)
            if x >= 0:
                tops.append(x)
                bottoms.append(0.0)
                colors.append(UP_COLOR)
            else:
                tops.append(0.0)
                bottoms.append(x)
                colors.append(DOWN_COLOR)
        return tops, bottoms, colors, values

    def _money_bar(title: str, values: pd.Series, yoy: pd.Series, unit_note: str):
        tops, bottoms, colors, vals = _signed_bar_parts(values)
        yoy_list = [None if pd.isna(v) else float(v) for v in yoy]
        finite = [v for v in vals if v is not None]
        median_v = float(np.nanmedian(finite)) if finite else None

        src = ColumnDataSource(
            data=dict(
                period=labels,
                kind=kinds,
                top=tops,
                bottom=bottoms,
                value=vals,
                yoy=yoy_list,
                color=colors,
                # 趋势折线用真实值（含负）
                line_y=[0.0 if v is None else v for v in vals],
            )
        )
        p = figure(
            title=title,
            x_range=FactorRange(*labels),
            width=1100,
            height=300,
            tools="pan,wheel_zoom,box_zoom,reset,save,hover",
            toolbar_location="above",
        )
        bars = p.vbar(
            x="period",
            top="top",
            bottom="bottom",
            width=0.55,
            source=src,
            fill_color="color",
            line_color="color",
            name="bars",
        )
        # 趋势折线（连接各期金额）
        line = p.line(
            x="period",
            y="line_y",
            source=src,
            line_color="#1f4e79",
            line_width=2,
            line_alpha=0.9,
            name="trend",
        )
        p.circle(
            x="period",
            y="line_y",
            source=src,
            size=6,
            fill_color="#1f4e79",
            line_color="white",
            line_width=1,
            name="trend_pts",
        )
        # 中位数参考线（数值写在标题里，避免 Label 在分类轴上报类型错误）
        if median_v is not None and not np.isnan(median_v):
            p.add_layout(
                Span(
                    location=median_v,
                    dimension="width",
                    line_color="#e6a23c",
                    line_dash="dashed",
                    line_width=2,
                    line_alpha=0.95,
                )
            )

        p.add_tools(
            HoverTool(
                renderers=[bars],
                tooltips=[
                    ("报告期", "@period"),
                    ("口径", "@kind"),
                    ("金额(亿)", "@value{0.00}"),
                    ("同比%", "@yoy{0.00}"),
                ],
            )
        )
        p.xaxis.major_label_orientation = 0.85
        p.yaxis.axis_label = "亿元"
        p.grid.grid_line_alpha = 0.25
        p.add_layout(Span(location=0, dimension="width", line_color="#666", line_width=1))
        # 图例
        legend = Legend(
            items=[
                LegendItem(label="盈利/正值", renderers=[bars]),
                LegendItem(label="趋势线", renderers=[line]),
            ],
            location="top_left",
            orientation="horizontal",
            label_text_font_size="9pt",
            padding=4,
            spacing=8,
            background_fill_alpha=0.7,
        )
        p.add_layout(legend)
        # 说明：负值=绿柱向下
        note = unit_note
        if median_v is not None:
            note += f"  |  中位数 {_fmt_num(median_v, 2)} 亿（虚线）"
        p.title.text = f"{title}\n{note}"
        p.title.text_font_size = "10.5pt"
        return p

    plots.append(
        _money_bar(
            "营业收入（亿元）",
            rev,
            rev_yoy,
            "前四年=年报全年累计，今年=单季；红柱向上=正，绿柱向下=负",
        )
    )
    plots.append(
        _money_bar(
            "净利润（亿元）",
            profit,
            p_yoy,
            "前四年=年报全年累计，今年=单季；红柱向上=盈利，绿柱向下=亏损",
        )
    )

    # 同比：同样红正绿负、向下
    def _yoy_bar(title: str, yoy: pd.Series):
        tops, bottoms, colors, vals = _signed_bar_parts(yoy)
        finite = [v for v in vals if v is not None]
        median_v = float(np.nanmedian(finite)) if finite else None
        src = ColumnDataSource(
            data=dict(
                period=labels,
                top=tops,
                bottom=bottoms,
                value=vals,
                color=colors,
                line_y=[0.0 if v is None else v for v in vals],
            )
        )
        p = figure(
            title=title,
            x_range=FactorRange(*labels),
            width=1100,
            height=240,
            tools="pan,wheel_zoom,box_zoom,reset,save,hover",
            toolbar_location="above",
        )
        bars = p.vbar(
            x="period",
            top="top",
            bottom="bottom",
            width=0.5,
            source=src,
            fill_color="color",
            line_color="color",
        )
        p.line(
            x="period",
            y="line_y",
            source=src,
            line_color="#1f4e79",
            line_width=2,
        )
        p.circle(
            x="period",
            y="line_y",
            source=src,
            size=5,
            fill_color="#1f4e79",
            line_color="white",
        )
        if median_v is not None and not np.isnan(median_v):
            p.add_layout(
                Span(
                    location=median_v,
                    dimension="width",
                    line_color="#e6a23c",
                    line_dash="dashed",
                    line_width=2,
                )
            )
        p.add_tools(
            HoverTool(
                renderers=[bars],
                tooltips=[("报告期", "@period"), ("同比%", "@value{0.00}")],
            )
        )
        p.xaxis.major_label_orientation = 0.85
        p.yaxis.axis_label = "%"
        p.grid.grid_line_alpha = 0.25
        p.add_layout(Span(location=0, dimension="width", line_color="#666", line_width=1))
        return p

    plots.append(_yoy_bar("营业收入同比（%）— 红正绿负", rev_yoy))
    plots.append(_yoy_bar("净利润同比（%）— 红正绿负", p_yoy))
    return plots


def render_quote_dashboard(
    df: pd.DataFrame,
    output_path: Union[str, Path],
    title: str = "行情可视化",
    meta: Optional[dict] = None,
) -> Path:
    """
    生成与默认回测页同类的 Bokeh 交互 HTML：
    鼠标悬停显示日期/OHLC/收益率/资金流向，支持拖动、滚轮缩放、框选放大、复位。
    """
    data = _prepare_ohlcv(df)
    meta = meta or {}
    start = data["date"].iloc[0].strftime("%Y-%m-%d")
    end = data["date"].iloc[-1].strftime("%Y-%m-%d")
    total_ret = float(data["cum_ret"].iloc[-1])

    # 拉取资金流向（失败不阻断主图）
    fund_flow = pd.DataFrame()
    northbound = pd.DataFrame()
    flow_note = ""
    finance = None
    code = meta.get("code") or ""
    if code:
        try:
            # 资金流只看近 30 个交易日（efinance/东财），不跟长 K 线全区间对齐
            from qbot.data.fund_flow import DEFAULT_LOOKBACK_DAYS, fetch_fund_flow_bundle

            bundle = fetch_fund_flow_bundle(code, lookback_days=DEFAULT_LOOKBACK_DAYS)
            # 注意：不能对 DataFrame 用 `x or default`（多行时 bool(df) 会抛歧义异常）
            fund_flow = bundle.get("fund_flow")
            if fund_flow is None:
                fund_flow = pd.DataFrame()
            northbound = bundle.get("northbound")
            if northbound is None:
                northbound = pd.DataFrame()
            if bundle.get("errors"):
                flow_note = "；".join(bundle["errors"])
            elif len(fund_flow) > 0 and len(fund_flow) < 5:
                flow_note = (
                    f"主力资金仅 {len(fund_flow)} 日（东财历史接口受限，当前多为 delay 当日）"
                )
        except Exception as exc:  # noqa: BLE001
            flow_note = f"资金流向获取异常: {exc}"
            fund_flow = pd.DataFrame()
            northbound = pd.DataFrame()
        try:
            from qbot.data.stock_finance import fetch_finance_bundle

            finance = fetch_finance_bundle(code)
        except Exception as exc:  # noqa: BLE001
            finance = {"valuation": {"code": code}, "income": pd.DataFrame(), "error": str(exc)}

    meta_line = (
        f"区间 {start} ~ {end} | 样本 {len(data)} | 累计收益 {total_ret:.2f}% | "
        f"数据源 {meta.get('source', '-')} | 周期 {meta.get('period', '-')} | "
        f"复权 {meta.get('adjust', '-')}"
    )
    if len(fund_flow):
        meta_line += f" | 资金流 {len(fund_flow)} 日"
    if len(northbound):
        meta_line += f" | 北向 {len(northbound)} 日"
    if flow_note:
        meta_line += f" | {flow_note}"
    if finance and (finance.get("valuation") or {}).get("pe_ttm") is not None:
        meta_line += f" | PE(TTM) {(finance['valuation'].get('pe_ttm')):.2f}"
    if finance and (finance.get("valuation") or {}).get("pb") is not None:
        meta_line += f" | PB {(finance['valuation'].get('pb')):.2f}"

    layout = build_interactive_layout(
        data,
        title,
        meta_line,
        fund_flow=fund_flow,
        northbound=northbound,
        finance=finance,
    )
    # INLINE：本地 file:// 打开不依赖外网 CDN（默认页用 CDN，WebView 更稳用内联）
    html = file_html(layout, INLINE, title=title)

    # 底部：个股近一周新闻 + 公告（替代原先最近 20 条行情表）
    news_html = _build_stock_news_section(code)

    tip = """
<style>
body { font-family: "Microsoft YaHei", "Segoe UI", Arial, sans-serif; margin: 0; padding: 10px 12px 20px; background:#f0f2f5; color:#222; }
.tip { display:none; }
.page-head { background:linear-gradient(180deg,#fff 0%,#f7f9fc 100%); border:1px solid #e5e8ef; border-radius:6px; padding:10px 14px; margin-bottom:10px; }
.page-head .tag { display:inline-block; background:#e8f1ff; color:#1a5fb4; font-size:12px; padding:2px 8px; border-radius:3px; margin-right:8px; }
.page-head .desc { color:#666; font-size:12px; margin-top:6px; line-height:1.5; }
.sec-bar { margin:14px 0 8px; padding:6px 10px; border-left:3px solid #d4380d; background:#fff; border-radius:0 4px 4px 0; font-size:14px; font-weight:600; color:#333; }
.card { background:#fff; border:1px solid #e5e8ef; border-radius:6px; padding:8px; margin-bottom:10px; box-shadow:0 1px 2px rgba(0,0,0,.03); }
.quote-table { border-collapse:collapse; width:100%; font-size:12px; background:#fff; }
.quote-table th,.quote-table td { border:1px solid #eee; padding:6px 8px; text-align:left; vertical-align:top; }
.quote-table th { background:#f5f7fa; color:#555; }
.quote-table a { color:#1a5fb4; text-decoration:none; }
.quote-table a:hover { text-decoration:underline; }
.quote-table .kind-news { color:#1a5fb4; }
.quote-table .kind-ann { color:#c64600; }
.sec-title { margin:14px 0 8px; font-size:14px; font-weight:600; border-left:3px solid #1a5fb4; padding-left:8px; }
</style>
<div class="page-head">
  <span class="tag">日K / 资金 / 财务 / 资讯</span>
  <span style="font-size:13px;color:#333">专业复盘视图（上方另有分时与实时主力净流入面板）</span>
  <div class="desc">滚轮缩放 · 拖动平移 · 悬停看明细 · 近30日资金流 · 财务估值与营收净利 · 近一周新闻公告</div>
</div>
"""
    if "<body>" in html:
        html = html.replace("<body>", "<body>" + tip + '<div class="card">', 1)
        html = html.replace(
            "</body>",
            '</div><div class="sec-bar">资讯中心</div>' + news_html + "</body>",
            1,
        )
    else:
        html = tip + html + news_html

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    return output_path


def _html_escape(text: str) -> str:
    return (
        str(text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _fmt_html_num(v, nd=2, suffix=""):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "-"
    try:
        return f"{float(v):.{nd}f}{suffix}"
    except (TypeError, ValueError):
        return str(v)


def _sign_cls(v) -> str:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "flat"
    if x > 0:
        return "up"
    if x < 0:
        return "down"
    return "flat"


def render_intraday_dashboard(
    code: str,
    output_path: Union[str, Path],
    name: str = "",
) -> Path:
    """
    东财风格：顶部报价条 + 分时价格 + 分时主力净流入（轻量，适合盘中刷新）。
    """
    from qbot.data.intraday import fetch_intraday_bundle

    bundle = fetch_intraday_bundle(code)
    quote = bundle.get("quote") or {}
    trends = bundle.get("trends")
    fflow = bundle.get("fflow")
    if trends is None:
        trends = pd.DataFrame()
    if fflow is None:
        fflow = pd.DataFrame()

    name = name or quote.get("name") or ""
    title = f"{name} {code}".strip()
    pct = quote.get("pct")
    price = quote.get("price")
    main_yi = quote.get("main_net_yi")
    main_pct = quote.get("main_pct")
    updated = bundle.get("updated_at") or ""
    trading = bool(bundle.get("trading"))

    # ---- 分时价 ----
    plots = []
    header = f"""
    <div class="qx-head">
      <div class="qx-title">
        <span class="name">{_html_escape(name or code)}</span>
        <span class="code">{_html_escape(code)}</span>
        <span class="badge">{'盘中刷新' if trading else '已收盘/休市'}</span>
      </div>
      <div class="qx-price {_sign_cls(pct)}">
        <span class="px">{_fmt_html_num(price)}</span>
        <span class="chg">{_fmt_html_num(quote.get('change'))} / {_fmt_html_num(pct)}%</span>
      </div>
      <div class="qx-metrics">
        <span>开 {_fmt_html_num(quote.get('open'))}</span>
        <span>高 {_fmt_html_num(quote.get('high'))}</span>
        <span>低 {_fmt_html_num(quote.get('low'))}</span>
        <span>昨收 {_fmt_html_num(quote.get('pre_close'))}</span>
        <span>换手 {_fmt_html_num(quote.get('turnover'))}%</span>
        <span>PE(TTM) {_fmt_html_num(quote.get('pe_ttm'))}</span>
        <span>市净率 {_fmt_html_num(quote.get('pb'))}</span>
        <span>总市值 {_fmt_html_num(quote.get('mcap'), 0)}亿</span>
        <span class="{_sign_cls(main_yi)}">主力净流入 {_fmt_html_num(main_yi)}亿</span>
        <span class="{_sign_cls(main_pct)}">主力净占比 {_fmt_html_num(main_pct)}%</span>
      </div>
      <div class="qx-sub">更新 { _html_escape(updated) } · 分时与主力资金为当日累计口径（东财）</div>
    </div>
    """
    plots.append(Div(text=header, width=1180, height=118))

    pre = quote.get("pre_close")
    if trends is not None and not trends.empty:
        labels = trends["time_label"].astype(str).tolist()
        src = ColumnDataSource(
            data=dict(
                t=labels,
                price=trends["price"].astype(float).tolist(),
                avg=trends["avg"].astype(float).tolist(),
                vol=trends["volume"].astype(float).tolist(),
                pct=[None if pd.isna(x) else float(x) for x in trends.get("pct", [])]
                if "pct" in trends.columns
                else [None] * len(trends),
            )
        )
        p_price = figure(
            title="分时走势",
            x_range=FactorRange(*labels),
            width=1180,
            height=280,
            tools="pan,wheel_zoom,box_zoom,reset,save,hover",
            toolbar_location="above",
        )
        p_price.line("t", "price", source=src, line_color="#d4380d", line_width=1.8, legend_label="现价")
        p_price.line("t", "avg", source=src, line_color="#faad14", line_width=1.2, legend_label="均价")
        if pre not in (None, ""):
            try:
                p_price.add_layout(
                    Span(
                        location=float(pre),
                        dimension="width",
                        line_color="#999",
                        line_dash="dashed",
                        line_width=1,
                    )
                )
            except (TypeError, ValueError):
                pass
        p_price.add_tools(
            HoverTool(
                tooltips=[
                    ("时间", "@t"),
                    ("现价", "@price{0.00}"),
                    ("均价", "@avg{0.00}"),
                    ("涨跌%", "@pct{0.00}"),
                    ("量", "@vol{0,0}"),
                ]
            )
        )
        p_price.legend.location = "top_left"
        p_price.legend.click_policy = "hide"
        p_price.xaxis.major_label_orientation = 1.0
        p_price.xaxis.major_label_text_font_size = "7pt"
        p_price.grid.grid_line_alpha = 0.2
        plots.append(p_price)

        p_vol = figure(
            title="分时成交量",
            x_range=p_price.x_range,
            width=1180,
            height=110,
            tools="pan,wheel_zoom,reset",
            toolbar_location=None,
        )
        colors = []
        prices = trends["price"].astype(float).tolist()
        for i, v in enumerate(prices):
            if i == 0:
                colors.append(UP_COLOR if (pre and v >= float(pre)) else DOWN_COLOR)
            else:
                colors.append(UP_COLOR if v >= prices[i - 1] else DOWN_COLOR)
        src_v = ColumnDataSource(data=dict(t=labels, vol=trends["volume"].astype(float).tolist(), color=colors))
        p_vol.vbar(x="t", top="vol", width=0.8, source=src_v, color="color", alpha=0.85)
        p_vol.xaxis.visible = False
        p_vol.grid.grid_line_alpha = 0.2
        plots.append(p_vol)
    else:
        plots.append(
            Div(
                text='<div style="padding:16px;color:#888">暂无分时数据（非交易日显示上一交易日）</div>',
                width=1180,
                height=40,
            )
        )

    # ---- 分时主力 ----
    if fflow is not None and not fflow.empty:
        labels_f = fflow["time_label"].astype(str).tolist()
        src_f = ColumnDataSource(
            data=dict(
                t=labels_f,
                main=fflow["main_net_yi"].astype(float).tolist(),
                super=fflow["super_net_yi"].astype(float).tolist(),
                large=fflow["large_net_yi"].astype(float).tolist(),
                mid=fflow["mid_net_yi"].astype(float).tolist(),
                retail=fflow["retail_net_yi"].astype(float).tolist(),
            )
        )
        p_f = figure(
            title="分时主力净流入（当日累计，亿元）",
            x_range=FactorRange(*labels_f),
            width=1180,
            height=240,
            tools="pan,wheel_zoom,box_zoom,reset,save,hover",
            toolbar_location="above",
        )
        p_f.line("t", "main", source=src_f, line_color="#d4380d", line_width=2, legend_label="主力")
        p_f.line("t", "super", source=src_f, line_color="#c0392b", line_width=1.2, legend_label="超大单")
        p_f.line("t", "large", source=src_f, line_color="#e67e22", line_width=1.2, legend_label="大单")
        p_f.line("t", "mid", source=src_f, line_color="#3498db", line_width=1, legend_label="中单")
        p_f.line("t", "retail", source=src_f, line_color="#27ae60", line_width=1, legend_label="散户")
        p_f.add_layout(Span(location=0, dimension="width", line_color="#666", line_width=1))
        p_f.add_tools(
            HoverTool(
                tooltips=[
                    ("时间", "@t"),
                    ("主力(亿)", "@main{0.00}"),
                    ("超大单", "@super{0.00}"),
                    ("大单", "@large{0.00}"),
                    ("中单", "@mid{0.00}"),
                    ("散户", "@retail{0.00}"),
                ]
            )
        )
        p_f.legend.location = "top_left"
        p_f.legend.click_policy = "hide"
        p_f.xaxis.major_label_orientation = 1.0
        p_f.xaxis.major_label_text_font_size = "7pt"
        p_f.yaxis.axis_label = "亿元"
        p_f.grid.grid_line_alpha = 0.2
        plots.append(p_f)
    else:
        plots.append(
            Div(
                text='<div style="padding:16px;color:#888">暂无分时资金流数据</div>',
                width=1180,
                height=40,
            )
        )

    layout = column(*plots, sizing_mode="stretch_width")
    html = file_html(layout, INLINE, title=f"{title} 分时")
    css = """
<style>
body { font-family:"Microsoft YaHei","Segoe UI",Arial,sans-serif; margin:0; padding:8px 10px 16px; background:#f0f2f5; }
.qx-head { background:#fff; border:1px solid #e5e8ef; border-radius:6px; padding:10px 14px; margin-bottom:8px; }
.qx-title .name { font-size:20px; font-weight:700; margin-right:8px; }
.qx-title .code { color:#888; font-size:13px; margin-right:10px; }
.qx-title .badge { display:inline-block; font-size:11px; padding:1px 8px; border-radius:3px; background:#fff1f0; color:#d4380d; border:1px solid #ffccc7; }
.qx-price { margin:6px 0; }
.qx-price .px { font-size:28px; font-weight:700; margin-right:12px; }
.qx-price .chg { font-size:16px; font-weight:600; }
.qx-price.up, .up { color:#ef232a; }
.qx-price.down, .down { color:#14b143; }
.qx-price.flat, .flat { color:#333; }
.qx-metrics { display:flex; flex-wrap:wrap; gap:8px 14px; font-size:12px; color:#555; margin-top:4px; }
.qx-metrics span { background:#f7f8fa; padding:2px 8px; border-radius:3px; }
.qx-sub { margin-top:6px; font-size:11px; color:#999; }
</style>
"""
    if "<body>" in html:
        html = html.replace("<body>", "<body>" + css, 1)
    else:
        html = css + html

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    return output_path


def _build_stock_news_section(code: str) -> str:
    """生成近一周新闻/公告 HTML 区块。"""
    if not code:
        return (
            '<h3 class="sec-title">近一周新闻与公告</h3>'
            '<div class="card"><p style="color:#888;margin:8px">未指定股票代码，无法拉取资讯。</p></div>'
        )
    try:
        from qbot.data.stock_news import fetch_stock_news_bundle

        news_df, ann_df = fetch_stock_news_bundle(code, days=7)
    except Exception as exc:  # noqa: BLE001
        return (
            '<h3 class="sec-title">近一周新闻与公告</h3>'
            f'<div class="card"><p style="color:#c00;margin:8px">资讯获取失败：{_html_escape(exc)}</p></div>'
        )

    def _table(df: pd.DataFrame, kind_cls: str) -> str:
        if df is None or df.empty:
            return '<p style="color:#888;margin:8px">近一周暂无记录。</p>'
        parts = [
            '<table class="quote-table"><thead><tr>'
            "<th style=\"width:130px\">时间</th>"
            "<th style=\"width:56px\">类型</th>"
            "<th style=\"width:100px\">来源/类别</th>"
            "<th>标题</th>"
            "</tr></thead><tbody>"
        ]
        for _, r in df.iterrows():
            title = _html_escape(r.get("title"))
            url = str(r.get("url") or "").strip()
            title_html = (
                f'<a href="{_html_escape(url)}" target="_blank" rel="noopener">{title}</a>'
                if url
                else title
            )
            summary = _html_escape(r.get("summary") or "")
            if summary and summary != _html_escape(r.get("source") or ""):
                title_html += f'<div style="color:#888;font-size:11px;margin-top:2px">{summary}</div>'
            parts.append(
                "<tr>"
                f"<td>{_html_escape(r.get('time'))}</td>"
                f'<td class="{kind_cls}">{_html_escape(r.get("kind"))}</td>'
                f"<td>{_html_escape(r.get('source'))}</td>"
                f"<td>{title_html}</td>"
                "</tr>"
            )
        parts.append("</tbody></table>")
        return "".join(parts)

    n_news = 0 if news_df is None else len(news_df)
    n_ann = 0 if ann_df is None else len(ann_df)
    return (
        f'<h3 class="sec-title">近一周相关新闻（{n_news}）</h3>'
        f'<div class="card" style="overflow:auto">{_table(news_df, "kind-news")}</div>'
        f'<h3 class="sec-title">近一周公司公告（{n_ann}）</h3>'
        f'<div class="card" style="overflow:auto">{_table(ann_df, "kind-ann")}</div>'
    )
