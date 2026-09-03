# -*- coding: utf-8 -*-
"""行业选股数据：热点新闻 / 板块资金与估值 / 成分股 / 选股名单。"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd
import requests


def _call_with_timeout(fn: Callable[[], Any], timeout: float, label: str = "请求") -> Any:
    """对无 timeout 的第三方调用加墙钟超时，避免启动刷新永久卡住。"""
    pool = ThreadPoolExecutor(max_workers=1)
    fut = pool.submit(fn)
    try:
        return fut.result(timeout=timeout)
    except FuturesTimeout as exc:
        fut.cancel()
        raise TimeoutError(f"{label}超时({timeout:.0f}s)") from exc
    finally:
        # 超时后不等待卡住的 worker，否则仍会永久阻塞
        pool.shutdown(wait=False, cancel_futures=True)

WATCHLIST_PATH = (
    Path(__file__).resolve().parents[1] / "gui" / "csv" / "industry_watchlist.json"
)

# 单次前瞻刷新内复用，避免同板块重复分页拉成分
_BOARD_CONSTITUENTS_CACHE: dict = {}
_BOARD_FETCH_MAX_PAGES: Optional[int] = None


def clear_board_constituents_cache() -> None:
    _BOARD_CONSTITUENTS_CACHE.clear()


def stats_from_constituents(cons: Optional[pd.DataFrame]) -> Dict[str, Any]:
    """用成分股重算板块行展示字段，保证与下方列表一致。"""
    empty = {
        "上涨家数": 0,
        "下跌家数": 0,
        "涨跌幅": 0.0,
        "涨跌幅_5日": 0.0,
        "主力净流入_亿": 0.0,
        "主力净流入_5日_亿": 0.0,
        "领涨股": "今日0只",
        "领涨涨跌幅": 0.0,
    }
    if cons is None or cons.empty:
        return dict(empty)
    pcts = pd.to_numeric(cons.get("涨跌幅"), errors="coerce")
    pct5 = pd.to_numeric(cons.get("涨跌幅_5日"), errors="coerce")
    main = pd.to_numeric(cons.get("主力净流入_亿"), errors="coerce")
    main5 = pd.to_numeric(cons.get("主力净流入_5日_亿"), errors="coerce")
    lead_name, lead_pct = "-", 0.0
    if "名称" in cons.columns and pcts.notna().any():
        i = int(pcts.idxmax())
        lead_name = str(cons.loc[i, "名称"] or "-")
        lead_pct = float(pcts.loc[i]) if pd.notna(pcts.loc[i]) else 0.0
    down_n = int((pcts < 0).sum()) if pcts.notna().any() else 0
    return {
        "上涨家数": int(len(cons)),
        "下跌家数": down_n,
        "涨跌幅": float(pcts.mean()) if pcts.notna().any() else 0.0,
        "涨跌幅_5日": float(pct5.mean()) if pct5.notna().any() else 0.0,
        "主力净流入_亿": float(main.sum()) if main.notna().any() else 0.0,
        "主力净流入_5日_亿": float(main5.sum()) if main5.notna().any() else 0.0,
        "领涨股": lead_name,
        "领涨涨跌幅": lead_pct,
    }


def _constituent_cache_keys(board: str) -> List[str]:
    board = str(board or "").strip()
    keys: List[str] = []
    for k in (board, board.upper()):
        if k and k not in keys:
            keys.append(k)
    if _is_sanlianyang_board(board):
        for k in (_SANLIANYANG_BOARD_CODE, _SANLIANYANG_BOARD_NAME):
            if k not in keys:
                keys.append(k)
    elif _is_lianyang_board(board):
        for k in (_LIANYANG_BOARD_CODE, _LIANYANG_BOARD_NAME):
            if k not in keys:
                keys.append(k)
    elif _is_lianban_board(board):
        for k in (_LIANBAN_BOARD_CODE, _LIANBAN_BOARD_NAME):
            if k not in keys:
                keys.append(k)
    return keys


def _pool_cache_get(cache: dict, *, empty_ttl: float = 20.0, full_ttl: float = 90.0) -> Optional[pd.DataFrame]:
    """池缓存：非空 90s；空结果仅短 TTL，过期当未命中（逼三连阳重扫，避免一直 0 只）。"""
    hit = cache.get("df")
    if not isinstance(hit, pd.DataFrame):
        return None
    ts = float(cache.get("ts") or 0.0)
    ttl = full_ttl if not hit.empty else empty_ttl
    if ts <= 0 or (time.time() - ts) >= ttl:
        return None
    return hit.copy()


def _lookup_constituents_cache(board: str) -> Optional[pd.DataFrame]:
    """虚拟板：只返回非空缓存；三连阳空结果永不命中（避免点击秒回 0 只）。"""
    pool_hit: Optional[pd.DataFrame] = None
    if _is_sanlianyang_board(board):
        # 启动扫完后应能管住整个交易日，不能 90 秒就丢
        pool_hit = _pool_cache_get(
            _SANLIANYANG_CACHE, empty_ttl=0.0, full_ttl=6 * 3600.0
        )
        if isinstance(pool_hit, pd.DataFrame) and pool_hit.empty:
            pool_hit = None
    elif _is_lianyang_board(board):
        pool_hit = _pool_cache_get(_LIANYANG_CACHE, empty_ttl=25.0, full_ttl=90.0)
    elif _is_lianban_board(board):
        pool_hit = _pool_cache_get(_LIANBAN_CACHE, empty_ttl=25.0, full_ttl=90.0)

    if isinstance(pool_hit, pd.DataFrame) and not pool_hit.empty:
        return pool_hit

    for k in _constituent_cache_keys(board):
        hit = _BOARD_CONSTITUENTS_CACHE.get(k)
        if isinstance(hit, pd.DataFrame) and not hit.empty:
            return hit.copy()

    # 两连板/连阳首板：空池短 TTL 内可返回；三连阳绝不返回空
    if isinstance(pool_hit, pd.DataFrame) and not _is_sanlianyang_board(board):
        return pool_hit
    return None


def is_virtual_zt_board(board: str) -> bool:
    return _is_virtual_zt_board(board)


def fetch_virtual_board_constituents(board: str, *, force: bool = False) -> pd.DataFrame:
    """两连板 / 连阳首板 / 三连阳：与板块行统计同源。"""
    board = str(board or "").strip()
    if not board:
        return pd.DataFrame()
    if force:
        if _is_sanlianyang_board(board):
            _SANLIANYANG_CACHE.update({"ts": 0.0, "date": "", "df": None})
        elif _is_lianyang_board(board):
            _LIANYANG_CACHE.update({"ts": 0.0, "date": "", "df": None})
        elif _is_lianban_board(board):
            _LIANBAN_CACHE.update({"ts": 0.0, "date": "", "df": None})
        for k in _constituent_cache_keys(board):
            _BOARD_CONSTITUENTS_CACHE.pop(k, None)
    else:
        hit = _lookup_constituents_cache(board)
        if hit is not None:
            return hit
        # 三连阳点选：优先磁盘缓存，禁止无缓存时在点击线程里慢扫
        if _is_sanlianyang_board(board):
            return get_sanlianyang_cached()
    if _is_lianban_board(board):
        df, _dt = fetch_lianban_zt_pool(min_boards=2)
        _pin_virtual_board_cache(_LIANBAN_BOARD_CODE, _LIANBAN_BOARD_NAME, df)
        _LIANBAN_CACHE.update({"ts": time.time(), "date": str(_dt or ""), "df": df})
        return df.copy() if df is not None else pd.DataFrame()
    if _is_lianyang_board(board):
        df, _dt = fetch_lianyang_shouban_pool(min_yang_days=1)
        _pin_virtual_board_cache(_LIANYANG_BOARD_CODE, _LIANYANG_BOARD_NAME, df)
        return df.copy() if df is not None else pd.DataFrame()
    if _is_sanlianyang_board(board):
        # 仅 force / 启动 warm 走实扫
        df, _dt = warm_sanlianyang_cache(force=True)
        return df.copy() if df is not None else pd.DataFrame()
    return pd.DataFrame()


def reconcile_virtual_board_rows(boards: pd.DataFrame) -> pd.DataFrame:
    """刷新全部后：虚拟板统计与成分股缓存对齐，避免上行有数、下行空。"""
    if boards is None or boards.empty:
        return boards
    out = boards.copy()
    pairs = (
        (_SANLIANYANG_BOARD_CODE, _SANLIANYANG_BOARD_NAME),
        (_LIANYANG_BOARD_CODE, _LIANYANG_BOARD_NAME),
        (_LIANBAN_BOARD_CODE, _LIANBAN_BOARD_NAME),
    )
    for code, name in pairs:
        cons = _lookup_constituents_cache(code)
        if cons is None and code == _SANLIANYANG_BOARD_CODE:
            cons = get_sanlianyang_cached()
        if cons is None:
            if code == _SANLIANYANG_BOARD_CODE:
                continue
            cons = pd.DataFrame()
        mask = match_board_key(out, code) | match_board_key(out, name)
        if not mask.any():
            continue
        st = stats_from_constituents(cons)
        idx = out.index[mask][0]
        for col, val in st.items():
            if col in out.columns:
                out.at[idx, col] = val
    return out


def match_board_key(df: pd.DataFrame, board_key: str) -> pd.Series:
    """按板块代码或名称匹配一行（用于回写统计）。"""
    if df is None or df.empty:
        return pd.Series(dtype=bool)
    key = str(board_key or "").strip()
    if not key:
        return pd.Series(False, index=df.index)
    upper = key.upper()
    code = df["板块代码"].astype(str).str.upper() if "板块代码" in df.columns else pd.Series("", index=df.index)
    name = df["板块名称"].astype(str) if "板块名称" in df.columns else pd.Series("", index=df.index)
    return (code == upper) | (name == key)


def set_board_fetch_fast(fast: bool) -> None:
    """前瞻刷新：限制成分股分页深度，避免单板块拉太久。"""
    global _BOARD_FETCH_MAX_PAGES
    _BOARD_FETCH_MAX_PAGES = 4 if fast else None

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    return s


def _get_json(url: str, params: dict, timeout: int = 20) -> dict:
    headers = {"User-Agent": _UA, "Referer": "https://quote.eastmoney.com/"}
    last_err: Optional[Exception] = None
    sess = _session()
    # 多 mirror 兜底
    candidates = [url]
    for m in ("17.push2", "29.push2", "82.push2", "90.push2", "push2"):
        if "push2.eastmoney.com" in url and m + ".eastmoney.com" not in url:
            candidates.append(
                url.replace("push2.eastmoney.com", m + ".eastmoney.com")
                if m != "push2"
                else url
            )
    # 去重保序
    seen = set()
    hosts = []
    for u in candidates:
        if u not in seen:
            seen.add(u)
            hosts.append(u)
    for host in hosts:
        try:
            r = sess.get(host, params=params, headers=headers, timeout=timeout)
            r.raise_for_status()
            return r.json() or {}
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(0.2)
    raise RuntimeError(f"请求失败: {last_err}")


# 政策/重大事件标题特征：国常会核准、万亿投资等突发利好，必须保留进前瞻新闻池
_MAJOR_CATALYST_PATTERNS = (
    "国常会",
    "国务院常务会议",
    "国务院总理",
    "核准",
    "新型电网",
    "六张网",
    "发改委",
    "投资超",
    "拟投资",
    "万亿元",
    "千亿元",
)

# 产业突发催化（科技链）：与政策稿同等优先，避免被财新杂志稿挤掉
_INDUSTRY_CATALYST_PATTERNS = (
    "CPO",
    "光模块",
    "光通信",
    "共封装光学",
    "硅光",
    "HBM",
    "先进封装",
    "液冷服务器",
    "MLCC",
    "人形机器人",
    "可控核聚变",
    "创新药",
    "医保目录",
    "CXO",
)


def news_title_is_industry_catalyst(title: str) -> bool:
    """英伟达CPO量产、光模块等产业催化。"""
    t = str(title or "")
    if not t:
        return False
    tech_giant = any(k in t for k in ("英伟达", "NVIDIA", "英偉達", "辉达", "輝達"))
    if tech_giant and any(
        k in t for k in ("CPO", "光模块", "光通訊", "光通信", "量产", "量產", "硅光")
    ):
        return True
    if "量产" in t or "量產" in t:
        if any(k in t for k in ("CPO", "光模块", "HBM", "先进封装", "共封装")):
            return True
    return any(k in t for k in _INDUSTRY_CATALYST_PATTERNS) and any(
        k in t for k in ("量产", "量產", "突破", "大涨", "涨价", "缺货", "交付", "官宣", "确认")
    )


def news_title_is_major_catalyst(title: str) -> bool:
    """判断标题是否像政策/重大投资/产业突发催化。"""
    t = str(title or "")
    if not t:
        return False
    # 国常会/核准/发改委投资口径，单独即足够
    if any(k in t for k in ("国常会", "国务院常务会议", "核准", "发改委", "新型电网")):
        return True
    # 「投资 + 亿」组合
    if ("投资" in t or "拟投" in t) and ("亿" in t or "万亿" in t):
        return True
    if news_title_is_industry_catalyst(t):
        return True
    return any(k in t for k in _MAJOR_CATALYST_PATTERNS)


def fetch_forward_news(
    finance_limit: int = 30, tech_limit: int = 30, pharma_limit: int = 20, *, fast: bool = False
) -> pd.DataFrame:
    """
    前瞻固定新闻池：财经 + 科技 + 医药（创新药等），各自过滤后合并。
    返回列：time/source/title/url/channel(财经|科技|医药)
    不做「今天谁涨补谁」；由下游再关联概念→行业。
    """
    import json
    import re

    finance_rows: List[dict] = []
    tech_rows: List[dict] = []
    pharma_rows: List[dict] = []

    def _date_from_url(url: str) -> str:
        m = re.search(r"(20\d{2}-\d{2}-\d{2})", str(url or ""))
        return m.group(1) if m else ""

    def _append_eastmoney(bucket: List[dict], columns: str, page_size: int, channel: str) -> None:
        sess = _session()
        r = sess.get(
            "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns",
            params={
                "client": "web",
                "biz": "web_news_col",
                "column": columns,
                "order": "1",
                "needInteractData": "0",
                "page_index": "1",
                "page_size": str(page_size),
                "req_trace": str(int(time.time() * 1000)),
            },
            headers={"User-Agent": _UA, "Referer": "https://finance.eastmoney.com/"},
            timeout=15,
        )
        data = (r.json() or {}).get("data") or {}
        for item in data.get("list") or []:
            t = str(item.get("showTime") or item.get("dateTime") or "")
            if not t:
                t = _date_from_url(str(item.get("url") or ""))
            if len(t) >= 10 and t[4] == "-":
                t = t[:16] if len(t) >= 16 else t[:10]
            bucket.append(
                {
                    "time": t,
                    "source": str(item.get("mediaName") or "东财"),
                    "title": str(item.get("title") or "")[:120],
                    "url": str(item.get("url") or item.get("url_w") or ""),
                    "channel": channel,
                }
            )

    def _append_sina(bucket: List[dict], lid: str, num: int, channel: str) -> None:
        sess = _session()
        r = sess.get(
            "https://feed.mix.sina.com.cn/api/roll/get",
            params={"pageid": "153", "lid": str(lid), "num": str(num), "page": "1"},
            headers={"User-Agent": _UA, "Referer": "https://finance.sina.com.cn/"},
            timeout=12,
        )
        lst = ((r.json() or {}).get("result") or {}).get("data") or []
        for item in lst:
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            ctime = str(item.get("ctime") or item.get("create_time") or "")
            t = ""
            try:
                if ctime.isdigit():
                    t = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ctime)))
                elif len(ctime) >= 10:
                    t = ctime[:16]
            except Exception:
                t = ctime[:16] if ctime else ""
            bucket.append(
                {
                    "time": t,
                    "source": "新浪财经",
                    "title": title[:120],
                    "url": str(item.get("url") or item.get("URL") or ""),
                    "channel": channel,
                }
            )

    def _append_eastmoney_keyword_search(
        bucket: List[dict], keyword: str, page_size: int, channel: str
    ) -> None:
        """东财搜索：专抓创新药/医药类标题（栏目接口没有稳定医药频道）。"""
        sess = _session()
        inner = {
            "uid": "",
            "keyword": keyword,
            "type": ["cmsArticleWebOld"],
            "client": "web",
            "clientType": "web",
            "clientVersion": "curr",
            "param": {
                "cmsArticleWebOld": {
                    "searchScope": "default",
                    "sort": "default",
                    "pageIndex": 1,
                    "pageSize": int(page_size),
                    "preTag": "",
                    "postTag": "",
                }
            },
        }
        cb = f"jQuery_{int(time.time() * 1000)}"
        r = sess.get(
            "https://search-api-web.eastmoney.com/search/jsonp",
            params={
                "cb": cb,
                "param": json.dumps(inner, ensure_ascii=False),
                "_": str(int(time.time() * 1000)),
            },
            headers={
                "User-Agent": _UA,
                "Referer": f"https://so.eastmoney.com/news/s?keyword={keyword}",
            },
            timeout=15,
        )
        text = (r.text or "").strip()
        if text.startswith(cb + "(") and text.endswith(")"):
            text = text[len(cb) + 1 : -1]
        payload = json.loads(text)
        items = (((payload or {}).get("result") or {}).get("cmsArticleWebOld")) or []
        for it in items:
            title = re.sub(r"</?em>", "", str(it.get("title") or "")).strip()
            if not title:
                continue
            t = str(it.get("date") or "")[:16]
            art = str(it.get("code") or "")
            url = str(it.get("url") or "")
            if not url and art:
                url = f"http://finance.eastmoney.com/a/{art}.html"
            bucket.append(
                {
                    "time": t,
                    "source": str(it.get("mediaName") or "东财"),
                    "title": title[:120],
                    "url": url,
                    "channel": channel,
                }
            )

    _FINANCE_KEEP = (
        "股", "A股", "沪深", "证券", "央行", "国务院", "发改委", "国常会",
        "基金", "债券", "银行", "保险", "券商", "IPO", "上市", "财报",
        "营收", "净利润", "亿元", "万亿", "投资", "核电", "电网", "物流",
        "白酒", "消费", "地产", "有色", "煤炭", "石油", "涨停", "跌停",
        # 医药也会出现在财经要闻里
        "医药", "创新药", "医保", "药企", "生物药", "CXO", "中药", "疫苗",
    )
    _TECH_KEEP = (
        "芯片", "半导体", "光模块", "CPO", "光通信", "硅光", "英伟达", "NVIDIA",
        "AI", "人工智能", "算力", "服务器", "液冷", "HBM", "存储", "先进封装",
        "机器人", "具身", "MLCC", "被动元件", "光伏", "新能源", "核电",
        "通信", "5G", "6G", "PCB", "消费电子", "苹果", "华为", "汽车电子",
        "软件", "信创", "数据中心", "交换机", "光芯片", "共封装",
    )
    _PHARMA_KEEP = (
        "医药", "创新药", "医保", "药企", "制药", "生物药", "生物医药",
        "CXO", "临床", "获批", "上市申请", "NDA", "BLA", "ADC", "GLP-1",
        "减肥药", "中药", "疫苗", "医疗器械", "医疗服务", "医院", "药店",
        "药明", "恒瑞", "百济", "康方", "信达", "君实", "凯莱英", "泰格",
        "医保目录", "集采", "国谈", "新药", "抗体", "细胞治疗", "基因治疗",
    )

    def _keep_finance(title: str) -> bool:
        t = str(title or "")
        if not t:
            return False
        if news_title_is_major_catalyst(t):
            return True
        return any(k in t for k in _FINANCE_KEEP)

    def _keep_tech(title: str) -> bool:
        t = str(title or "")
        if not t:
            return False
        if news_title_is_industry_catalyst(t) or news_title_is_major_catalyst(t):
            return True
        return any(k in t for k in _TECH_KEEP)

    def _keep_pharma(title: str) -> bool:
        t = str(title or "")
        if not t:
            return False
        if news_title_is_major_catalyst(t):
            return True
        return any(k in t for k in _PHARMA_KEEP)

    def _finalize(bucket: List[dict], keep_fn, limit: int) -> pd.DataFrame:
        if not bucket:
            return pd.DataFrame(columns=["time", "source", "title", "url", "channel"])
        df = pd.DataFrame(bucket).drop_duplicates(subset=["title"]).reset_index(drop=True)
        df = df[df["title"].map(keep_fn)].copy()
        if df.empty:
            return pd.DataFrame(columns=["time", "source", "title", "url", "channel"])
        df["_ord"] = df["time"].astype(str).str.slice(0, 10)
        df["_major"] = df["title"].map(
            lambda x: 1 if news_title_is_major_catalyst(x) else 0
        )
        df = df.sort_values(["_major", "_ord"], ascending=[False, False]).drop(
            columns=["_ord", "_major"]
        )
        return df.head(limit).reset_index(drop=True)

    # —— 财经通道 ——
    try:
        _append_eastmoney(finance_rows, "350|351|344", 40, "财经")
    except Exception:
        pass
    try:
        _append_eastmoney(finance_rows, "352", 25, "财经")
    except Exception:
        pass
    for lid in ("2511", "2512", "2517"):
        try:
            _append_sina(finance_rows, lid, 25, "财经")
        except Exception:
            pass
    # 财新限额进财经（前瞻 fast 模式跳过，常卡住 15s+）
    if not fast:
        try:
            import akshare as ak

            raw = _call_with_timeout(ak.stock_news_main_cx, 15.0, "财新要闻")
            if raw is not None and not raw.empty:
                kept = 0
                for _, r in raw.iterrows():
                    if kept >= 5:
                        break
                    title = str(r.get("summary") or "")[:120]
                    if not _keep_finance(title):
                        continue
                    tag = str(r.get("tag") or "财新")
                    if any(s in tag for s in ("家庭财富", "商圈", "量化观察", "交易簿")):
                        continue
                    finance_rows.append(
                        {
                            "time": _date_from_url(str(r.get("url") or "")),
                            "source": tag,
                            "title": title,
                            "url": str(r.get("url") or ""),
                            "channel": "财经",
                        }
                    )
                    kept += 1
        except Exception:
            pass

    # —— 科技通道 ——
    try:
        _append_eastmoney(tech_rows, "417", 40, "科技")
    except Exception:
        pass
    for lid in ("2515", "2516", "2518", "2519"):
        try:
            _append_sina(tech_rows, lid, 25, "科技")
        except Exception:
            pass
    # 东财要闻里偏科技的也进科技池（再过滤）
    try:
        _append_eastmoney(tech_rows, "350|351", 30, "科技")
    except Exception:
        pass

    # —— 医药通道（搜索创新药/CXO等，补足财经+科技漏掉的医药催化）——
    pharma_kws = ("创新药", "医药", "CXO") if fast else ("创新药", "医药", "CXO", "医保", "生物医药")
    for kw in pharma_kws:
        try:
            _append_eastmoney_keyword_search(pharma_rows, kw, 12, "医药")
        except Exception:
            pass
    # 财经要闻里医药相关再捞一批进医药池
    try:
        _append_eastmoney(pharma_rows, "350|351|344", 30, "医药")
    except Exception:
        pass

    fin_df = _finalize(finance_rows, _keep_finance, finance_limit)
    tech_df = _finalize(tech_rows, _keep_tech, tech_limit)
    pharma_df = _finalize(pharma_rows, _keep_pharma, pharma_limit)
    frames = [d for d in (fin_df, tech_df, pharma_df) if d is not None and not d.empty]
    if not frames:
        return pd.DataFrame(columns=["time", "source", "title", "url", "channel"])
    out = pd.concat(frames, ignore_index=True)
    out = out.drop_duplicates(subset=["title"]).reset_index(drop=True)
    # 合并后再按催化优先，但保留各通道初筛结果
    out["_major"] = out["title"].map(lambda x: 1 if news_title_is_major_catalyst(x) else 0)
    out["_ord"] = out["time"].astype(str).str.slice(0, 10)
    out = out.sort_values(["_major", "_ord"], ascending=[False, False]).drop(
        columns=["_major", "_ord"]
    )
    return out.reset_index(drop=True)


def fetch_hot_news(limit: int = 40) -> pd.DataFrame:
    """兼容旧接口：多通道新闻合并后截断。"""
    df = fetch_forward_news(finance_limit=30, tech_limit=30, pharma_limit=20)
    if df.empty:
        return pd.DataFrame(columns=["time", "source", "title", "url"])
    return df.head(limit).reset_index(drop=True)


def _clist_pages(
    params: dict,
    page_size: int = 100,
    max_pages: int = 10,
    *,
    page_sleep: float = 0.25,
    request_timeout: int = 20,
) -> list:
    """分页拉取东财 clist，优先 push2delay（本机 push2his/push2 常被掐）。"""
    bases = (
        "https://push2delay.eastmoney.com/api/qt/clist/get",
        "https://push2.eastmoney.com/api/qt/clist/get",
        "https://82.push2.eastmoney.com/api/qt/clist/get",
        "https://17.push2.eastmoney.com/api/qt/clist/get",
        "https://29.push2.eastmoney.com/api/qt/clist/get",
    )
    headers = {"User-Agent": _UA, "Referer": "https://quote.eastmoney.com/"}
    sess = _session()
    last_err: Optional[Exception] = None
    for base in bases:
        try:
            p = dict(params)
            p["pn"] = "1"
            p["pz"] = str(page_size)
            p["_"] = int(time.time() * 1000)
            r = sess.get(base, params=p, headers=headers, timeout=request_timeout)
            r.raise_for_status()
            payload = r.json() or {}
            data = payload.get("data") or {}
            diff = list(data.get("diff") or [])
            total = int(data.get("total") or len(diff) or 0)
            pages = min(max_pages, max(1, (total + page_size - 1) // page_size))
            for page in range(2, pages + 1):
                p["pn"] = str(page)
                p["_"] = int(time.time() * 1000)
                time.sleep(page_sleep)
                r2 = sess.get(base, params=p, headers=headers, timeout=request_timeout)
                r2.raise_for_status()
                more = ((r2.json() or {}).get("data") or {}).get("diff") or []
                diff.extend(more)
            if diff:
                return diff
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            continue
    if last_err:
        raise RuntimeError(str(last_err))
    return []


_BOARD_FIELDS = (
    "f12,f14,f2,f3,f8,f9,f20,f62,f104,f105,f128,f136,f184,f115,f109,f164,f165"
)

# 市场板块：成分股用东财市场过滤器，行情指标用对应指数代理
_MARKET_BOARDS = (
    {
        "板块代码": "MKT_CYB",
        "板块名称": "创业板",
        "类型": "市场",
        "fs": "m:0 t:6",
        "secid": "0.399006",  # 创业板指
    },
    {
        "板块代码": "MKT_KCB",
        "板块名称": "科创板",
        "类型": "市场",
        "fs": "m:1 t:23",
        "secid": "1.000688",  # 科创50
    },
)

# 打板专题：虚拟概念（成分来自东财涨停池连板数）
_LIANBAN_BOARD_CODE = "MKT_2LB"
_LIANBAN_BOARD_NAME = "两连板"
_LIANBAN_ALIASES = frozenset(
    {
        _LIANBAN_BOARD_CODE,
        _LIANBAN_BOARD_NAME,
        "两连板+",
        "昨今两连板",
        "连续涨停",
    }
)

# 连阳首板：今日首板（连板数=1），且前 1～2 日为上涨阳线
_LIANYANG_BOARD_CODE = "MKT_LYSB"
_LIANYANG_BOARD_NAME = "连阳首板"
_LIANYANG_ALIASES = frozenset(
    {
        _LIANYANG_BOARD_CODE,
        _LIANYANG_BOARD_NAME,
        "连阳板",
        "阳线首板",
    }
)
# 三连阳：近 3 个交易日均为上涨阳线，且今日未涨停
_SANLIANYANG_BOARD_CODE = "MKT_3LY"
_SANLIANYANG_BOARD_NAME = "三连阳"
_SANLIANYANG_ALIASES = frozenset(
    {
        _SANLIANYANG_BOARD_CODE,
        _SANLIANYANG_BOARD_NAME,
        "三连阳未涨停",
        "连阳三日",
    }
)
_VIRTUAL_BOARD_CODES = frozenset(
    {_LIANBAN_BOARD_CODE, _LIANYANG_BOARD_CODE, _SANLIANYANG_BOARD_CODE}
)


def _yi(val):
    try:
        return float(val) / 1e8 if val not in (None, "-", "") else None
    except (TypeError, ValueError):
        return None


def _rows_from_board_diff(diff: list, board_type: str) -> list:
    rows = []
    for item in diff:
        rows.append(
            {
                "板块代码": item.get("f12"),
                "板块名称": item.get("f14"),
                "类型": board_type,
                "最新价": item.get("f2"),
                "涨跌幅": item.get("f3"),
                "涨跌幅_5日": item.get("f109"),
                "换手率": item.get("f8"),
                "市盈率": item.get("f9")
                if item.get("f9") not in (None, "-")
                else item.get("f115"),
                "总市值_亿": _yi(item.get("f20")),
                "主力净流入_亿": _yi(item.get("f62")),
                "主力净流入_5日_亿": _yi(item.get("f164")),
                "主力净占比": item.get("f184"),
                "主力净占比_5日": item.get("f165"),
                "上涨家数": item.get("f104"),
                "下跌家数": item.get("f105"),
                "领涨股": item.get("f128"),
                "领涨涨跌幅": item.get("f136"),
            }
        )
    return rows


def _numeric_board_df(df: pd.DataFrame) -> pd.DataFrame:
    for col in [
        "最新价",
        "涨跌幅",
        "涨跌幅_5日",
        "换手率",
        "市盈率",
        "总市值_亿",
        "主力净流入_亿",
        "主力净流入_5日_亿",
        "主力净占比",
        "主力净占比_5日",
        "上涨家数",
        "下跌家数",
        "领涨涨跌幅",
    ]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _fetch_market_board_rows() -> list:
    """创业板 / 科创板：指数涨跌与资金流 + 全市场成分过滤器。"""
    secids = ",".join(m["secid"] for m in _MARKET_BOARDS)
    by_sec = {}
    try:
        sess = _session()
        r = sess.get(
            "https://push2delay.eastmoney.com/api/qt/ulist.np/get",
            params={
                "fltt": "2",
                "invt": "2",
                "fields": "f12,f14,f2,f3,f62,f104,f105,f109,f164,f128,f136",
                "secids": secids,
            },
            headers={"User-Agent": _UA, "Referer": "https://quote.eastmoney.com/"},
            timeout=15,
        )
        r.raise_for_status()
        for item in ((r.json() or {}).get("data") or {}).get("diff") or []:
            by_sec[str(item.get("f12") or "")] = item
    except Exception:
        by_sec = {}

    rows = []
    for m in _MARKET_BOARDS:
        code = m["secid"].split(".", 1)[-1]
        item = by_sec.get(code) or {}
        rows.append(
            {
                "板块代码": m["板块代码"],
                "板块名称": m["板块名称"],
                "类型": "市场",
                "最新价": item.get("f2"),
                "涨跌幅": item.get("f3"),
                "涨跌幅_5日": item.get("f109"),
                "换手率": None,
                "市盈率": None,
                "总市值_亿": None,
                "主力净流入_亿": _yi(item.get("f62")),
                "主力净流入_5日_亿": _yi(item.get("f164")),
                "主力净占比": None,
                "主力净占比_5日": None,
                "上涨家数": item.get("f104"),
                "下跌家数": item.get("f105"),
                "领涨股": item.get("f128") if item.get("f128") not in (None, "-") else "-",
                "领涨涨跌幅": item.get("f136")
                if item.get("f136") not in (None, "-")
                else None,
                "_fs": m["fs"],
            }
        )
    return rows


def _ths_industry_fallback() -> pd.DataFrame:
    import akshare as ak

    ths = ak.stock_fund_flow_industry(symbol="即时")
    ths5 = None
    try:
        ths5 = ak.stock_fund_flow_industry(symbol="5日排行")
    except Exception:
        ths5 = None
    if ths is None or ths.empty:
        raise RuntimeError("同花顺行业资金流为空")
    out = pd.DataFrame(
        {
            "板块名称": ths["行业"],
            "板块代码": "",
            "类型": "行业",
            "涨跌幅": pd.to_numeric(ths["行业-涨跌幅"], errors="coerce"),
            "涨跌幅_5日": pd.NA,
            "主力净流入_亿": pd.to_numeric(ths["净额"], errors="coerce"),
            "主力净流入_5日_亿": pd.NA,
            "换手率": pd.NA,
            "市盈率": pd.NA,
            "总市值_亿": pd.NA,
            "上涨家数": pd.NA,
            "下跌家数": pd.to_numeric(ths["公司家数"], errors="coerce"),
            "领涨股": ths["领涨股"],
            "领涨涨跌幅": pd.to_numeric(ths["领涨股-涨跌幅"], errors="coerce"),
            "主力净占比": pd.NA,
            "主力净占比_5日": pd.NA,
        }
    )
    if ths5 is not None and not ths5.empty and "行业" in ths5.columns:
        cols5 = {"行业": "板块名称"}
        if "净额" in ths5.columns:
            cols5["净额"] = "主力净流入_5日_亿"
        if "行业-涨跌幅" in ths5.columns:
            cols5["行业-涨跌幅"] = "涨跌幅_5日"
        m5 = ths5.rename(columns=cols5)[list(cols5.values())].drop_duplicates("板块名称")
        out = out.drop(columns=["涨跌幅_5日", "主力净流入_5日_亿"], errors="ignore")
        out = out.merge(m5, on="板块名称", how="left")
        for c in ("涨跌幅_5日", "主力净流入_5日_亿"):
            if c in out.columns:
                out[c] = pd.to_numeric(out[c], errors="coerce")
            else:
                out[c] = pd.NA
    out = out.sort_values("主力净流入_亿", ascending=False).reset_index(drop=True)
    out.insert(0, "排名", range(1, len(out) + 1))
    return out


def _is_lianban_board(board: str) -> bool:
    b = str(board or "").strip()
    if not b:
        return False
    if b in _LIANBAN_ALIASES or b.upper() == _LIANBAN_BOARD_CODE:
        return True
    return "两连板" in b


def _is_lianyang_board(board: str) -> bool:
    b = str(board or "").strip()
    if not b:
        return False
    if b in _LIANYANG_ALIASES or b.upper() == _LIANYANG_BOARD_CODE:
        return True
    return "连阳首板" in b or b == "连阳板"


def _is_sanlianyang_board(board: str) -> bool:
    b = str(board or "").strip()
    if not b:
        return False
    if b in _SANLIANYANG_ALIASES or b.upper() == _SANLIANYANG_BOARD_CODE:
        return True
    return "三连阳" in b


def _is_virtual_zt_board(board: str) -> bool:
    return (
        _is_lianban_board(board)
        or _is_lianyang_board(board)
        or _is_sanlianyang_board(board)
    )


def _stock_secid(code: str) -> str:
    """A 股 secid：沪市 1.xxxxxx，深市/创业/北交所等 0.xxxxxx。"""
    c = str(code or "").zfill(6)
    if c.startswith(("5", "6", "9")):
        return f"1.{c}"
    return f"0.{c}"


def _fetch_ulist_quote_chunk(chunk: list, fields: str, urls: tuple) -> dict:
    """拉取 ulist 一批代码的行情。"""
    if not chunk:
        return {}
    secids = ",".join(_stock_secid(c) for c in chunk)
    params = {"fltt": "2", "invt": "2", "fields": fields, "secids": secids}
    diff = []
    for url in urls:
        try:
            data = _get_json(url, params, timeout=12)
            diff = ((data or {}).get("data") or {}).get("diff") or []
            if diff:
                break
        except Exception:
            continue
    out: dict = {}
    for item in diff:
        code = str(item.get("f12") or "").zfill(6)
        if not code or code == "000000":
            continue
        out[code] = {
            "名称": item.get("f14"),
            "最新价": item.get("f2"),
            "涨跌幅": item.get("f3"),
            "涨跌幅_5日": item.get("f109"),
            "开盘": item.get("f17"),
            "最高": item.get("f15"),
            "最低": item.get("f16"),
            "昨收": item.get("f18"),
            "换手率": item.get("f8"),
            "市盈率": item.get("f9"),
            "市净率": item.get("f23"),
            "总市值_亿": _yi(item.get("f20")),
            "主力净流入_亿": _yi(item.get("f62")),
            "主力净流入_5日_亿": _yi(item.get("f164")),
            "量比": item.get("f10"),
            "成交额": item.get("f6"),
        }
    return out


def _fetch_ulist_quote_map(codes: list) -> dict:
    """
    批量拉个股行情（与概念成分同字段）：5日涨跌、市盈率、市净率、主力净流入等。
    返回 {代码: {字段...}}。
    """
    uniq = []
    seen = set()
    for c in codes:
        code = str(c or "").zfill(6)
        if len(code) != 6 or not code.isdigit() or code in seen:
            continue
        seen.add(code)
        uniq.append(code)
    if not uniq:
        return {}

    fields = "f12,f14,f2,f3,f8,f9,f20,f23,f62,f184,f10,f6,f109,f164,f165,f15,f16,f17,f18"
    urls = (
        "https://push2delay.eastmoney.com/api/qt/ulist.np/get",
        "https://push2.eastmoney.com/api/qt/ulist.np/get",
        "https://82.push2.eastmoney.com/api/qt/ulist.np/get",
    )
    out: dict = {}
    # 东财 ulist 一次不宜过多
    chunk_size = 80
    chunks = [uniq[i : i + chunk_size] for i in range(0, len(uniq), chunk_size)]
    if len(chunks) <= 1:
        out.update(_fetch_ulist_quote_chunk(chunks[0] if chunks else [], fields, urls))
    else:
        workers = min(6, len(chunks))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [
                pool.submit(_fetch_ulist_quote_chunk, ch, fields, urls) for ch in chunks
            ]
            for fut in as_completed(futs):
                try:
                    out.update(fut.result())
                except Exception:
                    continue
    return out


def _enrich_constituents_quotes(df: pd.DataFrame) -> pd.DataFrame:
    """用 ulist 行情补齐成分股缺失字段（涨停池本身没有 5日/PE/主力资金）。"""
    if df is None or df.empty or "代码" not in df.columns:
        return df
    qmap = _fetch_ulist_quote_map(df["代码"].tolist())
    if not qmap:
        return df

    qdf = pd.DataFrame.from_dict(qmap, orient="index")
    qdf.index.name = "代码"
    qdf = qdf.reset_index()
    qdf["代码"] = qdf["代码"].astype(str).str.zfill(6)

    out = df.copy()
    out["代码"] = out["代码"].astype(str).str.zfill(6)
    # 涨停池已有基础价量；行情侧补齐估值与资金，并回填空值
    always_from_quote = [
        "涨跌幅_5日",
        "市盈率",
        "市净率",
        "主力净流入_亿",
        "主力净流入_5日_亿",
        "量比",
    ]
    fill_if_empty = ["最新价", "涨跌幅", "换手率", "总市值_亿", "成交额"]

    merged = out.merge(qdf, on="代码", how="left", suffixes=("", "_q"))
    for col in always_from_quote + fill_if_empty:
        qcol = f"{col}_q"
        if qcol not in merged.columns:
            if col not in merged.columns and col in qdf.columns:
                merged[col] = qdf.set_index("代码").reindex(merged["代码"])[col].values
            continue
        if col not in merged.columns:
            merged[col] = merged[qcol]
        elif col in always_from_quote:
            merged[col] = merged[qcol].combine_first(merged[col])
        else:
            merged[col] = merged[col].combine_first(merged[qcol])
        merged = merged.drop(columns=[qcol])

    drop_q = [c for c in merged.columns if c.endswith("_q")]
    if drop_q:
        merged = merged.drop(columns=drop_q)

    for col in always_from_quote + fill_if_empty:
        if col in merged.columns:
            merged[col] = pd.to_numeric(merged[col], errors="coerce")
    return merged

def _zt_pool_trade_dates(max_back: int = 10) -> list:
    """
    最近可取涨停池的交易日（YYYYMMDD）。
    开盘前（09:25 前）不用「今天」——接口常仍回昨日池，却被当成今日导致连阳错判。
    跳过周末：周末调涨停池接口有时仍返回数据，但日K没有当日K，三连阳会全部判空。
    """
    from datetime import datetime, timedelta, time as dtime

    now = datetime.now()
    start = now.date()
    if now.time() < dtime(9, 25):
        start = start - timedelta(days=1)
    out = []
    # 多扫几天，保证跳过周末后仍够用
    for i in range(max_back + 8):
        d = start - timedelta(days=i)
        if d.weekday() >= 5:  # 六日
            continue
        out.append(d.strftime("%Y%m%d"))
        if len(out) >= max_back + 1:
            break
    return out


def _fetch_zt_pool_em_df(trade_date: str) -> pd.DataFrame:
    """
    东财涨停池（requests，不依赖 akshare/aiohttp）。
    列名对齐旧 ak.stock_zt_pool_em：代码/名称/最新价/涨跌幅/换手率/连板数/…
    """
    date = str(trade_date or "").replace("-", "")[:8]
    if len(date) != 8:
        return pd.DataFrame()
    url = "https://push2ex.eastmoney.com/getTopicZTPool"
    sess = _session()
    headers = {
        "User-Agent": _UA,
        "Referer": "https://quote.eastmoney.com/ztb/detail",
    }
    rows: List[dict] = []
    page = 0
    total = None
    while page < 20:
        params = {
            "ut": "7eea3edcaed734bea9cbfc24409ed989",
            "dpt": "wz.ztzt",
            "Pageindex": page,
            "pagesize": 100,
            "sort": "fbt:asc",
            "date": date,
        }
        try:
            r = sess.get(url, params=params, headers=headers, timeout=20)
            r.raise_for_status()
            payload = r.json() or {}
        except Exception:
            break
        data = payload.get("data") or {}
        pool = data.get("pool") or []
        if total is None:
            try:
                total = int(data.get("tc") or 0)
            except (TypeError, ValueError):
                total = 0
        if not pool:
            break
        for it in pool:
            if not isinstance(it, dict):
                continue
            code = str(it.get("c") or "").zfill(6)
            if not code.isdigit():
                continue
            # 东财价格单位为「分」
            try:
                px = float(it.get("p") or 0) / 100.0
            except (TypeError, ValueError):
                px = None
            mcap = pd.to_numeric(it.get("tshare"), errors="coerce")
            circ = pd.to_numeric(it.get("ltsz"), errors="coerce")
            seal = pd.to_numeric(it.get("fund"), errors="coerce")
            rows.append(
                {
                    "代码": code,
                    "名称": it.get("n"),
                    "最新价": px,
                    "涨跌幅": pd.to_numeric(it.get("zdp"), errors="coerce"),
                    "换手率": pd.to_numeric(it.get("hs"), errors="coerce"),
                    "连板数": pd.to_numeric(it.get("lbc"), errors="coerce"),
                    "炸板次数": pd.to_numeric(it.get("zbc"), errors="coerce"),
                    "封板资金": seal,
                    "成交额": pd.to_numeric(it.get("amount"), errors="coerce"),
                    "总市值": mcap,
                    "流通市值": circ,
                    "所属行业": it.get("hybk") or it.get("hy"),
                    "首次封板时间": it.get("fbt"),
                    "最后封板时间": it.get("lbt"),
                    "涨停统计": it.get("zttj"),
                }
            )
        if total and len(rows) >= total:
            break
        if len(pool) < 100:
            break
        page += 1
        time.sleep(0.05)
    return pd.DataFrame(rows)


def _snap_kline_trade_date(prefer_yyyymmdd: str = "") -> str:
    """
    将 prefer 对齐到「日K实际有数据的最近交易日」。
    节假日/接口用非交易日仍返回涨停池时，避免三连阳末根对不上。
    """
    from datetime import datetime

    candidates = []
    pref = str(prefer_yyyymmdd or "").replace("-", "")[:8]
    if pref:
        candidates.append(pref)
    for d in _zt_pool_trade_dates(max_back=12):
        if d not in candidates:
            candidates.append(d)
    # 用流动性好的标的探测最后一根日K日期
    for probe in ("000001", "600519", "000858"):
        for d in candidates:
            bars = _fetch_kline_bars(probe, end_yyyymmdd=d, limit=3)
            if not bars:
                continue
            last = str(bars[-1].get("date") or "")
            if last:
                return last
    return pref or (_zt_pool_trade_dates()[0] if _zt_pool_trade_dates() else "")


def fetch_lianban_zt_pool(min_boards: int = 2) -> Tuple[pd.DataFrame, str]:
    """
    东财涨停池中「连板数 >= min_boards」——即至少昨今连续涨停。
    返回 (成分表, 所用日期YYYYMMDD)。
    """
    last_err: Optional[Exception] = None
    for date in _zt_pool_trade_dates():
        try:
            raw = _fetch_zt_pool_em_df(date)
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            continue
        if raw is None or raw.empty:
            continue
        df = raw.copy()
        if "连板数" not in df.columns:
            last_err = RuntimeError("涨停池无连板数字段")
            continue
        df["连板数"] = pd.to_numeric(df["连板数"], errors="coerce")
        df = df[df["连板数"] >= int(min_boards)].copy()
        if df.empty:
            # 当天有涨停池但没有两连板，也算有效交易日
            return pd.DataFrame(), date

        rows = []
        for _, r in df.iterrows():
            code = str(r.get("代码") or "").zfill(6)
            mcap = pd.to_numeric(r.get("总市值"), errors="coerce")
            circ = pd.to_numeric(r.get("流通市值"), errors="coerce")
            seal = pd.to_numeric(r.get("封板资金"), errors="coerce")
            rows.append(
                {
                    "代码": code,
                    "名称": r.get("名称"),
                    "最新价": pd.to_numeric(r.get("最新价"), errors="coerce"),
                    "涨跌幅": pd.to_numeric(r.get("涨跌幅"), errors="coerce"),
                    "涨跌幅_5日": pd.NA,
                    "换手率": pd.to_numeric(r.get("换手率"), errors="coerce"),
                    "市盈率": pd.NA,
                    "市净率": pd.NA,
                    "总市值_亿": (float(mcap) / 1e8) if pd.notna(mcap) else None,
                    "流通市值_亿": (float(circ) / 1e8) if pd.notna(circ) else None,
                    "主力净流入_亿": pd.NA,
                    "主力净流入_5日_亿": pd.NA,
                    "量比": pd.NA,
                    "成交额": pd.to_numeric(r.get("成交额"), errors="coerce"),
                    "连板数": int(r["连板数"]) if pd.notna(r["连板数"]) else None,
                    "连阳天数": pd.NA,
                    "炸板次数": pd.to_numeric(r.get("炸板次数"), errors="coerce"),
                    "封板资金_亿": (float(seal) / 1e8) if pd.notna(seal) else None,
                    "涨停统计": r.get("涨停统计"),
                    "所属行业": r.get("所属行业"),
                    "首次封板时间": r.get("首次封板时间"),
                    "最后封板时间": r.get("最后封板时间"),
                }
            )
        out = pd.DataFrame(rows)
        out = _enrich_constituents_quotes(out)
        out = out.sort_values(
            ["连板数", "涨跌幅"], ascending=[False, False], na_position="last"
        ).reset_index(drop=True)
        out.insert(0, "序号", range(1, len(out) + 1))
        return out, date

    raise RuntimeError(f"两连板涨停池获取失败: {last_err or '近几日无数据'}")


def _fetch_kline_bars_tencent(code: str, end_yyyymmdd: str, limit: int = 8) -> list:
    """腾讯日K兜底（东财 kline 失败时用）。返回含 pct 的升序 bars。"""
    code = str(code or "").zfill(6)
    if len(code) != 6:
        return []
    end = str(end_yyyymmdd or "").replace("-", "")[:8]
    prefix = "sh" if code.startswith(("5", "6", "9")) else "sz"
    # 多取一根用于算首根涨跌幅
    need = max(int(limit or 8) + 2, 10)
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    params = {"param": f"{prefix}{code},day,,,{need},qfq"}
    try:
        r = _session().get(
            url,
            params=params,
            headers={"User-Agent": _UA, "Referer": "https://finance.qq.com/"},
            timeout=12,
        )
        r.raise_for_status()
        block = ((r.json() or {}).get("data") or {}).get(f"{prefix}{code}") or {}
        rows = block.get("qfqday") or block.get("day") or []
    except Exception:
        return []
    parsed = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 3:
            continue
        try:
            d = str(row[0]).replace("-", "")[:8]
            o, c = float(row[1]), float(row[2])
            hi = float(row[3]) if len(row) > 3 else max(o, c)
            lo = float(row[4]) if len(row) > 4 else min(o, c)
            vol = float(row[5]) if len(row) > 5 else None
        except (TypeError, ValueError, IndexError):
            continue
        if end and d > end:
            continue
        parsed.append({"date": d, "open": o, "close": c, "high": hi, "low": lo, "volume": vol})
    bars = []
    for i, b in enumerate(parsed):
        pct = 0.0
        if i > 0 and parsed[i - 1]["close"]:
            pct = (b["close"] / parsed[i - 1]["close"] - 1.0) * 100.0
        bars.append({**b, "pct": pct})
    if len(bars) > int(limit or 8):
        bars = bars[-int(limit or 8) :]
    return bars


def _fetch_kline_bars_fast(code: str, end_yyyymmdd: str, limit: int = 28) -> list:
    """前瞻风险预拉：短超时、不兜底腾讯/新浪，避免单票拖死整批。"""
    code = str(code or "").zfill(6)
    if len(code) != 6:
        return []
    sess = _session()
    urls = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        "https://push2delay.eastmoney.com/api/qt/stock/kline/get",
    )
    params = {
        "secid": _stock_secid(code),
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101",
        "fqt": "1",
        "end": end_yyyymmdd,
        "lmt": str(limit),
    }
    headers = {"User-Agent": _UA, "Referer": "https://quote.eastmoney.com/"}
    for url in urls:
        try:
            r = sess.get(url, params=params, headers=headers, timeout=5)
            r.raise_for_status()
            raw = ((r.json() or {}).get("data") or {}).get("klines") or []
            if not raw:
                continue
            bars = []
            for x in raw:
                p = str(x).split(",")
                if len(p) < 9:
                    continue
                try:
                    bars.append(
                        {
                            "date": p[0].replace("-", "")[:8],
                            "open": float(p[1]),
                            "close": float(p[2]),
                            "high": float(p[3]),
                            "low": float(p[4]),
                            "volume": float(p[5]) if p[5] not in ("", "-", "None") else None,
                            "pct": float(p[8]),
                        }
                    )
                except (TypeError, ValueError, IndexError):
                    continue
            if bars:
                return bars
        except Exception:
            continue
    return []


def _fetch_kline_bars_once(code: str, end_yyyymmdd: str, limit: int = 8) -> list:
    """单次拉日K（东财→腾讯→新浪），供 _fetch_kline_bars 重试。"""
    code = str(code or "").zfill(6)
    if len(code) != 6:
        return []
    sess = _session()
    urls = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        "https://push2delay.eastmoney.com/api/qt/stock/kline/get",
        "https://82.push2.eastmoney.com/api/qt/stock/kline/get",
    )
    params = {
        "secid": _stock_secid(code),
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101",
        "fqt": "1",
        "end": end_yyyymmdd,
        "lmt": str(limit),
    }
    headers = {"User-Agent": _UA, "Referer": "https://quote.eastmoney.com/"}
    raw = []
    for url in urls:
        try:
            r = sess.get(url, params=params, headers=headers, timeout=8)
            r.raise_for_status()
            raw = ((r.json() or {}).get("data") or {}).get("klines") or []
            if raw:
                break
        except Exception:
            continue
    bars = []
    for x in raw:
        p = str(x).split(",")
        if len(p) < 9:
            continue
        try:
            o = float(p[1])
            c = float(p[2])
            hi = float(p[3])
            lo = float(p[4])
            vol = float(p[5]) if p[5] not in ("", "-", "None") else None
            bars.append(
                {
                    "date": p[0].replace("-", "")[:8],
                    "open": o,
                    "close": c,
                    "high": hi,
                    "low": lo,
                    "volume": vol,
                    "pct": float(p[8]),
                }
            )
        except (TypeError, ValueError, IndexError):
            continue
    if bars:
        return bars
    bars = _fetch_kline_bars_tencent(code, end_yyyymmdd, limit=limit)
    if bars:
        return bars
    try:
        from qbot.data.eastmoney_quote import fetch_kline_sina

        end = str(end_yyyymmdd or "").replace("-", "")[:8]
        begin_dt = pd.Timestamp(f"{end[:4]}-{end[4:6]}-{end[6:8]}") - pd.Timedelta(
            days=max(int(limit or 8) * 3, 40)
        )
        begin = begin_dt.strftime("%Y%m%d")
        df = fetch_kline_sina(code=code, begin=begin, end=end, period="日线", adjust="前复权")
        out = []
        for _, r in df.iterrows():
            d = str(r.get("date") or "").replace("-", "")[:8]
            if not d:
                continue
            try:
                o = float(r["open"])
                c = float(r["close"])
                hi = float(r["high"]) if pd.notna(r.get("high")) else max(o, c)
                lo = float(r["low"]) if pd.notna(r.get("low")) else min(o, c)
                vol = float(r["volume"]) if pd.notna(r.get("volume")) else None
                pct = float(r["pct_chg"]) if pd.notna(r.get("pct_chg")) else 0.0
            except (TypeError, ValueError, KeyError):
                continue
            out.append(
                {
                    "date": d,
                    "open": o,
                    "close": c,
                    "high": hi,
                    "low": lo,
                    "volume": vol,
                    "pct": pct,
                }
            )
        if len(out) > int(limit or 8):
            out = out[-int(limit or 8) :]
        return out
    except Exception:
        return []


def _fetch_kline_bars(code: str, end_yyyymmdd: str, limit: int = 8) -> list:
    """
    取截止 end_yyyymmdd 的日K（含当日）。
    返回 [{date, open, close, high, low, volume, pct}, ...] 升序；失败返回 []。
    """
    for attempt in range(3):
        bars = _fetch_kline_bars_once(code, end_yyyymmdd, limit=limit)
        if bars:
            return bars
        if attempt < 2:
            time.sleep(0.15 * (attempt + 1))
    return []


def _is_up_yang_bar(bar: dict) -> bool:
    """上涨阳线：收盘>开盘 且 涨跌幅>0。"""
    try:
        o = float(bar.get("open"))
        c = float(bar.get("close"))
        pct = float(bar.get("pct"))
    except (TypeError, ValueError):
        return False
    return c > o and pct > 0


def _limit_up_threshold_pct(code: str, name: str) -> float:
    """按板块估算涨停阈值（略低于理论值，避免浮点误伤）。"""
    n = str(name or "").upper()
    c = str(code or "").zfill(6)
    if "ST" in n:
        return 4.9
    if c.startswith(("300", "301", "688")):
        return 19.9
    if c.startswith(("8", "4")):
        return 29.9
    return 9.9


def _is_limit_up_quote(code: str, name: str, pct) -> bool:
    try:
        p = float(pct)
    except (TypeError, ValueError):
        return False
    return p >= _limit_up_threshold_pct(code, name)


def _trailing_yang_days(bars: list, end_date: str) -> int:
    """
    截止 end_date 的连续上涨阳线天数。
    末根须为 end_date；若 end_date 为周末则回退到最近一根日K（避免周末刷不出）。
    交易日缺当日K则返回 0，避免把昨日连阳误当成今日。
    """
    from datetime import datetime

    if not bars:
        return 0
    ed = str(end_date or "").replace("-", "")[:8]
    use = [b for b in bars if str(b.get("date") or "") <= ed]
    if not use:
        return 0
    last_d = str(use[-1].get("date") or "")
    if last_d != ed:
        try:
            if datetime.strptime(ed, "%Y%m%d").weekday() < 5:
                return 0
        except ValueError:
            return 0
    n = 0
    for b in reversed(use):
        if _is_up_yang_bar(b):
            n += 1
        else:
            break
    return n


def _lianyang_days_before_limit(bars: list, limit_date: str) -> int:
    """
    连阳首板判定（只看涨停日前两根）：
    - 前一天(T-1)必须是上涨阳线，否则返回 0（即使 T-2 是阳也不算）
    - T-1、T-2 都阳 → 返回 2
    - 仅 T-1 阳 → 返回 1
    - T-3 及更早不判断
    """
    if not bars:
        return 0
    ld = str(limit_date or "").replace("-", "")[:8]
    prior = [b for b in bars if str(b.get("date") or "") < ld]
    if not prior:
        return 0
    # 前一天必须阳
    if not _is_up_yang_bar(prior[-1]):
        return 0
    if len(prior) >= 2 and _is_up_yang_bar(prior[-2]):
        return 2
    return 1


_SANLIANYANG_CACHE: dict = {"ts": 0.0, "date": "", "df": None}
_LIANYANG_CACHE: dict = {"ts": 0.0, "date": "", "df": None}
_LIANBAN_CACHE: dict = {"ts": 0.0, "date": "", "df": None}


def fetch_lianyang_shouban_pool(min_yang_days: int = 1) -> Tuple[pd.DataFrame, str]:
    """
    连阳首板：涨停池连板数=1，且涨停日前一天必须为上涨阳线；
    前两天都阳亦可。禁止「前一天阴、前两天阳」。
    返回 (成分表, 所用日期YYYYMMDD)。
    """
    now = time.time()
    cached = _LIANYANG_CACHE.get("df")
    cache_ts = float(_LIANYANG_CACHE.get("ts") or 0)
    cache_date = str(_LIANYANG_CACHE.get("date") or "")
    if isinstance(cached, pd.DataFrame) and cache_date:
        ttl = 90 if not cached.empty else 25
        if now - cache_ts < ttl:
            return cached.copy(), cache_date

    last_err: Optional[Exception] = None
    for date in _zt_pool_trade_dates():
        try:
            raw = _fetch_zt_pool_em_df(date)
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            continue
        if raw is None or raw.empty:
            continue
        df = raw.copy()
        if "连板数" not in df.columns:
            last_err = RuntimeError("涨停池无连板数字段")
            continue
        df["连板数"] = pd.to_numeric(df["连板数"], errors="coerce")
        df = df[df["连板数"] == 1].copy()
        if df.empty:
            empty = pd.DataFrame()
            _LIANYANG_CACHE.update({"ts": time.time(), "date": date, "df": empty})
            return empty, date

        trade_date = _snap_kline_trade_date(date)
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _one(row):
            code = str(row.get("代码") or "").zfill(6)
            bars = _fetch_kline_bars(code, end_yyyymmdd=trade_date, limit=8)
            yang_n = _lianyang_days_before_limit(bars, trade_date)
            return code, row, yang_n

        checked = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            futs = [pool.submit(_one, r) for _, r in df.iterrows()]
            for fut in as_completed(futs):
                try:
                    checked.append(fut.result())
                except Exception:
                    continue

        rows = []
        for code, r, yang_n in checked:
            # 前一天必须阳 → yang_n 至少为 1
            if yang_n < int(min_yang_days):
                continue
            mcap = pd.to_numeric(r.get("总市值"), errors="coerce")
            circ = pd.to_numeric(r.get("流通市值"), errors="coerce")
            seal = pd.to_numeric(r.get("封板资金"), errors="coerce")
            rows.append(
                {
                    "代码": code,
                    "名称": r.get("名称"),
                    "最新价": pd.to_numeric(r.get("最新价"), errors="coerce"),
                    "涨跌幅": pd.to_numeric(r.get("涨跌幅"), errors="coerce"),
                    "涨跌幅_5日": pd.NA,
                    "换手率": pd.to_numeric(r.get("换手率"), errors="coerce"),
                    "市盈率": pd.NA,
                    "市净率": pd.NA,
                    "总市值_亿": (float(mcap) / 1e8) if pd.notna(mcap) else None,
                    "流通市值_亿": (float(circ) / 1e8) if pd.notna(circ) else None,
                    "主力净流入_亿": pd.NA,
                    "主力净流入_5日_亿": pd.NA,
                    "量比": pd.NA,
                    "成交额": pd.to_numeric(r.get("成交额"), errors="coerce"),
                    "连板数": 1,
                    "连阳天数": int(yang_n),
                    "炸板次数": pd.to_numeric(r.get("炸板次数"), errors="coerce"),
                    "封板资金_亿": (float(seal) / 1e8) if pd.notna(seal) else None,
                    "涨停统计": r.get("涨停统计"),
                    "所属行业": r.get("所属行业"),
                    "首次封板时间": r.get("首次封板时间"),
                    "最后封板时间": r.get("最后封板时间"),
                }
            )

        out = pd.DataFrame(rows)
        if out.empty:
            _LIANYANG_CACHE.update({"ts": time.time(), "date": date, "df": out})
            return out, date
        out = _enrich_constituents_quotes(out)
        out = out.sort_values(
            ["连阳天数", "涨跌幅"], ascending=[False, False], na_position="last"
        ).reset_index(drop=True)
        out.insert(0, "序号", range(1, len(out) + 1))
        _LIANYANG_CACHE.update({"ts": time.time(), "date": date, "df": out})
        return out, date

    raise RuntimeError(f"连阳首板获取失败: {last_err or '近几日无数据'}")


def _cache_sanlianyang(df: pd.DataFrame, trade_date: str) -> None:
    """只缓存非空三连阳；空结果不落盘缓存，避免点击秒回 0 只。"""
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        _SANLIANYANG_CACHE.update({"ts": 0.0, "date": str(trade_date or ""), "df": None})
        for k in (
            _SANLIANYANG_BOARD_CODE,
            _SANLIANYANG_BOARD_NAME,
            str(_SANLIANYANG_BOARD_CODE).upper(),
        ):
            _BOARD_CONSTITUENTS_CACHE.pop(k, None)
        return
    _SANLIANYANG_CACHE.update(
        {"ts": time.time(), "date": str(trade_date or ""), "df": df.copy()}
    )


def invalidate_virtual_board_caches() -> None:
    """强制重扫虚拟板时清缓存。"""
    _SANLIANYANG_CACHE.update({"ts": 0.0, "date": "", "df": None})
    _LIANYANG_CACHE.update({"ts": 0.0, "date": "", "df": None})
    _LIANBAN_CACHE.update({"ts": 0.0, "date": "", "df": None})
    _BOARD_CONSTITUENTS_CACHE.clear()


def _pin_virtual_board_cache(
    board_code: str, board_name: str, df: Optional[pd.DataFrame]
) -> None:
    """板块扫描结果写入成分股缓存，点选时秒出、与上行统计一致。"""
    out = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
    # 三连阳禁止 pin 空表，否则点选会秒结束且下面 0 只
    if str(board_code).upper() == _SANLIANYANG_BOARD_CODE and out.empty:
        for k in (board_code, board_name, str(board_code or "").upper()):
            key = str(k or "").strip()
            if key:
                _BOARD_CONSTITUENTS_CACHE.pop(key, None)
        return
    for k in (board_code, board_name, str(board_code or "").upper()):
        key = str(k or "").strip()
        if key:
            _BOARD_CONSTITUENTS_CACHE[key] = out.copy()


def _fetch_rising_non_limit_candidates(max_pages: int = 25) -> pd.DataFrame:
    """
    全 A 上涨且未涨停候选（多翻页直到涨跌幅<=0）。
    注意：不能只取前几页大涨票——中国船舶这类今日 +0.x% 的慢连阳会落在很后面。
    """
    params = {
        "po": "1",
        "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2",
        "invt": "2",
        "fid": "f3",
        "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
        "fields": "f12,f14,f2,f3,f8,f9,f20,f23,f62,f184,f10,f6,f109,f164,f165",
    }
    diff: list = []
    last_exc: Optional[Exception] = None
    for attempt in range(3):
        try:
            diff = _clist_pages(params, page_size=100, max_pages=max_pages)
            if diff:
                break
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(0.25 * (attempt + 1))
    if not diff:
        if last_exc:
            raise RuntimeError(f"三连阳候选涨幅榜失败: {last_exc}") from last_exc
        return pd.DataFrame()
    rows = []
    for item in diff:
        code = str(item.get("f12") or "").zfill(6)
        if len(code) != 6 or not code.isdigit():
            continue
        name = item.get("f14")
        pct = item.get("f3")
        try:
            pct_f = float(pct)
        except (TypeError, ValueError):
            continue
        if pct_f <= 0:
            # 已按涨跌幅降序，其后多为平/跌
            break
        if _is_limit_up_quote(code, name, pct_f):
            continue
        pct5 = item.get("f109")
        try:
            pct5_f = float(pct5) if pct5 not in (None, "-", "") else None
        except (TypeError, ValueError):
            pct5_f = None
        # 5 日大跌的跳过；微跌仍可能近端连阳，留给 K 线判定
        if pct5_f is not None and pct5_f < -3:
            continue
        rows.append(
            {
                "代码": code,
                "名称": name,
                "最新价": item.get("f2"),
                "涨跌幅": pct_f,
                "涨跌幅_5日": pct5_f,
                "换手率": item.get("f8"),
                "市盈率": item.get("f9"),
                "市净率": item.get("f23"),
                "总市值_亿": _yi(item.get("f20")),
                "流通市值_亿": pd.NA,
                "主力净流入_亿": _yi(item.get("f62")),
                "主力净流入_5日_亿": _yi(item.get("f164")),
                "量比": item.get("f10"),
                "成交额": item.get("f6"),
            }
        )
    return pd.DataFrame(rows)


def _select_sanlianyang_scan_targets(
    cand: pd.DataFrame, *, limit: int = 360
) -> pd.DataFrame:
    """
    扫描子集：热涨 + 「微涨慢牛」核心带。
    核心带：今日约 0.4%～2%、5 日约 2%～10%——中国船舶（今 +0.7%、已连阳）必须能进。
    该带内优先大市值，避免小票比值把中军挤掉。
    """
    if cand is None or cand.empty:
        return pd.DataFrame()
    c = cand.copy()
    c["代码"] = c["代码"].astype(str).str.zfill(6)
    c["涨跌幅"] = pd.to_numeric(c["涨跌幅"], errors="coerce")
    c["涨跌幅_5日"] = pd.to_numeric(c["涨跌幅_5日"], errors="coerce")
    c["总市值_亿"] = pd.to_numeric(c.get("总市值_亿"), errors="coerce")
    c = c[c["涨跌幅"].fillna(0) > 0]
    if c.empty:
        return c

    hot = c.nlargest(min(80, len(c)), "涨跌幅")
    core = c[
        (c["涨跌幅"] >= 0.4)
        & (c["涨跌幅"] <= 2.0)
        & (c["涨跌幅_5日"].fillna(0) >= 2.0)
        & (c["涨跌幅_5日"].fillna(0) <= 10.0)
    ].copy()
    # 大市值慢牛（船舶/银行/汽车中军）
    core_big = core.nlargest(min(200, len(core)), "总市值_亿")
    # 再补一批高「5日/今日」比的中小票
    core_r = core.copy()
    core_r["_r"] = core_r["涨跌幅_5日"] / core_r["涨跌幅"].clip(lower=0.2)
    core_r = core_r.nlargest(min(120, len(core_r)), "_r")
    mid = c[(c["涨跌幅"] > 2.0) & (c["涨跌幅"] < 5.0)].nlargest(
        min(60, len(c)), "涨跌幅_5日"
    )
    out = pd.concat(
        [core_big, core_r, mid, hot], ignore_index=True
    ).drop_duplicates(subset=["代码"])
    out = out.drop(columns=["_r"], errors="ignore")
    if len(out) > int(limit):
        out = out.head(int(limit))
    return out.reset_index(drop=True)


def _fetch_kline_bars_for_sanlianyang(code: str, end_yyyymmdd: str, limit: int = 8) -> list:
    """三连阳专用：优先腾讯日K，避开东财板块刷新后的限流。"""
    bars = _fetch_kline_bars_tencent(code, end_yyyymmdd, limit=limit)
    if bars:
        return bars
    return _fetch_kline_bars(code, end_yyyymmdd, limit=limit)


def _sanlianyang_disk_path() -> Path:
    return Path(__file__).resolve().parents[1] / "gui" / "csv" / "sanlianyang_cache.json"


def _persist_sanlianyang_last(df: pd.DataFrame, trade_date: str) -> None:
    """落盘完整三连阳成分（启动扫描结果），点选直接读。"""
    try:
        path = _sanlianyang_disk_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        records = []
        if df is not None and not df.empty:
            rec_df = df.copy()
            # JSON 友好
            for col in rec_df.columns:
                if str(rec_df[col].dtype) == "object":
                    continue
                rec_df[col] = pd.to_numeric(rec_df[col], errors="coerce")
            records = json.loads(
                rec_df.to_json(orient="records", force_ascii=False, date_format="iso")
            )
        payload = {
            "trade_date": str(trade_date or ""),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "count": len(records),
            "rows": records,
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # 兼容旧摘要文件
        legacy = path.with_name("sanlianyang_last.json")
        legacy.write_text(
            json.dumps(
                {
                    "trade_date": payload["trade_date"],
                    "updated_at": payload["updated_at"],
                    "count": payload["count"],
                    "codes": [
                        {
                            "代码": r.get("代码"),
                            "名称": r.get("名称"),
                            "涨跌幅": r.get("涨跌幅"),
                            "连阳天数": r.get("连阳天数"),
                        }
                        for r in records[:200]
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def _load_sanlianyang_disk() -> Tuple[Optional[pd.DataFrame], str]:
    """读启动时落盘的三连阳；日期需与当前交易日对齐。"""
    try:
        path = _sanlianyang_disk_path()
        if not path.is_file():
            return None, ""
        payload = json.loads(path.read_text(encoding="utf-8"))
        trade_date = str(payload.get("trade_date") or "")
        want = ""
        try:
            want = _snap_kline_trade_date(_zt_pool_trade_dates()[0])
        except Exception:
            want = time.strftime("%Y%m%d")
        if trade_date and want and trade_date != want:
            return None, trade_date
        rows = payload.get("rows") or []
        if not rows:
            return None, trade_date
        df = pd.DataFrame(rows)
        if df.empty:
            return None, trade_date
        return df, trade_date
    except Exception:
        return None, ""


def warm_sanlianyang_cache(*, force: bool = False) -> Tuple[pd.DataFrame, str]:
    """
    启动/全量刷新：扫完三连阳并写入内存+磁盘。
    点选只读缓存，不再现扫。
    """
    if not force:
        hit = _pool_cache_get(_SANLIANYANG_CACHE, empty_ttl=0.0, full_ttl=6 * 3600.0)
        if isinstance(hit, pd.DataFrame) and not hit.empty:
            return hit.copy(), str(_SANLIANYANG_CACHE.get("date") or "")
        disk_df, disk_date = _load_sanlianyang_disk()
        if isinstance(disk_df, pd.DataFrame) and not disk_df.empty:
            _cache_sanlianyang(disk_df, disk_date)
            _pin_virtual_board_cache(
                _SANLIANYANG_BOARD_CODE, _SANLIANYANG_BOARD_NAME, disk_df
            )
            return disk_df.copy(), disk_date
    if force:
        _SANLIANYANG_CACHE.update({"ts": 0.0, "date": "", "df": None})
    df, date = fetch_sanlianyang_pool(min_yang_days=3)
    if df is not None and not df.empty:
        _pin_virtual_board_cache(_SANLIANYANG_BOARD_CODE, _SANLIANYANG_BOARD_NAME, df)
    return (df.copy() if df is not None else pd.DataFrame()), str(date or "")


def get_sanlianyang_cached() -> pd.DataFrame:
    """点选用：内存 → 磁盘，不触发实扫。"""
    hit = _lookup_constituents_cache(_SANLIANYANG_BOARD_CODE)
    if isinstance(hit, pd.DataFrame) and not hit.empty:
        return hit
    disk_df, disk_date = _load_sanlianyang_disk()
    if isinstance(disk_df, pd.DataFrame) and not disk_df.empty:
        _cache_sanlianyang(disk_df, disk_date)
        _pin_virtual_board_cache(
            _SANLIANYANG_BOARD_CODE, _SANLIANYANG_BOARD_NAME, disk_df
        )
        return disk_df.copy()
    return pd.DataFrame()


def fetch_sanlianyang_pool(min_yang_days: int = 3) -> Tuple[pd.DataFrame, str]:
    """
    三连阳：近 min_yang_days（默认 3）个交易日均为上涨阳线，且今日未涨停。
    候选覆盖「大涨票 + 慢牛微涨」，再逐只验 K（腾讯日K优先）。
    返回 (成分表, 所用日期YYYYMMDD)。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    cached = _pool_cache_get(_SANLIANYANG_CACHE, empty_ttl=0.0, full_ttl=90.0)
    cache_date = str(_SANLIANYANG_CACHE.get("date") or "")
    # 只吃非空缓存；空/None 一律重扫
    if isinstance(cached, pd.DataFrame) and not cached.empty and cache_date:
        if "连阳天数" in cached.columns:
            out = cached[
                pd.to_numeric(cached["连阳天数"], errors="coerce") >= int(min_yang_days)
            ].copy()
        else:
            out = cached.copy()
        return out.reset_index(drop=True), cache_date

    prev_good = _SANLIANYANG_CACHE.get("df")
    prev_date = str(_SANLIANYANG_CACHE.get("date") or "")
    if not (isinstance(prev_good, pd.DataFrame) and not prev_good.empty):
        prev_good = None

    # 先对齐到有日K的交易日（周末涨停池接口仍可能返回数据，但不能当 K 线末日）
    trade_date = _snap_kline_trade_date(_zt_pool_trade_dates()[0])
    zt_codes: set = set()
    try:
        for date in _zt_pool_trade_dates():
            try:
                raw = _fetch_zt_pool_em_df(date)
            except Exception:
                continue
            if raw is None or raw.empty:
                continue
            trade_date = _snap_kline_trade_date(date)
            if "代码" in raw.columns:
                zt_codes = {str(c).zfill(6) for c in raw["代码"].tolist()}
            break
    except Exception:
        pass

    try:
        # 多翻页：把 +0.5%~+2% 的慢连阳也纳入候选
        cand = _fetch_rising_non_limit_candidates(max_pages=25)
    except Exception:
        if prev_good is not None:
            return prev_good.copy(), prev_date or trade_date
        raise

    if cand is None or cand.empty:
        if prev_good is not None:
            return prev_good.copy(), prev_date or trade_date
        empty = pd.DataFrame()
        _cache_sanlianyang(empty, trade_date)
        return empty, trade_date

    if zt_codes:
        cand = cand[~cand["代码"].astype(str).str.zfill(6).isin(zt_codes)].copy()
    keep = []
    for _, r in cand.iterrows():
        code = str(r.get("代码") or "").zfill(6)
        if _is_limit_up_quote(code, r.get("名称"), r.get("涨跌幅")):
            continue
        keep.append(r)
    if not keep:
        if prev_good is not None:
            return prev_good.copy(), prev_date or trade_date
        empty = pd.DataFrame()
        _cache_sanlianyang(empty, trade_date)
        return empty, trade_date
    cand = _select_sanlianyang_scan_targets(pd.DataFrame(keep), limit=300)

    def _scan(workers: int) -> Tuple[list, int, int]:
        def _one(row):
            code = str(row.get("代码") or "").zfill(6)
            bars = _fetch_kline_bars_for_sanlianyang(code, trade_date, limit=8)
            return code, row, _trailing_yang_days(bars, trade_date), bool(bars)

        checked = []
        with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
            futs = [pool.submit(_one, r) for _, r in cand.iterrows()]
            for fut in as_completed(futs):
                try:
                    checked.append(fut.result())
                except Exception:
                    continue
        rows = []
        bars_ok = 0
        for code, r, yang_n, has_bars in checked:
            if has_bars:
                bars_ok += 1
            if yang_n < int(min_yang_days):
                continue
            rows.append(
                {
                    "代码": code,
                    "名称": r.get("名称"),
                    "最新价": pd.to_numeric(r.get("最新价"), errors="coerce"),
                    "涨跌幅": pd.to_numeric(r.get("涨跌幅"), errors="coerce"),
                    "涨跌幅_5日": pd.to_numeric(r.get("涨跌幅_5日"), errors="coerce"),
                    "换手率": pd.to_numeric(r.get("换手率"), errors="coerce"),
                    "市盈率": pd.to_numeric(r.get("市盈率"), errors="coerce"),
                    "市净率": pd.to_numeric(r.get("市净率"), errors="coerce"),
                    "总市值_亿": pd.to_numeric(r.get("总市值_亿"), errors="coerce"),
                    "流通市值_亿": pd.NA,
                    "主力净流入_亿": pd.to_numeric(r.get("主力净流入_亿"), errors="coerce"),
                    "主力净流入_5日_亿": pd.to_numeric(
                        r.get("主力净流入_5日_亿"), errors="coerce"
                    ),
                    "量比": pd.to_numeric(r.get("量比"), errors="coerce"),
                    "成交额": pd.to_numeric(r.get("成交额"), errors="coerce"),
                    "连板数": pd.NA,
                    "连阳天数": int(yang_n),
                    "炸板次数": pd.NA,
                    "封板资金_亿": pd.NA,
                    "涨停统计": pd.NA,
                    "所属行业": pd.NA,
                    "首次封板时间": pd.NA,
                    "最后封板时间": pd.NA,
                }
            )
        return rows, bars_ok, len(checked)

    rows, bars_ok, n_checked = _scan(workers=6)
    if not rows and n_checked and bars_ok / max(n_checked, 1) < 0.55:
        time.sleep(0.4)
        rows, bars_ok, n_checked = _scan(workers=2)

    if not rows:
        if n_checked and bars_ok / max(n_checked, 1) < 0.55 and prev_good is not None:
            return prev_good.copy(), prev_date or trade_date
        empty = pd.DataFrame()
        _cache_sanlianyang(empty, trade_date)
        return empty, trade_date

    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values(
            ["连阳天数", "涨跌幅"], ascending=[False, False], na_position="last"
        ).reset_index(drop=True)
        out.insert(0, "序号", range(1, len(out) + 1))
    _cache_sanlianyang(out, trade_date)
    _persist_sanlianyang_last(out, trade_date)
    return out, trade_date


def _virtual_zt_board_summary_row(
    cons: pd.DataFrame,
    trade_date: str,
    *,
    board_code: str,
    board_name: str,
) -> dict:
    """把涨停专题池汇总成板块列表里的一行。"""
    n = len(cons) if cons is not None and not cons.empty else 0
    pcts = (
        pd.to_numeric(cons["涨跌幅"], errors="coerce")
        if n and "涨跌幅" in cons.columns
        else pd.Series(dtype=float)
    )
    pct5 = (
        pd.to_numeric(cons["涨跌幅_5日"], errors="coerce")
        if n and "涨跌幅_5日" in cons.columns
        else pd.Series(dtype=float)
    )
    main = (
        pd.to_numeric(cons["主力净流入_亿"], errors="coerce")
        if n and "主力净流入_亿" in cons.columns
        else pd.Series(dtype=float)
    )
    main5 = (
        pd.to_numeric(cons["主力净流入_5日_亿"], errors="coerce")
        if n and "主力净流入_5日_亿" in cons.columns
        else pd.Series(dtype=float)
    )
    pe = (
        pd.to_numeric(cons["市盈率"], errors="coerce")
        if n and "市盈率" in cons.columns
        else pd.Series(dtype=float)
    )
    turnover = (
        pd.to_numeric(cons["换手率"], errors="coerce")
        if n and "换手率" in cons.columns
        else pd.Series(dtype=float)
    )
    mcap = (
        pd.to_numeric(cons["总市值_亿"], errors="coerce")
        if n and "总市值_亿" in cons.columns
        else pd.Series(dtype=float)
    )
    lead_name, lead_pct = "-", None
    if n and "名称" in cons.columns and not pcts.empty and pcts.notna().any():
        i = int(pcts.idxmax())
        lead_name = str(cons.loc[i, "名称"] or "-")
        lead_pct = float(pcts.loc[i]) if pd.notna(pcts.loc[i]) else None
    avg_pct = float(pcts.mean()) if n and pcts.notna().any() else 0.0
    avg_pct5 = float(pct5.mean()) if n and pct5.notna().any() else 0.0
    sum_main = float(main.sum()) if n and main.notna().any() else None
    sum_main5 = float(main5.sum()) if n and main5.notna().any() else None
    avg_pe = float(pe.mean()) if n and pe.notna().any() else None
    avg_turn = float(turnover.mean()) if n and turnover.notna().any() else None
    sum_mcap = float(mcap.sum()) if n and mcap.notna().any() else None
    date_label = (
        f"{trade_date[4:6]}-{trade_date[6:8]}" if len(trade_date) == 8 else trade_date
    )
    return {
        "板块代码": board_code,
        "板块名称": board_name,
        "类型": "概念",
        "最新价": None,
        "涨跌幅": avg_pct,
        "涨跌幅_5日": avg_pct5,
        "换手率": avg_turn,
        "市盈率": avg_pe,
        "总市值_亿": sum_mcap,
        "主力净流入_亿": sum_main,
        "主力净流入_5日_亿": sum_main5,
        "主力净占比": None,
        "主力净占比_5日": None,
        "上涨家数": n,
        "下跌家数": 0,
        "领涨股": lead_name if n else f"今日0只·{date_label}",
        "领涨涨跌幅": lead_pct,
    }


def _lianban_board_summary_row(cons: pd.DataFrame, trade_date: str) -> dict:
    return _virtual_zt_board_summary_row(
        cons,
        trade_date,
        board_code=_LIANBAN_BOARD_CODE,
        board_name=_LIANBAN_BOARD_NAME,
    )


def _lianyang_board_summary_row(cons: pd.DataFrame, trade_date: str) -> dict:
    return _virtual_zt_board_summary_row(
        cons,
        trade_date,
        board_code=_LIANYANG_BOARD_CODE,
        board_name=_LIANYANG_BOARD_NAME,
    )


def _sanlianyang_board_summary_row(cons: pd.DataFrame, trade_date: str) -> dict:
    return _virtual_zt_board_summary_row(
        cons,
        trade_date,
        board_code=_SANLIANYANG_BOARD_CODE,
        board_name=_SANLIANYANG_BOARD_NAME,
    )


def _empty_virtual_board_row(board_code: str, board_name: str) -> dict:
    return {
        "板块代码": board_code,
        "板块名称": board_name,
        "类型": "概念",
        "最新价": None,
        "涨跌幅": 0.0,
        "涨跌幅_5日": 0.0,
        "换手率": None,
        "市盈率": None,
        "总市值_亿": None,
        "主力净流入_亿": None,
        "主力净流入_5日_亿": None,
        "主力净占比": None,
        "主力净占比_5日": None,
        "上涨家数": 0,
        "下跌家数": 0,
        "领涨股": "暂无数据",
        "领涨涨跌幅": None,
    }


def fetch_industry_boards(*, fast: bool = False) -> pd.DataFrame:
    """
    板块一览：行业 + 概念（含 CPO/PCB/半导体等）+ 创业板/科创板市场。
    fast=True：前瞻刷新用，跳过连板/连阳虚拟板逐股K线扫描（极慢）。
    """
    page_sleep = 0.05 if fast else 0.25
    req_timeout = 8 if fast else 20
    ind_pages = 4 if fast else 5
    con_pages = 5 if fast else 6
    base = {
        "po": "1",
        "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2",
        "invt": "2",
        "fid": "f62",
        "fields": _BOARD_FIELDS,
    }
    last_err: Optional[Exception] = None
    rows: list = []
    virtual_rows: list = []

    if fast:
        virtual_rows.append(_empty_virtual_board_row(_LIANBAN_BOARD_CODE, _LIANBAN_BOARD_NAME))
        virtual_rows.append(_empty_virtual_board_row(_LIANYANG_BOARD_CODE, _LIANYANG_BOARD_NAME))
        virtual_rows.append(
            _empty_virtual_board_row(_SANLIANYANG_BOARD_CODE, _SANLIANYANG_BOARD_NAME)
        )
    else:
        # 虚拟板先扫：三连阳必须在启动/全量刷新时扫完并落盘
        def _load_sanlianyang() -> dict:
            try:
                # 优先磁盘/内存（启动脚本已暖好）；没有再实扫
                sly_cons, sly_date = warm_sanlianyang_cache(force=False)
                if sly_cons is None or sly_cons.empty:
                    sly_cons, sly_date = warm_sanlianyang_cache(force=True)
                if sly_cons is None or sly_cons.empty:
                    row = _empty_virtual_board_row(
                        _SANLIANYANG_BOARD_CODE, _SANLIANYANG_BOARD_NAME
                    )
                    row["领涨股"] = "扫描失败"
                    return row
                return _sanlianyang_board_summary_row(sly_cons, sly_date)
            except Exception:
                disk_df, disk_date = _load_sanlianyang_disk()
                if isinstance(disk_df, pd.DataFrame) and not disk_df.empty:
                    _cache_sanlianyang(disk_df, disk_date)
                    _pin_virtual_board_cache(
                        _SANLIANYANG_BOARD_CODE, _SANLIANYANG_BOARD_NAME, disk_df
                    )
                    return _sanlianyang_board_summary_row(disk_df, disk_date)
                return _empty_virtual_board_row(
                    _SANLIANYANG_BOARD_CODE, _SANLIANYANG_BOARD_NAME
                )

        def _load_lianban() -> dict:
            try:
                lb_cons, lb_date = fetch_lianban_zt_pool(min_boards=2)
                _pin_virtual_board_cache(_LIANBAN_BOARD_CODE, _LIANBAN_BOARD_NAME, lb_cons)
                _LIANBAN_CACHE.update(
                    {"ts": time.time(), "date": str(lb_date or ""), "df": lb_cons}
                )
                return _lianban_board_summary_row(lb_cons, lb_date)
            except Exception:
                empty = pd.DataFrame()
                _pin_virtual_board_cache(_LIANBAN_BOARD_CODE, _LIANBAN_BOARD_NAME, empty)
                _LIANBAN_CACHE.update({"ts": time.time(), "date": "", "df": empty})
                return _empty_virtual_board_row(_LIANBAN_BOARD_CODE, _LIANBAN_BOARD_NAME)

        def _load_lianyang() -> dict:
            try:
                ly_cons, ly_date = fetch_lianyang_shouban_pool(min_yang_days=1)
                _pin_virtual_board_cache(_LIANYANG_BOARD_CODE, _LIANYANG_BOARD_NAME, ly_cons)
                return _lianyang_board_summary_row(ly_cons, ly_date)
            except Exception:
                empty = pd.DataFrame()
                _pin_virtual_board_cache(_LIANYANG_BOARD_CODE, _LIANYANG_BOARD_NAME, empty)
                _LIANYANG_CACHE.update({"ts": time.time(), "date": "", "df": empty})
                return _empty_virtual_board_row(_LIANYANG_BOARD_CODE, _LIANYANG_BOARD_NAME)

        # 三连阳最先：扫完落盘后，点选只读缓存
        for loader in (_load_sanlianyang, _load_lianban, _load_lianyang):
            try:
                virtual_rows.append(loader())
            except Exception as exc:  # noqa: BLE001
                last_err = exc

    def _load_board_fs(fs: str, btype: str, max_pages: int) -> list:
        params = dict(base)
        params["fs"] = fs
        diff = _clist_pages(
            params,
            page_size=100,
            max_pages=max_pages,
            page_sleep=page_sleep,
            request_timeout=req_timeout,
        )
        return _rows_from_board_diff(diff, btype)

    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_ind = pool.submit(_load_board_fs, "m:90 t:2 f:!50", "行业", ind_pages)
        fut_con = pool.submit(_load_board_fs, "m:90 t:3 f:!50", "概念", con_pages)
        try:
            rows.extend(fut_ind.result(timeout=90))
        except Exception as exc:  # noqa: BLE001
            last_err = exc
        try:
            rows.extend(fut_con.result(timeout=90))
        except Exception as exc:  # noqa: BLE001
            last_err = exc

    try:
        rows.extend(_fetch_market_board_rows())
    except Exception as exc:  # noqa: BLE001
        last_err = exc

    rows.extend(virtual_rows)

    if not rows:
        try:
            return _ths_industry_fallback()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"板块获取失败: {last_err or exc}") from exc

    df = pd.DataFrame(rows)
    # 市场行的 _fs 仅内部用，成分股解析时靠板块代码映射
    drop_cols = [c for c in ("_fs", "_lianban_max", "_lianban_date") if c in df.columns]
    if drop_cols:
        df = df.drop(columns=drop_cols)
    df = _numeric_board_df(df)
    # 同名去重：优先保留市场 > 概念 > 行业（创业板综等仍保留不同名称）
    type_rank = {"市场": 0, "概念": 1, "行业": 2}
    df["_tr"] = df["类型"].map(lambda x: type_rank.get(x, 9))
    df = (
        df.sort_values(["板块名称", "_tr"])
        .drop_duplicates(subset=["板块代码"], keep="first")
        .drop(columns=["_tr"])
    )
    # 打板虚拟概念置顶：两连板 → 连阳首板 → 三连阳 → 其余按主力净流入
    rank_map = {
        _LIANBAN_BOARD_CODE: 0,
        _LIANYANG_BOARD_CODE: 1,
        _SANLIANYANG_BOARD_CODE: 2,
    }
    df["_pin"] = df["板块代码"].astype(str).map(lambda x: rank_map.get(x, 9))
    top = df.loc[df["_pin"] < 9].sort_values("_pin")
    rest = df.loc[df["_pin"] >= 9].sort_values(
        "主力净流入_亿", ascending=False, na_position="last"
    )
    df = pd.concat([top, rest], ignore_index=True).drop(columns=["_pin"])
    df.insert(0, "排名", range(1, len(df) + 1))
    return df


def _resolve_board_fs(board: str) -> Tuple[str, str]:
    """返回 (显示名或原入参, clist 的 fs 过滤器)。"""
    board = str(board or "").strip()
    if not board:
        return board, ""

    if _is_lianban_board(board):
        return _LIANBAN_BOARD_NAME, f"special:{_LIANBAN_BOARD_CODE}"
    if _is_lianyang_board(board):
        return _LIANYANG_BOARD_NAME, f"special:{_LIANYANG_BOARD_CODE}"
    if _is_sanlianyang_board(board):
        return _SANLIANYANG_BOARD_NAME, f"special:{_SANLIANYANG_BOARD_CODE}"

    upper = board.upper()
    for m in _MARKET_BOARDS:
        if upper == m["板块代码"] or board == m["板块名称"]:
            return m["板块名称"], m["fs"]

    if upper.startswith("BK"):
        return board, f"b:{upper} f:!50"

    if upper.startswith("MKT_"):
        for m in _MARKET_BOARDS:
            if upper == m["板块代码"]:
                return m["板块名称"], m["fs"]
        if upper == _LIANBAN_BOARD_CODE:
            return _LIANBAN_BOARD_NAME, f"special:{_LIANBAN_BOARD_CODE}"
        if upper == _LIANYANG_BOARD_CODE:
            return _LIANYANG_BOARD_NAME, f"special:{_LIANYANG_BOARD_CODE}"
        if upper == _SANLIANYANG_BOARD_CODE:
            return _SANLIANYANG_BOARD_NAME, f"special:{_SANLIANYANG_BOARD_CODE}"
        raise RuntimeError(f"未知市场板块: {board}")

    boards = fetch_industry_boards()
    hit = boards[boards["板块名称"] == board]
    if hit.empty:
        hit = boards[boards["板块名称"].astype(str).str.contains(board, na=False)]
    if hit.empty:
        raise RuntimeError(f"未找到板块: {board}")
    code = str(hit.iloc[0]["板块代码"] or "")
    name = str(hit.iloc[0]["板块名称"] or board)
    if not code:
        raise RuntimeError(f"板块无代码，无法拉成分股: {board}")
    if code.upper() == _LIANBAN_BOARD_CODE:
        return name, f"special:{_LIANBAN_BOARD_CODE}"
    if code.upper() == _LIANYANG_BOARD_CODE:
        return name, f"special:{_LIANYANG_BOARD_CODE}"
    if code.upper() == _SANLIANYANG_BOARD_CODE:
        return name, f"special:{_SANLIANYANG_BOARD_CODE}"
    if code.upper().startswith("MKT_"):
        for m in _MARKET_BOARDS:
            if code.upper() == m["板块代码"]:
                return name, m["fs"]
    return name, f"b:{code} f:!50"


def fetch_board_constituents(board: str) -> pd.DataFrame:
    """板块成分股。board 可为板块名称、BK 代码，或 MKT_CYB/MKT_KCB/MKT_2LB/MKT_LYSB/MKT_3LY。"""
    board = str(board or "").strip()
    if not board:
        return pd.DataFrame()

    if is_virtual_zt_board(board):
        return fetch_virtual_board_constituents(board, force=False)

    hit = _lookup_constituents_cache(board)
    if hit is not None:
        return hit

    df = _fetch_board_constituents_uncached(board)
    if df is None:
        df = pd.DataFrame()
    _BOARD_CONSTITUENTS_CACHE[board] = df.copy()
    return df


def _fetch_board_constituents_uncached(board: str) -> pd.DataFrame:
    board = str(board or "").strip()
    if not board:
        return pd.DataFrame()

    if is_virtual_zt_board(board):
        return fetch_virtual_board_constituents(board, force=True)

    _name, fs = _resolve_board_fs(board)
    if fs.startswith("special:"):
        return fetch_virtual_board_constituents(board, force=True)

    # 创业板/科创板股票多，多翻几页
    max_pages = 18 if fs.startswith("m:") else 8
    if _BOARD_FETCH_MAX_PAGES is not None:
        max_pages = min(max_pages, int(_BOARD_FETCH_MAX_PAGES))
    params = {
        "po": "1",
        "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2",
        "invt": "2",
        "fid": "f3",
        "fs": fs,
        "fields": "f12,f14,f2,f3,f8,f9,f20,f23,f62,f184,f10,f6,f109,f164,f165",
    }
    try:
        diff = _clist_pages(params, page_size=100, max_pages=max_pages)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"成分股获取失败: {exc}") from exc
    if not diff:
        raise RuntimeError(f"成分股为空: {board}")

    rows = []
    for item in diff:
        rows.append(
            {
                "代码": item.get("f12"),
                "名称": item.get("f14"),
                "最新价": item.get("f2"),
                "涨跌幅": item.get("f3"),
                "涨跌幅_5日": item.get("f109"),
                "换手率": item.get("f8"),
                "市盈率": item.get("f9"),
                "市净率": item.get("f23"),
                "总市值_亿": _yi(item.get("f20")),
                "主力净流入_亿": _yi(item.get("f62")),
                "主力净流入_5日_亿": _yi(item.get("f164")),
                "量比": item.get("f10"),
                "成交额": item.get("f6"),
                "连板数": pd.NA,
                "连阳天数": pd.NA,
            }
        )
    df = pd.DataFrame(rows)
    for col in [
        "最新价",
        "涨跌幅",
        "涨跌幅_5日",
        "换手率",
        "市盈率",
        "市净率",
        "总市值_亿",
        "主力净流入_亿",
        "主力净流入_5日_亿",
        "量比",
        "成交额",
    ]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.sort_values("涨跌幅", ascending=False, na_position="last").reset_index(
        drop=True
    )
    df.insert(0, "序号", range(1, len(df) + 1))
    return df


def _normalize_cons(raw: pd.DataFrame) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame()
    colmap = {
        "代码": "代码",
        "名称": "名称",
        "最新价": "最新价",
        "涨跌幅": "涨跌幅",
        "换手率": "换手率",
        "市盈率-动态": "市盈率",
        "市净率": "市净率",
    }
    df = raw.rename(columns={k: v for k, v in colmap.items() if k in raw.columns}).copy()
    keep = [c for c in ["代码", "名称", "最新价", "涨跌幅", "换手率", "市盈率", "市净率"] if c in df.columns]
    df = df[keep]
    for col in df.columns:
        if col not in ("代码", "名称"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.reset_index(drop=True)
    df.insert(0, "序号", range(1, len(df) + 1))
    return df


def load_watchlist() -> list:
    if not WATCHLIST_PATH.exists():
        return []
    try:
        data = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def save_watchlist(items: list) -> Path:
    WATCHLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    WATCHLIST_PATH.write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return WATCHLIST_PATH


def add_to_watchlist(code: str, name: str = "", board: str = "") -> list:
    code = str(code or "").strip()
    if not code:
        return load_watchlist()
    items = load_watchlist()
    for it in items:
        if str(it.get("code")) == code:
            return items
    items.append(
        {
            "code": code,
            "name": name or code,
            "board": board or "",
            "added_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    save_watchlist(items)
    return items


def remove_from_watchlist(code: str) -> list:
    code = str(code or "").strip()
    items = [it for it in load_watchlist() if str(it.get("code")) != code]
    save_watchlist(items)
    return items
