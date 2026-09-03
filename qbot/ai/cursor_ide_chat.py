# -*- coding: utf-8 -*-
"""用本机 Cursor IDE 登录态调用默认模型（无需手填 API Key）。"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import platform
import sqlite3
import struct
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

API2 = "https://api2.cursor.sh"
AUTH_CLIENT_ID = "KbZUR41cY7W6zRSdpSUJ7I7mLYBKOCmB"


class CursorIdeError(RuntimeError):
    pass


def _enc_varint(value: int) -> bytes:
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value & 0x7F)
    return bytes(out)


def _enc_field(field_num: int, wire_type: int, value) -> bytes:
    tag = _enc_varint((field_num << 3) | wire_type)
    if wire_type == 0:
        return tag + _enc_varint(int(value))
    if wire_type == 2:
        if isinstance(value, str):
            value = value.encode("utf-8")
        elif not isinstance(value, (bytes, bytearray)):
            value = bytes(value)
        return tag + _enc_varint(len(value)) + bytes(value)
    raise ValueError("unsupported wire type")


def _dec_varint(data: bytes, pos: int) -> Tuple[int, int]:
    result = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        result |= (b & 0x7F) << shift
        pos += 1
        if not (b & 0x80):
            break
        shift += 7
    return result, pos


def _state_db_path() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Cursor" / "User" / "globalStorage" / "state.vscdb"


def _read_state_values(*keys: str) -> Dict[str, str]:
    db = _state_db_path()
    if not db.is_file():
        raise CursorIdeError("未找到 Cursor 登录数据，请先在 Cursor 中登录账号")
    out: Dict[str, str] = {}
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        cur = conn.cursor()
        for key in keys:
            row = cur.execute(
                "SELECT value FROM ItemTable WHERE key=?", (key,)
            ).fetchone()
            if row and row[0]:
                out[key] = row[0]
    finally:
        conn.close()
    return out


def _detect_client_version() -> str:
    candidates = [
        Path(r"D:\Program Files\cursor\resources\app\product.json"),
        Path(r"C:\Program Files\cursor\resources\app\product.json"),
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Programs"
        / "cursor"
        / "resources"
        / "app"
        / "product.json",
    ]
    for path in candidates:
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                ver = data.get("version")
                if ver:
                    return str(ver)
        except Exception:
            continue
    return "3.2.11"


def _checksum(machine_id: str) -> str:
    timestamp = int(time.time() * 1000 // 1000000)
    byte_array = bytearray(
        [
            (timestamp >> 40) & 255,
            (timestamp >> 32) & 255,
            (timestamp >> 24) & 255,
            (timestamp >> 16) & 255,
            (timestamp >> 8) & 255,
            timestamp & 255,
        ]
    )
    t = 165
    for i in range(len(byte_array)):
        byte_array[i] = ((byte_array[i] ^ t) + (i % 256)) & 255
        t = byte_array[i]
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    encoded = ""
    for i in range(0, len(byte_array), 3):
        a = byte_array[i]
        b = byte_array[i + 1] if i + 1 < len(byte_array) else 0
        c = byte_array[i + 2] if i + 2 < len(byte_array) else 0
        encoded += alphabet[a >> 2]
        encoded += alphabet[((a & 3) << 4) | (b >> 4)]
        if i + 1 < len(byte_array):
            encoded += alphabet[((b & 15) << 2) | (c >> 6)]
        if i + 2 < len(byte_array):
            encoded += alphabet[c & 63]
    return f"{encoded}{machine_id}"


def _jwt_exp(token: str) -> Optional[int]:
    try:
        import base64

        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
        return int(data["exp"])
    except Exception:
        return None


def _refresh_token(refresh_token: str) -> Optional[str]:
    try:
        r = requests.post(
            f"{API2}/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": AUTH_CLIENT_ID,
                "refresh_token": refresh_token,
            },
            timeout=30,
        )
        if r.status_code != 200:
            return None
        return (r.json() or {}).get("access_token")
    except Exception:
        return None


def load_ide_session() -> Dict[str, str]:
    vals = _read_state_values(
        "cursorAuth/accessToken",
        "cursorAuth/refreshToken",
        "cursorAuth/cachedEmail",
        "storage.serviceMachineId",
    )
    token = vals.get("cursorAuth/accessToken") or ""
    refresh = vals.get("cursorAuth/refreshToken") or ""
    if not token:
        raise CursorIdeError("Cursor 未登录，请先打开 Cursor 登录后再试")
    exp = _jwt_exp(token)
    if exp is not None and exp - int(time.time()) < 120 and refresh:
        refreshed = _refresh_token(refresh)
        if refreshed:
            token = refreshed
    machine = vals.get("storage.serviceMachineId") or hashlib.sha256(
        token.encode()
    ).hexdigest()
    return {
        "token": token,
        "email": vals.get("cursorAuth/cachedEmail") or "",
        "machine_id": machine,
        "version": _detect_client_version(),
    }


def _encode_message(content: str, role: int, message_id: str, chat_mode_enum=None) -> bytes:
    msg = b""
    msg += _enc_field(1, 2, content)
    msg += _enc_field(2, 0, role)
    msg += _enc_field(13, 2, message_id)
    if chat_mode_enum is not None:
        msg += _enc_field(47, 0, chat_mode_enum)
    return msg


def _encode_request(messages: List[Dict[str, str]], model_name: str) -> bytes:
    msg = b""
    message_ids = []
    for user_msg in messages:
        role_name = user_msg.get("role", "user")
        role = 1 if role_name == "user" else 2
        msg_id = str(uuid.uuid4())
        chat_mode = 1 if role == 1 else None
        encoded = _encode_message(user_msg.get("content", ""), role, msg_id, chat_mode)
        msg += _enc_field(1, 2, encoded)
        message_ids.append((msg_id, role))

    msg += _enc_field(2, 0, 1)
    msg += _enc_field(3, 2, b"")  # empty instruction
    msg += _enc_field(4, 0, 1)

    model = _enc_field(1, 2, model_name) + _enc_field(4, 2, b"")
    msg += _enc_field(5, 2, model)
    msg += _enc_field(8, 2, "")
    msg += _enc_field(13, 0, 1)

    # cursor setting
    setting = b""
    setting += _enc_field(1, 2, "cursor\\aisettings")
    setting += _enc_field(3, 2, b"")
    unknown6 = _enc_field(1, 2, b"") + _enc_field(2, 2, b"")
    setting += _enc_field(6, 2, unknown6)
    setting += _enc_field(8, 0, 1)
    setting += _enc_field(9, 0, 1)
    msg += _enc_field(15, 2, setting)

    msg += _enc_field(19, 0, 1)
    msg += _enc_field(23, 2, str(uuid.uuid4()))

    meta = b""
    system = platform.system().lower()
    os_name = {"windows": "win32", "darwin": "darwin", "linux": "linux"}.get(
        system, system or "win32"
    )
    arch = platform.machine().lower()
    if arch in ("x86_64", "amd64"):
        arch = "x64"
    elif arch in ("aarch64", "arm64"):
        arch = "arm64"
    meta += _enc_field(1, 2, os_name)
    meta += _enc_field(2, 2, arch)
    meta += _enc_field(3, 2, platform.release() or "unknown")
    meta += _enc_field(4, 2, sys_executable())
    meta += _enc_field(5, 2, datetime.now().isoformat())
    msg += _enc_field(26, 2, meta)

    msg += _enc_field(27, 0, 0)
    for mid, role in message_ids:
        mid_msg = _enc_field(1, 2, mid) + _enc_field(3, 0, role)
        msg += _enc_field(30, 2, mid_msg)

    msg += _enc_field(35, 0, 0)
    msg += _enc_field(38, 0, 0)
    msg += _enc_field(46, 0, 1)
    msg += _enc_field(47, 2, "")
    msg += _enc_field(48, 0, 0)
    msg += _enc_field(49, 0, 0)
    msg += _enc_field(51, 0, 0)
    msg += _enc_field(53, 0, 1)
    msg += _enc_field(54, 2, "Ask")
    return msg


def sys_executable() -> str:
    return os.environ.get("COMSPEC") or "python"


def _build_body(messages: List[Dict[str, str]], model_name: str) -> bytes:
    request = _encode_request(messages, model_name)
    wrapper = _enc_field(1, 2, request)
    magic = 0x00
    if len(messages) >= 3:
        wrapper = gzip.compress(wrapper)
        magic = 0x01
    return bytes([magic]) + struct.pack(">I", len(wrapper)) + wrapper


def _headers(session: Dict[str, str]) -> Dict[str, str]:
    token = session["token"]
    req_id = str(uuid.uuid4())
    return {
        "authorization": f"Bearer {token}",
        "content-type": "application/connect+proto",
        "connect-protocol-version": "1",
        "user-agent": "connect-es/1.6.1",
        "x-amzn-trace-id": f"Root={req_id}",
        "x-client-key": hashlib.sha256(token.encode()).hexdigest(),
        "x-cursor-checksum": _checksum(session["machine_id"]),
        "x-cursor-client-version": session["version"],
        "x-cursor-client-type": "ide",
        "x-cursor-client-os": "win32",
        "x-cursor-client-arch": "x64",
        "x-cursor-client-os-version": platform.release() or "10.0.19045",
        "x-cursor-client-device-type": "desktop",
        "x-cursor-config-version": str(uuid.uuid4()),
        "x-cursor-timezone": "Asia/Shanghai",
        "x-ghost-mode": "false",
        "x-new-onboarding-completed": "true",
        "x-request-id": req_id,
        "x-session-id": str(uuid.uuid5(uuid.NAMESPACE_DNS, token)),
        "host": "api2.cursor.sh",
    }


def _iter_frames(data: bytes):
    i = 0
    while i + 5 <= len(data):
        flag = data[i]
        length = struct.unpack(">I", data[i + 1 : i + 5])[0]
        if length < 0 or i + 5 + length > len(data):
            break
        payload = data[i + 5 : i + 5 + length]
        if flag == 1:
            try:
                payload = gzip.decompress(payload)
            except Exception:
                pass
        yield flag, payload
        i += 5 + length


def _collect_strings(node: bytes, out: List[str], depth: int = 0):
    if depth > 8 or not node:
        return
    pos = 0
    while pos < len(node):
        try:
            tag, pos2 = _dec_varint(node, pos)
        except Exception:
            break
        if pos2 <= pos:
            break
        field_num = tag >> 3
        wire = tag & 7
        pos = pos2
        if wire == 0:
            _, pos = _dec_varint(node, pos)
        elif wire == 1:
            pos += 8
        elif wire == 5:
            pos += 4
        elif wire == 2:
            length, pos = _dec_varint(node, pos)
            value = node[pos : pos + length]
            pos += length
            if field_num in (1, 2, 25) and value:
                try:
                    s = value.decode("utf-8")
                    if s and not s.startswith("{") and "\x00" not in s:
                        # skip pure control / error json-ish
                        if any(ch.isalpha() or "\u4e00" <= ch <= "\u9fff" for ch in s):
                            out.append(s)
                except Exception:
                    pass
            _collect_strings(value, out, depth + 1)
        else:
            break


def _extract_text(raw: bytes) -> str:
    texts: List[str] = []
    for flag, payload in _iter_frames(raw):
        if flag == 2:
            try:
                text = payload.decode("utf-8", errors="ignore").lstrip("\x00\n\r ")
                err = json.loads(text)
                if isinstance(err, dict) and err.get("error"):
                    detail = ""
                    for item in err["error"].get("details") or []:
                        dbg = (item or {}).get("debug") or {}
                        d2 = dbg.get("details") or {}
                        detail = d2.get("detail") or d2.get("title") or detail
                    msg = detail or err["error"].get("message") or str(err["error"])
                    if "no longer supported" in msg.lower() or "update required" in msg.lower():
                        msg = (
                            "Cursor 限制了面板直连聊天。"
                            "将自动在已登录的 Cursor 中打开该问题（使用你的默认模型）。"
                        )
                    raise CursorIdeError(msg)
            except CursorIdeError:
                raise
            except Exception:
                pass
            continue
        _collect_strings(payload, texts)
    cleaned = []
    seen = set()
    for t in texts:
        t = t.strip()
        if not t or t in seen:
            continue
        if "outdated version of Cursor" in t:
            continue
        if t.startswith("ERROR_") or t.startswith("aiserver."):
            continue
        seen.add(t)
        cleaned.append(t)
    if not cleaned:
        return ""
    if all(len(x) < 80 for x in cleaned):
        return "".join(cleaned)
    return cleaned[-1] if len(cleaned) == 1 else "".join(cleaned)


def probe_ide() -> Tuple[bool, str]:
    try:
        session = load_ide_session()
        headers = _headers(session)
        r = requests.post(
            f"{API2}/aiserver.v1.AiService/AvailableModels",
            headers={**headers, "Content-Type": "application/json"},
            data=b"{}",
            timeout=30,
        )
        if r.status_code != 200:
            return False, f"Cursor 会话无效 ({r.status_code})"
        models = (r.json() or {}).get("models") or []
        default = next((m for m in models if m.get("defaultOn")), None)
        name = (
            (default or {}).get("clientDisplayName")
            or (default or {}).get("name")
            or "default"
        )
        email = session.get("email") or "Cursor"
        return True, f"已连接 {email} · {name}"
    except CursorIdeError as e:
        return False, str(e)
    except Exception as e:
        return False, f"连接失败: {e}"


def chat(messages: List[Dict[str, str]], model: str = "default") -> str:
    import httpx

    session = load_ide_session()
    headers = _headers(session)
    body = _build_body(messages, model or "default")
    with httpx.Client(http2=True, timeout=180.0) as client:
        # warm session
        client.post(
            f"{API2}/aiserver.v1.AiService/AvailableModels",
            headers={**headers, "Content-Type": "application/json"},
            content=b"{}",
        )
        r = client.post(
            f"{API2}/aiserver.v1.ChatService/StreamUnifiedChatWithTools",
            headers=headers,
            content=body,
        )
    if r.status_code == 401:
        raise CursorIdeError("Cursor 登录已过期，请重新打开 Cursor 登录")
    if r.status_code == 464:
        raise CursorIdeError("Cursor 接口拒绝连接(464)，请确认已登录且网络正常")
    if r.status_code >= 400:
        raise CursorIdeError(f"HTTP {r.status_code}: {r.text[:300]}")
    text = _extract_text(r.content)
    if not text:
        raise CursorIdeError("模型未返回文本，请稍后重试")
    return text
