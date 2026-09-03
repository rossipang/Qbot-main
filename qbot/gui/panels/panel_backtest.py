"""
Author: Charmve yidazhang1@gmail.com
Date: 2023-05-20 16:37:34
LastEditors: Charmve yidazhang1@gmail.com
LastEditTime: 2023-09-20 10:16:19
FilePath: /Qbot/gui/panels/panel_backtest.py
Version: 1.0.1
Blogs: charmve.blog.csdn.net
GitHub: https://github.com/Charmve
Description: 

Copyright (c) 2023 by Charmve, All Rights Reserved. 
Licensed under the MIT License.
"""
from pathlib import Path
import threading

import wx

from qbot.common.file_utils import extract_content
from qbot.common.logging.logger import LOGGER as logger
from qbot.common.macros import strategy_choices
from qbot.data.eastmoney_quote import fetch_kline, normalize_symbol
from qbot.data.market_overview import render_market_overview, write_loading_html
from qbot.data.quote_charts import render_quote_dashboard
from qbot.data.stock_detail_page import render_stock_detail_page
from qbot.gui import gui_utils
from qbot.gui.config import DATA_DIR_BKT_RESULT, DATA_DIR_CSV
from qbot.gui.elements.def_dialog import MessageDialog
from qbot.gui.panels.panel_quote_detail import open_quote_detail
from qbot.gui.widgets.widget_web import WebPanel


# https://zhuanlan.zhihu.com/p/376248349
def OnBkt(event):
    wx.MessageBox("ok")


class PanelBacktest(wx.Panel):
    def __init__(self, parent=None, id=-1, displaySize=(1600, 900)):
        super(PanelBacktest, self).__init__(parent)

        self.backtest_opts = {
            "start_time": "20100101",
            "end_time": "20211231",
            "benchmark": "000300.SH",
            "code": "399006.SZ",
            "select_strategy": "单因子-相对强弱指数RSI",
        }

        self.backtest_config = {
            "limit_threshold": 0.095,
            "init_cash": 10000,
            "deal_price": "close",
            "open_cost": 0.0005,
            "slippage": 0.1,
            "stake": 100,  # 每笔交易量
            "commission": 0.0005,
            "stamp_duty": 0.001,
            "close_cost": 0.0015,
            "min_cost": 5,
        }

        # M1 与 M2 横向布局时宽度分割
        self.M1_width = int(displaySize[0] * 0.1)
        self.M2_width = int(displaySize[0] * 0.9)
        # M1 纵向100%
        self.M1_length = int(displaySize[1])

        # M1中S1 S2 S3 纵向布局高度分割
        self.M1S1_length = int(self.M1_length * 0.2)
        self.M1S2_length = int(self.M1_length * 0.2)
        self.M1S3_length = int(self.M1_length * 0.6)

        self.BackWebPanel = WebPanel(self)

        # 第二层布局
        self.vbox_sizer_b = wx.BoxSizer(wx.VERTICAL)  # 纵向box
        self.vbox_sizer_b.Add(
            self._init_para_notebook(),
            proportion=1,
            flag=wx.EXPAND | wx.BOTTOM,
            border=5,
        )  # 添加行情参数布局
        # self.vbox_sizer_b.Add(
        #     self.patten_log_tx, proportion=10, flag=wx.EXPAND | wx.BOTTOM, border=5
        # )

        self.vbox_sizer_b.Add(
            self.BackWebPanel,
            proportion=10,
            flag=wx.EXPAND | wx.ALL | wx.CENTER,
            border=5,
        )

        # 第一层布局
        self.HBoxPanelSizer = wx.BoxSizer(wx.HORIZONTAL)
        self.HBoxPanelSizer.Add(
            self.vbox_sizer_b, proportion=0, border=2, flag=wx.EXPAND | wx.ALL
        )
        self.SetSizer(self.HBoxPanelSizer)  # 使布局有效

        # self.layout()

    def layout(self):
        vbox = wx.BoxSizer(wx.VERTICAL)
        self.SetSizer(vbox)

        hbox = wx.BoxSizer(wx.HORIZONTAL)
        vbox.Add(hbox)

        hbox.Add(wx.StaticText(self, label="请选择基准:"))
        combo_benchmarks = wx.ComboBox(self, size=(190, 25), pos=(10, 120))
        combo_benchmarks.SetItems(["沪深300指数(000300.SH)", "标普500指数(SPY)"])
        hbox.Add(combo_benchmarks)

        # vbox.Add(panel, 0)
        btn = wx.Button(self, label="开始回测", style=1)
        self.Bind(wx.EVT_BUTTON, self.StartBacktest, btn)
        vbox.Add(btn)

        # 底部是一个浏览器
        web = WebPanel(self)
        vbox.Add(web, 1, wx.EXPAND)
        web.show_file(
            write_loading_html(DATA_DIR_BKT_RESULT.joinpath("market_overview.html"))
        )

        # web.show_url('http://www.jisilu.cn')

        self.web = web

    def on_refresh_market(self, event=None):
        self._start_market_overview_load()

    def _start_market_overview_load(self):
        """后台生成大盘首页并展示。"""
        if getattr(self, "_market_loading", False):
            return
        self._market_loading = True
        try:
            if hasattr(self, "refresh_market_but"):
                self.refresh_market_but.Disable()
        except Exception:
            pass
        try:
            loading = write_loading_html(
                DATA_DIR_BKT_RESULT.joinpath("market_overview.html")
            )
            self.BackWebPanel.show_file(loading)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"展示大盘加载页失败: {exc}")

        def work():
            err = None
            path = None
            try:
                path = render_market_overview(
                    DATA_DIR_BKT_RESULT.joinpath("market_overview.html")
                )
            except Exception as exc:  # noqa: BLE001
                err = exc
                logger.error(f"生成大盘概况失败: {exc}")

            def done():
                self._market_loading = False
                try:
                    if hasattr(self, "refresh_market_but"):
                        self.refresh_market_but.Enable()
                except Exception:
                    pass
                if err is not None:
                    try:
                        MessageDialog(f"加载大盘概况失败：{err}")
                    except Exception:
                        pass
                    return
                try:
                    self.BackWebPanel.show_file(path)
                    self.BackWebPanel.Raise()
                    self.Layout()
                    self.Refresh()
                    logger.info(f"大盘概况已展示: {path}")
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"展示大盘概况失败: {exc}")
                    try:
                        MessageDialog(f"大盘数据已生成，但页面展示失败：{exc}")
                    except Exception:
                        pass

            wx.CallAfter(done)

        threading.Thread(target=work, daemon=True).start()

    def _init_para_notebook(self):

        # 创建参数区面板
        self.ParaNoteb = wx.Notebook(self)
        self.ParaStPanel = wx.Panel(self.ParaNoteb, -1)  # 行情
        self.ParaBtPanel = wx.Panel(self.ParaNoteb, -1)  # 回测 back test
        self.ParaPtPanel = wx.Panel(self.ParaNoteb, -1)  # 条件选股 pick stock
        self.ParaPaPanel = wx.Panel(self.ParaNoteb, -1)  # 形态选股 patten

        # 第二层布局
        self.ParaStPanel.SetSizer(self.add_stock_para_lay(self.ParaStPanel))
        self.ParaBtPanel.SetSizer(self.add_backt_para_lay(self.ParaBtPanel))
        # self.ParaPtPanel.SetSizer(self.add_pick_para_lay(self.ParaPtPanel))
        # self.ParaPaPanel.SetSizer(self.add_patten_para_lay(self.ParaPaPanel))

        self.ParaNoteb.AddPage(self.ParaStPanel, "行情参数")
        self.ParaNoteb.AddPage(self.ParaBtPanel, "回测参数")
        # self.ParaNoteb.AddPage(self.ParaPtPanel, "条件选股")
        # self.ParaNoteb.AddPage(self.ParaPaPanel, "形态选股")

        return self.ParaNoteb

    def add_stock_para_lay(self, sub_panel):

        # 行情参数
        stock_para_sizer = wx.BoxSizer(wx.HORIZONTAL)

        # 行情参数——日历控件时间周期
        self.dpc_end_time = wx.adv.DatePickerCtrl(
            sub_panel,
            -1,
            style=wx.adv.DP_DROPDOWN | wx.adv.DP_SHOWCENTURY | wx.adv.DP_ALLOWNONE,
        )  # 结束时间
        self.dpc_start_time = wx.adv.DatePickerCtrl(
            sub_panel,
            -1,
            style=wx.adv.DP_DROPDOWN | wx.adv.DP_SHOWCENTURY | wx.adv.DP_ALLOWNONE,
        )  # 起始时间

        self.start_date_box = wx.StaticBox(sub_panel, -1, "开始日期(Start)")
        self.end_date_box = wx.StaticBox(sub_panel, -1, "结束日期(End)")
        self.start_date_sizer = wx.StaticBoxSizer(self.start_date_box, wx.VERTICAL)
        self.end_date_sizer = wx.StaticBoxSizer(self.end_date_box, wx.VERTICAL)
        self.start_date_sizer.Add(
            self.dpc_start_time,
            proportion=0,
            flag=wx.EXPAND | wx.ALL | wx.CENTER,
            border=2,
        )
        self.end_date_sizer.Add(
            self.dpc_end_time,
            proportion=0,
            flag=wx.EXPAND | wx.ALL | wx.CENTER,
            border=2,
        )

        date_time_now = wx.DateTime.Now()  # wx.DateTime格式"03/03/18 00:00:00"
        self.dpc_end_time.SetValue(date_time_now)
        self.dpc_start_time.SetValue(date_time_now.SetYear(date_time_now.year - 1))

        self.Bind(wx.adv.EVT_DATE_CHANGED, self._on_end_time_changed, self.dpc_end_time)
        self.Bind(
            wx.adv.EVT_DATE_CHANGED, self._on_start_time_changed, self.dpc_start_time
        )

        self.backtest_opts["end_time"] = gui_utils._wxdate2pydate(
            self.dpc_end_time.GetValue()
        ).strftime("%Y%m%d")
        self.backtest_opts["start_time"] = gui_utils._wxdate2pydate(
            self.dpc_start_time.GetValue()
        ).strftime("%Y%m%d")

        # 行情参数——输入股票代码
        self.stock_code_box = wx.StaticBox(sub_panel, -1, "交易标的(股票/期货/比特币)代码")
        self.stock_code_sizer = wx.StaticBoxSizer(self.stock_code_box, wx.VERTICAL)
        self.stock_code_input = wx.TextCtrl(
            sub_panel, -1, "399006.SZ", style=wx.TE_PROCESS_ENTER
        )
        self.stock_code_sizer.Add(
            self.stock_code_input,
            proportion=0,
            flag=wx.EXPAND | wx.ALL | wx.CENTER,
            border=2,
        )
        self.stock_code_input.Bind(wx.EVT_TEXT_ENTER, self._ev_enter_stcode)
        self.Bind(wx.EVT_TEXT, self._on_combobox_code_changed, self.stock_code_input)
        select_code = self.stock_code_input.GetValue()
        logger.debug(f"select_code: {select_code}")
        self.backtest_opts["code"] = select_code

        # 行情参数——股票周期选择
        self.stock_period_box = wx.StaticBox(sub_panel, -1, "股票周期")
        self.stock_period_sizer = wx.StaticBoxSizer(self.stock_period_box, wx.VERTICAL)
        self.stock_period_cbox = wx.ComboBox(
            sub_panel, -1, "", choices=["30分钟", "60分钟", "日线", "周线"]
        )
        self.stock_period_cbox.SetSelection(2)
        self.stock_period_sizer.Add(
            self.stock_period_cbox,
            proportion=0,
            flag=wx.EXPAND | wx.ALL | wx.CENTER,
            border=2,
        )

        # 行情参数——股票复权选择
        self.stock_authority_box = wx.StaticBox(sub_panel, -1, "股票复权")
        self.stock_authority_sizer = wx.StaticBoxSizer(
            self.stock_authority_box, wx.VERTICAL
        )
        self.stock_authority_cbox = wx.ComboBox(
            sub_panel, -1, "", choices=["前复权", "后复权", "不复权"]
        )
        # 默认前复权：腾讯降级源对“不复权”常返回空
        self.stock_authority_cbox.SetSelection(0)
        self.stock_authority_sizer.Add(
            self.stock_authority_cbox,
            proportion=0,
            flag=wx.EXPAND | wx.ALL | wx.CENTER,
            border=2,
        )

        # 行情参数——多子图显示
        self.pick_graph_box = wx.StaticBox(sub_panel, -1, "多子图显示")
        self.pick_graph_sizer = wx.StaticBoxSizer(self.pick_graph_box, wx.VERTICAL)
        self.pick_graph_cbox = wx.ComboBox(
            sub_panel,
            -1,
            "未开启",
            choices=[
                "未开启",
                "A股票走势-MPL",
                "B股票走势-MPL",
                "C股票走势-MPL",
                "D股票走势-MPL",
                "A股票走势-WEB",
                "B股票走势-WEB",
                "C股票走势-WEB",
                "D股票走势-WEB",
            ],
            style=wx.CB_READONLY | wx.CB_DROPDOWN,
        )
        self.pick_graph_cbox.SetSelection(0)
        self.pick_graph_last = self.pick_graph_cbox.GetSelection()
        self.pick_graph_sizer.Add(
            self.pick_graph_cbox,
            proportion=0,
            flag=wx.EXPAND | wx.ALL | wx.CENTER,
            border=2,
        )
        # self.pick_graph_cbox.Bind(wx.EVT_COMBOBOX, self._ev_select_graph)

        # 行情参数——股票组合分析
        self.group_analy_box = wx.StaticBox(sub_panel, -1, "投资组合分析")
        self.group_analy_sizer = wx.StaticBoxSizer(self.group_analy_box, wx.VERTICAL)
        self.group_analy_cmbo = wx.ComboBox(
            sub_panel,
            -1,
            "预留A",
            choices=["预留A", "收益率/波动率", "走势叠加分析", "财务指标评分-预留"],
            style=wx.CB_READONLY | wx.CB_DROPDOWN,
        )  # 策略名称
        self.group_analy_sizer.Add(
            self.group_analy_cmbo,
            proportion=0,
            flag=wx.EXPAND | wx.ALL | wx.CENTER,
            border=2,
        )
        # self.group_analy_cmbo.Bind(wx.EVT_COMBOBOX, self._ev_group_analy)  # 绑定ComboBox事件

        # 回测按钮
        self.load_data_but = wx.Button(sub_panel, -1, "加载行情数据")
        self.load_data_but.SetBackgroundColour(wx.Colour(76, 187, 23))  # 设置背景颜色
        # self.load_data_but.Bind(wx.EVT_BUTTON, self._ev_start_run)  # 绑定按钮事件
        self.load_data_but.Bind(wx.EVT_BUTTON, self.LoadData)  # 绑定按钮事件

        self.refresh_market_but = wx.Button(sub_panel, -1, "刷新大盘")
        self.refresh_market_but.Bind(wx.EVT_BUTTON, self.on_refresh_market)

        stock_para_sizer.Add(
            self.start_date_sizer,
            proportion=0,
            flag=wx.EXPAND | wx.CENTER | wx.ALL,
            border=5,
        )
        stock_para_sizer.Add(
            self.end_date_sizer,
            proportion=0,
            flag=wx.EXPAND | wx.ALL | wx.CENTER,
            border=5,
        )
        stock_para_sizer.Add(
            self.stock_code_sizer,
            proportion=0,
            flag=wx.EXPAND | wx.ALL | wx.CENTER,
            border=5,
        )
        stock_para_sizer.Add(
            self.stock_period_sizer,
            proportion=0,
            flag=wx.EXPAND | wx.ALL | wx.CENTER,
            border=5,
        )
        stock_para_sizer.Add(
            self.stock_authority_sizer,
            proportion=0,
            flag=wx.EXPAND | wx.ALL | wx.CENTER,
            border=5,
        )
        stock_para_sizer.Add(
            self.pick_graph_sizer,
            proportion=0,
            flag=wx.EXPAND | wx.ALL | wx.CENTER,
            border=5,
        )
        stock_para_sizer.Add(
            self.group_analy_sizer,
            proportion=0,
            flag=wx.EXPAND | wx.ALL | wx.CENTER,
            border=5,
        )
        stock_para_sizer.Add(
            self.load_data_but,
            proportion=0,
            flag=wx.EXPAND | wx.ALL | wx.CENTER,
            border=5,
        )
        stock_para_sizer.Add(
            self.refresh_market_but,
            proportion=0,
            flag=wx.EXPAND | wx.ALL | wx.CENTER,
            border=5,
        )

        return stock_para_sizer

    def add_backt_para_lay(self, sub_panel):

        # 回测参数
        back_para_sizer = wx.BoxSizer(wx.HORIZONTAL)

        # 行情参数——输入股票基准
        self.stock_benchmark_box = wx.StaticBox(sub_panel, -1, "回测基准选取")
        self.stock_benchmark_sizer = wx.StaticBoxSizer(
            self.stock_benchmark_box, wx.VERTICAL
        )
        self.stock_benchmark_cbox = wx.ComboBox(
            sub_panel,
            -1,
            "",
            choices=["沪深300指数(000300.SH)", "标普500指数(SPX)", "恒生指数(HSI)"],
        )
        self.stock_benchmark_cbox.SetSelection(0)
        # self.stock_benchmark_cbox.Bind(wx.EVT_COMBOBOX, self._on_combobox_benchmarks_changed)  # noqa: E501
        self.select_benchmark = self.stock_benchmark_cbox.GetValue()
        self.benchmark_code = extract_content(self.select_benchmark)[0]
        logger.debug(f"select_benchmark: {self.benchmark_code}")
        self.backtest_opts["benchmark"] = self.benchmark_code
        self.stock_benchmark_sizer.Add(
            self.stock_benchmark_cbox,
            proportion=0,
            flag=wx.EXPAND | wx.ALL | wx.CENTER,
            border=2,
        )

        # 行情参数——初始资金
        self.init_cash_box = wx.StaticBox(sub_panel, -1, "初始资金")
        self.init_cash_sizer = wx.StaticBoxSizer(self.init_cash_box, wx.VERTICAL)
        self.init_cash_input = wx.TextCtrl(
            sub_panel, -1, str(self.backtest_config["init_cash"]), style=wx.TE_LEFT
        )
        # self.Bind(wx.EVT_TEXT, self._on_init_cash_changed, self.init_cash_input)
        self.init_cash_sizer.Add(
            self.init_cash_input,
            proportion=0,
            flag=wx.EXPAND | wx.ALL | wx.CENTER,
            border=2,
        )

        self.init_stake_box = wx.StaticBox(sub_panel, -1, "交易规模")
        self.init_stake_sizer = wx.StaticBoxSizer(self.init_stake_box, wx.VERTICAL)
        self.init_stake_input = wx.TextCtrl(
            sub_panel, -1, str(self.backtest_config["stake"]), style=wx.TE_LEFT
        )
        # self.Bind(wx.EVT_TEXT, self._on_stake_changed, self.init_stake_input)
        self.init_stake_sizer.Add(
            self.init_stake_input,
            proportion=0,
            flag=wx.EXPAND | wx.ALL | wx.CENTER,
            border=2,
        )

        self.init_slippage_box = wx.StaticBox(sub_panel, -1, "滑点")
        self.init_slippage_sizer = wx.StaticBoxSizer(
            self.init_slippage_box, wx.VERTICAL
        )
        self.init_slippage_input = wx.TextCtrl(
            sub_panel, -1, str(self.backtest_config["slippage"]), style=wx.TE_LEFT
        )
        # self.Bind(wx.EVT_TEXT, self._on_slippage_changed, self.init_slippage_input)
        self.init_slippage_sizer.Add(
            self.init_slippage_input,
            proportion=0,
            flag=wx.EXPAND | wx.ALL | wx.CENTER,
            border=2,
        )

        self.init_commission_box = wx.StaticBox(sub_panel, -1, "手续费")
        self.init_commission_sizer = wx.StaticBoxSizer(
            self.init_commission_box, wx.VERTICAL
        )
        self.init_commission_input = wx.TextCtrl(
            sub_panel, -1, str(self.backtest_config["commission"]), style=wx.TE_LEFT
        )
        # self.Bind(wx.EVT_TEXT, self._on_commission_changed, self.init_commission_input)
        self.init_commission_sizer.Add(
            self.init_commission_input,
            proportion=0,
            flag=wx.EXPAND | wx.ALL | wx.CENTER,
            border=2,
        )

        self.init_tax_box = wx.StaticBox(sub_panel, -1, "印花税")
        self.init_tax_sizer = wx.StaticBoxSizer(self.init_tax_box, wx.VERTICAL)
        self.init_tax_input = wx.TextCtrl(
            sub_panel, -1, str(self.backtest_config["stamp_duty"]), style=wx.TE_LEFT
        )
        # self.Bind(wx.EVT_TEXT, self._on_stamp_duty_changed, self.init_tax_input)
        self.init_tax_sizer.Add(
            self.init_tax_input,
            proportion=0,
            flag=wx.EXPAND | wx.ALL | wx.CENTER,
            border=2,
        )

        # 行情参数——回测策略选择
        self.stock_strategy_box = wx.StaticBox(sub_panel, -1, "回测策略选取")
        self.stock_strategy_sizer = wx.StaticBoxSizer(
            self.stock_strategy_box, wx.HORIZONTAL
        )
        self.stock_strategy_cbox = wx.ComboBox(
            sub_panel,
            -1,
            "",
            choices=list(strategy_choices)[0],
            style=wx.CB_READONLY | wx.CB_DROPDOWN,
        )
        self.stock_strategy_cbox.SetSelection(0)
        self.stock_strategy_sizer.Add(
            self.stock_strategy_cbox,
            proportion=0,
            flag=wx.EXPAND | wx.ALL | wx.CENTER,
            border=2,
        )
        # self.stock_strategy_cbox.Bind(wx.EVT_RADIOBUTTON, self._ev_src_choose)
        # self.stock_strategy_cbox.Bind(wx.EVT_COMBOBOX, self._on_combobox_strategy_changed)
        select_strategy = self.stock_strategy_cbox.GetStringSelection()
        self.backtest_opts["select_strategy"] = select_strategy
        logger.debug(f"select_strategy: {select_strategy}")

        # 回测按钮
        self.start_back_but = wx.Button(sub_panel, -1, "开始回测")
        self.start_back_but.SetBackgroundColour(wx.Colour(76, 187, 23))  # 设置背景颜色
        # self.start_back_but.Bind(wx.EVT_BUTTON, self._ev_start_run)  # 绑定按钮事件
        self.start_back_but.Bind(wx.EVT_BUTTON, self.StartBacktest)  # 绑定按钮事件

        # 交易日志
        self.trade_log_but = wx.Button(sub_panel, -1, "交易日志")
        # self.trade_log_but.Bind(wx.EVT_BUTTON, self._ev_trade_log)  # 绑定按钮事件

        self.BackWebPanel.show_file(
            write_loading_html(DATA_DIR_BKT_RESULT.joinpath("market_overview.html"))
        )
        # 启动后默认加载当天上证分时 + 大盘概况（后台，不卡 UI）
        wx.CallAfter(self._start_market_overview_load)

        back_para_sizer.Add(
            self.stock_benchmark_sizer,
            proportion=0,
            flag=wx.EXPAND | wx.ALL | wx.CENTER,
            border=5,
        )
        back_para_sizer.Add(
            self.init_cash_sizer,
            proportion=0,
            flag=wx.EXPAND | wx.ALL | wx.CENTER,
            border=5,
        )
        back_para_sizer.Add(
            self.init_stake_sizer,
            proportion=0,
            flag=wx.EXPAND | wx.ALL | wx.CENTER,
            border=5,
        )
        back_para_sizer.Add(
            self.init_slippage_sizer,
            proportion=0,
            flag=wx.EXPAND | wx.ALL | wx.CENTER,
            border=5,
        )
        back_para_sizer.Add(
            self.init_commission_sizer,
            proportion=0,
            flag=wx.EXPAND | wx.ALL | wx.CENTER,
            border=5,
        )
        back_para_sizer.Add(
            self.init_tax_sizer,
            proportion=0,
            flag=wx.EXPAND | wx.ALL | wx.CENTER,
            border=5,
        )
        back_para_sizer.Add(
            self.stock_strategy_sizer,
            proportion=0,
            flag=wx.EXPAND | wx.ALL | wx.CENTER,
            border=5,
        )
        back_para_sizer.Add(
            self.start_back_but,
            proportion=0,
            flag=wx.EXPAND | wx.ALL | wx.CENTER,
            border=5,
        )
        back_para_sizer.Add(
            self.trade_log_but,
            proportion=0,
            flag=wx.EXPAND | wx.ALL | wx.CENTER,
            border=5,
        )

        return back_para_sizer

    def _ev_enter_stcode(self, event):  # 输入股票代码

        # 第一步:收集控件中设置的选项
        st_code = self.stock_code_input.GetValue()
        st_name = self.code_table.get_name(st_code)
        self.backtest_opts["code"] = st_code
        logger.info(f"回测股票/基金: You select {st_code}")

    def _on_start_time_changed(self, event):
        py_date = gui_utils._wxdate2pydate(self.dpc_start_time.GetValue())
        if py_date is None:
            return
        start_time = py_date.strftime("%Y%m%d")
        self.backtest_opts["start_time"] = start_time
        logger.info(f"start_time: {start_time}")

    def _on_end_time_changed(self, event):
        py_date = gui_utils._wxdate2pydate(self.dpc_end_time.GetValue())
        if py_date is None:
            return
        end_time = py_date.strftime("%Y%m%d")
        self.backtest_opts["end_time"] = end_time
        logger.info(f"end_time: {end_time}")

    def _on_init_cash_changed(self, event):
        self.backtest_config["init_cash"] = self.init_cash_input.GetValue()

    def _on_slippage_changed(self, event):
        self.backtest_config["slippage"] = self.init_slippage_input.GetValue()

    def _on_stake_changed(self, event):
        self.backtest_config["stake"] = self.init_stake_input.GetValue()

    def _on_commission_changed(self, event):
        self.backtest_config["commission"] = self.init_commission_input.GetValue()

    def _on_stamp_duty_changed(self, event):
        self.backtest_config["stamp_duty"] = self.init_tax_input.GetValue()

    # def _on_text_changed(self, event):
    #     with open(RESULT_DIR.joinpath("backtest.log"), "r") as f:
    #         self.log_text_ctrl.SetValue(f.read())

    def _on_combobox_benchmarks_changed(self, event):
        self.select_benchmark = self.stock_benchmark_cbox.GetValue()
        self.benchmark_code = extract_content(self.select_benchmark)[0]
        logger.debug(f"select_benchmark: {self.benchmark_code}")
        self.backtest_opts["benchmark"] = self.benchmark_code
        logger.info(f"基准: You select {self.benchmark_code}")

        # data_files_tmp = [x for x in self.data_files if x != self.benchmark_code]
        # # logger.debug(f"new code list: {data_files_tmp}")
        # self.combo_codes.SetItems(data_files_tmp)
        # self.combo_codes.SetValue("399006.SZ")

    def _on_combobox_strategy_changed(self, event):
        select_strategy = self.stock_strategy_cbox.GetValue()
        self.backtest_opts["select_strategy"] = select_strategy
        logger.info(f"交易策略: You select {select_strategy}")

    def _on_combobox_code_changed(self, event):
        select_code = self.stock_code_input.GetValue()
        self.backtest_opts["code"] = select_code
        logger.info(f"回测股票/基金: You select {select_code}")

    def _ev_trade_log(self, event):
        pass

    def StartBacktest(self, event):
        msg = "在线回测属于付费功能，请联系微信：Yida_Zhang2"
        MessageDialog(msg)
        print(msg)
        pass

    def LoadData(self, event):
        """从东方财富免费接口加载行情数据。"""
        code = self.stock_code_input.GetValue().strip()
        start_py = gui_utils._wxdate2pydate(self.dpc_start_time.GetValue())
        end_py = gui_utils._wxdate2pydate(self.dpc_end_time.GetValue())
        if start_py is None or end_py is None:
            MessageDialog("请选择有效的开始/结束日期")
            return
        start_time = start_py.strftime("%Y%m%d")
        end_time = end_py.strftime("%Y%m%d")
        period = self.stock_period_cbox.GetValue() or "日线"
        adjust = self.stock_authority_cbox.GetValue() or "前复权"

        self.backtest_opts["code"] = code
        self.backtest_opts["start_time"] = start_time
        self.backtest_opts["end_time"] = end_time

        if not code:
            MessageDialog("请先填写交易标的代码")
            return
        if start_time > end_time:
            MessageDialog("开始日期不能晚于结束日期")
            return

        self.load_data_but.Disable()
        try:
            wx.BeginBusyCursor()
            df = fetch_kline(
                code=code,
                begin=start_time,
                end=end_time,
                period=period,
                adjust=adjust,
            )
        except Exception as exc:
            logger.error(f"加载东方财富行情失败: {exc}")
            MessageDialog(f"加载行情失败：{exc}")
            return
        finally:
            try:
                wx.EndBusyCursor()
            except Exception:
                pass
            self.load_data_but.Enable()

        if df is None or df.empty:
            MessageDialog(f"未获取到行情数据：{code}（{start_time} ~ {end_time}）")
            return

        self.market_data = df
        symbol = normalize_symbol(code)
        DATA_DIR_CSV.mkdir(parents=True, exist_ok=True)
        csv_path = DATA_DIR_CSV.joinpath(
            f"{symbol}_{period}_{adjust}_{start_time}_{end_time}.csv"
        )
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")

        name = str(df.iloc[0].get("name", "")) if "name" in df.columns else ""
        title = f"{name}({code})" if name else code
        source = df.attrs.get("source", "未知来源")

        try:
            wx.BeginBusyCursor()
            html_path = self._render_quote_html(
                df=df,
                code=code,
                period=period,
                adjust=adjust,
                start_time=start_time,
                end_time=end_time,
                csv_path=csv_path,
            )
            # 先展示图表，再弹提示，避免用户只看到“已保存CSV”
            self.BackWebPanel.show_file(html_path)
            self.BackWebPanel.Raise()
            self.Layout()
            self.Refresh()
            logger.info(f"行情图表已展示: {html_path}")
        except Exception as exc:
            logger.error(f"展示行情图表失败: {exc}")
            MessageDialog(f"数据已保存，但图表展示失败：{exc}\nCSV：{csv_path}")
            return
        finally:
            try:
                wx.EndBusyCursor()
            except Exception:
                pass

        # 成功时不弹窗，直接展示图表；仅失败时提示
        logger.info(
            f"行情加载完成: code={code}, source={source}, rows={len(df)}, "
            f"file={csv_path}, title={title}"
        )

    def _render_quote_html(
        self, df, code, period, adjust, start_time, end_time, csv_path
    ):
        """
        个股详情与「行业选股」一致：分时 / 日K（含当天）/ 资金等标签页。
        非日线周期仍用原交互看板，便于回测区间预览。
        """
        name = ""
        if "name" in df.columns and not df.empty:
            name = str(df.iloc[0]["name"])
        title = f"{name} {code}".strip() or str(code)
        source = df.attrs.get("source", "未知来源")
        html_path = DATA_DIR_BKT_RESULT.joinpath("quote_preview.html")

        # 日线：专业详情页（含当天日K/资金柱）
        period_s = str(period or "")
        if "日" in period_s or period_s in ("101", "日线", "day"):
            try:
                path = render_stock_detail_page(
                    code=code,
                    output_path=html_path,
                    name=name,
                )
                # 同步弹出与行业选股相同的详情窗（可盘中刷新）
                wx.CallAfter(open_quote_detail, self, code, name)
                return path
            except Exception as exc:
                logger.error(f"专业个股详情失败，回退看板: {exc}")

        try:
            return render_quote_dashboard(
                df=df,
                output_path=html_path,
                title=title,
                meta={
                    "code": code,
                    "source": source,
                    "period": period,
                    "adjust": adjust,
                    "csv": str(csv_path),
                    "start": start_time,
                    "end": end_time,
                },
            )
        except Exception as exc:
            # 图表失败时回退到表格，避免加载流程整体中断
            logger.error(f"生成行情图表失败，回退表格预览: {exc}")
            preview = df.head(200)
            table_html = preview.to_html(index=False, border=0, classes="quote-table")
            html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/><title>{title}</title></head>
<body>
<h2>{title} 行情数据</h2>
<p>图表渲染失败：{exc}</p>
<p>数据源：{source}，区间：{start_time} ~ {end_time}，CSV：{Path(csv_path).as_posix()}</p>
{table_html}
</body></html>"""
            html_path.write_text(html, encoding="utf-8")
            return html_path
