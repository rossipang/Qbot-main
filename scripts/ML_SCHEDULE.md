# Qbot ML 定时任务与因子库

## 计划（本机 Windows）

| 任务 | 时间 | 脚本 |
|------|------|------|
| 日更因子库 | 工作日 16:05 | `run_ml_daily_refresh.bat` |
| 周训 GBDT | 周五 22:00 | `run_ml_friday_train.bat` |
| 周日补训 | 周日 22:00 | `run_ml_sunday_catchup.bat`（周五已成功周训则跳过） |

安装 / 卸载（自动识别仓库根目录，兼容家里 `D:\project` 与公司路径）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_ml_schedulers.ps1
powershell -ExecutionPolicy Bypass -File scripts\install_ml_schedulers.ps1 -Uninstall
```

`run_ml_*.bat` 用脚本所在目录定位项目根，并用 `_ml_resolve_python.bat` 探测 `Qbot` 环境的 python（anaconda / miniforge）。

任务设置了 `StartWhenAvailable`：若触发时关机，开机后会补跑错过的那一次。

日志：`qbot/gui/csv/ml_schedule.log`  
库：`qbot/gui/csv/ml_factor_store.sqlite`

日更规则：**只写入有用行**（有效 OHLC 收盘；资金须有主力净流入数值，含 0）。拉空换源重试；仍空记 `skip_reasons`，不存空壳垃圾行。

短池硬过滤只保留股价≥800；市值上下限已去掉，中军/流动性交给 ML 分。

## 手动

```text
python scripts/refresh_ml_factor_store.py
python scripts/train_forward_gbdt.py --from-store
python scripts/train_forward_gbdt.py --from-store --catchup-missed-friday
python scripts/query_ml_factor_store.py --code 600487 --days 10
```
