# -*- coding: utf-8 -*-
"""ETF 成分股 / 重仓股: 东财基金持仓明细, 落本地库缓存。

东财接口一次给一个自然年内的全部已披露季度, 取最新季度即可。QDII 的持仓代码是
海外代码(如 NVDA), A 股接口取不到行情, 只展示权重。
"""
import datetime as _dt
import json
import re
import sqlite3
import urllib.request

DB = None          # 由 get_holdings 传入的库路径
UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}


def _conn(db_path):
    global DB
    DB = db_path or DB
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS holding(
        code TEXT, quarter TEXT, rank INTEGER, stock TEXT, sname TEXT,
        pct REAL, shares REAL, value REAL, fetched_at TEXT,
        PRIMARY KEY(code, quarter, stock))""")
    con.commit()
    return con


def _qkey(q):
    """'2026年3季度股票投资明细' -> (2026, 3)"""
    m = re.match(r"(\d{4})年(\d)季度", str(q))
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def cached(db_path, code):
    con = _conn(db_path)
    try:
        rows = con.execute(
            "SELECT quarter, rank, stock, sname, pct, shares, value, fetched_at "
            "FROM holding WHERE code=? ORDER BY pct DESC", (code,)).fetchall()
    finally:
        con.close()
    if not rows:
        return None
    rows.sort(key=lambda r: _qkey(r[0]))
    qs = []
    for r in rows:
        if r[0] not in qs:
            qs.append(r[0])
    q = qs[-1]
    prev_q = qs[-2] if len(qs) > 1 else None
    keep = [r for r in rows if r[0] == q]
    prev = {r[2]: (r[4] or 0) for r in rows if prev_q and r[0] == prev_q}
    prev_n = sum(1 for r in rows if prev_q and r[0] == prev_q)
    # 东财原始序号会从 201 起(接在半年报后面), 这里按权重重新编 1..N
    tot10 = sum((r[4] or 0) for r in keep[:10])
    out = []
    for i, r in enumerate(keep):
        p = prev.get(r[2])
        out.append(dict(rank=i + 1, stock=r[2], sname=r[3], pct=r[4], shares=r[5],
                        value=r[6], prev=p,
                        delta=(round((r[4] or 0) - p, 3) if p is not None else None),
                        new=(p is None and prev_q is not None)))
    return dict(code=code, quarter=q, prev_quarter=prev_q, prev_rows=prev_n,
                fetched=keep[0][7], rows=out, top10=round(tot10, 2))


def cached_codes(db_path):
    """已落库的 ETF 代码集合, 列表上用来区分「有明细 / 还没取过」。"""
    con = _conn(db_path)
    try:
        return {r[0] for r in con.execute("SELECT DISTINCT code FROM holding")}
    finally:
        con.close()


def cached_counts(db_path):
    """每只 ETF 最新季度的明细行数: {code: n}。用一次 SQL 出, 不用逐只 cached()。"""
    con = _conn(db_path)
    try:
        rows = con.execute(
            "SELECT code, quarter, COUNT(*) FROM holding GROUP BY code, quarter").fetchall()
    finally:
        con.close()
    best = {}
    for code, q, n in rows:
        k = _qkey(q)
        if code not in best or k > best[code][0]:
            best[code] = (k, n)
    return {c: v[1] for c, v in best.items()}


def fetch(code, db_path, force=False, max_age_days=10):
    """取最新季度重仓明细。force=False 且缓存新鲜则直接读库。"""
    hit = cached(db_path, code)
    if hit and not force:
        try:
            age = (_dt.datetime.now() -
                   _dt.datetime.strptime(hit["fetched"][:10], "%Y-%m-%d")).days
        except Exception:
            age = 99
        # 季度末才更新, 缓存 10 天内不重复拉; 季报刚开始披露时靠 force 手动刷
        if age <= max_age_days:
            return hit
    import akshare as ak
    # 取当年 + 去年: 权重环比要上一季度的占比, 只取当年的话 Q1 就没有可比期
    frames, err = [], None
    for y in (_dt.date.today().year, _dt.date.today().year - 1):
        try:
            _df = ak.fund_portfolio_hold_em(symbol=code, date=str(y))
        except Exception as e:
            err = "%s: %s" % (type(e).__name__, e)
            continue
        if _df is not None and len(_df) and "季度" in _df.columns:
            frames.append(_df)
    if not frames:
        if hit:
            hit["err"] = err or "接口无数据"
            return hit
        return dict(code=code, rows=[], err=err or "接口无数据")
    df = frames[0] if len(frames) == 1 else __import__("pandas").concat(frames, ignore_index=True)
    df = df.drop_duplicates(subset=["季度", "股票代码"], keep="first")
    if df is None or len(df) == 0:
        return hit or dict(code=code, quarter="", rows=[], top10=0.0)
    df["_k"] = df["季度"].map(_qkey)
    def _f(v):
        try:
            return float(v)
        except Exception:
            return 0.0
    # 所有季度都落库: 权重环比要用上一季度的占比, 只存最新期就永远算不出环比
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    all_rows = []
    for qq, grp in df.groupby("季度"):
        g = grp.copy()
        g["_p"] = g["占净值比例"].map(_f)
        g = g.sort_values("_p", ascending=False)     # 权重排序 + 自己编号, 不用东财 201 起的号
        for i, (_, r) in enumerate(g.iterrows()):
            try:
                pct = float(r["占净值比例"])
            except Exception:
                pct = None
            all_rows.append((code, qq, i + 1, str(r["股票代码"]).strip(),
                             str(r["股票名称"]).strip(), pct,
                             float(r["持股数"] or 0), float(r["持仓市值"] or 0), now))
    q = df.loc[df["_k"].idxmax(), "季度"]
    con = _conn(db_path)
    try:
        con.execute("DELETE FROM holding WHERE code=?", (code,))
        con.executemany(
            "INSERT OR REPLACE INTO holding VALUES(?,?,?,?,?,?,?,?,?)", all_rows)
        con.commit()
    finally:
        con.close()
    return cached(db_path, code) or dict(code=code, quarter=q, rows=[], top10=0.0)


def stock_quotes(codes, timeout=8):
    """A 股当日行情: {代码: dict(pct, price, turnover, amt, pe, pb, mcap, mflow, ind)}。

    海外代码(QDII 持仓)自动跳过; 成交额/市值/主力净流入统一折成「亿」。
    """
    ids = [c for c in codes if len(c) == 6 and c.isdigit()]
    if not ids:
        return {}
    out = {}
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        sec = ",".join(("1." if c[0] in "56" else "0.") + c for c in chunk)
        url = ("https://push2.eastmoney.com/api/qt/ulist.np/get?secids=" + sec +
               "&fields=f2,f3,f6,f8,f9,f12,f21,f62,f100,f127&fltt=2&invt=2")
        try:
            d = json.loads(urllib.request.urlopen(
                urllib.request.Request(url, headers=UA), timeout=timeout).read().decode())
        except Exception:
            continue
        for it in (d.get("data") or {}).get("diff") or []:
            def num(k, scale=1.0):
                v = it.get(k)
                return float(v) / scale if isinstance(v, (int, float)) else None
            p = num("f3")
            if p is None:
                continue
            out[str(it.get("f12"))] = dict(
                pct=p, price=num("f2"), turnover=num("f8"),
                amt=num("f6", 1e8), pe=num("f9"), pb=num("f127"),
                mcap=num("f21", 1e8), mflow=num("f62", 1e4),
                ind=(it.get("f100") or "").strip() or None)
    return out


def copy_text(hit, name=""):
    """把持仓表转成可粘贴文本, 方便丢进文章或表格。"""
    lines = ["%s %s  重仓明细 (%s)  前十大合计 %.2f%%" %
             (hit.get("code", ""), name, hit.get("quarter", ""), hit.get("top10") or 0)]
    lines.append("排名\t代码\t名称\t占净值%\t环比pp\t持仓市值(万)\t持股数(万股)")
    for r in hit.get("rows", []):
        lines.append("%d\t%s\t%s\t%s\t%s\t%.0f\t%.1f" % (
            r["rank"], r["stock"], r["sname"],
            ("%.2f" % r["pct"]) if r["pct"] is not None else "-",
            ("%+.2f" % r["delta"]) if r.get("delta") is not None
            else ("新进" if r.get("new") else "-"),
            r["value"] or 0, r["shares"] or 0))
    return "\n".join(lines)
