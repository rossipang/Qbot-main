# -*- coding: utf-8 -*-
"""等待 price_watch_local.json 里的 pushplus_token，然后发测试消息。"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from qbot.notify.wechat_push import push_wechat  # noqa: E402

CFG = ROOT / "qbot" / "gui" / "csv" / "price_watch_local.json"


def main() -> int:
    print(f"等待 token：{CFG}", flush=True)
    print("请扫码登录 PushPlus，把 token 写入上述文件并保存。", flush=True)
    deadline = time.time() + 600
    last = None
    while time.time() < deadline:
        try:
            cfg = json.loads(CFG.read_text(encoding="utf-8"))
            tok = str(cfg.get("pushplus_token") or "").strip()
        except Exception as exc:  # noqa: BLE001
            print(f"读配置失败: {exc}", flush=True)
            time.sleep(3)
            continue
        if tok != last:
            last = tok
            print(f"检测到内容变更，长度={len(tok)}", flush=True)
        ok = (
            len(tok) >= 16
            and re.fullmatch(r"[0-9A-Za-z_\-]+", tok) is not None
            and "http" not in tok
            and "填到" not in tok
        )
        if ok:
            print("token 有效，发送测试…", flush=True)
            ch = push_wechat(
                "Qbot盯盘·测试",
                "微信推送正常。\n时间 {}\n将盯：盛美<=308 / 光迅<=181".format(
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                ),
                pushplus_token=tok,
            )
            print(f"发送成功 渠道={ch}", flush=True)
            return 0
        time.sleep(3)
    print("超时：10分钟内未检测到有效 token", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
