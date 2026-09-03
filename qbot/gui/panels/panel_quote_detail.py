# -*- coding: utf-8 -*-
"""个股详情弹窗：与行业选股 / 回测搜股共用同一套分时·日K·资金版面。"""

from __future__ import annotations

import threading
from pathlib import Path

import wx

from qbot.common.logging.logger import LOGGER as logger
from qbot.data.intraday import is_cn_trading_session
from qbot.data.stock_detail_page import render_stock_detail_page
from qbot.gui.config import DATA_DIR_BKT_RESULT
from qbot.gui.widgets.widget_web import WebPanel


class QuoteDetailDialog(wx.Dialog):
    """东财/同花顺式单页标签（分时/日K/周K/月K/资金/财务/资讯）。"""

    REFRESH_MS = 15000

    def __init__(self, parent, code: str, name: str = ""):
        title = f"个股详情 - {name}({code})" if name else f"个股详情 - {code}"
        super().__init__(
            parent,
            title=title,
            size=(1280, 900),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX,
        )
        self.code = code
        self.name = name
        self._busy = False
        self._closed = False
        self._cache: dict = {}

        root = wx.BoxSizer(wx.VERTICAL)
        bar = wx.BoxSizer(wx.HORIZONTAL)
        self.lbl_status = wx.StaticText(self, label="正在加载…")
        self.chk_auto = wx.CheckBox(self, label="盘中自动刷新分时(15秒)")
        self.chk_auto.SetValue(True)
        self.btn_refresh = wx.Button(self, label="刷新")
        self.btn_close = wx.Button(self, wx.ID_CLOSE, label="关闭")
        bar.Add(self.lbl_status, 1, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 8)
        bar.Add(self.chk_auto, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        bar.Add(self.btn_refresh, 0, wx.RIGHT, 6)
        bar.Add(self.btn_close, 0, wx.RIGHT, 8)
        root.Add(bar, 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 4)

        self.web = WebPanel(self)
        root.Add(self.web, 1, wx.EXPAND | wx.ALL, 2)
        self.SetSizer(root)

        self.btn_close.Bind(wx.EVT_BUTTON, lambda e: self.Close())
        self.btn_refresh.Bind(wx.EVT_BUTTON, lambda e: self._load(full=True))
        self.Bind(wx.EVT_CLOSE, self._on_close)

        self._timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_timer, self._timer)

        wx.CallAfter(self._load, True)
        wx.CallAfter(self._timer.Start, self.REFRESH_MS)

    def _on_close(self, event):
        self._closed = True
        try:
            self._timer.Stop()
        except Exception:
            pass
        event.Skip()

    def _on_timer(self, event):
        if self._closed or not self.chk_auto.GetValue():
            return
        if is_cn_trading_session():
            self._load(full=False, silent=True)

    def _set_status(self, text: str):
        if not self._closed:
            self.lbl_status.SetLabel(text)

    def _load(self, full: bool = True, silent: bool = False):
        if self._busy or self._closed:
            return
        self._busy = True
        if not silent:
            self._set_status(
                f"正在加载{'全部' if full else '分时'}：{self.name}({self.code})…"
            )

        def work():
            err = ""
            path = None
            try:
                out = DATA_DIR_BKT_RESULT.joinpath(f"quote_detail_{self.code}.html")
                path = render_stock_detail_page(
                    code=self.code,
                    output_path=out,
                    name=self.name,
                    cache=self._cache,
                    refresh_heavy=full or not self._cache.get("ready"),
                )
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                logger.error(f"个股详情失败: {exc}")

            def done():
                try:
                    if self._closed:
                        return
                    if err or path is None:
                        if not silent:
                            self._set_status(f"加载失败：{err or '未知错误'}")
                        return
                    try:
                        self.web.show_file(Path(path))
                    except Exception as exc:  # noqa: BLE001
                        self._set_status(f"展示失败：{exc}")
                        logger.error(f"个股详情展示失败: {exc}")
                        return
                    tag = (
                        "盘中自动刷新中"
                        if is_cn_trading_session()
                        else "点击标签切换 分时/日K/周K/月K"
                    )
                    self._set_status(f"{self.name}({self.code}) · {tag}")
                finally:
                    self._busy = False

            wx.CallAfter(done)

        threading.Thread(target=work, daemon=True).start()


def open_quote_detail(parent, code: str, name: str = ""):
    """统一入口：弹出与行业选股一致的个股详情。"""
    dlg = QuoteDetailDialog(parent, code=code, name=name or "")
    dlg.Show()
    return dlg
