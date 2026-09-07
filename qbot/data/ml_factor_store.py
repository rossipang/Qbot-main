# -*- coding: utf-8 -*-
"""前瞻 ML 轻量因子库（SQLite，无服务/无权限）。

路径默认：qbot/gui/csv/ml_factor_store.sqlite
用途：日更攒个股日K+资金流+主题板涨跌；周训读库；顺便查历史。

表：
  stock_day   个股日频（OHLCV + 主力流入）
  theme_day   主题等权日涨跌
  code_theme  代码↔主题
  sync_log    任务日志
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

DEFAULT_DB_PATH = (
    Path(__file__).resolve().parents[1] / "gui" / "csv" / "ml_factor_store.sqlite"
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS stock_day (
  code TEXT NOT NULL,
  date TEXT NOT NULL,
  open REAL,
  high REAL,
  low REAL,
  close REAL,
  volume REAL,
  pct REAL,
  main_net_yi REAL,
  main_pct REAL,
  flow_source TEXT,
  updated_at TEXT,
  PRIMARY KEY (code, date)
);
CREATE INDEX IF NOT EXISTS idx_stock_day_date ON stock_day(date);
CREATE INDEX IF NOT EXISTS idx_stock_day_code ON stock_day(code);

CREATE TABLE IF NOT EXISTS theme_day (
  theme_id TEXT NOT NULL,
  date TEXT NOT NULL,
  board_pct REAL,
  n_members INTEGER,
  updated_at TEXT,
  PRIMARY KEY (theme_id, date)
);
CREATE INDEX IF NOT EXISTS idx_theme_day_date ON theme_day(date);

CREATE TABLE IF NOT EXISTS code_theme (
  code TEXT NOT NULL,
  theme_id TEXT NOT NULL,
  name TEXT,
  PRIMARY KEY (code, theme_id)
);
CREATE INDEX IF NOT EXISTS idx_code_theme_theme ON code_theme(theme_id);

CREATE TABLE IF NOT EXISTS sync_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ran_at TEXT NOT NULL,
  job TEXT NOT NULL,
  ok INTEGER NOT NULL,
  detail TEXT
);
"""


def db_path(path: Optional[Path] = None) -> Path:
    return Path(path) if path else DEFAULT_DB_PATH


@contextmanager
def connect(path: Optional[Path] = None, *, readonly: bool = False):
    p = db_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if readonly and p.exists():
        uri = f"file:{p.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    else:
        conn = sqlite3.connect(str(p), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        if not readonly:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.executescript(_SCHEMA)
        yield conn
        if not readonly:
            conn.commit()
    finally:
        conn.close()


def init_db(path: Optional[Path] = None) -> Path:
    p = db_path(path)
    with connect(p) as conn:
        conn.execute("SELECT 1")
    return p


def log_sync(
    job: str,
    ok: bool,
    detail: Any,
    *,
    path: Optional[Path] = None,
) -> None:
    with connect(path) as conn:
        conn.execute(
            "INSERT INTO sync_log(ran_at, job, ok, detail) VALUES (?,?,?,?)",
            (
                time.strftime("%Y-%m-%d %H:%M:%S"),
                str(job),
                1 if ok else 0,
                json.dumps(detail, ensure_ascii=False, default=str)
                if not isinstance(detail, str)
                else detail,
            ),
        )


def last_sync(job: str, *, path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    with connect(path, readonly=True) as conn:
        row = conn.execute(
            "SELECT ran_at, ok, detail FROM sync_log WHERE job=? ORDER BY id DESC LIMIT 1",
            (job,),
        ).fetchone()
    if not row:
        return None
    detail = row["detail"]
    try:
        detail = json.loads(detail)
    except Exception:
        pass
    return {"ran_at": row["ran_at"], "ok": bool(row["ok"]), "detail": detail}


def sync_code_themes(
    pairs: Sequence[Tuple[str, str, str]],
    *,
    path: Optional[Path] = None,
    replace: bool = True,
) -> int:
    """pairs: (code, theme_id, name)。"""
    with connect(path) as conn:
        if replace:
            conn.execute("DELETE FROM code_theme")
        n = 0
        for code, theme_id, name in pairs:
            c = str(code or "").zfill(6)[-6:]
            tid = str(theme_id or "").strip()
            if len(c) != 6 or not c.isdigit() or not tid:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO code_theme(code, theme_id, name) VALUES (?,?,?)",
                (c, tid, str(name or "")),
            )
            n += 1
        return n


def theme_seed_pairs_from_hints() -> List[Tuple[str, str, str]]:
    from qbot.data.forward_watch import THEME_HINTS

    out: List[Tuple[str, str, str]] = []
    for h in THEME_HINTS or []:
        tid = str(h.get("id") or "")
        for c, n in list(h.get("seed_stocks") or []):
            out.append((str(c), tid, str(n or "")))
    return out


def upsert_stock_days(
    rows: Sequence[Dict[str, Any]],
    *,
    path: Optional[Path] = None,
) -> int:
    if not rows:
        return 0
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with connect(path) as conn:
        n = 0
        for r in rows:
            code = str(r.get("code") or "").zfill(6)[-6:]
            date = str(r.get("date") or "").replace("-", "")[:8]
            if len(code) != 6 or len(date) != 8:
                continue
            # 部分更新：已有 OHLCV 时只补资金，不把 close 写成 NULL
            existing = conn.execute(
                "SELECT close, main_net_yi FROM stock_day WHERE code=? AND date=?",
                (code, date),
            ).fetchone()
            if existing:
                close = r.get("close") if r.get("close") is not None else existing["close"]
                main = (
                    r.get("main_net_yi")
                    if r.get("main_net_yi") is not None
                    else existing["main_net_yi"]
                )
                conn.execute(
                    """
                    UPDATE stock_day SET
                      open=COALESCE(?, open),
                      high=COALESCE(?, high),
                      low=COALESCE(?, low),
                      close=COALESCE(?, close),
                      volume=COALESCE(?, volume),
                      pct=COALESCE(?, pct),
                      main_net_yi=COALESCE(?, main_net_yi),
                      main_pct=COALESCE(?, main_pct),
                      flow_source=COALESCE(?, flow_source),
                      updated_at=?
                    WHERE code=? AND date=?
                    """,
                    (
                        r.get("open"),
                        r.get("high"),
                        r.get("low"),
                        close,
                        r.get("volume"),
                        r.get("pct"),
                        main,
                        r.get("main_pct"),
                        r.get("flow_source"),
                        now,
                        code,
                        date,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO stock_day(
                      code, date, open, high, low, close, volume, pct,
                      main_net_yi, main_pct, flow_source, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        code,
                        date,
                        r.get("open"),
                        r.get("high"),
                        r.get("low"),
                        r.get("close"),
                        r.get("volume"),
                        r.get("pct"),
                        r.get("main_net_yi"),
                        r.get("main_pct"),
                        r.get("flow_source"),
                        now,
                    ),
                )
            n += 1
        return n


def upsert_theme_days(
    rows: Sequence[Dict[str, Any]],
    *,
    path: Optional[Path] = None,
) -> int:
    if not rows:
        return 0
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with connect(path) as conn:
        n = 0
        for r in rows:
            tid = str(r.get("theme_id") or "").strip()
            date = str(r.get("date") or "").replace("-", "")[:8]
            if not tid or len(date) != 8:
                continue
            conn.execute(
                """
                INSERT INTO theme_day(theme_id, date, board_pct, n_members, updated_at)
                VALUES (?,?,?,?,?)
                ON CONFLICT(theme_id, date) DO UPDATE SET
                  board_pct=excluded.board_pct,
                  n_members=excluded.n_members,
                  updated_at=excluded.updated_at
                """,
                (
                    tid,
                    date,
                    r.get("board_pct"),
                    int(r.get("n_members") or 0),
                    now,
                ),
            )
            n += 1
        return n


def recompute_theme_days(
    *,
    path: Optional[Path] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> int:
    """用 code_theme 成员等权 pct 重算 theme_day。"""
    with connect(path) as conn:
        themes = [
            r["theme_id"]
            for r in conn.execute("SELECT DISTINCT theme_id FROM code_theme").fetchall()
        ]
        if not themes:
            return 0
        sql = """
        SELECT ct.theme_id AS theme_id, sd.date AS date,
               AVG(sd.pct) AS board_pct, COUNT(sd.pct) AS n_members
        FROM code_theme ct
        JOIN stock_day sd ON sd.code = ct.code
        WHERE sd.pct IS NOT NULL
        """
        params: List[Any] = []
        if date_from:
            sql += " AND sd.date >= ?"
            params.append(str(date_from).replace("-", "")[:8])
        if date_to:
            sql += " AND sd.date <= ?"
            params.append(str(date_to).replace("-", "")[:8])
        sql += " GROUP BY ct.theme_id, sd.date HAVING COUNT(sd.pct) >= 2"
        rows = conn.execute(sql, params).fetchall()
    payload = [
        {
            "theme_id": r["theme_id"],
            "date": r["date"],
            "board_pct": float(r["board_pct"]),
            "n_members": int(r["n_members"]),
        }
        for r in rows
    ]
    return upsert_theme_days(payload, path=path)


def count_stock_days(*, path: Optional[Path] = None) -> int:
    try:
        with connect(path, readonly=True) as conn:
            return int(conn.execute("SELECT COUNT(*) FROM stock_day").fetchone()[0])
    except Exception:
        return 0


def count_codes_with_flow(*, path: Optional[Path] = None) -> int:
    try:
        with connect(path, readonly=True) as conn:
            return int(
                conn.execute(
                    "SELECT COUNT(DISTINCT code) FROM stock_day WHERE main_net_yi IS NOT NULL"
                ).fetchone()[0]
            )
    except Exception:
        return 0


def stats(*, path: Optional[Path] = None) -> Dict[str, Any]:
    with connect(path, readonly=True) as conn:
        sd = conn.execute(
            "SELECT COUNT(*) n, COUNT(DISTINCT code) codes, MIN(date) d0, MAX(date) d1 "
            "FROM stock_day"
        ).fetchone()
        flow = conn.execute(
            "SELECT COUNT(*) FROM stock_day WHERE main_net_yi IS NOT NULL"
        ).fetchone()[0]
        td = conn.execute(
            "SELECT COUNT(*) n, COUNT(DISTINCT theme_id) themes, MIN(date) d0, MAX(date) d1 "
            "FROM theme_day"
        ).fetchone()
        ct = conn.execute("SELECT COUNT(*) FROM code_theme").fetchone()[0]
    return {
        "db": str(db_path(path)),
        "stock_rows": int(sd["n"] or 0),
        "stock_codes": int(sd["codes"] or 0),
        "stock_date_min": sd["d0"],
        "stock_date_max": sd["d1"],
        "flow_rows": int(flow or 0),
        "theme_rows": int(td["n"] or 0),
        "themes": int(td["themes"] or 0),
        "theme_date_min": td["d0"],
        "theme_date_max": td["d1"],
        "code_theme_links": int(ct or 0),
    }


def query_stock_history(
    code: str,
    *,
    days: int = 30,
    path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    code = str(code or "").zfill(6)[-6:]
    with connect(path, readonly=True) as conn:
        rows = conn.execute(
            """
            SELECT * FROM stock_day WHERE code=?
            ORDER BY date DESC LIMIT ?
            """,
            (code, int(days)),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def query_theme_history(
    theme_id: str,
    *,
    days: int = 30,
    path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    with connect(path, readonly=True) as conn:
        rows = conn.execute(
            """
            SELECT * FROM theme_day WHERE theme_id=?
            ORDER BY date DESC LIMIT ?
            """,
            (str(theme_id), int(days)),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def load_bars_from_store(
    code: str,
    *,
    path: Optional[Path] = None,
    limit: int = 220,
) -> List[Dict[str, Any]]:
    """转成与 _fetch_kline_bars 同形的 bars 列表。"""
    code = str(code or "").zfill(6)[-6:]
    with connect(path, readonly=True) as conn:
        rows = conn.execute(
            """
            SELECT date, open, high, low, close, volume, pct
            FROM stock_day
            WHERE code=? AND close IS NOT NULL
            ORDER BY date DESC LIMIT ?
            """,
            (code, int(limit)),
        ).fetchall()
    bars = []
    for r in reversed(rows):
        bars.append(
            {
                "date": r["date"],
                "open": r["open"],
                "high": r["high"],
                "low": r["low"],
                "close": r["close"],
                "volume": r["volume"],
                "pct": r["pct"],
            }
        )
    return bars


def load_index_close_map(
    index_code: str = "510300",
    *,
    path: Optional[Path] = None,
) -> Dict[str, float]:
    with connect(path, readonly=True) as conn:
        rows = conn.execute(
            "SELECT date, close FROM stock_day WHERE code=? AND close IS NOT NULL",
            (str(index_code).zfill(6)[-6:],),
        ).fetchall()
    return {r["date"]: float(r["close"]) for r in rows if r["close"]}


def primary_theme_for_code(
    code: str, *, path: Optional[Path] = None
) -> Optional[str]:
    code = str(code or "").zfill(6)[-6:]
    with connect(path, readonly=True) as conn:
        row = conn.execute(
            "SELECT theme_id FROM code_theme WHERE code=? ORDER BY theme_id LIMIT 1",
            (code,),
        ).fetchone()
    return str(row["theme_id"]) if row else None


def load_flow_board_panel(
    code: str,
    *,
    path: Optional[Path] = None,
    limit: int = 220,
) -> Dict[str, Dict[str, Any]]:
    """date -> {main_net_yi, pct, board_pct, theme_id} 供训练拼因子。"""
    code = str(code or "").zfill(6)[-6:]
    theme_id = primary_theme_for_code(code, path=path)
    with connect(path, readonly=True) as conn:
        rows = conn.execute(
            """
            SELECT date, pct, main_net_yi FROM stock_day
            WHERE code=? ORDER BY date DESC LIMIT ?
            """,
            (code, int(limit)),
        ).fetchall()
        board_map: Dict[str, float] = {}
        if theme_id:
            brows = conn.execute(
                "SELECT date, board_pct FROM theme_day WHERE theme_id=?",
                (theme_id,),
            ).fetchall()
            board_map = {
                b["date"]: float(b["board_pct"])
                for b in brows
                if b["board_pct"] is not None
            }
    out: Dict[str, Dict[str, Any]] = {}
    # 按日期升序算 rolling flow
    chron = list(reversed(rows))
    flows: List[Optional[float]] = []
    dates: List[str] = []
    for r in chron:
        d = r["date"]
        dates.append(d)
        flows.append(
            float(r["main_net_yi"]) if r["main_net_yi"] is not None else None
        )
        pct = float(r["pct"]) if r["pct"] is not None else None
        bp = board_map.get(d)
        out[d] = {
            "pct": pct,
            "main_net_yi": flows[-1],
            "board_pct": bp,
            "theme_id": theme_id,
            "rs_board": (pct - bp) if pct is not None and bp is not None else None,
        }
    # rolling 3/5
    for i, d in enumerate(dates):
        def _sum_last(k: int) -> Optional[float]:
            chunk = flows[max(0, i - k + 1) : i + 1]
            vals = [x for x in chunk if x is not None]
            if not vals:
                return None
            return float(sum(vals))

        out[d]["flow_3d"] = _sum_last(3)
        out[d]["flow_5d"] = _sum_last(5)
        # board 5d sum of theme daily pct
        if theme_id:
            b5 = []
            for j in range(max(0, i - 4), i + 1):
                bd = dates[j]
                if bd in board_map:
                    b5.append(board_map[bd])
            out[d]["board_pct_5"] = float(sum(b5)) if b5 else None
        else:
            out[d]["board_pct_5"] = None
    return out


def list_store_codes(*, path: Optional[Path] = None, min_bars: int = 65) -> List[str]:
    with connect(path, readonly=True) as conn:
        rows = conn.execute(
            """
            SELECT code FROM stock_day
            WHERE close IS NOT NULL
            GROUP BY code
            HAVING COUNT(*) >= ?
            ORDER BY code
            """,
            (int(min_bars),),
        ).fetchall()
    return [r["code"] for r in rows]
