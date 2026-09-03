#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
热点波段策略 · V5

目标：连涨尽量拿住；出现「比较明显」的下跌迹象再卖；收益尽量大、风险尽量小。

买入：连续两日上涨 + 第 2 日放量（跟趋势）
卖出（满足任一条即卖，都好懂）：
  1) 收盘跌破 MA20 —— 中期趋势坏了
  2) 确认两连阴破位：连跌两日 + 第2日放量 + 收盘跌破近5日低点
  3) 加速破位大阴：单日跌≥7%，且已从持仓最高价回撤≥3%
  4) 从持仓最高价回撤≥8% —— 主出场
  5) 相对成本亏≥12% —— 硬止损

小阴、缩量阴、刚创新高后的普通回调 —— 不卖。

用法：
  python -m qbot.strategies.hot_sector_risk_managed_bt
"""

from __future__ import annotations

from datetime import datetime, timedelta

import backtrader as bt
import pandas as pd

DEFAULT_POOL = [
    "600011",
    "600900",
    "600938",
    "601898",
    "601166",
    "002422",
]


class HotSectorRiskManagedStrategy(bt.Strategy):
    """V5：两连阳放量买 + 明显转弱卖。"""

    params = dict(
        # —— 买入 ——
        vol_ma_period=5,
        vol_expand_ratio=1.0,
        need_vol_gt_prev=True,
        need_above_ma20=True,
        # —— 卖出：明显转弱 ——
        big_drop=0.07,  # 单日跌≥7%
        big_drop_min_peak_dd=0.08,  # 与主回撤一致，避免洗盘日抢先卖
        peak_drawdown=0.08,  # 距持仓最高价回撤≥8% 即卖
        hard_stop=0.12,  # 相对成本亏≥12%
        swing_low_period=5,
        # —— 其它 ——
        stake=100,
        trade_start=None,
        printlog=True,
    )

    def __init__(self):
        self.order = None
        self.entry_price = None
        self.peak_price = None  # 持仓以来最高收盘价

        self.sma20 = bt.indicators.SimpleMovingAverage(self.data.close, period=20)
        self.vol_ma = bt.indicators.SimpleMovingAverage(
            self.data.volume, period=self.p.vol_ma_period
        )
        self.lowest5 = bt.indicators.Lowest(
            self.data.low, period=self.p.swing_low_period
        )

    def log(self, txt, dt=None):
        if not self.p.printlog:
            return
        dt = dt or self.datas[0].datetime.date(0)
        print(f"{dt.isoformat()}, {txt}")

    def _reset_pos_state(self):
        self.entry_price = None
        self.peak_price = None

    def notify_order(self, order):
        if order.status in (order.Submitted, order.Accepted):
            return
        if order.status == order.Completed:
            if order.isbuy():
                self.entry_price = float(order.executed.price)
                self.peak_price = self.entry_price
                self.log(
                    f"买入 价={order.executed.price:.2f} 费={order.executed.comm:.2f}"
                )
            else:
                self.log(f"卖出 价={order.executed.price:.2f} 费={order.executed.comm:.2f}")
                self._reset_pos_state()
        elif order.status in (order.Canceled, order.Margin, order.Rejected):
            self.log("订单失败/取消")
        self.order = None

    def notify_trade(self, trade):
        if trade.isclosed:
            self.log(f"平仓 毛利={trade.pnl:.2f} 净利={trade.pnlcomm:.2f}")

    def _vol_expand(self, vol: float, vma: float) -> bool:
        return vma > 0 and vol >= vma * self.p.vol_expand_ratio

    def _entry_ok(self, close: float) -> tuple[bool, str]:
        if len(self) < max(3, self.p.vol_ma_period + 1, 20):
            return False, "数据未就绪"

        c0, c1, c2 = close, float(self.data.close[-1]), float(self.data.close[-2])
        v0, v1 = float(self.data.volume[0]), float(self.data.volume[-1])
        vma = float(self.vol_ma[0])

        if not (c0 > c1 > c2):
            return False, f"非两连阳 {c2:.2f}->{c1:.2f}->{c0:.2f}"
        if not self._vol_expand(v0, vma):
            return False, f"第2日未放量 量={v0:.0f}<均量{vma:.0f}"
        if self.p.need_vol_gt_prev and v0 <= v1:
            return False, f"第2日量未大于第1日 {v0:.0f}<={v1:.0f}"
        if self.p.need_above_ma20:
            ma20 = float(self.sma20[0])
            if close <= ma20:
                return False, f"未站上MA20 {close:.2f}<={ma20:.2f}"

        return True, (
            f"两连阳放量 {c2:.2f}->{c1:.2f}->{c0:.2f} "
            f"量={v0:.0f}(昨{v1:.0f}/均{vma:.0f})"
        )

    def _exit_reason(self, close: float) -> str | None:
        """明显转弱才卖；小阴线不卖。"""
        if self.entry_price is None:
            return None

        c1 = float(self.data.close[-1])
        c2 = float(self.data.close[-2])
        v0 = float(self.data.volume[0])
        vma = float(self.vol_ma[0])
        ma20 = float(self.sma20[0])
        day_ret = (close - c1) / c1 if c1 else 0.0
        cost = self.entry_price
        ret = (close - cost) / cost if cost else 0.0

        if self.peak_price is None:
            self.peak_price = close
        else:
            self.peak_price = max(self.peak_price, close)
        dd_peak = (
            (self.peak_price - close) / self.peak_price if self.peak_price else 0.0
        )

        # 5) 硬止损：相对成本
        if ret <= -self.p.hard_stop:
            return f"硬止损 {ret:.1%}(成本{cost:.2f})"

        # 4) 从持仓最高价回撤过大（兜底）
        if dd_peak >= self.p.peak_drawdown:
            return (
                f"高点回撤{dd_peak:.1%}≥{self.p.peak_drawdown:.0%} "
                f"(峰{self.peak_price:.2f})"
            )

        # 1) 跌破 MA20：中期趋势坏了
        if close < ma20:
            return f"跌破MA20 {close:.2f}<{ma20:.2f}"

        # 2) 加速砸盘：大阴 + 已经离开高点一段（排除主升洗盘）
        if (
            day_ret <= -self.p.big_drop
            and dd_peak >= self.p.big_drop_min_peak_dd
            and self._vol_expand(v0, vma)
        ):
            return (
                f"加速砸盘 日跌{day_ret:.1%} 高点已回撤{dd_peak:.1%} "
                f"量={v0:.0f}≥均量{vma:.0f}"
            )

        # 3) 确认两连阴 + 破近5日低点
        if len(self) > self.p.swing_low_period + 1:
            prior_low = float(self.lowest5[-1])
            two_down = close < c1 < c2
            if two_down and self._vol_expand(v0, vma) and close < prior_low:
                return (
                    f"放量两连阴破位 {c2:.2f}->{c1:.2f}->{close:.2f} "
                    f"<近低{prior_low:.2f}"
                )

        return None

    def next(self):
        if self.order:
            return

        close = float(self.data.close[0])

        if not self.position:
            if self.p.trade_start is not None:
                if self.datas[0].datetime.date(0) < self.p.trade_start:
                    return
            ok, why = self._entry_ok(close)
            if ok:
                self.log(f"BUY CREATE {why}")
                self.order = self.buy(size=self.p.stake)
            return

        reason = self._exit_reason(close)
        if reason:
            self.log(f"SELL CREATE {reason} close={close:.2f}")
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
    code: str = "600011",
    start: str = "20250101",
    end: str | None = None,
    cash: float = 430000,
    plot: bool = False,
    warmup_days: int = 100,
    position_pct: float = 0.20,
):
    end = end or datetime.now().strftime("%Y%m%d")
    start_dt = datetime.strptime(start, "%Y%m%d")
    fetch_start = (start_dt - timedelta(days=warmup_days)).strftime("%Y%m%d")
    df = _load_daily(code, fetch_start, end)

    data = bt.feeds.PandasData(dataname=df)
    cerebro = bt.Cerebro()
    cerebro.adddata(data, name=code)
    trade_df = df[df.index >= pd.Timestamp(start_dt)]
    approx_price = float(
        trade_df["close"].iloc[0] if not trade_df.empty else df["close"].iloc[-1]
    )
    stake = max(100, int(cash * position_pct / approx_price / 100) * 100)
    cerebro.addstrategy(
        HotSectorRiskManagedStrategy,
        stake=stake,
        printlog=True,
        trade_start=start_dt.date(),
    )
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="dd")
    cerebro.broker.setcash(cash)
    cerebro.broker.setcommission(commission=0.0005)

    start_val = cerebro.broker.getvalue()
    print(f"起始资金: {start_val:.2f}  标的: {code}  [V5 连涨拿住/明显转弱卖]")
    print(f"回测区间: {start}~{end} (预热从 {fetch_start})")
    print(f"K线根数: {len(df)}  每笔股数: {stake}  参考价: {approx_price:.2f}")
    results = cerebro.run()
    end_val = cerebro.broker.getvalue()
    pnl = end_val - start_val
    print(f"期末资金: {end_val:.2f}")
    print(f"盈亏金额: {pnl:.2f}  ({pnl / start_val * 100:.2f}%)")
    strat = results[0]
    ta = strat.analyzers.trades.get_analysis()
    closed = ta.get("total", {}).get("closed", 0) if isinstance(ta.get("total"), dict) else 0
    dd = strat.analyzers.dd.get_analysis()
    max_dd = dd.get("max", {}).get("drawdown", 0)
    print(f"完成交易笔数: {closed}  最大回撤: {max_dd:.2f}%")
    if plot:
        try:
            cerebro.plot()
        except Exception as e:
            print(f"绘图跳过: {e}")
    return {"start": start_val, "end": end_val, "pnl": pnl, "trades": closed, "max_dd": max_dd}


if __name__ == "__main__":
    print("=" * 60)
    print("【兆易创新 2026年6月1日~7月20日 连续回测】")
    run_backtest(code="603986", start="20260601", end="20260720", plot=False)
