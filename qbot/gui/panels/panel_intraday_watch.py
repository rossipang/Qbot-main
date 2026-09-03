# -*- coding: utf-8 -*-
"""今日盯盘：观察池买点 + 持仓卖点。"""

from __future__ import annotations

import threading

import wx
import wx.grid

from qbot.common.logging.logger import LOGGER as logger
from qbot.data.intraday_watch import (
    MAX_HOLDINGS,
    MAX_WATCH,
    add_holding,
    add_to_intraday_watch,
    alert_new_buy_ok,
    is_cn_session,
    load_intraday_watch,
    mark_holding_sold,
    refresh_holdings,
    refresh_watch_pool,
    remove_from_intraday_watch,
)
from qbot.gui.panels.panel_industry_screener import (
    SortableListCtrl,
    _event_row,
    _fmt,
)
from qbot.gui.panels.panel_quote_detail import open_quote_detail

_COL_CODE = 2
_COL_NAME = 3
_COL_PCT = 5
_H_COL_CODE = 2
_H_COL_NAME = 3
_REFRESH_MS = 180000  # 3 分钟


def notify_intraday_panels(win: wx.Window) -> None:
    """加入名单后，让已打开的盯盘页立刻刷新。"""
    top = win.GetTopLevelParent() if win else None
    if top is None:
        return
    nb = getattr(top, "tabs", None)
    if nb is None:
        return
    try:
        n = int(nb.GetPageCount())
    except Exception:
        return
    for i in range(n):
        page = nb.GetPage(i)
        fn = getattr(page, "reload_watch", None)
        if callable(fn):
            wx.CallAfter(fn, True)


class IntradayWatchPanel(wx.Panel):
    def __init__(self, parent):
        super().__init__(parent)
        self._busy_watch = False
        self._busy_hold = False
        self._rows = []
        self._hold_rows = []
        self._auto = True
        self._detail_dialogs = []
        self._watch_ts = "-"
        self._hold_ts = "-"
        self._init_ui()
        self._timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_timer, self._timer)
        self._timer.Start(_REFRESH_MS)
        wx.CallAfter(self.reload_watch, True)

    def _init_ui(self):
        root = wx.BoxSizer(wx.VERTICAL)
        self.SetSizer(root)

        bar = wx.BoxSizer(wx.HORIZONTAL)
        self.txt_code = wx.TextCtrl(self, size=(90, -1))
        self.txt_code.SetHint("观察池代码")
        self.btn_add = wx.Button(self, label="加入观察")
        self.btn_del = wx.Button(self, label="删除观察")
        self.btn_refresh = wx.Button(self, label="立即刷新")
        self.chk_auto = wx.CheckBox(self, label="盘中每3分钟自动刷")
        self.chk_auto.SetValue(True)
        self.lbl_status = wx.StaticText(
            self,
            label="观察池/持仓并行刷新；持仓不弹窗：绿=持有，黄=卖区，红=危险",
        )
        for w in (
            self.txt_code,
            self.btn_add,
            self.btn_del,
            self.btn_refresh,
            self.chk_auto,
        ):
            bar.Add(w, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        bar.Add(self.lbl_status, 1, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 8)
        root.Add(bar, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 4)

        tip = wx.StaticText(
            self,
            label=(
                "观察池最多 "
                f"{MAX_WATCH} 只；持仓最多 {MAX_HOLDINGS} 只。"
                "两边并行刷新互不堵。持仓：绿持有 / 黄卖区(回吐约20%或低开预警) / "
                "红危险(回吐深、昨强今砸、破成本)；无弹窗。"
            ),
        )
        tip.Wrap(1100)
        root.Add(tip, 0, wx.ALL, 6)

        split = wx.SplitterWindow(self, style=wx.SP_LIVE_UPDATE | wx.SP_BORDER)
        split.SetMinimumPaneSize(120)

        top_p = wx.Panel(split)
        top_s = wx.BoxSizer(wx.VERTICAL)
        top_s.Add(
            wx.StaticText(top_p, label="观察池（买点）"),
            0,
            wx.LEFT | wx.TOP,
            4,
        )
        self.list_watch = SortableListCtrl(top_p)
        self.list_watch.set_columns(
            [
                ("信号", 46),
                ("候选", 40),
                ("代码", 58),
                ("名称", 78),
                ("现价", 58),
                ("涨跌%", 52),
                ("5日%", 50),
                ("流入亿", 58),
                ("量比", 46),
                ("K线", 110),
                ("买点", 80),
                ("买入方法", 100),
                ("建议买入", 90),
                ("操作建议", 200),
                ("主题", 90),
            ]
        )
        top_s.Add(self.list_watch, 1, wx.EXPAND | wx.ALL, 4)
        top_p.SetSizer(top_s)

        bot_p = wx.Panel(split)
        bot_s = wx.BoxSizer(wx.VERTICAL)
        bot_s.Add(
            wx.StaticText(bot_p, label="持仓股（卖点）"),
            0,
            wx.LEFT | wx.TOP,
            4,
        )
        hold_bar = wx.BoxSizer(wx.HORIZONTAL)
        self.txt_hold_q = wx.TextCtrl(bot_p, size=(120, -1))
        self.txt_hold_q.SetHint("名称或代码")
        self.txt_hold_cost = wx.TextCtrl(bot_p, size=(80, -1))
        self.txt_hold_cost.SetHint("成本价*")
        self.txt_hold_theme = wx.TextCtrl(bot_p, size=(100, -1))
        self.txt_hold_theme.SetHint("主题/板块(可空)")
        self.btn_hold_add = wx.Button(bot_p, label="加入持仓")
        for w in (
            self.txt_hold_q,
            self.txt_hold_cost,
            self.txt_hold_theme,
            self.btn_hold_add,
        ):
            hold_bar.Add(w, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 3)
        bot_s.Add(hold_bar, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 2)

        self.list_hold = SortableListCtrl(bot_p)
        self.list_hold.set_columns(
            [
                ("信号", 40),
                ("卖出建议", 60),
                ("代码", 58),
                ("名称", 78),
                ("成本", 58),
                ("现价", 58),
                ("浮盈%", 56),
                ("涨跌%", 52),
                ("量比", 46),
                ("走势类型", 72),
                ("高点回撤%", 70),
                ("板块", 80),
                ("板块%", 52),
                ("大盘", 40),
                ("操作建议", 200),
                ("依据", 260),
            ]
        )
        bot_s.Add(self.list_hold, 1, wx.EXPAND | wx.ALL, 4)
        bot_p.SetSizer(bot_s)

        split.SplitHorizontally(top_p, bot_p, 320)
        root.Add(split, 1, wx.EXPAND | wx.ALL, 4)

        self.btn_add.Bind(wx.EVT_BUTTON, self.on_add)
        self.btn_del.Bind(wx.EVT_BUTTON, self.on_del)
        self.btn_refresh.Bind(wx.EVT_BUTTON, lambda e: self.refresh_quotes())
        self.chk_auto.Bind(wx.EVT_CHECKBOX, self.on_auto)
        self.btn_hold_add.Bind(wx.EVT_BUTTON, self.on_hold_add)
        self.list_watch.Bind(wx.grid.EVT_GRID_CELL_RIGHT_CLICK, self.on_row_menu)
        self.list_watch.Bind(wx.grid.EVT_GRID_CELL_LEFT_DCLICK, self.on_row_menu)
        self.list_hold.Bind(wx.grid.EVT_GRID_CELL_RIGHT_CLICK, self.on_hold_menu)
        self.list_hold.Bind(wx.grid.EVT_GRID_CELL_LEFT_DCLICK, self.on_hold_menu)

    def _set_status(self, text: str):
        self.lbl_status.SetLabel(text)

    def on_auto(self, event=None):
        self._auto = bool(self.chk_auto.GetValue())

    def _on_timer(self, event):
        if not self._auto:
            return
        if not is_cn_session():
            return
        self.refresh_quotes()

    def _status_line(self, extra: str = ""):
        w = "刷观察…" if self._busy_watch else f"观察{len(self._rows)}"
        h = "刷持仓…" if self._busy_hold else f"持仓{len(self._hold_rows)}"
        n_ok = sum(1 for r in self._rows if str(r.get("买入候选")) == "是")
        n_warn = sum(
            1
            for r in self._hold_rows
            if str(r.get("卖出建议") or "") in ("观察", "减仓")
        )
        n_danger = sum(
            1 for r in self._hold_rows if str(r.get("卖出建议") or "") == "卖出"
        )
        msg = (
            f"{w}(买点{n_ok}/{self._watch_ts}) · "
            f"{h}(黄{n_warn}/红{n_danger}/{self._hold_ts})"
        )
        if not is_cn_session():
            msg += " · 非盘中"
        if extra:
            msg += f" · {extra}"
        self._set_status(msg)

    def on_add(self, event=None):
        code = (self.txt_code.GetValue() or "").strip()
        items, err = add_to_intraday_watch(code)
        if err:
            wx.MessageBox(err, "今日盯盘")
            return
        self.txt_code.Clear()
        self._set_status(f"已加入观察 {code}，共 {len(items)} 只")
        self.refresh_quotes()

    def on_del(self, event=None):
        row = self.list_watch.GetFirstSelected()
        code = self.list_watch.get_cell_text(row, _COL_CODE)
        if not code:
            wx.MessageBox("请先选中观察池一行", "今日盯盘")
            return
        remove_from_intraday_watch(code)
        self.reload_watch(False)

    def on_hold_add(self, event=None):
        q = (self.txt_hold_q.GetValue() or "").strip()
        cost_s = (self.txt_hold_cost.GetValue() or "").strip()
        theme = (self.txt_hold_theme.GetValue() or "").strip()
        if not q:
            wx.MessageBox("请填写股票名称或代码", "持仓")
            return
        if not cost_s:
            wx.MessageBox("加入持仓必须填写成本价", "持仓")
            return
        try:
            cost = float(cost_s)
        except ValueError:
            wx.MessageBox("成本价格式不对", "持仓")
            return
        holdings, err = add_holding(q, cost, theme=theme)
        if err:
            wx.MessageBox(err, "持仓")
            return
        self.txt_hold_q.Clear()
        self.txt_hold_cost.Clear()
        self._set_status(f"已加入持仓，当前 {len(holdings)} 只")
        self.refresh_quotes()

    def on_row_menu(self, event):
        idx = _event_row(event, self.list_watch)
        if idx < 0:
            return
        self.list_watch.Select(idx)
        code = self.list_watch.get_cell_text(idx, _COL_CODE)
        name = self.list_watch.get_cell_text(idx, _COL_NAME)
        menu = wx.Menu()
        item_d = menu.Append(wx.ID_ANY, f"查看详情（{name or code}）")
        item_h = menu.Append(wx.ID_ANY, "转入持仓（填成本）…")
        item_x = menu.Append(wx.ID_ANY, "从观察池删除")
        self.Bind(
            wx.EVT_MENU,
            lambda e, c=code, n=name: self._open_detail(c, n),
            item_d,
        )
        self.Bind(
            wx.EVT_MENU,
            lambda e, c=code, n=name: self._prompt_to_holding(c, n),
            item_h,
        )
        self.Bind(
            wx.EVT_MENU,
            lambda e, c=code: (
                remove_from_intraday_watch(c),
                self.reload_watch(False),
            ),
            item_x,
        )
        self.PopupMenu(menu)
        menu.Destroy()
        event.Skip()

    def on_hold_menu(self, event):
        idx = _event_row(event, self.list_hold)
        if idx < 0:
            return
        self.list_hold.Select(idx)
        code = self.list_hold.get_cell_text(idx, _H_COL_CODE)
        name = self.list_hold.get_cell_text(idx, _H_COL_NAME)
        menu = wx.Menu()
        item_d = menu.Append(wx.ID_ANY, f"查看详情（{name or code}）")
        item_s = menu.Append(wx.ID_ANY, "已卖出（移出持仓）")
        self.Bind(
            wx.EVT_MENU,
            lambda e, c=code, n=name: self._open_detail(c, n),
            item_d,
        )
        self.Bind(
            wx.EVT_MENU,
            lambda e, c=code: self._mark_sold(c),
            item_s,
        )
        self.PopupMenu(menu)
        menu.Destroy()
        event.Skip()

    def _mark_sold(self, code: str):
        mark_holding_sold(code)
        self.reload_watch(False)
        self._set_status(f"已标记卖出并移出持仓：{code}")

    def _prompt_to_holding(self, code: str, name: str = ""):
        dlg = wx.TextEntryDialog(self, f"{name or code} 成本价：", "转入持仓")
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return
        cost_s = (dlg.GetValue() or "").strip()
        dlg.Destroy()
        try:
            cost = float(cost_s)
        except ValueError:
            wx.MessageBox("成本价无效", "持仓")
            return
        _holdings, err = add_holding(code, cost)
        if err:
            wx.MessageBox(err, "持仓")
            return
        self.refresh_quotes()

    def _open_detail(self, code: str, name: str = ""):
        try:
            dlg = open_quote_detail(self, code, name=name)
            if dlg is not None:
                self._detail_dialogs.append(dlg)
        except Exception as exc:  # noqa: BLE001
            wx.MessageBox(f"打开详情失败: {exc}", "错误")

    def reload_watch(self, fetch: bool = True):
        data = load_intraday_watch()
        self._rows = list(data.get("last_rows") or [])
        self._hold_rows = list(data.get("holdings_last_rows") or [])
        self._paint(self._rows)
        self._paint_hold(self._hold_rows)
        self._watch_ts = str(data.get("watch_updated_at") or data.get("updated_at") or "-")
        self._hold_ts = str(
            data.get("holdings_updated_at") or data.get("updated_at") or "-"
        )
        self._status_line()
        if fetch:
            self.refresh_quotes()

    def refresh_quotes(self):
        """观察池与持仓并行刷新，互不占用对方 busy。"""
        data0 = load_intraday_watch()
        items = data0.get("items") or []
        holdings = data0.get("holdings") or []
        if not items and not holdings:
            self._rows = []
            self._hold_rows = []
            self._paint([])
            self._paint_hold([])
            self._set_status(
                f"名单空 · 观察最多 {MAX_WATCH} / 持仓最多 {MAX_HOLDINGS}"
            )
            return
        if items:
            self._refresh_watch()
        if holdings:
            self._refresh_hold()
        self._status_line()

    def _refresh_watch(self):
        if self._busy_watch:
            return
        self._busy_watch = True
        self._status_line()
        old_rows = list(self._rows)

        def work():
            err = ""
            payload = {}
            try:
                payload = refresh_watch_pool()
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                logger.error(f"观察池刷新失败: {exc}")

            def done():
                self._busy_watch = False
                rows = list((payload or {}).get("rows") or [])
                self._rows = rows
                self._paint(rows)
                self._watch_ts = str((payload or {}).get("updated_at") or "-")
                self._status_line(err)
                # 仅观察池买点弹窗；持仓永不弹窗
                fired = alert_new_buy_ok(old_rows, rows)
                if fired:
                    names = "、".join(
                        f"{x.get('名称')}({x.get('代码')})" for x in fired[:4]
                    )
                    self._toast(f"买点出现：{names}")

            wx.CallAfter(done)

        threading.Thread(target=work, daemon=True).start()

    def _refresh_hold(self):
        if self._busy_hold:
            return
        self._busy_hold = True
        self._status_line()

        def work():
            err = ""
            payload = {}
            try:
                payload = refresh_holdings()
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                logger.error(f"持仓刷新失败: {exc}")

            def done():
                self._busy_hold = False
                hrows = list((payload or {}).get("holdings_rows") or [])
                self._hold_rows = hrows
                self._paint_hold(hrows)
                self._hold_ts = str((payload or {}).get("updated_at") or "-")
                self._status_line(err)

            wx.CallAfter(done)

        threading.Thread(target=work, daemon=True).start()

    def _toast(self, text: str):
        try:
            note = wx.NotificationMessage("今日盯盘", text, self)
            note.Show(timeout=8)
        except Exception:
            wx.MessageBox(text, "今日盯盘")

    def _paint(self, rows):
        display = []
        keys = []
        colors = {}
        styles = {}

        def nf(v, empty=-9999.0):
            try:
                if v is None or v == "":
                    return empty
                return float(v)
            except (TypeError, ValueError):
                return empty

        for i, r in enumerate(rows or []):
            display.append(
                [
                    str(r.get("信号") or ""),
                    str(r.get("买入候选") or ""),
                    str(r.get("代码") or ""),
                    str(r.get("名称") or ""),
                    _fmt(r.get("最新价")),
                    _fmt(r.get("涨跌幅%")),
                    _fmt(r.get("5日涨跌%")),
                    _fmt(r.get("主力净流入亿")),
                    _fmt(r.get("量比")),
                    str(r.get("K线") or ""),
                    str(r.get("买点") or ""),
                    str(r.get("买入方法") or ""),
                    str(r.get("建议买入") or ""),
                    str(r.get("操作建议") or ""),
                    str(r.get("主题") or ""),
                ]
            )
            keys.append(
                (
                    str(r.get("信号") or ""),
                    0 if str(r.get("买入候选")) == "是" else 1,
                    str(r.get("代码") or ""),
                    str(r.get("名称") or ""),
                    nf(r.get("最新价")),
                    nf(r.get("涨跌幅%")),
                    nf(r.get("5日涨跌%")),
                    nf(r.get("主力净流入亿")),
                    nf(r.get("量比")),
                    str(r.get("K线") or ""),
                    str(r.get("买点") or ""),
                    str(r.get("买入方法") or ""),
                    str(r.get("建议买入") or ""),
                    str(r.get("操作建议") or ""),
                    str(r.get("主题") or ""),
                )
            )
            colors[i] = {_COL_PCT: r.get("涨跌幅%")}
            if str(r.get("买入候选")) == "是":
                styles[i] = {"bg": wx.Colour(255, 236, 179)}
        self.list_watch.fill_rows(display, keys, color_cols=colors, row_styles=styles)

    def _paint_hold(self, rows):
        display = []
        keys = []
        colors = {}
        styles = {}

        def nf(v, empty=-9999.0):
            try:
                if v is None or v == "":
                    return empty
                return float(v)
            except (TypeError, ValueError):
                return empty

        # 绿=正常持有；黄=卖区(观察/减仓)；红=危险(卖出或浮盈≤-2.5%)
        green = wx.Colour(200, 230, 201)
        yellow = wx.Colour(255, 236, 179)
        red = wx.Colour(255, 205, 210)
        for i, r in enumerate(rows or []):
            display.append(
                [
                    str(r.get("信号") or ""),
                    str(r.get("卖出建议") or ""),
                    str(r.get("代码") or ""),
                    str(r.get("名称") or ""),
                    _fmt(r.get("成本")),
                    _fmt(r.get("最新价")),
                    _fmt(r.get("浮盈%")),
                    _fmt(r.get("涨跌幅%")),
                    _fmt(r.get("量比")),
                    str(r.get("走势类型") or ""),
                    _fmt(r.get("高点回撤%")),
                    str(r.get("板块") or ""),
                    _fmt(r.get("板块涨跌%")),
                    str(r.get("大盘") or ""),
                    str(r.get("操作建议") or ""),
                    str(r.get("依据") or ""),
                ]
            )
            keys.append(
                (
                    str(r.get("信号") or ""),
                    str(r.get("卖出建议") or ""),
                    str(r.get("代码") or ""),
                    str(r.get("名称") or ""),
                    nf(r.get("成本")),
                    nf(r.get("最新价")),
                    nf(r.get("浮盈%")),
                    nf(r.get("涨跌幅%")),
                    nf(r.get("量比")),
                    str(r.get("走势类型") or ""),
                    nf(r.get("高点回撤%")),
                    str(r.get("板块") or ""),
                    nf(r.get("板块涨跌%")),
                    str(r.get("大盘") or ""),
                    str(r.get("操作建议") or ""),
                    str(r.get("依据") or ""),
                )
            )
            colors[i] = {6: r.get("浮盈%"), 7: r.get("涨跌幅%"), 12: r.get("板块涨跌%")}
            adv = str(r.get("卖出建议") or "持有")
            pnl = nf(r.get("浮盈%"), empty=0.0)
            if adv == "卖出" or pnl <= -2.5:
                styles[i] = {"bg": red}
            elif adv in ("观察", "减仓"):
                styles[i] = {"bg": yellow}
            else:
                styles[i] = {"bg": green}
        self.list_hold.fill_rows(display, keys, color_cols=colors, row_styles=styles)
