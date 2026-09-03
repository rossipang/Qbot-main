# -*- coding: utf-8 -*-
"""每日新闻大事：网站风格版面，近 3 天重点新闻 + 板块多空客观分析。"""

from __future__ import annotations

import threading
from pathlib import Path

import wx

from qbot.common.logging.logger import LOGGER as logger
from qbot.data.daily_news_digest import (
    DIGEST_DAYS,
    HTML_PATH,
    build_daily_news_digest,
    load_latest_daily_news_digest,
)
from qbot.gui.widgets.widget_web import WebPanel


class DailyNewsPanel(wx.Panel):
    def __init__(self, parent):
        super().__init__(parent)
        self._busy = False

        root = wx.BoxSizer(wx.VERTICAL)

        bar = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_refresh = wx.Button(self, label="刷新近3天新闻")
        self.btn_today = wx.Button(self, label="只刷当天")
        self.lbl = wx.StaticText(self, label="启动后自动拉取近三天重点新闻（优先当天）…")
        bar.Add(self.btn_refresh, 0, wx.RIGHT, 8)
        bar.Add(self.btn_today, 0, wx.RIGHT, 12)
        bar.Add(self.lbl, 1, wx.ALIGN_CENTER_VERTICAL)
        root.Add(bar, 0, wx.EXPAND | wx.ALL, 10)

        self.web = WebPanel(self)
        root.Add(self.web, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 4)
        self.SetSizer(root)

        self.btn_refresh.Bind(wx.EVT_BUTTON, lambda e: self.refresh(days=DIGEST_DAYS))
        self.btn_today.Bind(wx.EVT_BUTTON, lambda e: self.refresh(days=1))

        wx.CallAfter(self._boot)

    def _boot(self) -> None:
        cached = load_latest_daily_news_digest()
        if cached and HTML_PATH.exists():
            self._show_html()
            asof = cached.get("asof") or ""
            upd = cached.get("updated_at") or ""
            self.lbl.SetLabel(f"已加载缓存 {asof} · 更新于 {upd} · 后台刷新近{DIGEST_DAYS}天…")
        self.refresh(days=DIGEST_DAYS)

    def _show_html(self) -> None:
        path = Path(HTML_PATH)
        if not path.exists():
            self.web.browser.SetPage(
                "<html><body style='font-family:sans-serif;padding:24px'>"
                "暂无日报，请点「刷新近3天新闻」。</body></html>",
                "",
            )
            return
        try:
            self.web.show_file(str(path))
        except Exception as exc:  # noqa: BLE001
            logger.warning("加载每日新闻 HTML 失败: %s", exc)
            try:
                html = path.read_text(encoding="utf-8", errors="ignore")
                self.web.browser.SetPage(html, path.as_uri())
            except Exception:
                pass

    def refresh(self, days: int = DIGEST_DAYS) -> None:
        if self._busy:
            return
        self._busy = True
        self.btn_refresh.Enable(False)
        self.btn_today.Enable(False)
        self.lbl.SetLabel(f"正在抓取近 {days} 天重点新闻…")

        def work() -> None:
            err = ""
            payload = None
            try:
                payload = build_daily_news_digest(days=days, persist=True, fast=True)
            except Exception as exc:  # noqa: BLE001
                err = str(exc)
                logger.exception("每日新闻大事刷新失败")

            def done() -> None:
                self._busy = False
                self.btn_refresh.Enable(True)
                self.btn_today.Enable(True)
                if err or not payload:
                    self.lbl.SetLabel(f"刷新失败：{err or '无数据'}")
                    return
                self._show_html()
                today_n = int(payload.get("today_count") or 0)
                total = int(payload.get("total") or 0)
                upd = payload.get("updated_at") or ""
                self.lbl.SetLabel(
                    f"近{days}天重点 {total} 条（当天 {today_n}）· {upd}"
                )

            wx.CallAfter(done)

        threading.Thread(target=work, daemon=True).start()
