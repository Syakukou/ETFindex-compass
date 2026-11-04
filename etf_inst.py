# -*- coding: utf-8 -*-
"""个股的机构持仓（社保/QFII/保险/券商/信托/基金）与陆股通持股。

数据口径
- 机构: 东财「主力数据-机构持仓」按报告期分类型(1 基金 / 2 QFII / 3 社保 /
  4 券商 / 5 保险 / 6 信托)给全市场, 一次一类, 不需要逐只查。
  列里的「持有基金家数」其实是该类型的机构家数。
- 陆股通: **北向每日个股持股已停止披露**(东财接口只剩南向 002), 所以只能用
  季度口径代理 —— 十大流通股东里的「香港中央结算有限公司」。
  只有进前十大流通股东的才看得到, 覆盖不全, UI 里必须写明。
"""
import datetime as _dt
import sqlite3

KINDS = (("社保持仓", "社保"), ("QFII持仓", "QFII"), ("保险持仓", "保险"),
         ("券商持仓", "券商"), ("信托持仓", "信托"), ("基金持仓", "基金"))
KIND_ORDER = {k[1]: i for i, k in enumerate(KINDS)}
TYPE_MAP = {"基金持仓": "1", "QFII持仓": "2", "社保持仓": "3",
            "券商持仓": "4", "保险持仓": "5", "信托持仓": "6"}


def _conn(db_path):
    con = sqlite3.connect(db_path)
    con.execute("""CREATE TABLE IF NOT EXISTS inst(
        stock TEXT, date TEXT, kind TEXT, n INTEGER, shares REAL, value REAL,
        chg TEXT, chg_pct REAL, fetched_at TEXT,
        PRIMARY KEY(stock, date, kind))""")
    con.execute("""CREATE TABLE IF NOT EXISTS hsgt(
        stock TEXT, date TEXT, shares REAL, value REAL, fetched_at TEXT,
        PRIMARY KEY(stock, date))""")
    con.execute("CREATE INDEX IF NOT EXISTS i_inst_d ON inst(date)")
    con.execute("CREATE INDEX IF NOT EXISTS i_hsgt_d ON hsgt(date)")
    con.commit()
    return con


def quarters(n=3):
    """最近几个已过披露滞后的报告期, 新→旧。"""
    today = _dt.date.today()
    out = []
    y = today.year
    for yy in (y, y - 1, y - 2):
        for mm, dd in ((12, 31), (9, 30), (6, 30), (3, 31)):
            d = _dt.date(yy, mm, dd)
            if d <= today:
                out.append("%d%02d%02d" % (yy, mm, dd))
    out.sort(reverse=True)
    return out[:n]


def latest(db_path):
    """库里已有数据的最新报告期。"""
    con = _conn(db_path)
    try:
        r = con.execute("SELECT MAX(date) FROM inst").fetchone()
        r2 = con.execute("SELECT MAX(date) FROM hsgt").fetchone()
        return (r[0] if r else None), (r2[0] if r2 else None)
    finally:
        con.close()


def refresh_inst(db_path, date, force=False):
    """拉六类机构持仓, 返回 (写入行数, 错误列表)。"""
    con = _conn(db_path)
    try:
        have = con.execute("SELECT COUNT(*) FROM inst WHERE date=?", (date,)).fetchone()[0]
    finally:
        con.close()
    if have and not force:
        return have, []
    import akshare as ak
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows, errs = [], []
    for label, short in KINDS:
        try:
            df = ak.stock_report_fund_hold(symbol=label, date=date)
        except Exception as e:
            errs.append("%s: %s" % (label, e))
            continue
        if df is None or len(df) == 0:
            continue
        for _, r in df.iterrows():
            try:
                code = str(r["股票代码"]).strip().zfill(6)
                rows.append((code, date, short, int(r["持有基金家数"] or 0),
                             float(r["持股总数"] or 0), float(r["持股市值"] or 0),
                             str(r["持股变化"] or ""), float(r["持股变动比例"] or 0), now))
            except Exception:
                continue
    con = _conn(db_path)
    try:
        con.execute("DELETE FROM inst WHERE date=?", (date,))
        con.executemany("INSERT OR REPLACE INTO inst VALUES(?,?,?,?,?,?,?,?,?)", rows)
        con.commit()
    finally:
        con.close()
    return len(rows), errs


def refresh_hsgt(db_path, date, force=False):
    """十大流通股东里的香港中央结算 = 陆股通持股的季度代理。"""
    con = _conn(db_path)
    try:
        have = con.execute("SELECT COUNT(*) FROM hsgt WHERE date=?", (date,)).fetchone()[0]
    finally:
        con.close()
    if have and not force:
        return have
    import akshare as ak
    df = ak.stock_gdfx_free_holding_detail_em(date=date)
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    if df is not None and len(df):
        hk = df[df["股东名称"].astype(str).str.contains("香港中央结算")]
        for _, r in hk.iterrows():
            try:
                rows.append((str(r["股票代码"]).strip().zfill(6), date,
                             float(r["期末持股-数量"] or 0),
                             float(r["期末持股-流通市值"] or 0), now))
            except Exception:
                continue
        # 同一股票可能因不同股东名(结算有限公司/结算有限公司(陆股通))重复, 取最大
        dedup = {}
        for code, d, sh, val, t in rows:
            if code not in dedup or sh > dedup[code][0]:
                dedup[code] = (sh, val, t)
        rows = [(c, date, v[0], v[1], v[2]) for c, v in dedup.items()]
    con = _conn(db_path)
    try:
        con.execute("DELETE FROM hsgt WHERE date=?", (date,))
        con.executemany("INSERT OR REPLACE INTO hsgt VALUES(?,?,?,?,?)", rows)
        con.commit()
    finally:
        con.close()
    return len(rows)


def load(db_path, date=None):
    """(inst_map, hsgt_map): inst_map[stock][kind] = dict(n, shares, value, chg);
    hsgt_map[stock] = dict(shares, value)。"""
    con = _conn(db_path)
    try:
        if not date:
            r = con.execute("SELECT MAX(date) FROM inst").fetchone()
            date = r[0] if r else None
        inst, hs = {}, {}
        if date:
            for code, kind, n, sh, val, chg, cp in con.execute(
                    "SELECT stock, kind, n, shares, value, chg, chg_pct FROM inst WHERE date=?",
                    (date,)):
                inst.setdefault(code, {})[kind] = dict(n=n, shares=sh, value=val,
                                                       chg=chg, chg_pct=cp)
            for code, sh, val in con.execute(
                    "SELECT stock, shares, value FROM hsgt WHERE date=?", (date,)):
                hs[code] = dict(shares=sh, value=val)
        return date, inst, hs
    finally:
        con.close()


def summary(hit, inst_map):
    """把一只股票的机构持仓压成一行短文本 + 长 tooltip。"""
    d = inst_map.get(hit) or {}
    if not d:
        return "", ""
    parts, longs = [], []
    for label, short in (("社保", "社保"), ("保险", "险"), ("QFII", "Q"), ("券商", "券"),
                         ("信托", "信")):
        v = d.get(label)
        if v:
            parts.append("%s%d" % (short, v["n"] or 1))
            longs.append("%s %d 家 · 持股 %.0f 万股 · 占流通未披露 · %s %.2f%%"
                         % (label, v["n"] or 1, (v["shares"] or 0) / 1e4,
                            v["chg"] or "", v["chg_pct"] or 0))
    f = d.get("基金")
    if f:
        longs.append("基金 %d 家 · 持股 %.0f 万股 · 市值 %.2f 亿"
                     % (f["n"] or 0, (f["shares"] or 0) / 1e4, (f["value"] or 0) / 1e8))
    return " ".join(parts), "\n".join(longs)
