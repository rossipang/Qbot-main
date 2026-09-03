#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
双均线 + ADX + ATR 趋势策略（经典公开体系）

买入：MA20 上穿 MA60（金叉），且 ADX ≥ 阈值；
      若回测起点已是多头排列，允许一次「追趋势」入场（避免错过已在进行的主升）。
卖出：
  1) MA20 下穿 MA60（死叉）
  2) 跌破 ATR 移动止损（入场用 2.5×ATR；浮盈大后收紧）

仓位按账户比例下单；日志会同时打印「单票涨跌」和「账户盈亏」，避免误解。

用法：
  python -m qbot.strategies.dual_ma_adx_atr_bt
"""

from __future__ import annotations

from datetime import datetime, timedelta

import backtrader as bt
import pandas as pd


class DualMaAdxAtrStrategy(bt.Strategy):
    """双均线趋势 + ADX 过滤 + ATR 移动止损。"""

    params = dict(
        fast=20,
        slow=60,
        adx_period=14,
        adx_min=20.0,  # ADX 低于此值视为无趋势，不买
        atr_period=14,
        atr_stop_mult=2.5,  # 初始止损距离
        atr_trail_mult=2.5,  # 移动止损
        atr_tight_mult=1.2,  # 浮盈达标后收紧
        atr_tight_trigger=3.0,  # 浮盈 > 此倍数×ATR 后收紧
        allow_trend_chase=True,  # 起点已多头时允许追一次
        stake=100,
        trade_start=None,
        printlog=True,
    )

    def __init__(self):
        self.order = None
        self.entry_price = None
        self.stop_price = None
        self.chase_used = False  # 追趋势入场只用一次

        self.sma_fast = bt.indicators.SMA(self.data.close, period=self.p.fast)
        self.sma_slow = bt.indicators.SMA(self.data.close, period=self.p.slow)
        self.crossover = bt.indicators.CrossOver(self.sma_fast, self.sma_slow)
        self.adx = bt.indicators.AverageDirectionalMovementIndex(
            self.data, period=self.p.adx_period
        )
        self.atr = bt.indicators.ATR(self.data, period=self.p.atr_period)

    def log(self, txt, dt=None):
        if not self.p.printlog:
            return
        dt = dt or self.datas[0].datetime.date(0)
        print(f"{dt.isoformat()}, {txt}")

    def _reset(self):
        self.entry_price = None
        self.stop_price = None

    def notify_order(self, order):
        if order.status in (order.Submitted, order.Accepted):
            return
        if order.status == order.Completed:
            if order.isbuy():
                self.entry_price = float(order.executed.price)
                atr = float(self.atr[0]) if self.atr[0] == self.atr[0] else 0.0
                self.stop_price = self.entry_price - self.p.atr_stop_mult * atr
                self.log(
                    f"买入 价={self.entry_price:.2f} "
                    f"止损={self.stop_price:.2f} "
                    f"ADX={float(self.adx.adx[0]):.1f} "
                    f"ATR={atr:.2f} 费={order.executed.comm:.2f}"
                )
            else:
                self.log(f"卖出 价={order.executed.price:.2f} 费={order.executed.comm:.2f}")
                self._reset()
        elif order.status in (order.Canceled, order.Margin, order.Rejected):
            self.log("订单失败/取消")
        self.order = None

    def notify_trade(self, trade):
        if trade.isclosed:
            self.log(f"平仓 毛利={trade.pnl:.2f} 净利={trade.pnlcomm:.2f}")

    def next(self):
        if self.order:
            return

        if self.p.trade_start is not None:
            if self.datas[0].datetime.date(0) < self.p.trade_start:
                return

        if len(self) < self.p.slow + 2:
            return

        close = float(self.data.close[0])
        adx = float(self.adx.adx[0])
        atr = float(self.atr[0]) if self.atr[0] == self.atr[0] else 0.0

        # ---------- 空仓：金叉，或一次追趋势 ----------
        if not self.position:
            if adx < self.p.adx_min:
                return
            gold = self.crossover[0] > 0
            bull = float(self.sma_fast[0]) > float(self.sma_slow[0])
            chase = (
                self.p.allow_trend_chase
                and (not self.chase_used)
                and bull
                and close > float(self.sma_fast[0])
            )
            if gold or chase:
                why = "金叉" if gold else "追趋势(已多头排列)"
                self.log(
                    f"BUY CREATE {why} MA{self.p.fast}>MA{self.p.slow} "
                    f"ADX={adx:.1f} close={close:.2f}"
                )
                if chase and not gold:
                    self.chase_used = True
                self.order = self.buy(size=self.p.stake)
            return

        # ---------- 持仓：更新 ATR 移动止损 ----------
        if self.entry_price is not None and atr > 0:
            profit = close - self.entry_price
            mult = self.p.atr_trail_mult
            if profit > self.p.atr_tight_trigger * atr:
                mult = self.p.atr_tight_mult
            trail = close - mult * atr
            if self.stop_price is None:
                self.stop_price = trail
            else:
                self.stop_price = max(self.stop_price, trail)

        reason = None
        # 1) 死叉
        if self.crossover[0] < 0:
            reason = (
                f"死叉 MA{self.p.fast}<MA{self.p.slow} "
                f"close={close:.2f}"
            )
        # 2) ATR 移动止损
        elif self.stop_price is not None and close <= self.stop_price:
            ret = (
                (close - self.entry_price) / self.entry_price
                if self.entry_price
                else 0.0
            )
            reason = (
                f"ATR止损 close={close:.2f}<={self.stop_price:.2f} "
                f"浮盈{ret:.1%}"
            )

        if reason:
            self.log(f"SELL CREATE {reason}")
            self.order = self.close()


def _load_daily(code: str, start: str, end: str) -> pd.DataFrame:
    from qbot.data.eastmoney_quote import fetch_kline

    raw = fetch_kline(code=code, begin=start, end=end, period="日线", adjust="前复权")
    if raw is None or raw.empty:
        raise RuntimeError(f"无行情数据: {code}")
    df = raw.rename(columns={"date": "datetime"}).copy()
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.set_index("datetime").sort_index()
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna()


def run_backtest(
    code: str = "603986",
    start: str = "20260601",
    end: str | None = None,
    cash: float = 430000,
    plot: bool = False,
    warmup_days: int = 150,
    position_pct: float = 0.20,
):
    end = end or datetime.now().strftime("%Y%m%d")
    start_dt = datetime.strptime(start, "%Y%m%d")
    fetch_start = (start_dt - timedelta(days=warmup_days)).strftime("%Y%m%d")
    df = _load_daily(code, fetch_start, end)

    trade_df = df[df.index >= pd.Timestamp(start_dt)]
    approx_price = float(
        trade_df["close"].iloc[0] if not trade_df.empty else df["close"].iloc[-1]
    )
    stake = max(100, int(cash * position_pct / approx_price / 100) * 100)

    cerebro = bt.Cerebro()
    cerebro.adddata(bt.feeds.PandasData(dataname=df), name=code)
    cerebro.addstrategy(
        DualMaAdxAtrStrategy,
        stake=stake,
        printlog=True,
        trade_start=start_dt.date(),
    )
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="dd")
    cerebro.broker.setcash(cash)
    cerebro.broker.setcommission(commission=0.0005)

    start_val = cerebro.broker.getvalue()
    print(f"起始资金: {start_val:.2f}  标的: {code}  [双均线+ADX+ATR]")
    print(f"回测区间: {start}~{end} (预热从 {fetch_start})")
    print(
        f"K线根数: {len(df)}  每笔股数: {stake}  "
        f"仓位约{position_pct:.0%}  参考价: {approx_price:.2f}"
    )
    results = cerebro.run()
    end_val = cerebro.broker.getvalue()
    pnl = end_val - start_val
    acct_pct = pnl / start_val * 100

    # 区间买持涨跌（便于对比「单票 vs 账户」）
    if not trade_df.empty:
        bh = (float(trade_df["close"].iloc[-1]) / float(trade_df["close"].iloc[0]) - 1) * 100
    else:
        bh = 0.0

    strat = results[0]
    ta = strat.analyzers.trades.get_analysis()
    closed = ta.get("total", {}).get("closed", 0) if isinstance(ta.get("total"), dict) else 0
    max_dd = strat.analyzers.dd.get_analysis().get("max", {}).get("drawdown", 0)

    print(f"期末资金: {end_val:.2f}")
    print(f"账户盈亏: {pnl:.2f}  ({acct_pct:.2f}%)")
    print(f"同期买持涨跌(单票参考): {bh:.2f}%")
    print(f"完成交易笔数: {closed}  最大回撤: {max_dd:.2f}%")
    if plot:
        try:
            cerebro.plot()
        except Exception as e:
            print(f"绘图跳过: {e}")
    return {
        "code": code,
        "start": start_val,
        "end": end_val,
        "pnl": pnl,
        "acct_pct": acct_pct,
        "buyhold_pct": bh,
        "trades": closed,
        "max_dd": max_dd,
    }


if __name__ == "__main__":
    for name, code in [("兆易创新", "603986"), ("新易盛", "300502")]:
        print("=" * 60)
        print(f"【{name} {code}  2026-06-01 ~ 2026-07-20】")
        run_backtest(code=code, start="20260601", end="20260720", plot=False)
        print()
