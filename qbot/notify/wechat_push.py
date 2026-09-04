# -*- coding: utf-8 -*-
"""到价提醒推送：优先强通知通道（Bark/钉钉/ntfy），微信 PushPlus 作兜底。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests

# 本机代理（如 7897）重启后常挂掉，推送走直连，避免「积极拒绝」
_SESSION = requests.Session()
_SESSION.trust_env = False


def push_alert(
    title: str,
    content: str,
    *,
    pushplus_token: str = "",
    serverchan_sendkey: str = "",
    bark_url: str = "",
    dingtalk_webhook: str = "",
    ntfy_topic: str = "",
    ntfy_server: str = "https://ntfy.sh",
    timeout: int = 15,
) -> str:
    """
    按「息屏可见性」优先尝试：
    1) Bark（iPhone 系统通知）
    2) 钉钉机器人（钉钉通知）
    3) ntfy（安卓/iOS App 系统通知）
    4) PushPlus / Server酱（进微信，常无息屏横幅）

    成功返回渠道名。
    """
    title = (title or "Qbot提醒")[:100]
    content = content or ""
    errors: List[str] = []

    bark = (bark_url or "").strip().rstrip("/")
    if bark:
        try:
            # 支持填 https://api.day.app/设备码 或完整前缀
            url = f"{bark}/{quote(title)}/{quote(content[:500])}"
            # level=timeSensitive / critical 更易亮屏（Bark）
            r = _SESSION.get(
                url,
                params={"level": "timeSensitive", "group": "Qbot盯盘", "sound": "alarm"},
                timeout=timeout,
            )
            data = r.json() if r.content else {}
            if r.status_code == 200 and int(data.get("code") or 0) == 200:
                return "bark"
            errors.append(f"bark:{data or r.text[:120]}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"bark:{exc}")

    ding = (dingtalk_webhook or "").strip()
    if ding:
        try:
            text = f"{title}\n{content}"
            r = _SESSION.post(
                ding,
                json={"msgtype": "text", "text": {"content": text}},
                timeout=timeout,
            )
            data = r.json() if r.content else {}
            if r.status_code == 200 and int(data.get("errcode") or -1) == 0:
                return "dingtalk"
            errors.append(f"dingtalk:{data}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"dingtalk:{exc}")

    topic = (ntfy_topic or "").strip()
    if topic:
        try:
            base = (ntfy_server or "https://ntfy.sh").rstrip("/")
            r = _SESSION.post(
                f"{base}/{topic}",
                data=content.encode("utf-8"),
                headers={
                    "Title": title,
                    "Priority": "high",
                    "Tags": "warning,chart_with_upwards_trend",
                },
                timeout=timeout,
            )
            # ntfy 成功一般 200
            if r.status_code in (200, 201):
                return "ntfy"
            errors.append(f"ntfy:{r.status_code}:{r.text[:120]}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"ntfy:{exc}")

    token = (pushplus_token or "").strip()
    if token:
        try:
            r = _SESSION.post(
                "https://www.pushplus.plus/send",
                json={
                    "token": token,
                    "title": title,
                    "content": content,
                    "template": "txt",
                },
                timeout=timeout,
            )
            data = r.json() if r.content else {}
            if r.status_code == 200 and int(data.get("code") or 0) == 200:
                return "pushplus"
            errors.append(f"pushplus:{data}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"pushplus:{exc}")

    sendkey = (serverchan_sendkey or "").strip()
    if sendkey:
        try:
            url = f"https://sctapi.ftqq.com/{sendkey}.send"
            r = _SESSION.post(
                url,
                data={"title": title, "desp": content},
                timeout=timeout,
            )
            data = r.json() if r.content else {}
            if r.status_code == 200 and int(data.get("code") or -1) == 0:
                return "serverchan"
            errors.append(f"serverchan:{data}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"serverchan:{exc}")

    if not any([bark, ding, topic, token, sendkey]):
        raise RuntimeError(
            "未配置任何推送通道：请配置 bark_url / dingtalk_webhook / ntfy_topic "
            "或 pushplus_token（见 price_watch.example.json）"
        )
    raise RuntimeError("推送失败: " + "; ".join(errors))


def push_wechat(
    title: str,
    content: str,
    *,
    pushplus_token: str = "",
    serverchan_sendkey: str = "",
    timeout: int = 15,
) -> str:
    """兼容旧接口：仅微信通道。"""
    return push_alert(
        title,
        content,
        pushplus_token=pushplus_token,
        serverchan_sendkey=serverchan_sendkey,
        timeout=timeout,
    )


def push_from_cfg(title: str, content: str, cfg: Optional[Dict[str, Any]] = None) -> str:
    cfg = cfg or {}
    return push_alert(
        title,
        content,
        pushplus_token=str(cfg.get("pushplus_token") or ""),
        serverchan_sendkey=str(cfg.get("serverchan_sendkey") or ""),
        bark_url=str(cfg.get("bark_url") or ""),
        dingtalk_webhook=str(cfg.get("dingtalk_webhook") or ""),
        ntfy_topic=str(cfg.get("ntfy_topic") or ""),
        ntfy_server=str(cfg.get("ntfy_server") or "https://ntfy.sh"),
    )
