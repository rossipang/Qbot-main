# -*- coding: utf-8 -*-
"""策略编写对话面板。"""

from __future__ import annotations

import threading
from datetime import datetime
from urllib.parse import quote

import wx

from qbot.ai.cursor_chat import CursorChatError, CursorStrategyChat, probe_connection
from qbot.common.logging.logger import LOGGER as logger

WELCOME = (
    "【Qbot · Cursor 策略助手】\n"
    "A股短线模式：禁追昨日赢家、必看K线、分池（生益≠沪电、长飞≠PCB）。\n"
    "无「候选=是+贴价买区」时默认观望，不极力推荐。\n"
    "用法：描述策略/个股/买点，Ctrl+Enter 发送。\n"
)


class StrategyChatPanel(wx.Panel):
    def __init__(self, parent):
        super().__init__(parent)
        self._busy = False
        self._chat = CursorStrategyChat()
        self._build_ui()
        self._append("系统", WELCOME)
        wx.CallAfter(self._refresh_status)

    def _build_ui(self):
        root = wx.BoxSizer(wx.VERTICAL)

        bar = wx.BoxSizer(wx.HORIZONTAL)
        self.status_label = wx.StaticText(self, label="正在连接 Cursor…")
        bar.Add(self.status_label, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)

        self.btn_new = wx.Button(self, label="新对话", size=(80, 28))
        self.btn_clear = wx.Button(self, label="清空记录", size=(80, 28))
        bar.Add(self.btn_new, 0, wx.RIGHT, 6)
        bar.Add(self.btn_clear, 0)
        root.Add(bar, 0, wx.EXPAND | wx.ALL, 8)

        self.history = wx.TextCtrl(
            self,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_WORDWRAP | wx.BORDER_SUNKEN,
        )
        font = self.history.GetFont()
        font.SetPointSize(max(font.GetPointSize(), 10))
        self.history.SetFont(font)
        root.Add(self.history, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        tip = wx.StaticText(
            self,
            label="自动使用本机 Cursor 登录账号 · Ctrl+Enter 发送",
        )
        tip.SetForegroundColour(wx.Colour(100, 100, 100))
        root.Add(tip, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)

        input_row = wx.BoxSizer(wx.HORIZONTAL)
        self.input = wx.TextCtrl(
            self,
            style=wx.TE_MULTILINE | wx.TE_WORDWRAP,
            size=(-1, 90),
        )
        input_row.Add(self.input, 1, wx.EXPAND | wx.RIGHT, 8)

        btns = wx.BoxSizer(wx.VERTICAL)
        self.btn_send = wx.Button(self, label="发送", size=(96, 36))
        self.btn_send.SetDefault()
        btns.Add(self.btn_send, 0, wx.BOTTOM, 6)
        input_row.Add(btns, 0, wx.ALIGN_BOTTOM)
        root.Add(input_row, 0, wx.EXPAND | wx.ALL, 8)

        self.SetSizer(root)

        self.btn_send.Bind(wx.EVT_BUTTON, self.on_send)
        self.btn_new.Bind(wx.EVT_BUTTON, self.on_new_chat)
        self.btn_clear.Bind(wx.EVT_BUTTON, self.on_clear)
        self.input.Bind(wx.EVT_KEY_DOWN, self.on_input_key)

    def _append(self, role: str, text: str):
        stamp = datetime.now().strftime("%H:%M:%S")
        block = f"[{stamp}] {role}\n{text.rstrip()}\n\n"
        self.history.AppendText(block)
        self.history.ShowPosition(self.history.GetLastPosition())

    def _set_busy(self, busy: bool):
        self._busy = busy
        self.btn_send.Enable(not busy)
        self.input.Enable(not busy)
        self.btn_send.SetLabel("思考中…" if busy else "发送")

    def _refresh_status(self):
        def work():
            ok, msg = probe_connection()
            wx.CallAfter(self._apply_status, ok, msg)

        threading.Thread(target=work, daemon=True).start()

    def _apply_status(self, ok: bool, msg: str):
        prefix = "● " if ok else "○ "
        self.status_label.SetLabel(prefix + msg)
        color = wx.Colour(20, 140, 60) if ok else wx.Colour(180, 90, 20)
        self.status_label.SetForegroundColour(color)
        self.Layout()

    def on_input_key(self, event):
        key = event.GetKeyCode()
        if key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER) and event.ControlDown():
            self.on_send(event)
            return
        event.Skip()

    def on_new_chat(self, event):
        self._chat.reset()
        self._append("系统", "已开始新对话（上下文已重置）。")

    def on_clear(self, event):
        self.history.SetValue("")
        self._append("系统", WELCOME)

    def on_send(self, event):
        if self._busy:
            return
        text = self.input.GetValue().strip()
        if not text:
            return

        self.input.SetValue("")
        self._append("你", text)
        self._set_busy(True)

        def work():
            try:
                result = self._chat.send(text)
                wx.CallAfter(self._on_reply, result.text)
            except CursorChatError as e:
                logger.warning("Cursor chat failed: %s", e)
                wx.CallAfter(self._on_error, str(e), text)
            except Exception as e:
                logger.exception("Cursor chat unexpected error")
                wx.CallAfter(self._on_error, f"意外错误: {e}", text)

        threading.Thread(target=work, daemon=True).start()

    def _on_reply(self, text: str):
        self._set_busy(False)
        self._append("Cursor", text)

    def _on_error(self, msg: str, prompt: str = ""):
        self._set_busy(False)
        self._append("错误", msg)
        # 接口受限时，一键丢到已登录的 Cursor 里用默认模型
        low = (msg or "").lower()
        if prompt and (
            "cursor 限制了面板直连" in low
            or "update required" in low
            or "no longer supported" in low
            or "resource_exhausted" in low
            or "未返回文本" in msg
            or "464" in msg
        ):
            try:
                import webbrowser

                url = (
                    "cursor://anysphere.cursor-deeplink/prompt?text="
                    + quote(prompt, safe="")
                )
                webbrowser.open(url)
                self._append("系统", "已在 Cursor 中打开该问题，请在 Cursor 确认发送。")
            except Exception:
                pass
