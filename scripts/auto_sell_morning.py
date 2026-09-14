# -*- coding: utf-8 -*-
"""
早盘「涨不动 / 高开低走」→ 券商客户端自动卖出（easytrader）。

默认只空跑；确认规则与账户后再加 --live，且配置 dry_run=false。

依赖：
  - 已登录可用的同花顺/华泰/海通等「独立委托」客户端（xiadan.exe）
  - pywinauto + 本仓库自带 easytrader

用法：
  set PYTHONPATH=.
  python -u scripts/auto_sell_morning.py --once
  python -u scripts/auto_sell_morning.py --once --live
  python -u scripts/auto_sell_morning.py
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

EASY_ROOT = ROOT / "qbot" / "engine" / "trade" / "easytrader"
if EASY_ROOT.exists() and str(EASY_ROOT) not in sys.path:
    sys.path.insert(0, str(EASY_ROOT))

# 行情仅卖出评估时需要；--positions 只读挂接不依赖 pandas/numpy
# from qbot.data.intraday import fetch_realtime_quote  # lazy

CFG_LOCAL = ROOT / "qbot" / "gui" / "csv" / "auto_sell_local.json"
CFG_EXAMPLE = ROOT / "qbot" / "gui" / "csv" / "auto_sell.example.json"
STATE_PATH = ROOT / "qbot" / "gui" / "csv" / "auto_sell_state.json"


def _log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _load_cfg() -> Dict[str, Any]:
    path = CFG_LOCAL if CFG_LOCAL.exists() else CFG_EXAMPLE
    raw = json.loads(path.read_text(encoding="utf-8"))
    _log(f"配置: {path.name}")
    return raw


def _load_state() -> Dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: Dict[str, Any]) -> None:
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _parse_hhmm(s: str) -> dtime:
    hh, mm = str(s).strip().split(":")[:2]
    return dtime(int(hh), int(mm))


def _lot_size(code: str) -> int:
    c = str(code).zfill(6)
    return 200 if c.startswith("68") else 100


def _round_lot(shares: int, code: str) -> int:
    lot = _lot_size(code)
    n = int(shares) // lot * lot
    return max(0, n)


def _f(x: Any, default: float = 0.0) -> float:
    try:
        if x is None or x == "":
            return default
        return float(x)
    except Exception:
        return default


def _bind_main_window(user, app) -> None:
    """绑定「网上股票交易系统5.0」主窗，避免 top_window() 落到 IE Hidden。"""
    user._app = app
    user._close_prompt_windows()
    try:
        main = app.window(title_re=r".*网上股票交易系统5\.0.*")
        main.wait("exists enabled visible ready", timeout=8)
        user._main = main
    except Exception:
        user._main = app.top_window()
    user._init_toolbar()


def _connect_trader(cfg: Dict[str, Any]):
    import easytrader
    import pywinauto

    broker = str(cfg.get("broker") or "universal_client")
    acct = dict(cfg.get("account") or {})
    exe_path = str(acct.get("exe_path") or "").strip()
    # connect=已手动扫码/登录后只挂接窗口；password=脚本自动填资金账号密码
    login_mode = str(cfg.get("login_mode") or acct.get("login_mode") or "connect").lower()
    user = easytrader.use(broker)
    if login_mode in ("connect", "qr", "扫码", "attach"):
        last_err: Optional[Exception] = None
        # 1) 配置路径
        if exe_path:
            try:
                user.connect(exe_path=exe_path)
                _bind_main_window(user, user._app)
                _prepare_trader_io(user)
                _log("已按 exe 路径挂接，并绑定交易主窗")
                return user
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                _log(f"按路径连接失败: {exc}，尝试按进程/窗口挂接…")
        # 2) 已运行的 xiadan 进程
        try:
            import psutil  # type: ignore
        except Exception:
            psutil = None
        pids: List[int] = []
        if psutil is not None:
            for p in psutil.process_iter(["pid", "name"]):
                try:
                    if str(p.info.get("name") or "").lower() == "xiadan.exe":
                        pids.append(int(p.info["pid"]))
                except Exception:
                    pass
        else:
            # 无 psutil 时用 pywinauto 枚举
            try:
                from pywinauto import findwindows

                for w in findwindows.find_elements(title_re=".*网上股票交易系统.*"):
                    if getattr(w, "process_id", None):
                        pids.append(int(w.process_id))
            except Exception:
                pass
        for pid in pids:
            try:
                app = pywinauto.Application(backend="win32").connect(process=pid, timeout=8)
                _bind_main_window(user, app)
                _log(f"已挂接 xiadan pid={pid}")
                return user
            except Exception as exc:  # noqa: BLE001
                last_err = exc
        # 3) 窗口标题（路径/PID 因权限读不到进程时仍可用）
        try:
            app = pywinauto.Application(backend="win32").connect(
                title_re=r".*网上股票交易系统5\.0.*", timeout=8
            )
            _bind_main_window(user, app)
            _log("已按窗口标题挂接交易系统")
            return user
        except Exception as exc:  # noqa: BLE001
            last_err = exc
        raise RuntimeError(f"无法挂接已登录客户端: {last_err}")
    user.prepare(
        user=str(acct.get("user") or ""),
        password=str(acct.get("password") or ""),
        exe_path=exe_path,
        comm_password=acct.get("comm_password") or None,
    )
    _bind_main_window(user, user._app)
    return user


def _normalize_position_rows(raw: Any) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if raw is None:
        return rows
    if isinstance(raw, dict):
        # 少数接口返回 dict
        raw = [raw]
    for item in list(raw):
        if not isinstance(item, dict):
            continue
        code = (
            item.get("证券代码")
            or item.get("股票代码")
            or item.get("code")
            or item.get("股票代码 ")
            or ""
        )
        code = str(code).strip().zfill(6)[-6:]
        if not code.isdigit():
            continue
        name = str(item.get("证券名称") or item.get("股票名称") or item.get("name") or "")
        avail = item.get("可用余额")
        if avail is None:
            avail = item.get("可用股份")
        if avail is None:
            avail = item.get("股份余额")
        if avail is None:
            avail = item.get("股票余额")
        if avail is None:
            avail = item.get("enable_amount")
        if avail is None:
            avail = item.get("当前持仓")
        avail_i = int(_f(avail, 0))
        if avail_i <= 0:
            continue
        rows.append({"code": code, "name": name, "avail": avail_i, "raw": item})
    return rows


def _quote_metrics(code: str) -> Dict[str, float]:
    from qbot.data.intraday import fetch_realtime_quote

    q = fetch_realtime_quote(code) or {}
    last = _f(q.get("最新价") or q.get("price") or q.get("f43"))
    pre = _f(q.get("昨收") or q.get("pre_close") or q.get("f60"))
    open_ = _f(q.get("开盘") or q.get("open") or q.get("f46"))
    high = _f(q.get("最高") or q.get("high") or q.get("f44"))
    pct = _f(q.get("涨跌幅") or q.get("pct_chg") or q.get("f170"))
    if pre > 0 and last > 0 and abs(pct) < 1e-9:
        pct = (last / pre - 1.0) * 100.0
    open_pct = ((open_ / pre) - 1.0) * 100.0 if pre > 0 and open_ > 0 else 0.0
    below_open = ((open_ - last) / open_ * 100.0) if open_ > 0 and last > 0 else 0.0
    from_high = ((high - last) / high * 100.0) if high > 0 and last > 0 else 0.0
    return {
        "last": last,
        "pre": pre,
        "open": open_,
        "high": high,
        "pct": pct,
        "open_pct": open_pct,
        "below_open": below_open,
        "from_high": from_high,
    }


def _decide(
    metrics: Dict[str, float], rules: Dict[str, Any], weekday: int
) -> Tuple[bool, str, float]:
    """返回 (是否卖, 原因, 卖出比例)。"""
    weak_lt = _f(rules.get("weak_pct_lt"), 0.5)
    open_ge = _f(rules.get("open_fade_open_pct_ge"), 1.0)
    below_open = _f(rules.get("open_fade_below_open_pct"), 0.3)
    from_high = _f(rules.get("from_high_fade_pct"), 1.5)
    ratio_weak = _f(rules.get("sell_ratio_weak"), 0.5)
    ratio_fade = _f(rules.get("sell_ratio_fade"), 1.0)
    thu_full = bool(rules.get("thursday_force_full", True))

    reasons: List[str] = []
    fade = False
    weak = False

    if metrics["pct"] < weak_lt:
        weak = True
        reasons.append(f"涨不动({metrics['pct']:.2f}%<{weak_lt}%)")
    if metrics["open_pct"] >= open_ge and metrics["below_open"] >= below_open:
        fade = True
        reasons.append(
            f"高开低走(开{metrics['open_pct']:.2f}%/低开盘{metrics['below_open']:.2f}%)"
        )
    if metrics["from_high"] >= from_high and metrics["high"] > 0:
        fade = True
        reasons.append(f"冲高回落(距高{metrics['from_high']:.2f}%)")

    if not reasons:
        return False, "", 0.0

    if fade:
        ratio = ratio_fade
    else:
        ratio = ratio_weak
    # 周四：触发即倾向清可卖
    if weekday == 3 and thu_full:
        ratio = max(ratio, 1.0)
    # 周五弱卖半仓、回落可全清（ratio_fade 已是 1）
    if weekday == 4 and weak and not fade:
        ratio = min(ratio, ratio_weak)

    return True, "+".join(reasons), max(0.0, min(1.0, ratio))


def _already_sold(state: Dict[str, Any], code: str, asof: str) -> bool:
    day = state.get(asof) or {}
    return bool((day.get(code) or {}).get("sold"))


def _mark_sold(
    state: Dict[str, Any], asof: str, code: str, info: Dict[str, Any]
) -> None:
    day = state.setdefault(asof, {})
    day[code] = {"sold": True, **info, "ts": datetime.now().strftime("%H:%M:%S")}
    _save_state(state)


def list_positions(cfg: Dict[str, Any]) -> int:
    """只读持仓，不评估、不委托。"""
    acct = cfg.get("account") or {}
    exe_path = str(acct.get("exe_path") or "").strip()
    login_mode = str(cfg.get("login_mode") or acct.get("login_mode") or "connect").lower()
    if not exe_path:
        _log("account.exe_path 未填")
        return 1
    if login_mode not in ("connect", "qr", "扫码", "attach"):
        if not str(acct.get("user") or "").strip() or not str(acct.get("password") or "").strip():
            _log("password 登录模式需要 account.user/password")
            return 1
    _log(
        f"连接 broker={cfg.get('broker')} mode={login_mode} exe={exe_path} "
        f"（只读持仓，不下单）"
    )
    try:
        trader = _connect_trader(cfg)
        raw = trader.position
        positions = _normalize_position_rows(raw)
    except Exception as exc:  # noqa: BLE001
        _log(f"连接/持仓失败: {exc}")
        _log(
            "扫码用户请：1) 先手动打开 xiadan.exe 并扫码进入「网上股票交易系统5.0」"
            " 2) 保持窗口打开 3) 用【管理员】运行 scripts\\run_auto_sell_py32.bat --positions"
            "（非管理员会挂错窗或读不了左侧菜单/持仓表）"
        )
        return 2

    if not positions:
        _log("持仓列表为空（或字段名未识别）。原始类型=" + str(type(raw)))
        try:
            sample = list(raw)[:2] if raw is not None else []
            _log(f"原始样例(截断): {str(sample)[:500]}")
        except Exception:
            pass
        return 0

    _log(f"读到可卖持仓 {len(positions)} 只：")
    for p in positions:
        _log(f"  {p['code']} {p['name']} 可用={p['avail']}")
    _log("只读完成，未下任何卖单")
    return 0


def run_once(cfg: Dict[str, Any], *, live: bool) -> int:
    now = datetime.now()
    if now.weekday() >= 5:
        _log("周末，跳过")
        return 0

    sched = cfg.get("schedule") or {}
    weekdays = [int(x) for x in (sched.get("weekdays") or [3, 4])]
    if now.weekday() not in weekdays:
        _log(f"今日 weekday={now.weekday()} 不在执行日 {weekdays}，跳过")
        return 0

    t0 = _parse_hhmm(str(sched.get("evaluate_after") or "09:45"))
    t1 = _parse_hhmm(str(sched.get("evaluate_until") or "10:30"))
    if not (t0 <= now.time() <= t1):
        _log(f"不在评估窗 {t0.strftime('%H:%M')}-{t1.strftime('%H:%M')}，跳过")
        return 0

    dry = bool(cfg.get("dry_run", True)) and not live
    if live and bool(cfg.get("dry_run", True)):
        _log("警告: --live 已开，但配置 dry_run=true；仍按空跑。请改 local 配置。")
        dry = True
    if live and not bool(cfg.get("dry_run", True)):
        dry = False

    rules = cfg.get("rules") or {}
    white = [str(x).zfill(6) for x in (cfg.get("codes_whitelist") or []) if str(x).strip()]
    black = set(
        str(x).zfill(6) for x in (cfg.get("codes_blacklist") or []) if str(x).strip()
    )
    min_shares = int(cfg.get("min_sell_shares") or 100)

    trader = None
    positions: List[Dict[str, Any]] = []
    if dry and not CFG_LOCAL.exists():
        _log("空跑且无 auto_sell_local.json：无法读券商持仓，退出")
        return 1

    try:
        trader = _connect_trader(cfg)
        positions = _normalize_position_rows(trader.position)
    except Exception as exc:  # noqa: BLE001
        _log(f"连接/持仓失败: {exc}")
        if dry:
            _log("空跑可先手工确认客户端已登录、exe_path 正确")
        return 2

    if white:
        positions = [p for p in positions if p["code"] in white]
    positions = [p for p in positions if p["code"] not in black]

    if not positions:
        _log("无可处理持仓")
        return 0

    asof = now.strftime("%Y-%m-%d")
    state = _load_state()
    sold_n = 0

    for pos in positions:
        code = pos["code"]
        name = pos["name"]
        avail = int(pos["avail"])
        if _already_sold(state, code, asof):
            _log(f"{code} {name} 今日已处理，跳过")
            continue

        try:
            m = _quote_metrics(code)
        except Exception as exc:  # noqa: BLE001
            _log(f"{code} 行情失败: {exc}")
            continue

        ok, reason, ratio = _decide(m, rules, now.weekday())
        if not ok:
            _log(
                f"{code} {name} 不触发 涨={m['pct']:.2f}% 开={m['open_pct']:.2f}% "
                f"距高={m['from_high']:.2f}%"
            )
            continue

        qty = _round_lot(int(math.floor(avail * ratio)), code)
        if qty < max(min_shares, _lot_size(code)):
            _log(f"{code} {name} 触发[{reason}] 但可卖手数不足 qty={qty} avail={avail}")
            continue

        last = m["last"]
        discount = _f(rules.get("limit_price_discount"), 0.005)
        price = round(last * (1.0 - discount), 2) if last > 0 else 0.0
        use_mkt = bool(rules.get("market_sell"))

        _log(
            f"{'【空跑】' if dry else '【实卖】'}{code} {name} {reason} "
            f"ratio={ratio:.0%} qty={qty} last={last} px={price}"
        )

        if dry:
            _mark_sold(
                state,
                asof,
                code,
                {
                    "dry_run": True,
                    "reason": reason,
                    "qty": qty,
                    "price": price,
                    "pct": m["pct"],
                },
            )
            sold_n += 1
            continue

        assert trader is not None
        try:
            if use_mkt and hasattr(trader, "market_sell"):
                ret = trader.market_sell(code, qty)
            else:
                ret = trader.sell(code, price=price, amount=qty)
            _log(f"委托回报 {code}: {ret}")
            _mark_sold(
                state,
                asof,
                code,
                {
                    "dry_run": False,
                    "reason": reason,
                    "qty": qty,
                    "price": price,
                    "ret": str(ret),
                    "pct": m["pct"],
                },
            )
            sold_n += 1
        except Exception as exc:  # noqa: BLE001
            _log(f"{code} 卖出失败: {exc}")

    _log(f"本轮处理完成，触发 {sold_n} 只")
    return 0


def _prepare_trader_io(trader) -> None:
    """导出持仓用共享目录（避免管理员 TEMP 与同花顺用户 TEMP 不一致）。"""
    from easytrader import grid_strategies

    xls_dir = ROOT / "qbot" / "gui" / "csv" / "ths_xls"
    xls_dir.mkdir(parents=True, exist_ok=True)
    trader.enable_type_keys_for_editor()
    inst = grid_strategies.Xls(tmp_folder=str(xls_dir))
    inst.set_trader(trader)
    trader._grid_strategy_instance = inst


def _fill_edit(trader, control_id: int, text: str) -> str:
    main = trader._main
    ed = main.child_window(control_id=control_id, class_name="Edit")
    ed.set_focus()
    trader.wait(0.05)
    # 清空再键入，比 set_edit_text 更可靠
    ed.type_keys("^a{BACKSPACE}", set_foreground=False)
    trader.wait(0.05)
    ed.type_keys(str(text), with_spaces=True, set_foreground=False)
    trader.wait(0.12)
    try:
        got = ed.window_text()
    except Exception:
        got = ""
    _log(f"  控件{control_id} 写入={text!r} 回读={got!r}")
    return str(got or "")


def _confirm_trade_dialogs(trader) -> Dict[str, Any]:
    """处理委托确认/提示；无真实对话框时返回 failed，避免假 success。"""
    saw_dialog = False
    last: Dict[str, Any] = {"message": "no_dialog"}
    # 最多处理几轮弹窗
    for _ in range(8):
        trader.wait(0.35)
        try:
            if trader._main.wrapper_object() == trader._app.top_window().wrapper_object():
                break
        except Exception:
            break
        saw_dialog = True
        top = trader._app.top_window()
        title = ""
        content = ""
        try:
            title = top.child_window(
                control_id=trader._config.POP_DIALOD_TITLE_CONTROL_ID
            ).window_text()
        except Exception:
            try:
                title = top.window_text()
            except Exception:
                title = ""
        try:
            content = top.Static.window_text()
        except Exception:
            content = ""
        _log(f"  弹窗 title={title!r} content={content!r}")

        # 确认类：Alt+Y
        if any(k in title for k in ("委托确认", "提示信息", "撤单确认")) or (
            "确认" in title and "提示" in title
        ):
            try:
                top.set_focus()
            except Exception:
                pass
            top.type_keys("%Y", set_foreground=False)
            last = {"message": "confirmed", "title": title, "content": content}
            continue

        if "提示" in title or title == "提示":
            if "成功" in content:
                last = {"message": "success", "title": title, "content": content}
                try:
                    top["确定"].click()
                except Exception:
                    top.type_keys("{ENTER}", set_foreground=False)
                break
            # 失败提示
            try:
                top["确定"].click()
            except Exception:
                try:
                    top.type_keys("{ENTER}", set_foreground=False)
                except Exception:
                    top.close()
            return {"message": "trade_error", "title": title, "content": content}

        # 未知弹窗：尝试确定 / 是
        try:
            if top["是(Y)"].exists(timeout=0.2):
                top["是(Y)"].click()
                last = {"message": "clicked_yes", "title": title, "content": content}
                continue
        except Exception:
            pass
        try:
            top["确定"].click()
            last = {"message": "clicked_ok", "title": title, "content": content}
            continue
        except Exception:
            try:
                top.type_keys("%Y", set_foreground=False)
                last = {"message": "alt_y", "title": title, "content": content}
            except Exception:
                last = {"message": "unhandled", "title": title, "content": content}
                break

    if not saw_dialog:
        return {"message": "no_dialog", "hint": "未出现确认框，委托可能未提交"}
    return last


def test_sell_one(
    cfg: Dict[str, Any],
    code: str,
    amount: int,
    *,
    live: bool,
) -> int:
    """强制卖出一只（测试用）。F2+键盘填单，并以持仓回读校验，禁止假 success。"""
    code = str(code).zfill(6)
    if not live:
        _log("拒绝：测试实卖必须加 --live")
        return 1

    rules = cfg.get("rules") or {}
    _log(f"【测试实卖】{code} amount={amount or '全部可卖'}（不改 dry_run 配置）")
    try:
        trader = _connect_trader(cfg)
        _prepare_trader_io(trader)
        positions = _normalize_position_rows(trader.position)
    except Exception as exc:  # noqa: BLE001
        _log(f"连接/持仓失败: {exc}")
        return 2

    pos = next((p for p in positions if p["code"] == code), None)
    if not pos:
        shown = ", ".join(f"{p['code']}:{p['avail']}" for p in positions)
        _log(f"持仓中无 {code}，当前可卖: {shown}")
        return 3

    avail_before = int(pos["avail"])
    qty = _round_lot(amount if amount > 0 else avail_before, code)
    qty = min(qty, avail_before)
    if qty < _lot_size(code):
        _log(f"{code} {pos['name']} 可卖不足 avail={avail_before} qty={qty}")
        return 4

    try:
        from qbot.data.intraday import fetch_realtime_quote

        q = fetch_realtime_quote(code) or {}
        last = _f(q.get("price") or q.get("last"))
    except Exception as exc:  # noqa: BLE001
        _log(f"行情失败: {exc}")
        last = 0.0

    discount = _f(rules.get("limit_price_discount"), 0.005)
    if last <= 0:
        _log("无有效现价，中止以免错价")
        return 5
    # 测试单略加大折扣，提高成交概率
    price = round(last * (1.0 - max(discount, 0.01)), 2)

    _log(f"【实卖】{code} {pos['name']} qty={qty} last={last} px={price}（F2键盘路径）")
    main = trader._main
    try:
        main.set_focus()
    except Exception:
        pass
    trader.wait(0.2)
    # 直接快捷键进卖出，避开左侧树偶发点不中
    main.type_keys("{F2}", set_foreground=True)
    trader.wait(0.6)

    got_code = _fill_edit(trader, trader._config.TRADE_SECURITY_CONTROL_ID, code)
    trader.wait(0.35)
    got_px = _fill_edit(trader, trader._config.TRADE_PRICE_CONTROL_ID, f"{price:.2f}")
    got_qty = _fill_edit(trader, trader._config.TRADE_AMOUNT_CONTROL_ID, str(int(qty)))
    if code not in got_code.replace(" ", ""):
        _log(f"代码未写入成功 code={got_code!r}，中止")
        return 7
    if str(int(qty)) not in got_qty.replace(",", "").replace(" ", ""):
        _log(f"数量未写入成功 qty={got_qty!r}，中止")
        return 7

    # 点卖出/确认按钮
    try:
        main.child_window(
            control_id=trader._config.TRADE_SUBMIT_CONTROL_ID, class_name="Button"
        ).click()
    except Exception as exc:  # noqa: BLE001
        _log(f"点击卖出按钮失败: {exc}，尝试回车")
        main.type_keys("{ENTER}", set_foreground=False)

    ret = _confirm_trade_dialogs(trader)
    _log(f"委托回报: {ret}")

    # 回读持仓校验
    avail_after = avail_before
    try:
        trader.wait(0.8)
        main.type_keys("{F4}", set_foreground=True)
        trader.wait(0.3)
        main.type_keys("{F5}", set_foreground=False)
        trader.wait(0.5)
        positions2 = _normalize_position_rows(trader.position)
        pos2 = next((p for p in positions2 if p["code"] == code), None)
        avail_after = int(pos2["avail"]) if pos2 else 0
        _log(f"持仓回读 {code} 可卖: {avail_before} → {avail_after}")
    except Exception as exc:  # noqa: BLE001
        _log(f"持仓回读失败: {exc}（请人工看当日委托）")

    ok = False
    if isinstance(ret, dict) and (
        "成功" in str(ret.get("content") or "")
        or ret.get("message") in ("success", "confirmed", "clicked_yes")
    ):
        # 仍必须以持仓变化或明确成功文案为准
        if "成功" in str(ret.get("content") or "") or avail_after < avail_before:
            ok = True
    if avail_after < avail_before:
        ok = True

    if not ok:
        _log(
            "【失败】未确认成交/减仓。上次假 success 已修掉。"
            "请看客户端是否弹出委托确认；若无弹窗，多半是未点到卖出页。"
        )
        return 6

    asof = datetime.now().strftime("%Y-%m-%d")
    state = _load_state()
    _mark_sold(
        state,
        asof,
        code,
        {
            "dry_run": False,
            "reason": "test_sell",
            "qty": qty,
            "price": price,
            "ret": str(ret),
            "avail_before": avail_before,
            "avail_after": avail_after,
            "pct": None,
        },
    )
    _log("测试卖出已确认（持仓减少或提示成功），请再对一下当日成交")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="早盘涨不动/高开低走自动卖")
    ap.add_argument("--once", action="store_true", help="只跑一轮")
    ap.add_argument(
        "--positions",
        action="store_true",
        help="只读账户持仓并打印，不卖出、不看时间窗",
    )
    ap.add_argument(
        "--test-sell",
        metavar="CODE",
        default="",
        help="强制卖出指定代码（测试；须配合 --live；不改 dry_run）",
    )
    ap.add_argument(
        "--amount",
        type=int,
        default=0,
        help="配合 --test-sell：卖出股数，0=该票全部可卖",
    )
    ap.add_argument(
        "--live",
        action="store_true",
        help="允许实盘；常规 --once 仍需 dry_run=false；--test-sell 仅需本开关",
    )
    args = ap.parse_args()
    cfg = _load_cfg()
    sched = cfg.get("schedule") or {}
    poll = int(sched.get("poll_seconds") or 20)

    if args.positions:
        return list_positions(cfg)

    if str(args.test_sell or "").strip():
        return test_sell_one(
            cfg,
            str(args.test_sell).strip(),
            int(args.amount or 0),
            live=bool(args.live),
        )

    if args.once:
        return run_once(cfg, live=bool(args.live))

    _log("循环模式启动（Ctrl+C 结束）")
    while True:
        now = datetime.now()
        if now.weekday() < 5:
            t0 = _parse_hhmm(str(sched.get("evaluate_after") or "09:45"))
            t1 = _parse_hhmm(str(sched.get("evaluate_until") or "10:30"))
            if t0 <= now.time() <= t1:
                run_once(cfg, live=bool(args.live))
            else:
                _log("等待评估窗口…")
        time.sleep(max(5, poll))


if __name__ == "__main__":
    raise SystemExit(main())
