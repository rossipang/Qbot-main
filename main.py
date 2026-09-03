#!/usr/bin/python
# -*- coding: UTF-8 -*-

import sys

import wx

from qbot.gui.mainframe import MainFrame

_SINGLE_INSTANCE_NAME = "Qbot.AIQuant.MainFrame.v1"


def _another_instance_running() -> bool:
    checker = wx.SingleInstanceChecker(_SINGLE_INSTANCE_NAME)
    return checker.IsAnotherRunning()


if __name__ == "__main__":
    app = wx.App()
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
