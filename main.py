#!/usr/bin/python
# -*- coding: UTF-8 -*-

import os
import sys
import warnings

# Quiet third-party / future noise before heavy imports.
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=PendingDeprecationWarning)
warnings.filterwarnings("ignore", message=r".*np\.bool8.*")
warnings.filterwarnings("ignore", message=r".*InconsistentVersionWarning.*")
os.environ.setdefault("PYTHONWARNINGS", "ignore")

import wx

from qbot.gui.mainframe import MainFrame


_SINGLE_INSTANCE_NAME = "Qbot.AIQuant.MainFrame.v1"


def _another_instance_running() -> bool:
    checker = wx.SingleInstanceChecker(_SINGLE_INSTANCE_NAME)
    return checker.IsAnotherRunning()


def _quiet_wx_logs() -> None:
    """Hide wx StaticBoxSizer parenting chatter and similar non-fatal logs."""
    if os.environ.get("QBOT_DEBUG", "").strip().lower() in ("1", "true", "yes"):
        return
    try:
        wx.Log.SetLogLevel(wx.LOG_Error)
    except Exception:
        pass


if __name__ == "__main__":
    app = wx.App(redirect=False)
    _quiet_wx_logs()
    if _another_instance_running():
        wx.MessageBox(
            "Qbot 已在运行，请勿重复启动。\n若看不到窗口，请先在任务栏切换或结束旧进程后再开。",
            "提示",
            wx.OK | wx.ICON_INFORMATION,
        )
        sys.exit(0)

    frame = MainFrame(None, title="AI智能量化投研平台")
    frame.Show()

    app.MainLoop()
