# -*- coding: utf-8 -*-
"""行业选股面板：热点新闻 → 板块分析 → 成分股 → 选股名单。"""

from __future__ import annotations

import threading
import webbrowser
from typing import Optional

import pandas as pd
import wx
import wx.grid

from qbot.common.logging.logger import LOGGER as logger
from qbot.data.industry_screener import (
    add_to_watchlist,
    fetch_board_constituents,
    fetch_hot_news,
    fetch_industry_boards,
    fetch_virtual_board_constituents,
    invalidate_virtual_board_caches,
    is_virtual_zt_board,
    load_watchlist,
    match_board_key,
    reconcile_virtual_board_rows,
    remove_from_watchlist,
    save_watchlist,
    stats_from_constituents,
)
from qbot.gui.panels.panel_quote_detail import open_quote_detail

UP_COLOR = wx.Colour(239, 35, 42)
DOWN_COLOR = wx.Colour(20, 177, 67)
DEFAULT_COLOR = wx.Colour(0, 0, 0)

# 板块/成分股列表默认可视行数（多出的在表格内滚动）
LIST_VISIBLE_ROWS = 20


def _grid_viewport_height(grid: wx.grid.Grid, n_rows: int) -> int:
    """按可见行数估算 Grid 固定高度（含表头）。"""
    try:
        row_h = int(grid.GetDefaultRowSize() or 25)
    except Exception:
        row_h = 25
    try:
        hdr = int(grid.GetColLabelSize() or 26)
    except Exception:
        hdr = 26
    return hdr + row_h * max(int(n_rows), 1) + 8


def _fix_grid_viewport(grid, n_rows):
    h = _grid_viewport_height(grid, n_rows)
    grid.SetMinSize((-1, h))
    grid.SetMaxSize((-1, h))


def _fmt(v, nd=2):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "-"
    try:
        return f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return str(v)


def _to_float(v):
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        if isinstance(v, str):
            v = v.replace("%", "").replace(",", "").strip()
            if v in ("", "-", "None"):
                return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _sign_color(v):
    num = _to_float(v)
    if num is None or num == 0:
        return None
    return UP_COLOR if num > 0 else DOWN_COLOR


# 前瞻可买信号色（按用户约定：红=更可买，绿=不建议）
BUY_SIGNAL_COLORS = {
    "红": wx.Colour(0xD3, 0x2F, 0x2F),
    "橙": wx.Colour(0xEF, 0x6C, 0x00),
    "黄": wx.Colour(0xF9, 0xA8, 0x25),
    "绿": wx.Colour(0x2E, 0x7D, 0x32),
    "red": wx.Colour(0xD3, 0x2F, 0x2F),
    "orange": wx.Colour(0xEF, 0x6C, 0x00),
    "yellow": wx.Colour(0xF9, 0xA8, 0x25),
    "green": wx.Colour(0x2E, 0x7D, 0x32),
}


def _resolve_style_colour(v):
    if v is None:
        return None
    if isinstance(v, wx.Colour):
        return v
    return BUY_SIGNAL_COLORS.get(str(v).strip())


def _event_row(event, ctrl) -> int:
    try:
        if hasattr(event, "GetRow"):
            return int(event.GetRow())
    except Exception:
        pass
    try:
        return int(ctrl.GetFirstSelected())
    except Exception:
        return -1


class SortableListCtrl(wx.grid.Grid):
    """表格列表：表头可反复排序，单元格可按列红涨绿跌（高性能）。"""

    def __init__(self, parent, style=0):
        super().__init__(parent)
        self.CreateGrid(0, 0)
        self.SetRowLabelSize(0)
        self.SetColLabelSize(26)
        self.EnableEditing(False)
        self.EnableDragRowSize(False)
        self.EnableDragColMove(False)
        self.SetSelectionMode(wx.grid.Grid.SelectRows)
        self.SetDefaultCellAlignment(wx.ALIGN_LEFT, wx.ALIGN_CENTER)
        self._rows = []  # (display_row, sort_key, cell_colors, row_style)
        self._col_titles = []
        self._sort_col = -1
        self._sort_asc = True
        self.Bind(wx.grid.EVT_GRID_LABEL_LEFT_CLICK, self._on_label_click)
        self.Bind(wx.grid.EVT_GRID_CELL_CHANGING, lambda e: e.Veto())

    def set_columns(self, cols):
        """cols: [(title, width), ...]"""
        old_n = self.GetNumberCols()
        if old_n:
            self.DeleteCols(0, old_n)
        self._col_titles = [str(name) for name, _w in cols]
        self.AppendCols(len(cols))
        for i, (name, width) in enumerate(cols):
            self.SetColLabelValue(i, name)
            self.SetColSize(i, int(width))
        self._sort_col = -1
        self._sort_asc = True

    def fill_rows(self, rows, sort_keys, color_cols=None, row_styles=None):
        """
        rows: 显示字符串行
        sort_keys: 与 rows 对齐的可排序元组
        color_cols: {row_index: {col_index: raw_value}} 按各列数值正负单独着色（红涨绿跌）
        row_styles: {row_index: {"fg": Colour|str, "bg": Colour|str,
            "cols": [col,...], "fg_cols": [col,...], "bg_cols": [col,...]}}
            fg/bg 可为 wx.Colour，或 "红"/"橙"/"黄"/"绿" 等别名；
            cols 同时限定 fg/bg 列；fg_cols/bg_cols 可分别限定（未指定则整行，兼容旧用法）
        """
        color_cols = color_cols or {}
        row_styles = row_styles or {}
        self._rows = []
        for i, row in enumerate(rows):
            key = tuple(sort_keys[i]) if i < len(sort_keys) else tuple(row)
            self._rows.append(
                (
                    list(row),
                    key,
                    dict(color_cols.get(i) or {}),
                    dict(row_styles.get(i) or {}),
                )
            )
        if self._sort_col >= 0:
            self._apply_sort()
        else:
            self._paint()

    def get_cell_text(self, row: int, col: int) -> str:
        if row is None or row < 0 or col is None or col < 0:
            return ""
        if row >= self.GetNumberRows() or col >= self.GetNumberCols():
            return ""
        return self.GetCellValue(row, col)

    def GetFirstSelected(self) -> int:
        selected = self.GetSelectedRows()
        if selected:
            return int(selected[0])
        try:
            row = int(self.GetGridCursorRow())
            return row if row >= 0 else -1
        except Exception:
            return -1

    def Select(self, idx):
        if idx is None or idx < 0 or idx >= self.GetNumberRows():
            return
        self.ClearSelection()
        self.SelectRow(idx)
        self.SetGridCursor(idx, 0)
        self.MakeCellVisible(idx, 0)

    def GetItemCount(self):
        return self.GetNumberRows()

    def _on_label_click(self, event):
        col = event.GetCol()
        if col < 0:
            event.Skip()
            return
        if self._sort_col == col:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_col = col
            self._sort_asc = True
        self._apply_sort()
        # 表头只排序，不向父级冒泡成「选中行」去拉成分股
        # （部分环境下 Skip 会导致误触发 SELECT_CELL / 空行加载）

    @staticmethod
    def _cell_sort_value(v):
        """把排序键/单元格内容收成可比较值：(0, float) 数值优先，(1, str) 文本。"""
        if v is None:
            return (2, "")
        if isinstance(v, bool):
            return (0, float(v))
        if isinstance(v, (int, float)):
            try:
                if v != v:  # NaN
                    return (2, "")
            except Exception:
                pass
            return (0, float(v))
        s = str(v).strip().replace(",", "").replace("%", "")
        if not s or s in ("-", "—", "None", "nan"):
            return (2, "")
        try:
            return (0, float(s))
        except ValueError:
            return (1, s)

    def _apply_sort(self):
        if not self._rows or self._sort_col < 0:
            self._paint()
            return
        col = self._sort_col

        def sort_key(item):
            display = item[0] if item else []
            key = item[1] if len(item) > 1 else ()
            v = None
            if col < len(key):
                v = key[col]
            # 排序键过短或该列为空时，回退到显示文本（盯盘曾只传 2 列键导致点列头无感）
            if v is None and col < len(display):
                v = display[col]
            return self._cell_sort_value(v)

        self._rows.sort(key=sort_key, reverse=not self._sort_asc)
        self._paint()

    def _update_headers(self):
        for i, title in enumerate(self._col_titles):
            mark = ""
            if i == self._sort_col:
                mark = " ▲" if self._sort_asc else " ▼"
            self.SetColLabelValue(i, title + mark)

    def _paint(self):
        self.BeginBatch()
        try:
            old_n = self.GetNumberRows()
            if old_n:
                self.DeleteRows(0, old_n)
            n = len(self._rows)
            if n:
                self.AppendRows(n)
            default = wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT)
            default_bg = wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW)
            for i, item in enumerate(self._rows):
                # 兼容旧三元组 (row, key, cell_colors)
                if len(item) >= 4:
                    row, _key, cell_colors, row_style = (
                        item[0],
                        item[1],
                        item[2],
                        item[3],
                    )
                else:
                    row, _key, cell_colors = item[0], item[1], item[2]
                    row_style = {}
                style_fg = _resolve_style_colour((row_style or {}).get("fg"))
                style_bg = _resolve_style_colour((row_style or {}).get("bg"))
                # cols：同时限定信号色列；fg_cols/bg_cols 可分别限定（前瞻面板用）
                cols = (row_style or {}).get("cols")
                fg_cols = (row_style or {}).get("fg_cols", cols)
                bg_cols = (row_style or {}).get("bg_cols", cols)
                fg_col_set = set(fg_cols) if fg_cols is not None else None
                bg_col_set = set(bg_cols) if bg_cols is not None else None
                for j, val in enumerate(row):
                    if j >= self.GetNumberCols():
                        break
                    self.SetCellValue(i, j, str(val))
                    if style_fg is not None and (
                        fg_col_set is None or j in fg_col_set
                    ):
                        self.SetCellTextColour(i, j, style_fg)
                    else:
                        self.SetCellTextColour(i, j, default)
                    if style_bg is not None and (
                        bg_col_set is None or j in bg_col_set
                    ):
                        self.SetCellBackgroundColour(i, j, style_bg)
                    else:
                        self.SetCellBackgroundColour(i, j, default_bg)
                # 涨跌/资金等列：始终按正负红绿覆盖（优先于信号色）
                for j, raw in (cell_colors or {}).items():
                    if j < 0 or j >= self.GetNumberCols():
                        continue
                    colour = _sign_color(raw)
                    if colour is not None:
                        self.SetCellTextColour(i, j, colour)
            self._update_headers()
        finally:
            self.EndBatch()
        self.ForceRefresh()


class IndustryScreenerPanel(wx.Panel):
    def __init__(self, parent):
        super().__init__(parent)
        self._boards = pd.DataFrame()
        self._boards_all = pd.DataFrame()
        self._cons = pd.DataFrame()
        self._news = pd.DataFrame()
        self._current_board = ""
        self._loading_board = ""
        self._busy_all = False
        self._busy_cons = False
        self._pending_board = None
        self._cons_pulse = None
        self._cons_busy_until = 0.0
        self._cons_req_id = 0
        self._detail_dialogs = []
        self._init_ui()
        wx.CallAfter(self.refresh_all)

    def _init_ui(self):
        root = wx.BoxSizer(wx.VERTICAL)
        self.SetSizer(root)

        bar = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_refresh = wx.Button(self, label="刷新全部")
        self.btn_refresh_board = wx.Button(self, label="刷新成分股")
        self.btn_remove = wx.Button(self, label="从名单删除")
        self.btn_clear = wx.Button(self, label="一键清空名单")
        self.lbl_status = wx.StaticText(self, label="就绪")
        bar.Add(self.btn_refresh, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        bar.Add(self.btn_refresh_board, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        bar.Add(self.btn_remove, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        bar.Add(self.btn_clear, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        bar.Add(self.lbl_status, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        root.Add(bar, 0, wx.EXPAND | wx.ALL, 4)

        tip = wx.StaticText(
            self,
            label=(
                "流程：热点新闻 → 板块 → 成分股 → 选股名单。"
                "点选板块后上方置灰，刷完才能换；下方标题显示当前板块。"
                "虚拟三连阳/连阳首板/两连板：上下涨跌幅与只数一致。"
            ),
        )
        tip.Wrap(1200)
        root.Add(tip, 0, wx.ALL, 6)

        news_box = wx.StaticBox(self, label="近一周热点新闻（点击表头排序，双击打开链接）")
        news_sizer = wx.StaticBoxSizer(news_box, wx.VERTICAL)
        news_bar = wx.BoxSizer(wx.HORIZONTAL)
        news_bar.AddStretchSpacer(1)
        self.btn_news_fold = wx.Button(self, label="展开 ▼", size=(72, 26))
        self.btn_news_fold.SetToolTip(
            "展开/收起近一周热点新闻，避免占满下方板块与成分股"
        )
        news_bar.Add(self.btn_news_fold, 0, wx.RIGHT | wx.TOP, 2)
        news_sizer.Add(news_bar, 0, wx.EXPAND)
        self.list_news = SortableListCtrl(self)
        self.list_news.set_columns(
            [
                ("日期", 120),
                ("来源", 80),
                ("标题", 720),
                ("链接", 180),
            ]
        )
        self._news_sizer = news_sizer
        self._news_expanded = False
        self.list_news.SetMinSize((-1, 0))
        self.list_news.SetMaxSize((-1, 0))
        self.list_news.Hide()
        news_sizer.Add(self.list_news, 0, wx.EXPAND | wx.ALL, 4)
        root.Add(news_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 4)
        self.btn_news_fold.Bind(wx.EVT_BUTTON, self.on_toggle_news)

        board_box = wx.StaticBox(
            self, label="板块分析（行业+概念+市场；涨红跌绿；点选加载成分股）"
        )
        board_sizer = wx.StaticBoxSizer(board_box, wx.VERTICAL)
        filt = wx.BoxSizer(wx.HORIZONTAL)
        filt.Add(
            wx.StaticText(self, label="筛选"),
            0,
            wx.ALIGN_CENTER_VERTICAL | wx.RIGHT,
            4,
        )
        self.choice_board_type = wx.Choice(
            self, choices=["全部", "行业", "概念", "市场", "科技热点", "连板"]
        )
        self.choice_board_type.SetSelection(0)
        filt.Add(self.choice_board_type, 0, wx.RIGHT, 8)
        self.txt_board_search = wx.SearchCtrl(self, style=wx.TE_PROCESS_ENTER)
        self.txt_board_search.SetDescriptiveText(
            "搜索板块名，如 连涨回踩 / 两连板 / 三连阳 / CPO / 机器人"
        )
        self.txt_board_search.ShowCancelButton(True)
        filt.Add(self.txt_board_search, 1, wx.EXPAND)
        board_sizer.Add(filt, 0, wx.EXPAND | wx.ALL, 4)
        self.list_boards = SortableListCtrl(self)
        self.list_boards.set_columns(
            [
                ("排名", 45),
                ("类型", 50),
                ("板块", 110),
                ("涨跌幅%", 70),
                ("5日涨跌%", 70),
                ("主力净流入(亿)", 100),
                ("5日主力净流入(亿)", 110),
                ("主力净占比%", 80),
                ("市盈率", 60),
                ("换手率%", 60),
                ("上涨", 45),
                ("下跌", 45),
                ("领涨股", 80),
                ("领涨涨跌幅%", 80),
                ("总市值(亿)", 80),
                ("代码", 80),
            ]
        )
        _fix_grid_viewport(self.list_boards, LIST_VISIBLE_ROWS)
        board_sizer.Add(self.list_boards, 0, wx.EXPAND | wx.ALL, 4)
        root.Add(board_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 4)

        bottom = wx.BoxSizer(wx.HORIZONTAL)

        cons_box = wx.StaticBox(self, label="板块成分股（未选板块）")
        self.cons_box = cons_box
        cons_sizer = wx.StaticBoxSizer(cons_box, wx.VERTICAL)
        self.lbl_cons_loading = wx.StaticText(self, label="")
        self.gauge_cons = wx.Gauge(self, range=100, style=wx.GA_HORIZONTAL | wx.GA_SMOOTH)
        self.gauge_cons.SetMinSize((-1, 14))
        self.gauge_cons.Hide()
        cons_sizer.Add(self.lbl_cons_loading, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 4)
        cons_sizer.Add(self.gauge_cons, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 4)
        self.list_cons = SortableListCtrl(self)
        self.list_cons.set_columns(
            [
                ("序号", 40),
                ("代码", 65),
                ("名称", 75),
                ("最新价", 65),
                ("涨跌幅%", 65),
                ("连板数", 50),
                ("连阳天数", 55),
                ("回撤%", 50),
                ("5日涨跌%", 65),
                ("市盈率", 60),
                ("市净率", 60),
                ("换手率%", 60),
                ("主力净流入(亿)", 95),
                ("5日主力(亿)", 85),
                ("所属板块", 90),
                ("总市值(亿)", 80),
            ]
        )
        _fix_grid_viewport(self.list_cons, LIST_VISIBLE_ROWS)
        cons_sizer.Add(self.list_cons, 1, wx.EXPAND | wx.ALL, 4)
        bottom.Add(cons_sizer, 3, wx.EXPAND | wx.ALL, 2)

        watch_box = wx.StaticBox(self, label="选股名单（点击弹出：查看详情 / 删自选）")
        watch_sizer = wx.StaticBoxSizer(watch_box, wx.VERTICAL)
        watch_bar = wx.BoxSizer(wx.HORIZONTAL)
        watch_bar.AddStretchSpacer(1)
        self.btn_clear_watch = wx.Button(self, label="一键清空", size=(80, 26))
        self.btn_clear_watch.SetToolTip("清空选股名单历史，不保留旧票")
        watch_bar.Add(self.btn_clear_watch, 0, wx.RIGHT | wx.TOP, 2)
        watch_sizer.Add(watch_bar, 0, wx.EXPAND)
        self.list_watch = SortableListCtrl(self)
        self.list_watch.set_columns(
            [
                ("代码", 80),
                ("名称", 90),
                ("所属板块", 100),
                ("加入时间", 140),
            ]
        )
        _fix_grid_viewport(self.list_watch, LIST_VISIBLE_ROWS)
        watch_sizer.Add(self.list_watch, 1, wx.EXPAND | wx.ALL, 4)
        bottom.Add(watch_sizer, 1, wx.EXPAND | wx.ALL, 2)

        root.Add(bottom, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 4)

        self.btn_refresh.Bind(wx.EVT_BUTTON, lambda e: self.refresh_all())
        self.btn_refresh_board.Bind(
            wx.EVT_BUTTON, lambda e: self.refresh_constituents(force=True)
        )
        self.btn_remove.Bind(wx.EVT_BUTTON, self.on_remove_watch)
        self.btn_clear.Bind(wx.EVT_BUTTON, self.on_clear_watch)
        self.btn_clear_watch.Bind(wx.EVT_BUTTON, self.on_clear_watch)
        self.list_news.Bind(wx.grid.EVT_GRID_CELL_LEFT_DCLICK, self.on_news_open)
        # 用左键点选更稳：表头排序后 SELECT_CELL 有时丢行号，导致成分股一直空
        self.list_boards.Bind(wx.grid.EVT_GRID_CELL_LEFT_CLICK, self.on_board_selected)
        self.list_boards.Bind(wx.grid.EVT_GRID_CELL_LEFT_DCLICK, self.on_board_selected)
        self.list_cons.Bind(wx.grid.EVT_GRID_CELL_LEFT_CLICK, self.on_cons_click)
        self.list_watch.Bind(wx.grid.EVT_GRID_CELL_LEFT_CLICK, self.on_watch_click)
        self.list_watch.Bind(wx.grid.EVT_GRID_CELL_RIGHT_CLICK, self.on_watch_click)
        self.list_watch.Bind(wx.EVT_KEY_DOWN, self.on_watch_key)
        self.choice_board_type.Bind(wx.EVT_CHOICE, lambda e: self._apply_board_filter())
        self.txt_board_search.Bind(wx.EVT_TEXT, lambda e: self._apply_board_filter())
        self.txt_board_search.Bind(
            wx.EVT_SEARCHCTRL_SEARCH_BTN, lambda e: self._apply_board_filter()
        )
        self.txt_board_search.Bind(
            wx.EVT_SEARCHCTRL_CANCEL_BTN, self.on_board_search_cancel
        )
        self._reload_watch_ui()

    def on_toggle_news(self, event):
        """展开/收起近一周热点新闻区域（展开时固定约 20 行高度，表格内滚动）。"""
        self._news_expanded = not getattr(self, "_news_expanded", False)
        root = self.GetSizer()
        if self._news_expanded:
            self.list_news.Show()
            self.btn_news_fold.SetLabel("收起 ▲")
            _fix_grid_viewport(self.list_news, LIST_VISIBLE_ROWS)
        else:
            self.list_news.Hide()
            self.btn_news_fold.SetLabel("展开 ▼")
            self.list_news.SetMinSize((-1, 0))
            self.list_news.SetMaxSize((-1, 0))
        if root:
            root.Layout()
        self.Layout()
        self.Refresh()

    def _set_status(self, text: str):
        self.lbl_status.SetLabel(text)

    def _set_busy_all(self, busy: bool, msg: str = ""):
        self._busy_all = busy
        self.btn_refresh.Enable(not busy)
        self.btn_refresh_board.Enable(not busy)
        try:
            self.list_boards.Enable(not busy)
            self.choice_board_type.Enable(not busy)
            self.txt_board_search.Enable(not busy)
        except Exception:
            pass
        if msg:
            self._set_status(msg)

    def _resolve_board_title(self, key: str) -> str:
        key = str(key or "").strip()
        if not key:
            return "未选板块"
        if self._boards_all is not None and not self._boards_all.empty:
            mask = match_board_key(self._boards_all, key)
            if mask.any():
                r = self._boards_all.loc[mask].iloc[0]
                name = str(r.get("板块名称") or "").strip()
                code = str(r.get("板块代码") or "").strip()
                if name and code:
                    return f"{name}（{code}）"
                return name or code or key
        return key

    def _set_cons_box_title(self, key: str, loading: bool = False) -> None:
        title = self._resolve_board_title(key)
        if loading:
            self.cons_box.SetLabel(f"板块成分股 · 正在加载：{title}")
        elif title == "未选板块":
            self.cons_box.SetLabel("板块成分股（未选板块）")
        else:
            self.cons_box.SetLabel(
                f"板块成分股 · 当前：{title}（点击行内：查看详情 / 加入候选）"
            )
        try:
            self.cons_box.GetParent().Layout()
        except Exception:
            pass

    def _set_busy_cons(self, busy: bool, msg: str = ""):
        import time as _time

        if not busy:
            remain = float(getattr(self, "_cons_busy_until", 0.0) or 0.0) - _time.time()
            if remain > 0 and msg:
                wx.CallLater(int(remain * 1000), lambda m=msg: self._set_busy_cons(False, m))
                return
            self._cons_busy_until = 0.0
        else:
            self._cons_busy_until = _time.time() + 0.55
        self._busy_cons = busy
        self.btn_refresh_board.Enable(not busy)
        # 加载中置灰板块区，刷完前不能换概念（避免上下不对应）
        try:
            self.list_boards.Enable(not busy)
            self.choice_board_type.Enable(not busy)
            self.txt_board_search.Enable(not busy)
        except Exception:
            pass
        if busy:
            self._set_cons_box_title(self._loading_board or self._current_board, loading=True)
            self.lbl_cons_loading.SetLabel(msg or "正在加载成分股…")
            self.gauge_cons.Show()
            self.gauge_cons.Pulse()
            if self._cons_pulse is None:
                self._cons_pulse = wx.Timer(self)
                self.Bind(wx.EVT_TIMER, self._on_cons_pulse, self._cons_pulse)
            if not self._cons_pulse.IsRunning():
                self._cons_pulse.Start(120)
        else:
            if self._cons_pulse and self._cons_pulse.IsRunning():
                self._cons_pulse.Stop()
            self.gauge_cons.SetValue(0)
            self.gauge_cons.Hide()
            self.lbl_cons_loading.SetLabel("")
            if self._current_board:
                self._set_cons_box_title(self._current_board, loading=False)
        if msg:
            self._set_status(msg)
        try:
            self.Layout()
        except Exception:
            pass

    def _on_cons_pulse(self, event):
        if self._busy_cons and self.gauge_cons.IsShown():
            self.gauge_cons.Pulse()

    def refresh_all(self):
        if self._busy_all:
            return
        self._set_busy_all(True, "正在刷新新闻与板块（含三连阳启动扫描，约1分钟）…")

        def work():
            invalidate_virtual_board_caches()
            news = pd.DataFrame()
            boards = pd.DataFrame()
            err = ""
            sly_n = 0
            try:
                news = fetch_hot_news(40)
            except Exception as exc:
                logger.error("热点新闻失败: %s", exc)
                err = f"新闻: {exc}; "
            try:
                boards = fetch_industry_boards()
            except Exception as exc:
                logger.error("板块分析失败: %s", exc)
                err += f"板块: {exc}"
            try:
                from qbot.data.industry_screener import get_sanlianyang_cached

                sly_n = len(get_sanlianyang_cached())
            except Exception:
                sly_n = 0

            def done():
                self._news = news if news is not None else pd.DataFrame()
                self._boards_all = reconcile_virtual_board_rows(
                    boards if boards is not None else pd.DataFrame()
                )
                self._apply_board_filter()
                self._render_news()
                self._reload_watch_ui()
                n_ind = n_cpt = n_mkt = 0
                if not self._boards_all.empty and "类型" in self._boards_all.columns:
                    n_ind = int((self._boards_all["类型"] == "行业").sum())
                    n_cpt = int((self._boards_all["类型"] == "概念").sum())
                    n_mkt = int((self._boards_all["类型"] == "市场").sum())
                msg = (
                    f"已刷新：新闻 {len(self._news)} 条，板块 {len(self._boards_all)} "
                    f"（行业{n_ind}/概念{n_cpt}/市场{n_mkt}）；三连阳缓存 {sly_n} 只"
                )
                if err.strip():
                    msg = err.strip() + "；" + msg
                self._set_busy_all(False, msg)

            wx.CallAfter(done)

        threading.Thread(target=work, daemon=True).start()

    def _render_news(self):
        rows, keys = [], []
        for _, r in self._news.iterrows():
            t = str(r.get("time") or "-")
            src = str(r.get("source") or "-")
            title = str(r.get("title") or "")
            url = str(r.get("url") or "")
            rows.append([t, src, title, url])
            keys.append([t, src, title, url])
        self.list_news.fill_rows(rows, keys)

    def on_board_search_cancel(self, event):
        self.txt_board_search.SetValue("")
        self._apply_board_filter()

    def _apply_board_filter(self):
        df = self._boards_all
        if df is None or df.empty:
            self._boards = pd.DataFrame()
            self._render_boards()
            return
        typ = self.choice_board_type.GetStringSelection() or "全部"
        q = (self.txt_board_search.GetValue() or "").strip()
        out = df
        if typ in ("行业", "概念", "市场") and "类型" in out.columns:
            out = out[out["类型"].astype(str) == typ]
        elif typ == "连板" and "板块名称" in out.columns:
            out = out[
                out["板块名称"].astype(str).str.contains(
                    "两连板|连阳首板|连阳板|三连阳|连涨回踩|缩量回踩",
                    na=False,
                    regex=True,
                )
                | out["板块代码"]
                .astype(str)
                .str.upper()
                .isin(["MKT_2LB", "MKT_LYSB", "MKT_3LY", "MKT_LZHC"])
            ]
        elif typ == "科技热点" and "板块名称" in out.columns:
            tech_keys = (
                "CPO|PCB|半导体|存储|机器人|芯片|算力|光模块|人工智能|华为|英伟达|"
                "消费电子|软件|通信|计算机|电子|科创|创业板|光刻|先进封装|HBM|AI|"
                "两连板|连阳首板|三连阳|连涨回踩"
            )
            out = out[
                out["板块名称"].astype(str).str.contains(
                    tech_keys, case=False, na=False, regex=True
                )
            ]
        if q and "板块名称" in out.columns:
            out = out[
                out["板块名称"].astype(str).str.contains(q, case=False, na=False)
                | out["板块代码"].astype(str).str.contains(q, case=False, na=False)
            ]
        self._boards = out.reset_index(drop=True)
        self._render_boards()

    def _render_boards(self):
        rows, keys, colors = [], [], {}
        for i, (_, r) in enumerate(self._boards.iterrows()):
            pct = r.get("涨跌幅")
            pct5 = r.get("涨跌幅_5日")
            main = r.get("主力净流入_亿")
            main5 = r.get("主力净流入_5日_亿")
            lead_pct = r.get("领涨涨跌幅")

            def nk(v, empty=-9999):
                f = _to_float(v)
                return f if f is not None else empty

            row = [
                r.get("排名", ""),
                r.get("类型", "-"),
                r.get("板块名称", ""),
                _fmt(pct),
                _fmt(pct5),
                _fmt(main),
                _fmt(main5),
                _fmt(r.get("主力净占比")),
                _fmt(r.get("市盈率"), 1),
                _fmt(r.get("换手率")),
                _fmt(r.get("上涨家数"), 0),
                _fmt(r.get("下跌家数"), 0),
                r.get("领涨股", "-"),
                _fmt(lead_pct),
                _fmt(r.get("总市值_亿"), 0),
                r.get("板块代码", ""),
            ]
            rows.append(row)
            keys.append(
                (
                    nk(r.get("排名"), 0),
                    str(r.get("类型") or ""),
                    str(r.get("板块名称") or ""),
                    nk(pct),
                    nk(pct5),
                    nk(main),
                    nk(main5),
                    nk(r.get("主力净占比")),
                    nk(r.get("市盈率")),
                    nk(r.get("换手率")),
                    nk(r.get("上涨家数"), 0),
                    nk(r.get("下跌家数"), 0),
                    str(r.get("领涨股") or ""),
                    nk(lead_pct),
                    nk(r.get("总市值_亿")),
                    str(r.get("板块代码") or ""),
                )
            )
            colors[i] = {
                3: pct,
                4: pct5,
                5: main,
                6: main5,
                7: r.get("主力净占比"),
                13: lead_pct,
            }
        self.list_boards.fill_rows(rows, keys, color_cols=colors)

    def _render_cons(self):
        rows, keys, colors = [], [], {}
        for i, (_, r) in enumerate(self._cons.iterrows()):
            pct = r.get("涨跌幅")
            pct5 = r.get("涨跌幅_5日")
            main = r.get("主力净流入_亿")
            main5 = r.get("主力净流入_5日_亿")
            lb = r.get("连板数")
            ly = r.get("连阳天数")
            dd = r.get("回撤%")
            board = r.get("所属板块") or r.get("所属行业") or ""

            def nk(v, empty=-9999):
                f = _to_float(v)
                return f if f is not None else empty

            def fmt_int_or_dash(v):
                if v is None or (isinstance(v, float) and pd.isna(v)):
                    return "-"
                return _fmt(v, 0)

            row = [
                r.get("序号", ""),
                r.get("代码", ""),
                r.get("名称", ""),
                _fmt(r.get("最新价")),
                _fmt(pct),
                fmt_int_or_dash(lb),
                fmt_int_or_dash(ly),
                _fmt(dd)
                if dd is not None and not (isinstance(dd, float) and pd.isna(dd))
                else "-",
                _fmt(pct5),
                _fmt(r.get("市盈率"), 1),
                _fmt(r.get("市净率")),
                _fmt(r.get("换手率")),
                _fmt(main),
                _fmt(main5),
                str(board)
                if board and not (isinstance(board, float) and pd.isna(board))
                else "-",
                _fmt(r.get("总市值_亿"), 0),
            ]
            rows.append(row)
            keys.append(
                (
                    nk(r.get("序号"), 0),
                    str(r.get("代码") or ""),
                    str(r.get("名称") or ""),
                    nk(r.get("最新价")),
                    nk(pct),
                    nk(lb, 0),
                    nk(ly, 0),
                    nk(dd),
                    nk(pct5),
                    nk(r.get("市盈率")),
                    nk(r.get("市净率")),
                    nk(r.get("换手率")),
                    nk(main),
                    nk(main5),
                    str(board or ""),
                    nk(r.get("总市值_亿")),
                )
            )
            colors[i] = {
                4: pct,  # 涨跌幅
                8: pct5,  # 5日涨跌
                12: main,  # 主力净流入
                13: main5,  # 5日主力净流入
            }
        self.list_cons.fill_rows(rows, keys, color_cols=colors)

    def on_news_open(self, event):
        idx = _event_row(event, self.list_news)
        if idx is None or idx < 0 or self._news is None or self._news.empty:
            return
        url = self.list_news.get_cell_text(idx, 3)
        if url:
            webbrowser.open(url)

    def on_board_selected(self, event):
        if self._busy_cons:
            self._set_status("成分股加载中，请稍候再点其他概念")
            event.Skip()
            return
        idx = _event_row(event, self.list_boards)
        if idx is None or idx < 0:
            event.Skip()
            return
        # 点到空白/表头区域不拉成分股
        try:
            if hasattr(event, "GetCol") and int(event.GetCol()) < 0:
                event.Skip()
                return
        except Exception:
            pass
        name = (self.list_boards.get_cell_text(idx, 2) or "").strip()
        code = (self.list_boards.get_cell_text(idx, 15) or "").strip()
        # 三连阳：强制用标准代码，避免列错位/空代码走错分支秒回空表
        if (
            code.upper() == "MKT_3LY"
            or name == "三连阳"
            or "三连阳" in name
            or code == "三连阳"
        ):
            key = "MKT_3LY"
        else:
            key = code or name
        if not key:
            self._set_status("该行无板块代码/名称，无法加载成分股")
            event.Skip()
            return
        self.list_boards.Select(idx)
        self._current_board = key
        self._loading_board = key
        # 三连阳：点选只读启动缓存（不 force 实扫）；强制刷新按钮才重扫
        self.refresh_constituents(board=key, force=False)
        event.Skip()

    def _sync_board_row_from_cons(self, board_key: str, cons: pd.DataFrame) -> None:
        """成分股加载后回写板块行：涨跌幅=成分均涨，0只则涨跌幅=0。"""
        if self._boards_all is None or self._boards_all.empty:
            return
        mask = match_board_key(self._boards_all, board_key)
        if not mask.any() and str(board_key).upper().startswith("MKT_"):
            # 代码点选时再用名称兜底
            for i in range(self.list_boards.GetNumberRows()):
                if self.list_boards.get_cell_text(i, 15).upper() == str(board_key).upper():
                    name = self.list_boards.get_cell_text(i, 2)
                    if name:
                        mask = match_board_key(self._boards_all, name)
                        break
        if not mask.any():
            return
        st = stats_from_constituents(cons)
        idx = self._boards_all.index[mask][0]
        for col, val in st.items():
            if col in self._boards_all.columns:
                self._boards_all.at[idx, col] = val
        self._apply_board_filter()
        # 尽量保持当前选中行
        sel_name = str(board_key)
        for i in range(self.list_boards.GetNumberRows()):
            if (
                self.list_boards.get_cell_text(i, 2) == sel_name
                or self.list_boards.get_cell_text(i, 15).upper() == sel_name.upper()
            ):
                self.list_boards.Select(i)
                break

    def refresh_constituents(
        self, board: Optional[str] = None, force: bool = False
    ):
        key = board if board is not None else self._current_board
        if not key:
            self._set_status("请先点选一个板块")
            return
        if self._busy_cons:
            self._pending_board = key
            self._set_status("成分股加载中，完成后自动切换…")
            return
        self._current_board = key
        self._loading_board = key
        self._pending_board = None
        self._cons_req_id = int(getattr(self, "_cons_req_id", 0) or 0) + 1
        req_id = self._cons_req_id
        self._cons = pd.DataFrame()
        self._render_cons()
        is_sly = str(key).upper() == "MKT_3LY" or "三连阳" in str(key)
        self._set_busy_cons(
            True,
            (
                f"正在加载三连阳缓存：{key}…"
                if is_sly and not force
                else (
                    f"正在重扫三连阳（约1分钟）：{key}…"
                    if is_sly and force
                    else f"正在加载成分股：{key}…"
                )
            ),
        )
        try:
            self.gauge_cons.GetParent().Layout()
            wx.SafeYield()
        except Exception:
            pass

        def work():
            import time as _time

            cons = pd.DataFrame()
            err = ""
            t0 = _time.time()
            try:
                from qbot.data.industry_screener import (
                    _is_sanlianyang_board,
                    get_sanlianyang_cached,
                    warm_sanlianyang_cache,
                )

                is_sly_local = _is_sanlianyang_board(key) or str(key).upper() == "MKT_3LY"
                if is_sly_local:
                    if force:
                        cons, _ = warm_sanlianyang_cache(force=True)
                    else:
                        cons = get_sanlianyang_cached()
                        _time.sleep(0.7)
                elif is_virtual_zt_board(key):
                    cons = fetch_virtual_board_constituents(key, force=force)
                    if (cons is None or cons.empty) and not force:
                        cons = fetch_virtual_board_constituents(key, force=True)
                elif force:
                    from qbot.data.industry_screener import (
                        _fetch_board_constituents_uncached,
                    )

                    cons = _fetch_board_constituents_uncached(key)
                else:
                    cons = fetch_board_constituents(key)
            except Exception as exc:
                err = str(exc)
                logger.error("成分股失败: %s", exc)
            min_sec = 0.55 if (is_virtual_zt_board(key) or is_sly) else 0.15
            elapsed = _time.time() - t0
            if elapsed < min_sec:
                _time.sleep(min_sec - elapsed)

            def done():
                if req_id != getattr(self, "_cons_req_id", 0):
                    return
                if str(self._current_board or "") != str(key):
                    return
                msg = ""
                try:
                    self._cons = cons if cons is not None else pd.DataFrame()
                    self._render_cons()
                    try:
                        self._sync_board_row_from_cons(key, self._cons)
                    except Exception as exc:  # noqa: BLE001
                        logger.error("回写板块统计失败: %s", exc)
                    n = len(self._cons)
                    st = stats_from_constituents(self._cons)
                    avg = st.get("涨跌幅", 0.0)
                    if err:
                        msg = f"成分股失败: {err}"
                    elif is_sly:
                        if n == 0:
                            msg = (
                                "三连阳缓存为空。请等「刷新全部」扫完（状态栏显示"
                                "三连阳缓存 N 只），或点「刷新成分股」强制重扫约1分钟"
                            )
                        else:
                            msg = f"三连阳（启动缓存）：{n} 只 · 成分均涨 {avg:+.2f}%"
                    elif n == 0:
                        if str(key).upper() == "MKT_2LB" or str(key) == "两连板":
                            msg = "两连板：今日暂无连板数≥2 的个股"
                        elif str(key).upper() in ("MKT_LYSB",) or str(key) in (
                            "连阳首板",
                            "连阳板",
                        ):
                            msg = "连阳首板：今日暂无符合条件的个股"
                        else:
                            msg = f"板块【{key}】暂无成分股"
                    else:
                        msg = f"板块【{key}】成分股 {n} 只 · 成分均涨 {avg:+.2f}%"
                except Exception as exc:  # noqa: BLE001
                    logger.error("成分股刷新UI失败: %s", exc)
                    msg = f"成分股刷新失败: {exc}"
                finally:
                    self._loading_board = ""
                    self._set_busy_cons(False, msg)
                    pending = self._pending_board
                    self._pending_board = None
                    if pending and str(pending) != str(key):
                        wx.CallAfter(
                            lambda p=pending: self.refresh_constituents(
                                board=p, force=False
                            )
                        )

            wx.CallAfter(done)

        wx.CallAfter(lambda: threading.Thread(target=work, daemon=True).start())

    def _cons_from_list_idx(self, idx: int):
        if idx is None or idx < 0:
            return "", ""
        code = self.list_cons.get_cell_text(idx, 1)
        name = self.list_cons.get_cell_text(idx, 2)
        return code, name

    def on_cons_click(self, event):
        idx = _event_row(event, self.list_cons)
        if idx is None or idx < 0:
            event.Skip()
            return
        self.list_cons.Select(idx)
        code, name = self._cons_from_list_idx(idx)
        if not code:
            event.Skip()
            return
        menu = wx.Menu()
        item_detail = menu.Append(wx.ID_ANY, f"查看详情（{name or code}）")
        item_add = menu.Append(wx.ID_ANY, "加入候选名单")
        self.Bind(
            wx.EVT_MENU,
            lambda e, c=code, n=name: self.open_quote_detail(c, n),
            item_detail,
        )
        self.Bind(
            wx.EVT_MENU,
            lambda e, c=code, n=name: self.add_watch(c, n),
            item_add,
        )
        self.PopupMenu(menu)
        menu.Destroy()
        event.Skip()

    def open_quote_detail(self, code: str = "", name: str = ""):
        dlg = open_quote_detail(self, code=code, name=name)
        self._detail_dialogs.append(dlg)

        def _cleanup(evt):
            try:
                self._detail_dialogs.remove(dlg)
            except ValueError:
                pass
            evt.Skip()

        dlg.Bind(wx.EVT_CLOSE, _cleanup)

    def add_watch(self, code: str = "", name: str = ""):
        if not code:
            return
        items = add_to_watchlist(code, name=name, board=self._current_board)
        self._reload_watch_ui()
        self._set_status(f"已加入候选：{name}({code})，共 {len(items)} 只")

    def on_watch_click(self, event):
        """点击自选：弹出查看详情 / 删自选。"""
        idx = _event_row(event, self.list_watch)
        if idx is None or idx < 0:
            event.Skip()
            return
        self.list_watch.Select(idx)
        code = self.list_watch.get_cell_text(idx, 0)
        name = self.list_watch.get_cell_text(idx, 1)
        if not code:
            event.Skip()
            return
        menu = wx.Menu()
        item_detail = menu.Append(wx.ID_ANY, f"查看详情（{name or code}）")
        item_del = menu.Append(wx.ID_ANY, "删自选")
        self.Bind(
            wx.EVT_MENU,
            lambda e, c=code, n=name: self.open_quote_detail(c, n),
            item_detail,
        )
        self.Bind(
            wx.EVT_MENU,
            lambda e, c=code: self._remove_watch_code(c),
            item_del,
        )
        self.PopupMenu(menu)
        menu.Destroy()
        event.Skip()

    def _remove_watch_code(self, code: str):
        items = remove_from_watchlist(code)
        self._reload_watch_ui()
        self._set_status(f"已从名单删除 {code}，剩余 {len(items)} 只")

    def on_remove_watch(self, event):
        idx = self.list_watch.GetFirstSelected()
        if idx is None or idx < 0:
            wx.MessageBox(
                "请先在选股名单中选中要删除的股票，或点击名单弹出「删自选」",
                "提示",
            )
            return
        code = self.list_watch.get_cell_text(idx, 0)
        if code:
            self._remove_watch_code(code)

    def on_watch_key(self, event):
        if event.GetKeyCode() in (wx.WXK_DELETE, wx.WXK_BACK):
            self.on_remove_watch(event)
            return
        event.Skip()

    def on_clear_watch(self, event):
        n = len(load_watchlist())
        save_watchlist([])
        self._reload_watch_ui()
        self._set_status(f"选股名单已一键清空（原 {n} 只）")

    def _reload_watch_ui(self):
        items = load_watchlist()
        rows, keys = [], []
        for it in items:
            row = [
                it.get("code", ""),
                it.get("name", ""),
                it.get("board", ""),
                it.get("added_at", ""),
            ]
            rows.append(row)
            keys.append(tuple(row))
        self.list_watch.fill_rows(rows, keys)
