# -*- coding: utf-8 -*-
"""个股估值与财务（营收 / 净利润，同比环比）。"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

import pandas as pd
import requests

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    return s


def _norm_code(code: str) -> str:
    code = str(code or "").strip().upper()
    code = re.sub(r"^(SH|SZ|BJ)", "", code)
    code = re.sub(r"\D", "", code)
    return code.zfill(6) if code else ""


def _secid(code: str) -> str:
    code = _norm_code(code)
    if code.startswith(("5", "6", "9")):
        return f"1.{code}"
    if code.startswith(("4", "8")):
        return f"0.{code}"  # 北交所部分用 0，东财常见 bj 为 0
    return f"0.{code}"


def _parse_yi(v: Any) -> Optional[float]:
    """将 '547.03亿' / 数字 转为亿元。"""
    if v is None or v is False:
        return None
    if isinstance(v, (int, float)):
        if pd.isna(v):
            return None
        # 原始金额（元）过大时换算为亿
        x = float(v)
        return x / 1e8 if abs(x) >= 1e6 else x
    s = str(v).replace(",", "").strip()
    if s in ("", "-", "--", "False", "None", "nan"):
        return None
    mult = 1.0
    if s.endswith("万亿"):
        s, mult = s[:-2], 1e4
    elif s.endswith("亿"):
        s, mult = s[:-1], 1.0
    elif s.endswith("万"):
        s, mult = s[:-1], 1e-4
    try:
        return float(s) * mult
    except ValueError:
        return None


def _parse_pct(v: Any) -> Optional[float]:
    if v is None or v is False:
        return None
    if isinstance(v, (int, float)):
        if pd.isna(v):
            return None
        return float(v)
    s = str(v).replace(",", "").replace("%", "").strip()
    if s in ("", "-", "--", "False", "None", "nan"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def fetch_valuation(code: str) -> Dict[str, Any]:
    """
    最新估值：市盈率(TTM/静)、市净率、市销率、PEG、总市值等。
    """
    code = _norm_code(code)
    out: Dict[str, Any] = {"code": code, "name": "", "source": ""}
    if not code:
        return out

    # 1) 东财估值分析明细（仅最新一行，很快）
    try:
        sess = _session()
        r = sess.get(
            "https://datacenter.eastmoney.com/securities/api/data/v1/get",
            params={
                "reportName": "RPT_VALUEANALYSIS_DET",
                "columns": "ALL",
                "quoteColumns": "",
                "filter": f'(SECURITY_CODE="{code}")',
                "pageNumber": "1",
                "pageSize": "1",
                "sortTypes": "-1",
                "sortColumns": "TRADE_DATE",
                "source": "SECURITIES",
                "client": "WEB",
            },
            headers={
                "User-Agent": _UA,
                "Referer": "https://data.eastmoney.com/",
            },
            timeout=15,
        )
        r.raise_for_status()
        rows = ((r.json() or {}).get("result") or {}).get("data") or []
        if rows:
            d = rows[0]
            out.update(
                {
                    "name": str(d.get("SECURITY_NAME_ABBR") or ""),
                    "date": str(d.get("TRADE_DATE") or "")[:10],
                    "price": _parse_pct(d.get("CLOSE_PRICE")),
                    "pe_ttm": _parse_pct(d.get("PE_TTM")),
                    "pe_static": _parse_pct(d.get("PE_LAR")),
                    "pb": _parse_pct(d.get("PB_MRQ")),
                    "ps": _parse_pct(d.get("PS_TTM")),
                    "peg": _parse_pct(d.get("PEG_CAR")),
                    "pcf": _parse_pct(d.get("PCF_OCF_TTM") or d.get("PCF_OCF_LAR")),
                    "mcap": _parse_yi(d.get("TOTAL_MARKET_CAP")),
                    "float_mcap": _parse_yi(d.get("NOTLIMITED_MARKETCAP_A")),
                    "source": "eastmoney_value_det",
                }
            )
    except Exception:
        pass

    # 2) 实时报价补名称 / ROE / 若上面失败则补 PE/PB
    try:
        sess = _session()
        r = sess.get(
            "https://push2delay.eastmoney.com/api/qt/stock/get",
            params={
                "secid": _secid(code),
                "fltt": "2",
                "invt": "2",
                "fields": "f57,f58,f43,f170,f162,f167,f116,f117,f173,f9,f23",
            },
            headers={"User-Agent": _UA, "Referer": "https://quote.eastmoney.com/"},
            timeout=15,
        )
        d = (r.json() or {}).get("data") or {}
        if d:
            out["name"] = str(d.get("f58") or out.get("name") or "")
            if out.get("pe_ttm") is None:
                out["pe_ttm"] = _parse_pct(d.get("f162") or d.get("f9"))
            if out.get("pb") is None:
                out["pb"] = _parse_pct(d.get("f167") or d.get("f23"))
            if out.get("mcap") is None:
                out["mcap"] = _parse_yi(d.get("f116"))
            if out.get("float_mcap") is None:
                out["float_mcap"] = _parse_yi(d.get("f117"))
            if out.get("price") is None:
                out["price"] = _parse_pct(d.get("f43"))
            if out.get("pct") is None:
                out["pct"] = _parse_pct(d.get("f170"))
            if d.get("f173") is not None:
                out["roe"] = _parse_pct(d.get("f173"))
            if not out.get("source"):
                out["source"] = "eastmoney_quote"
    except Exception:
        pass

    return out


def _ytd_to_single_quarter(dates: pd.Series, values: pd.Series) -> pd.Series:
    """
    将累计（年至今）营收/净利润拆成单季：
    Q1=Q1累计；Q2=H1-Q1；Q3=9M-H1；Q4=全年-9M。
    """
    df = pd.DataFrame({"date": pd.to_datetime(dates), "v": values}).dropna(subset=["date"])
    df = df.sort_values("date")
    by_ym = {(d.year, d.month): float(v) for d, v in zip(df["date"], df["v"]) if pd.notna(v)}
    out = []
    for d, v in zip(df["date"], df["v"]):
        if pd.isna(v):
            out.append(None)
            continue
        y, m = d.year, d.month
        if m == 3:
            out.append(float(v))
        elif m == 6:
            prev = by_ym.get((y, 3))
            out.append(float(v) - prev if prev is not None else None)
        elif m == 9:
            prev = by_ym.get((y, 6))
            out.append(float(v) - prev if prev is not None else None)
        elif m == 12:
            prev = by_ym.get((y, 9))
            out.append(float(v) - prev if prev is not None else None)
        else:
            out.append(float(v))
    return pd.Series(out, index=df.index)


def fetch_income_series(code: str, limit: int = 24) -> pd.DataFrame:
    """
    近若干报告期：营业收入、净利润（亿元）及同比、环比（%）。
    同时保留累计值，供同花顺风格「年报+当年季度」柱图使用。
    """
    code = _norm_code(code)
    cols = [
        "报告期",
        "报告期标签",
        "营业收入_亿",
        "营收同比",
        "营收环比",
        "净利润_亿",
        "净利同比",
        "净利环比",
        "营收累计_亿",
        "净利累计_亿",
    ]
    if not code:
        return pd.DataFrame(columns=cols)

    raw = None
    try:
        import akshare as ak

        raw = ak.stock_financial_abstract_ths(symbol=code, indicator="按报告期")
    except Exception:
        raw = None

    if raw is None or raw.empty:
        return pd.DataFrame(columns=cols)

    df = raw.copy()
    df["报告期"] = pd.to_datetime(df["报告期"], errors="coerce")
    df = df.dropna(subset=["报告期"]).sort_values("报告期")
    df["营收累计_亿"] = df["营业总收入"].map(_parse_yi)
    df["净利累计_亿"] = df["净利润"].map(_parse_yi)
    yoy_rev_fb = df["营业总收入同比增长率"].map(_parse_pct)
    yoy_profit_fb = df["净利润同比增长率"].map(_parse_pct)

    df["营业收入_亿"] = _ytd_to_single_quarter(df["报告期"], df["营收累计_亿"]).values
    df["净利润_亿"] = _ytd_to_single_quarter(df["报告期"], df["净利累计_亿"]).values

    # 单季同比：与去年同报告期单季对比；若算不出则回退报表披露同比
    def _yoy_single(dates: pd.Series, singles: pd.Series, fallback: pd.Series) -> pd.Series:
        mp = {
            (pd.Timestamp(d).year, pd.Timestamp(d).month): float(v)
            for d, v in zip(dates, singles)
            if pd.notna(d) and pd.notna(v)
        }
        out = []
        for d, v, fb in zip(dates, singles, fallback):
            if pd.isna(d) or pd.isna(v):
                out.append(fb if pd.notna(fb) else None)
                continue
            prev = mp.get((pd.Timestamp(d).year - 1, pd.Timestamp(d).month))
            if prev is None or prev == 0:
                out.append(fb if pd.notna(fb) else None)
            else:
                out.append((float(v) / prev - 1.0) * 100.0)
        return pd.Series(out, index=dates.index)

    df["营收同比"] = _yoy_single(df["报告期"], df["营业收入_亿"], yoy_rev_fb)
    df["净利同比"] = _yoy_single(df["报告期"], df["净利润_亿"], yoy_profit_fb)

    # 环比：单季环比
    df["营收环比"] = df["营业收入_亿"].pct_change() * 100.0
    df["净利环比"] = df["净利润_亿"].pct_change() * 100.0

    def _label(ts) -> str:
        m = int(ts.month)
        tag = {3: "一季报", 6: "中报", 9: "三季报", 12: "年报"}.get(m, f"{m}月")
        return f"{ts.year}{tag}"

    df["报告期标签"] = df["报告期"].map(_label)
    out = df.tail(limit).copy()
    out["报告期"] = out["报告期"].dt.strftime("%Y-%m-%d")
    return out[cols].reset_index(drop=True)


def prepare_ths_style_income(
    income: pd.DataFrame, cur_year: Optional[int] = None
) -> pd.DataFrame:
    """
    同花顺风格财务柱图数据：
    - 前四个完整年度：只保留年报，金额用全年累计；
    - 当年：按已披露季度显示，金额用单季。
    """
    cols = [
        "报告期",
        "报告期标签",
        "口径",
        "营业收入_亿",
        "净利润_亿",
        "营收同比",
        "净利同比",
    ]
    if income is None or income.empty:
        return pd.DataFrame(columns=cols)

    df = income.copy()
    df["报告期"] = pd.to_datetime(df["报告期"], errors="coerce")
    df = df.dropna(subset=["报告期"]).sort_values("报告期")
    if df.empty:
        return pd.DataFrame(columns=cols)

    if cur_year is None:
        cur_year = int(pd.Timestamp.now().year)

    annual_years = [cur_year - 4, cur_year - 3, cur_year - 2, cur_year - 1]
    # 年报累计索引，便于算同比
    annual_rev = {}
    annual_profit = {}
    for y in range(cur_year - 6, cur_year):
        hit = df[(df["报告期"].dt.year == y) & (df["报告期"].dt.month == 12)]
        if hit.empty:
            continue
        r = hit.iloc[-1]
        rv = r.get("营收累计_亿")
        pv = r.get("净利累计_亿")
        if pd.isna(rv):
            rv = r.get("营业收入_亿")
        if pd.isna(pv):
            pv = r.get("净利润_亿")
        if pd.notna(rv):
            annual_rev[y] = float(rv)
        if pd.notna(pv):
            annual_profit[y] = float(pv)

    def _yoy(cur_v, prev_v):
        if cur_v is None or prev_v is None or pd.isna(cur_v) or pd.isna(prev_v) or prev_v == 0:
            return None
        return (float(cur_v) / float(prev_v) - 1.0) * 100.0

    rows = []
    for y in annual_years:
        if y not in annual_rev and y not in annual_profit:
            continue
        rev = annual_rev.get(y)
        profit = annual_profit.get(y)
        rows.append(
            {
                "报告期": f"{y}-12-31",
                "报告期标签": f"{y}年报",
                "口径": "年报",
                "营业收入_亿": rev,
                "净利润_亿": profit,
                "营收同比": _yoy(rev, annual_rev.get(y - 1)),
                "净利同比": _yoy(profit, annual_profit.get(y - 1)),
            }
        )

    tag_map = {3: "一季报", 6: "中报", 9: "三季报", 12: "年报"}
    cur = df[df["报告期"].dt.year == cur_year].sort_values("报告期")
    for _, r in cur.iterrows():
        ts = pd.Timestamp(r["报告期"])
        m = int(ts.month)
        if m not in tag_map:
            continue
        # 当年年报用全年累计；季报用单季
        if m == 12:
            rev = r.get("营收累计_亿")
            profit = r.get("净利累计_亿")
            if pd.isna(rev):
                rev = r.get("营业收入_亿")
            if pd.isna(profit):
                profit = r.get("净利润_亿")
            rev_yoy = _yoy(
                float(rev) if pd.notna(rev) else None, annual_rev.get(cur_year - 1)
            )
            p_yoy = _yoy(
                float(profit) if pd.notna(profit) else None,
                annual_profit.get(cur_year - 1),
            )
            kind = "年报"
        else:
            rev = r.get("营业收入_亿")
            profit = r.get("净利润_亿")
            rev_yoy = r.get("营收同比")
            p_yoy = r.get("净利同比")
            kind = "单季"
        rows.append(
            {
                "报告期": ts.strftime("%Y-%m-%d"),
                "报告期标签": f"{cur_year}{tag_map[m]}",
                "口径": kind,
                "营业收入_亿": float(rev) if pd.notna(rev) else None,
                "净利润_亿": float(profit) if pd.notna(profit) else None,
                "营收同比": float(rev_yoy) if pd.notna(rev_yoy) else None,
                "净利同比": float(p_yoy) if pd.notna(p_yoy) else None,
            }
        )

    return pd.DataFrame(rows, columns=cols)


def _em_f10_code(code: str) -> str:
    """东财 F10 代码：SH600519 / SZ000001 / BJ830xxx。"""
    code = _norm_code(code)
    if not code:
        return ""
    if code.startswith(("5", "6", "9")):
        return f"SH{code}"
    if code.startswith(("4", "8")):
        return f"BJ{code}"
    return f"SZ{code}"


def _clean_text(v: Any) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    s = str(v).strip()
    if s in ("", "-", "--", "None", "nan", "False"):
        return ""
    return s


def _ratio_pct(v: Any) -> Optional[float]:
    """MBI_RATIO 可能是 0.86 或 86。"""
    x = _parse_pct(v)
    if x is None:
        return None
    return x * 100.0 if abs(x) <= 1.5 else x


def _pick_latest_segments(rows: list, mainop_type: int) -> Tuple[str, list]:
    """取最新报告期、指定口径(1行业/2产品/3地区)的主营构成。"""
    typed = [r for r in rows if str(r.get("MAINOP_TYPE")) == str(mainop_type)]
    if not typed:
        return "", []
    dates = sorted(
        {
            str(r.get("REPORT_DATE") or "")[:10]
            for r in typed
            if r.get("REPORT_DATE")
        },
        reverse=True,
    )
    if not dates:
        return "", []
    latest = dates[0]
    items = [r for r in typed if str(r.get("REPORT_DATE") or "")[:10] == latest]
    items.sort(
        key=lambda r: (
            -(_ratio_pct(r.get("MBI_RATIO")) or 0.0),
            str(r.get("ITEM_NAME") or ""),
        )
    )
    return latest, items


def fetch_company_profile(code: str) -> Dict[str, Any]:
    """
    公司介绍：所属行业、公司简介、经营范围、主营构成（产品/地区收入占比）。
    数据源：东财 F10 CompanySurvey + BusinessAnalysis。
    """
    code = _norm_code(code)
    out: Dict[str, Any] = {
        "code": code,
        "name": "",
        "full_name": "",
        "industry": "",
        "csrc_industry": "",
        "intro": "",
        "business_scope": "",
        "registered_capital": "",
        "employees": "",
        "website": "",
        "list_date": "",
        "found_date": "",
        "business_review": "",
        "review_date": "",
        "segment_date": "",
        "segments_product": [],
        "segments_region": [],
        "segments_industry": [],
        "source": "",
    }
    if not code:
        return out

    em_code = _em_f10_code(code)
    headers = {
        "User-Agent": _UA,
        "Referer": "https://emweb.securities.eastmoney.com/",
    }
    sess = _session()

    # 1) 公司概况
    try:
        r = sess.get(
            "https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/CompanySurveyAjax",
            params={"code": em_code},
            headers=headers,
            timeout=15,
        )
        r.raise_for_status()
        data = r.json() or {}
        jb = data.get("jbzl") or {}
        fx = data.get("fxxg") or {}
        out.update(
            {
                "name": _clean_text(jb.get("agjc") or data.get("SecurityShortName")),
                "full_name": _clean_text(jb.get("gsmc")),
                "industry": _clean_text(jb.get("sshy")),
                "csrc_industry": _clean_text(jb.get("sszjhhy")),
                "intro": _clean_text(jb.get("gsjj")),
                "business_scope": _clean_text(jb.get("jyfw")),
                "registered_capital": _clean_text(jb.get("zczb")),
                "employees": _clean_text(jb.get("gyrs")),
                "website": _clean_text(jb.get("gswz")),
                "found_date": _clean_text(fx.get("clrq")),
                "list_date": _clean_text(fx.get("ssrq")),
                "source": "eastmoney_f10",
            }
        )
    except Exception:
        pass

    # 2) 经营分析：经营范围补充 + 主营构成 + 经营评述
    try:
        r = sess.get(
            "https://emweb.securities.eastmoney.com/PC_HSF10/BusinessAnalysis/PageAjax",
            params={"code": em_code},
            headers=headers,
            timeout=20,
        )
        r.raise_for_status()
        data = r.json() or {}
        if not out.get("business_scope"):
            zyfw = data.get("zyfw") or []
            if zyfw:
                out["business_scope"] = _clean_text(zyfw[0].get("BUSINESS_SCOPE"))
        jyps = data.get("jyps") or []
        if jyps:
            out["business_review"] = _clean_text(jyps[0].get("BUSINESS_REVIEW"))
            out["review_date"] = str(jyps[0].get("REPORT_DATE") or "")[:10]
        zygc = data.get("zygcfx") or []
        if zygc:
            d2, items2 = _pick_latest_segments(zygc, 2)
            d1, items1 = _pick_latest_segments(zygc, 1)
            d3, items3 = _pick_latest_segments(zygc, 3)
            out["segment_date"] = d2 or d1 or d3

            def _seg_rows(items: list) -> list:
                rows = []
                for it in items:
                    name = _clean_text(it.get("ITEM_NAME"))
                    if not name:
                        continue
                    raw_inc = it.get("MAIN_BUSINESS_INCOME")
                    income_yi = None
                    if isinstance(raw_inc, (int, float)) and not pd.isna(raw_inc):
                        income_yi = float(raw_inc) / 1e8
                    elif raw_inc not in (None, "", "-"):
                        income_yi = _parse_yi(raw_inc)
                    rows.append(
                        {
                            "name": name,
                            "ratio": _ratio_pct(it.get("MBI_RATIO")),
                            "income_yi": income_yi,
                            "gross_margin": _ratio_pct(it.get("GROSS_RPOFIT_RATIO")),
                        }
                    )
                return rows

            out["segments_product"] = _seg_rows(items2)
            out["segments_industry"] = _seg_rows(items1)
            out["segments_region"] = _seg_rows(items3)
            if not out.get("source"):
                out["source"] = "eastmoney_f10"
    except Exception:
        pass

    # 3) 兜底：巨潮公司概况（简介/主营业务）
    if not out.get("intro") or not out.get("industry"):
        try:
            import akshare as ak

            df = ak.stock_profile_cninfo(symbol=code)
            if df is not None and not df.empty:
                row = df.iloc[0]
                out["full_name"] = out["full_name"] or _clean_text(row.get("公司名称"))
                out["name"] = out["name"] or _clean_text(row.get("A股简称"))
                out["industry"] = out["industry"] or _clean_text(row.get("所属行业"))
                out["intro"] = out["intro"] or _clean_text(row.get("机构简介"))
                if not out.get("business_scope"):
                    out["business_scope"] = _clean_text(row.get("经营范围"))
                main_biz = _clean_text(row.get("主营业务"))
                if main_biz:
                    out["main_business"] = main_biz
                if not out.get("source"):
                    out["source"] = "cninfo"
        except Exception:
            pass

    return out


def fetch_finance_bundle(code: str) -> Dict[str, Any]:
    """估值 + 利润表序列 + 公司介绍。"""
    return {
        "valuation": fetch_valuation(code),
        "income": fetch_income_series(code, limit=24),
        "profile": fetch_company_profile(code),
    }
