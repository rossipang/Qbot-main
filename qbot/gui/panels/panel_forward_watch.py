# -*- coding: utf-8 -*-
"""前瞻观察面板：新闻+板块资金 → 观察池；个股双星（主线星/买点星）+ 买入候选。"""

from __future__ import annotations

import threading

import pandas as pd
import wx
import wx.grid

from qbot.common.logging.logger import LOGGER as logger
from qbot.data.forward_watch import (
    build_forward_watch,
    forward_watch_to_frames,
    load_latest_forward_watch,
)
from qbot.data.industry_screener import add_to_watchlist
from qbot.data.intraday_watch import add_to_intraday_watch
from qbot.gui.panels.panel_intraday_watch import notify_intraday_panels
from qbot.gui.panels.panel_industry_screener import (
    SortableListCtrl,
    _event_row,
    _fix_grid_viewport,
    _fmt,
)

# 固定可视行数：多出的走表格内滚动，避免主题池把个股挤没
_THEME_VISIBLE_ROWS = 8
_SHORT_VISIBLE_ROWS = 6
_STOCK_VISIBLE_ROWS = 16
from qbot.gui.panels.panel_quote_detail import open_quote_detail

# 个股表列序（改列时同步改这里，避免再把行业当成代码）
# 信号0 主线星1 买点星2 涨跌概率3 风险4 连入5 状态6 候选7 方法8 操作9 持有出场10
# 概念11 行业12 代码13 名称14 现价15 建议16 涨跌17 5日18 依据19
_S_COL_CONCEPT = 11
_S_COL_CODE = 13
_S_COL_NAME = 14
_S_COL_PCT = 17
_S_COL_PCT5 = 18
_S_COL_BIAS = 3
_S_COL_RISK = 4

# 今日短线列序：信号0 代码1 名称2 板块3 现价4 建议买入5 买入方法6 风险值7 ML分8 涨跌%9 5日%10 操作11 持有12 入选13
_D_COL_CODE = 1
_D_COL_NAME = 2
_D_COL_BOARD = 3
_D_COL_PCT = 9
_D_COL_PCT5 = 10


class ForwardWatchPanel(wx.Panel):
    def __init__(self, parent):
        super().__init__(parent)
        self._busy = False
        self._payload = {}
        self._stock_details = {}  # code -> 详细依据
        self._detail_dialogs = []
        self._init_ui()
        wx.CallAfter(self._load_cached_or_refresh)

    def _init_ui(self):
        root = wx.BoxSizer(wx.VERTICAL)
        self.SetSizer(root)

        bar = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_refresh = wx.Button(self, label="刷新前瞻分析")
        self.btn_add = wx.Button(self, label="加入选股名单")
        self.btn_intraday = wx.Button(self, label="加入今日盯盘")
        self.btn_detail = wx.Button(self, label="查看详情K线")
        self.lbl_status = wx.StaticText(self, label="就绪：点击刷新，结合新闻与板块资金生成观察池")
        for w in (self.btn_refresh, self.btn_add, self.btn_intraday, self.btn_detail):
            bar.Add(w, 0, wx.ALL, 4)
        bar.Add(self.lbl_status, 1, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 8)
        root.Add(bar, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 4)

        tip = wx.StaticText(
            self,
            label=(
                "短线周转：跟热板、持有1～3天；买入方法多选一 + 风险值(-100~100，越负越不宜买)。"
                "盘口结构否决直拉/冲高回落追买；板强个弱不作补涨；持有出场看峰值回撤/双阴/破均线。"
                "短线排序含GBDT(ML分)+因子贡献；今日短线都有方法与风险值；个股仅候选=是才填买入方法。"
            ),
        )
        tip.Wrap(1100)
        root.Add(tip, 0, wx.ALL, 6)

        news_box = wx.StaticBox(self, label="财经+科技+医药新闻（已过滤）→ 关联概念/行业")
        news_sizer = wx.StaticBoxSizer(news_box, wx.VERTICAL)
        self.list_news = SortableListCtrl(self)
        self.list_news.set_columns(
            [
                ("时间", 90),
                ("频道", 40),
                ("来源", 50),
                ("标题", 400),
                ("相关概念/行业", 200),
            ]
        )
        self.list_news.SetMinSize((-1, 140))
        news_sizer.Add(self.list_news, 1, wx.EXPAND | wx.ALL, 4)
        root.Add(news_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 4)

        mid = wx.BoxSizer(wx.HORIZONTAL)

        left = wx.BoxSizer(wx.VERTICAL)

        # 今日短线：放在前瞻观察池上方，最多6只，空则只留标题+一行说明
        short_box = wx.StaticBox(
            self,
            label="今日短线（最多6只 · 跟热板 · 持有1～3天）",
        )
        self._short_sizer = wx.StaticBoxSizer(short_box, wx.VERTICAL)
        self.lbl_short_empty = wx.StaticText(
            self, label="暂无符合短线周转买点（热板连涨/回调续涨/轻涨承接）"
        )
        self.list_shorts = SortableListCtrl(self)
        self.list_shorts.set_columns(
            [
                ("信号", 70),
                ("代码", 58),
                ("名称", 70),
                ("板块", 90),
                ("现价", 55),
                ("建议买入", 85),
                ("买入方法", 90),
                ("风险值", 50),
                ("ML分", 50),
                ("涨跌%", 48),
                ("5日%", 48),
                ("操作建议", 90),
                ("持有出场", 90),
                ("入选原因", 150),
            ]
        )
        self._short_sizer.Add(self.lbl_short_empty, 0, wx.LEFT | wx.RIGHT | wx.TOP, 4)
        self._short_sizer.Add(self.list_shorts, 0, wx.EXPAND | wx.ALL, 4)
        _fix_grid_viewport(self.list_shorts, _SHORT_VISIBLE_ROWS)
        left.Add(self._short_sizer, 0, wx.EXPAND | wx.ALL, 2)
        self._set_short_empty(True)

        theme_box = wx.StaticBox(self, label="前瞻观察池（概念可共用 × 行业要细分）")
        theme_sizer = wx.StaticBoxSizer(theme_box, wx.VERTICAL)
        self.list_themes = SortableListCtrl(self)
        self.list_themes.set_columns(
            [
                ("信号", 70),
                ("状态", 55),
                ("星级", 55),
                ("概念", 90),
                ("细分行业", 100),
                ("今日%", 55),
                ("5日%", 55),
                ("流入亿", 65),
                ("新闻", 40),
                ("入池原因", 220),
                ("主题逻辑", 200),
            ]
        )
        theme_sizer.Add(self.list_themes, 0, wx.EXPAND | wx.ALL, 4)
        _fix_grid_viewport(self.list_themes, _THEME_VISIBLE_ROWS)
        left.Add(theme_sizer, 0, wx.EXPAND | wx.ALL, 2)

        stock_box = wx.StaticBox(
            self, label="个股观察池（点击/右键：查看详情 / 加入选股）"
        )
        stock_sizer = wx.StaticBoxSizer(stock_box, wx.VERTICAL)
        self.list_stocks = SortableListCtrl(self)
        self.list_stocks.set_columns(
            [
                ("信号", 70),
                ("主线星", 55),
                ("买点星", 55),
                ("涨跌概率", 78),
                ("风险值", 55),
                ("连入", 40),
                ("状态", 45),
                ("候选", 40),
                ("买入方法", 95),
                ("操作建议", 90),
                ("持有出场", 100),
                ("概念", 85),
                ("细分行业", 80),
                ("代码", 58),
                ("名称", 70),
                ("现价", 55),
                ("建议买入", 85),
                ("涨跌%", 50),
                ("5日%", 50),
                ("依据", 110),
            ]
        )
        stock_sizer.Add(self.list_stocks, 0, wx.EXPAND | wx.ALL, 4)
        _fix_grid_viewport(self.list_stocks, _STOCK_VISIBLE_ROWS)
        left.Add(stock_sizer, 1, wx.EXPAND | wx.ALL, 2)
        mid.Add(left, 3, wx.EXPAND)

        right_box = wx.StaticBox(self, label="选中个股：详细依据（新闻/资金/评分）")
        right_sizer = wx.StaticBoxSizer(right_box, wx.VERTICAL)
        self.txt_reason = wx.TextCtrl(
            self,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_WORDWRAP,
        )
        self.txt_reason.SetMinSize((280, -1))
        right_sizer.Add(self.txt_reason, 1, wx.EXPAND | wx.ALL, 4)
        mid.Add(right_sizer, 1, wx.EXPAND | wx.ALL, 2)

        root.Add(mid, 1, wx.EXPAND)

        # 刷新雾面：进度未完成时盖住数据区，避免看着旧缓存
        self.pnl_fog = wx.Panel(self, name="forwardFog")
        self.pnl_fog.SetBackgroundColour(wx.Colour(236, 238, 244))
        fog_sz = wx.BoxSizer(wx.VERTICAL)
        fog_sz.AddStretchSpacer(1)
        self.lbl_fog_title = wx.StaticText(self.pnl_fog, label="正在刷新前瞻分析")
        title_font = self.lbl_fog_title.GetFont()
        title_font.SetPointSize(max(12, title_font.GetPointSize() + 2))
        title_font.MakeBold()
        self.lbl_fog_title.SetFont(title_font)
        self.lbl_fog_msg = wx.StaticText(self.pnl_fog, label="准备中…")
        self.gauge = wx.Gauge(
            self.pnl_fog, range=100, style=wx.GA_HORIZONTAL | wx.GA_SMOOTH
        )
        self.gauge.SetMinSize((460, 20))
        for w in (self.lbl_fog_title, self.lbl_fog_msg, self.gauge):
            fog_sz.Add(w, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.ALL, 8)
        fog_sz.AddStretchSpacer(1)
        self.pnl_fog.SetSizer(fog_sz)
        self.pnl_fog.Hide()
        self.Bind(wx.EVT_SIZE, self._on_size_fog)

        self.btn_refresh.Bind(wx.EVT_BUTTON, lambda e: self.refresh())
        self.btn_add.Bind(wx.EVT_BUTTON, self.on_add_watch)
        self.btn_intraday.Bind(wx.EVT_BUTTON, self.on_add_intraday)
        self.btn_detail.Bind(wx.EVT_BUTTON, self.on_open_detail)
        self.list_stocks.Bind(wx.grid.EVT_GRID_SELECT_CELL, self.on_stock_select)
        # 与行业选股一致：点击/右键/双击弹出菜单，用正确代码开详情
        self.list_stocks.Bind(wx.grid.EVT_GRID_CELL_LEFT_CLICK, self.on_stock_click)
        self.list_stocks.Bind(wx.grid.EVT_GRID_CELL_RIGHT_CLICK, self.on_stock_click)
        self.list_stocks.Bind(wx.grid.EVT_GRID_CELL_LEFT_DCLICK, self.on_stock_click)
        self.list_shorts.Bind(wx.grid.EVT_GRID_SELECT_CELL, self.on_short_select)
        self.list_shorts.Bind(wx.grid.EVT_GRID_CELL_LEFT_CLICK, self.on_short_click)
        self.list_shorts.Bind(wx.grid.EVT_GRID_CELL_RIGHT_CLICK, self.on_short_click)
        self.list_shorts.Bind(wx.grid.EVT_GRID_CELL_LEFT_DCLICK, self.on_short_click)

    def _on_size_fog(self, event):
        if event is not None:
            event.Skip()
        if not hasattr(self, "pnl_fog"):
            return
        w, h = self.GetClientSize()
        top = 44
        if self.pnl_fog.IsShown():
            self.pnl_fog.SetPosition((0, top))
            self.pnl_fog.SetSize((max(1, w), max(1, h - top)))
            self.pnl_fog.Raise()

    def _clear_views(self):
        """清空列表，避免雾面下仍能瞥见旧数据。"""
        empty = {
            "weekly_news": [],
            "themes": [],
            "stocks": [],
            "daily_shorts": [],
            "note": "",
        }
        self._payload = empty
        self._stock_details = {}
        try:
            self._render(empty)
        except Exception:  # noqa: BLE001
            pass
        if hasattr(self, "txt_reason"):
            self.txt_reason.SetValue("正在刷新，请稍候…")

    def _set_fog(self, on: bool, msg: str = "正在刷新前瞻分析…"):
        if on:
            self._clear_views()
            self.lbl_fog_title.SetLabel("正在刷新前瞻分析")
            self.lbl_fog_msg.SetLabel(msg)
            self.gauge.SetValue(0)
            self.pnl_fog.Show()
            self._on_size_fog(None)
            self.btn_refresh.Enable(False)
            self.btn_add.Enable(False)
            self.btn_intraday.Enable(False)
            self.btn_detail.Enable(False)
        else:
            self.pnl_fog.Hide()
            self.btn_refresh.Enable(True)
            self.btn_add.Enable(True)
            self.btn_intraday.Enable(True)
            self.btn_detail.Enable(True)
        self.Layout()

    def _set_progress(self, pct: int, msg: str):
        if not self._busy:
            return
        try:
            self.gauge.SetValue(int(max(0, min(100, pct))))
            if msg:
                self.lbl_fog_msg.SetLabel(str(msg)[:80])
                self.lbl_fog_msg.Wrap(520)
            self._set_status(f"刷新中 {int(pct)}% · {msg}")
        except Exception:  # noqa: BLE001
            pass

    def _set_status(self, text: str):
        self.lbl_status.SetLabel(text)

    def _set_short_empty(self, empty: bool):
        """空仓时只留标题+一行说明，不占大块空白表。"""
        if empty:
            self.lbl_short_empty.Show()
            self.list_shorts.Hide()
            self.list_shorts.SetMinSize((-1, 1))
            self.list_shorts.SetMaxSize((-1, 1))
        else:
            self.lbl_short_empty.Hide()
            self.list_shorts.Show()
            _fix_grid_viewport(self.list_shorts, _SHORT_VISIBLE_ROWS)
        if self.GetSizer():
            self.Layout()

    def _load_cached_or_refresh(self):
        """启动：若磁盘已有当日缓存则先展示，再后台强制重刷，避免一直停在昨日界面。"""
        cached = load_latest_forward_watch() or {}
        asof = str(cached.get("asof") or "")
        from datetime import date

        today = date.today().isoformat()
        if asof == today and (cached.get("themes") or cached.get("stocks")):
            self._payload = cached
            self._render(cached)
            self._set_status(
                f"已加载今日缓存 {asof} {cached.get('updated_at') or ''}，正在后台复核刷新…"
            )
        else:
            self._set_status("无今日缓存，正在拉取最新行情与前瞻新闻…")
        self.refresh(force=True)

    def refresh(self, force: bool = False):
        # 后台线程若卡死会一直 _busy，导致按钮点不动、界面停在旧数据
        if self._busy and not force:
            self._set_status("上次刷新仍在进行；再点一次可强制重刷…")
            # 第二次点击允许抢占
            self._busy = False
        if self._busy:
            return
        self._busy = True
        self._set_fog(True, "开始拉取新闻与板块…")
        self._set_status("正在分析热点新闻与板块资金，生成前瞻观察池…")

        def on_progress(pct: int, msg: str):
            wx.CallAfter(self._set_progress, pct, msg)

        def work():
            err = ""
            payload = {}
            try:
                payload = build_forward_watch(persist=True, progress_cb=on_progress)
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                logger.error(f"前瞻观察失败: {exc}")
                payload = load_latest_forward_watch() or {
                    "themes": [],
                    "stocks": [],
                    "daily_shorts": [],
                    "errors": err,
                }
            # 构建失败但磁盘已有今日结果时，优先用磁盘，避免界面空白/旧内存
            if err or not (payload.get("themes") or payload.get("stocks")):
                disk = load_latest_forward_watch() or {}
                if disk.get("themes") or disk.get("stocks"):
                    payload = disk
                    if err:
                        payload = dict(payload)
                        payload["errors"] = err

            def done():
                self._busy = False
                self._set_fog(False)
                self._payload = payload
                self._render(payload)
                n_t = len(payload.get("themes") or [])
                n_s = len(payload.get("stocks") or [])
                n_d = len(payload.get("daily_shorts") or [])
                n_buy = sum(
                    1
                    for r in (payload.get("stocks") or [])
                    if str(r.get("买入候选")) == "是"
                )
                n_kick = len(payload.get("kicked") or [])
                n_frz = len(payload.get("frozen") or [])
                msg = (
                    err
                    or payload.get("errors")
                    or (
                        f"完成 {payload.get('asof')} {payload.get('updated_at')}: "
                        f"今日短线{n_d} / 主题{n_t} / 个股{n_s} / 买入候选{n_buy}"
                        + (f" / 走坏冻结{n_frz}" if n_frz else "")
                        + (f" / 两日踢出{n_kick}" if n_kick else "")
                        + ("（短线无符合周转买点）" if n_d == 0 else "")
                    )
                )
                self._set_status(msg)

            wx.CallAfter(done)

        threading.Thread(target=work, daemon=True).start()

    def _render(self, payload: dict):
        themes_df, stocks_df = forward_watch_to_frames(payload)
        self._stock_details = {}
        _sig_rank = {"红": 0, "橙": 1, "黄": 2, "绿": 3}

        # weekly news
        n_rows, n_keys = [], []
        for item in payload.get("weekly_news") or []:
            n_rows.append(
                [
                    str(item.get("时间") or ""),
                    str(item.get("频道") or item.get("channel") or "-"),
                    str(item.get("来源") or ""),
                    str(item.get("标题") or ""),
                    str(item.get("相关板块") or item.get("相关概念/行业") or "-"),
                ]
            )
            n_keys.append(
                (
                    str(item.get("时间") or ""),
                    str(item.get("频道") or ""),
                    str(item.get("来源") or ""),
                    str(item.get("标题") or ""),
                    str(item.get("相关板块") or ""),
                )
            )
        self.list_news.fill_rows(n_rows, n_keys)

        # themes
        t_rows, t_keys, t_colors, t_styles = [], [], {}, {}
        for _, r in themes_df.iterrows():
            sig = str(r.get("信号色") or "黄")
            sig_txt = str(r.get("信号") or "")
            concept = str(r.get("概念") or r.get("板块主题") or "")
            industry = str(r.get("行业") or r.get("匹配板块") or "-")
            idx = len(t_rows)
            t_rows.append(
                [
                    f"{sig}·{sig_txt}"[:18],
                    str(r.get("状态") or "观察中"),
                    str(r.get("星级显示") or ""),
                    concept,
                    industry,
                    _fmt(r.get("板块涨跌%")),
                    _fmt(r.get("板块5日%")),
                    _fmt(r.get("主力净流入亿")),
                    str(int(r.get("新闻条数") or 0)),
                    str(r.get("入池原因") or "")[:120],
                    str(r.get("主题逻辑") or "")[:160],
                ]
            )
            t_keys.append(
                (
                    _sig_rank.get(sig, 9),
                    str(r.get("状态") or ""),
                    float(r.get("星级") or 0),
                    concept,
                    industry,
                    float(r.get("板块涨跌%") or 0)
                    if pd.notna(r.get("板块涨跌%"))
                    else None,
                    float(r.get("板块5日%") or 0)
                    if pd.notna(r.get("板块5日%"))
                    else None,
                    float(r.get("主力净流入亿") or 0)
                    if pd.notna(r.get("主力净流入亿"))
                    else None,
                    int(r.get("新闻条数") or 0),
                    str(r.get("入池原因") or ""),
                    str(r.get("主题逻辑") or ""),
                )
            )
            t_colors[idx] = {
                5: r.get("板块涨跌%"),
                6: r.get("板块5日%"),
                7: r.get("主力净流入亿"),
            }
            t_styles[idx] = {"fg": sig, "fg_cols": [0, 2]}
        self.list_themes.fill_rows(t_rows, t_keys, t_colors, row_styles=t_styles)

        # 今日短线
        d_rows, d_keys, d_colors, d_styles = [], [], {}, {}
        for item in (payload.get("daily_shorts") or [])[:4]:
            code = str(item.get("代码") or "")
            if code:
                self._stock_details[code] = str(item.get("详细依据") or "")
            sig = str(item.get("信号色") or "黄")
            sig_lab = str(item.get("信号") or "")
            idx = len(d_rows)
            d_rows.append(
                [
                    f"{sig}·{sig_lab}"[:16],
                    code,
                    str(item.get("名称") or ""),
                    str(item.get("所属板块") or ""),
                    _fmt(item.get("最新价")),
                    str(item.get("建议买入") or "-"),
                    str(item.get("买入方法") or "")[:14],
                    _fmt(item.get("风险值")),
                    _fmt(item.get("ML分")),
                    _fmt(item.get("涨跌幅%")),
                    _fmt(item.get("5日涨跌%")),
                    str(item.get("操作建议") or "")[:40],
                    str(item.get("持有出场") or "")[:40],
                    str(item.get("因子贡献") or item.get("入选原因") or "")[:140],
                ]
            )
            d_keys.append(
                (
                    _sig_rank.get(sig, 9),
                    code,
                    str(item.get("名称") or ""),
                    str(item.get("所属板块") or ""),
                    float(item.get("最新价") or 0)
                    if item.get("最新价") is not None
                    else None,
                    str(item.get("建议买入") or ""),
                    str(item.get("买入方法") or ""),
                    float(item.get("风险值") or 0)
                    if item.get("风险值") is not None
                    else None,
                    float(item.get("ML分") or 0)
                    if item.get("ML分") is not None
                    else None,
                    float(item.get("涨跌幅%") or 0)
                    if item.get("涨跌幅%") is not None
                    else None,
                    float(item.get("5日涨跌%") or 0)
                    if item.get("5日涨跌%") is not None
                    else None,
                    str(item.get("操作建议") or ""),
                    str(item.get("持有出场") or ""),
                    str(item.get("因子贡献") or item.get("入选原因") or ""),
                )
            )
            d_colors[idx] = {
                _D_COL_PCT: item.get("涨跌幅%"),
                _D_COL_PCT5: item.get("5日涨跌%"),
            }
            d_styles[idx] = {"fg": sig, "fg_cols": [0]}
        self.list_shorts.fill_rows(d_rows, d_keys, d_colors, row_styles=d_styles)
        self._set_short_empty(len(d_rows) == 0)

        # stocks
        s_rows, s_keys, s_colors, s_styles = [], [], {}, {}
        for _, r in stocks_df.iterrows():
            code = str(r.get("代码") or "")
            self._stock_details[code] = str(r.get("详细依据") or "")
            sig = str(r.get("信号色") or "绿")
            sig_lab = str(r.get("信号") or "")
            idx = len(s_rows)
            s_rows.append(
                [
                    f"{sig}·{sig_lab}"[:16],
                    str(
                        r.get("主线星显示")
                        or r.get("主线星显示")
                        or r.get("星级显示")
                        or ""
                    ),
                    str(
                        r.get("买点星显示")
                        or r.get("买点星显示")
                        or r.get("星级显示")
                        or ""
                    ),
                    str(r.get("涨跌概率显示") or r.get("涨跌概率") or "-"),
                    _fmt(r.get("风险值")),
                    str(int(r.get("连入天数") or 0)),
                    str(r.get("当日状态") or "-"),
                    str(r.get("买入候选") or "否"),
                    str(r.get("买入方法") or "")[:14],
                    str(r.get("操作建议") or "")[:36],
                    str(r.get("持有出场") or "")[:40],
                    str(r.get("概念") or r.get("板块主题") or ""),
                    str(r.get("行业") or r.get("匹配板块") or ""),
                    code,
                    str(r.get("名称") or ""),
                    _fmt(r.get("最新价")),
                    str(r.get("建议买入") or "-"),
                    _fmt(r.get("涨跌幅%")),
                    _fmt(r.get("5日涨跌%")),
                    str(r.get("依据摘要") or "")[:80],
                ]
            )
            bias_v = None
            try:
                if pd.notna(r.get("涨跌概率")):
                    bias_v = float(r.get("涨跌概率"))
            except (TypeError, ValueError):
                bias_v = None
            risk_v = None
            try:
                if pd.notna(r.get("风险值")):
                    risk_v = float(r.get("风险值"))
            except (TypeError, ValueError):
                risk_v = None
            s_keys.append(
                (
                    _sig_rank.get(sig, 9),
                    float(r.get("主线星") or r.get("星级") or 0),
                    float(r.get("买点星") or r.get("星级") or 0),
                    bias_v if bias_v is not None else 0.0,
                    risk_v if risk_v is not None else 0.0,
                    int(r.get("连入天数") or 0),
                    0 if r.get("当日状态") == "走强" else 1,
                    1 if r.get("买入候选") == "是" else 0,
                    str(r.get("买入方法") or ""),
                    str(r.get("操作建议") or ""),
                    str(r.get("持有出场") or ""),
                    str(r.get("概念") or r.get("板块主题") or ""),
                    str(r.get("行业") or r.get("匹配板块") or ""),
                    code,
                    str(r.get("名称") or ""),
                    float(r.get("最新价") or 0) if pd.notna(r.get("最新价")) else None,
                    str(r.get("建议买入") or ""),
                    float(r.get("涨跌幅%") or 0) if pd.notna(r.get("涨跌幅%")) else None,
                    float(r.get("5日涨跌%") or 0) if pd.notna(r.get("5日涨跌%")) else None,
                    str(r.get("依据摘要") or ""),
                )
            )
            s_colors[idx] = {
                _S_COL_PCT: r.get("涨跌幅%"),
                _S_COL_PCT5: r.get("5日涨跌%"),
                _S_COL_BIAS: bias_v,
            }
            # 信号列0、主线星1、买点星2、涨跌概率3
            s_styles[idx] = {"fg": sig, "fg_cols": [0, 1, 2]}
        self.list_stocks.fill_rows(s_rows, s_keys, s_colors, row_styles=s_styles)

        note = str(payload.get("note") or "")
        if note and not self.txt_reason.GetValue():
            self.txt_reason.SetValue(note)

    def _stock_from_row(self, row: int):
        if row is None or row < 0:
            return "", "", ""
        code = self.list_stocks.get_cell_text(int(row), _S_COL_CODE)
        name = self.list_stocks.get_cell_text(int(row), _S_COL_NAME)
        concept = self.list_stocks.get_cell_text(int(row), _S_COL_CONCEPT)
        return code, name, concept

    def _short_from_row(self, row: int):
        if row is None or row < 0:
            return "", "", ""
        code = self.list_shorts.get_cell_text(int(row), _D_COL_CODE)
        name = self.list_shorts.get_cell_text(int(row), _D_COL_NAME)
        board = self.list_shorts.get_cell_text(int(row), _D_COL_BOARD)
        return code, name, board

    def _selected_stock(self):
        # 优先短线表选中（空窗期常用）
        srow = self.list_shorts.GetFirstSelected()
        if srow is not None and srow >= 0:
            code, name, _ = self._short_from_row(srow)
            if code:
                return code, name
        row = self.list_stocks.GetFirstSelected()
        code, name, _ = self._stock_from_row(row)
        return code, name

    def on_stock_select(self, event):
        event.Skip()
        row = (
            event.GetRow()
            if hasattr(event, "GetRow")
            else self.list_stocks.GetFirstSelected()
        )
        if row is None or row < 0:
            return
        code, _, _ = self._stock_from_row(int(row))
        detail = self._stock_details.get(code) or ""
        note = str((self._payload or {}).get("note") or "")
        self.txt_reason.SetValue(detail or note)

    def on_short_select(self, event):
        event.Skip()
        row = (
            event.GetRow()
            if hasattr(event, "GetRow")
            else self.list_shorts.GetFirstSelected()
        )
        if row is None or row < 0:
            return
        code, _, _ = self._short_from_row(int(row))
        detail = self._stock_details.get(code) or ""
        note = str((self._payload or {}).get("note") or "")
        self.txt_reason.SetValue(detail or note)

    def _popup_stock_menu(self, code: str, name: str, tag: str):
        if not code:
            return
        detail = self._stock_details.get(code) or ""
        note = str((self._payload or {}).get("note") or "")
        self.txt_reason.SetValue(detail or note)
        menu = wx.Menu()
        item_detail = menu.Append(wx.ID_ANY, f"查看详情（{name or code}）")
        item_add = menu.Append(wx.ID_ANY, "加入选股名单")
        item_intra = menu.Append(wx.ID_ANY, "加入今日盯盘")
        self.Bind(
            wx.EVT_MENU,
            lambda e, c=code, n=name: self._open_quote_detail(c, n),
            item_detail,
        )
        self.Bind(
            wx.EVT_MENU,
            lambda e, c=code, n=name, t=tag: self._add_watch_code(c, n, t),
            item_add,
        )
        self.Bind(
            wx.EVT_MENU,
            lambda e, c=code, n=name, t=tag: self._add_intraday_code(c, n, t),
            item_intra,
        )
        self.PopupMenu(menu)
        menu.Destroy()

    def on_stock_click(self, event):
        """与行业选股一致：弹出「查看详情 / 加入选股名单」。"""
        idx = _event_row(event, self.list_stocks)
        if idx < 0:
            return
        self.list_stocks.Select(idx)
        code, name, concept = self._stock_from_row(idx)
        self._popup_stock_menu(code, name, concept)
        event.Skip()

    def on_short_click(self, event):
        idx = _event_row(event, self.list_shorts)
        if idx < 0:
            return
        self.list_shorts.Select(idx)
        code, name, board = self._short_from_row(idx)
        self._popup_stock_menu(code, name, f"短线:{board or ''}")
        event.Skip()

    def _add_intraday_code(self, code: str, name: str = "", theme: str = ""):
        items, err = add_to_intraday_watch(code, name=name, theme=theme)
        if err:
            wx.MessageBox(err, "今日盯盘")
            return
        notify_intraday_panels(self)
        self._set_status(f"已加入今日盯盘：{name}({code})，共 {len(items)} 只")

    def on_add_intraday(self, event=None):
        srow = self.list_shorts.GetFirstSelected()
        if srow is not None and srow >= 0:
            code, name, board = self._short_from_row(srow)
            if code:
                self._add_intraday_code(code, name, board)
                return
        row = self.list_stocks.GetFirstSelected()
        code, name, concept = self._stock_from_row(row)
        if not code:
            wx.MessageBox("请先选中观察池或今日短线中的个股", "提示")
            return
        self._add_intraday_code(code, name, concept)

    def _add_watch_code(self, code: str, name: str = "", concept: str = ""):
        tag = concept or ""
        prefix = "前瞻短线" if tag.startswith("短线:") else "前瞻"
        add_to_watchlist(code, name=name, board=f"{prefix}:{tag}")
        self._set_status(f"已加入选股名单：{name}({code})")

    def on_add_watch(self, event=None):
        srow = self.list_shorts.GetFirstSelected()
        if srow is not None and srow >= 0:
            code, name, board = self._short_from_row(srow)
            if code:
                self._add_watch_code(code, name, f"短线:{board or ''}")
                return
        row = self.list_stocks.GetFirstSelected()
        code, name, concept = self._stock_from_row(row)
        if not code:
            wx.MessageBox("请先选中观察池或今日短线中的个股", "提示")
            return
        self._add_watch_code(code, name, concept)

    def _open_quote_detail(self, code: str, name: str = ""):
        try:
            dlg = open_quote_detail(self, code, name=name)
            if dlg is not None:
                self._detail_dialogs.append(dlg)

                def _cleanup(evt):
                    try:
                        self._detail_dialogs.remove(dlg)
                    except ValueError:
                        pass
                    evt.Skip()

                dlg.Bind(wx.EVT_CLOSE, _cleanup)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"打开详情失败: {exc}")
            wx.MessageBox(f"打开详情失败: {exc}", "错误")

    def on_open_detail(self, event=None):
        code, name = self._selected_stock()
        if not code:
            wx.MessageBox("请先选中个股", "提示")
            return
        self._open_quote_detail(code, name)
