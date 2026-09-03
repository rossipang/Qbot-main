# -*- coding: utf-8 -*-
"""个股行情本地缓存：接口风控时回退到最近一次成功数据。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import pandas as pd

from qbot.gui.config import DATA_DIR_CSV

CACHE_DIR = DATA_DIR_CSV.joinpath("quote_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 缓存最长可用时间（秒）：7 天内的旧 K/资金仍可展示
MAX_AGE_SEC = 7 * 24 * 3600


def _path(code: str, kind: str) -> Path:
    code = "".join(ch for ch in str(code or "") if ch.isdigit())[-6:].zfill(6)
    return CACHE_DIR.joinpath(f"{code}_{kind}.json")


def save_frame(code: str, kind: str, df: pd.DataFrame) -> None:
    if df is None or df.empty:
        return
    try:
        out = df.copy()
        # 时间列统一成字符串，避免 Timestamp JSON 问题
        for col in ("date", "datetime"):
            if col in out.columns:
                out[col] = pd.to_datetime(out[col], errors="coerce").dt.strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                # 日级只留日期
                if col == "date":
                    out[col] = out[col].astype(str).str[:10]
        payload = {
            "saved_at": time.time(),
            "kind": kind,
            "code": str(code),
            "rows": out.to_dict(orient="records"),
        }
        _path(code, kind).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


def load_frame(code: str, kind: str, max_age_sec: float = MAX_AGE_SEC) -> Optional[pd.DataFrame]:
    p = _path(code, kind)
    if not p.exists():
        return None
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
        saved = float(payload.get("saved_at") or 0)
        if saved and (time.time() - saved) > max_age_sec:
            return None
        rows = payload.get("rows") or []
        if not rows:
            return None
        df = pd.DataFrame(rows)
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
        if "datetime" in df.columns:
            df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        df.attrs["source"] = f"本地缓存({kind})"
        df.attrs["cache_age_hours"] = round((time.time() - saved) / 3600.0, 1) if saved else None
        return df
    except Exception:
        return None
