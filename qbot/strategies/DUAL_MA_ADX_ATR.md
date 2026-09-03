# 双均线 + ADX + ATR（策略 B）

## 白话规则

| 动作 | 条件 |
|------|------|
| **买** | MA20 **上穿** MA60（金叉）且 **ADX ≥ 20**；若区间起点已是多头，允许 **追趋势一次** |
| **卖** | 死叉，**或** 跌破 **ATR 移动止损** |

ATR 止损：入场价 − 2.5×ATR；持仓中止损只上不下；浮盈超过 3×ATR 后收紧为 1.2×ATR。

## 怎么跑

```powershell
cd d:\project\Qbot-main
$env:PYTHONPATH="d:\project\Qbot-main"
python -m qbot.strategies.dual_ma_adx_atr_bt
```

默认对比：兆易、新易盛，区间 2026-06-01～07-20。日志会同时打印 **账户盈亏** 和 **同期买持（单票）**。
