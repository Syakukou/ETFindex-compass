# -*- coding: utf-8 -*-
"""
ETF 跟踪器
- 数据源: akshare(主, 东财ETF行情一次拉全) + tushare(备)
- 功能: 收盘快照(价格/涨跌/IOPV/溢价/主力资金/份额) + SQLite 每日落库
- 用法:
    python etf_tracker.py snapshot               # 采集当日快照并落库
    python etf_tracker.py report                 # 最新一日报告
    python etf_tracker.py history --code 510300  # 单只 ETF 历史快照
    python etf_tracker.py add 512890 华泰红利低波  # 添加标的
"""
import argparse
import json
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB = BASE / "etf_tracker.db"
CONFIG = BASE / "etf_list.json"

DEFAULT_ETFS = [
    {"code": "510300", "name": "沪深300ETF", "note": "宽基"},
    {"code": "510500", "name": "中证500ETF", "note": "宽基"},
    {"code": "512100", "name": "中证1000ETF", "note": "宽基"},
    {"code": "588000", "name": "科创50ETF", "note": "宽基"},
    {"code": "512890", "name": "红利低波ETF", "note": "红利"},
    {"code": "513100", "name": "纳指ETF", "note": "QDII"},
    {"code": "518880", "name": "黄金ETF", "note": "商品"},
]

COLUMNS = """CREATE TABLE IF NOT EXISTS snapshot (
    trade_date TEXT NOT NULL,
    code       TEXT NOT NULL,
    name       TEXT,
    close      REAL,          -- 收盘/最新价
    pct_chg    REAL,          -- 涨跌幅 %
    amount     REAL,          -- 成交额(万元)
    turnover   REAL,          -- 换手率 %
    vol_ratio  REAL,          -- 量比
    iopv       REAL,          -- IOPV 实时估值
    premium    REAL,          -- 溢价率 % (现价-IOPV)/IOPV*100
    main_in    REAL,          -- 主力净流入(万元)
    shares     REAL,          -- 最新份额(亿份)
    source     TEXT,          -- akshare / tushare
    fetched_at TEXT,
    PRIMARY KEY (trade_date, code)
)"""


def load_etfs():
    if CONFIG.exists():
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    CONFIG.write_text(
        json.dumps(DEFAULT_ETFS, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return DEFAULT_ETFS


def get_db():
    conn = sqlite3.connect(DB)
    conn.execute(COLUMNS)
    return conn


def _f(v):
    try:
        x = float(v)
        return x if x == x else None  # 过滤 NaN
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- akshare 主源(批量)
def fetch_akshare_batch(codes):
    """一次拉东财全部 ETF 行情, 返回 {code: row}"""
    import akshare as ak

    df = ak.fund_etf_spot_em()
    out = {}
    for code in codes:
        hit = df[df["代码"] == code]
        if hit.empty:
            continue
        r = hit.iloc[0]
        close = _f(r.get("最新价"))
        if not close:
            continue
        iopv = _f(r.get("IOPV实时估值"))
        out[code] = {
            "name": str(r.get("名称", "")),
            "close": close,
            "pct_chg": _f(r.get("涨跌幅")),
            "amount": round(_f(r.get("成交额")) / 1e4, 1),       # 元 -> 万
            "turnover": _f(r.get("换手率")),
            "vol_ratio": _f(r.get("量比")),
            "iopv": iopv,
            "premium": round((close - iopv) / iopv * 100, 3) if iopv else None,
            "main_in": round(_f(r.get("主力净流入-净额")) / 1e4, 1),  # 元 -> 万
            "shares": round(_f(r.get("最新份额")) / 1e8, 2),          # 份 -> 亿份
        }
    return out


# ---------------------------------------------------------------- tushare 备源(单只)
def fetch_tushare_one(code, trade_date):
    import tushare as ts

    pro = ts.pro_api()  # token 从环境变量 TUSHARE_TOKEN 读取
    ts_code = f"{code}.SH" if code.startswith(("5", "6")) else f"{code}.SZ"
    row = {}
    try:
        df = pro.fund_daily(ts_code=ts_code, trade_date=trade_date.replace("-", ""))
        if not df.empty:
            r = df.iloc[0]
            row = {
                "close": _f(r.get("close")),
                "pct_chg": _f(r.get("pct_chg")),
                "amount": round(_f(r.get("amount")) * 1000 / 1e4, 1),  # 千元 -> 万
            }
    except Exception as e:
        print(f"  [tushare] {type(e).__name__}: {e}", file=sys.stderr)
    return row if row.get("close") else None


# ---------------------------------------------------------------- 采集 + 落库
def run_snapshot(only_codes=None):
    etfs = load_etfs()
    if only_codes:
        etfs = [e for e in etfs if e["code"] in only_codes] or etfs
    codes = [e["code"] for e in etfs]
    today = date.today().isoformat()
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()

    print(f"拉取东财 ETF 行情({len(codes)} 只目标)...")
    try:
        rows = fetch_akshare_batch(codes)
    except Exception as e:
        print(f"[akshare 批量] {type(e).__name__}: {e}, 逐只转 tushare 备源", file=sys.stderr)
        rows = {}

    for etf in etfs:
        code = etf["code"]
        row = rows.get(code)
        source = "akshare" if row else None

        # 备源兜底
        if not row:
            row = fetch_tushare_one(code, today)
            source = "tushare" if row else None
        if not row:
            print(f"[{code}] 双源均失败, 跳过")
            continue

        name = row.get("name") or etf["name"]
        conn.execute(
            """INSERT OR REPLACE INTO snapshot
               (trade_date, code, name, close, pct_chg, amount, turnover,
                vol_ratio, iopv, premium, main_in, shares, source, fetched_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (today, code, name, row.get("close"), row.get("pct_chg"),
             row.get("amount"), row.get("turnover"), row.get("vol_ratio"),
             row.get("iopv"), row.get("premium"), row.get("main_in"),
             row.get("shares"), source, now),
        )
        conn.commit()
        print(f"[{code}] {name}  收盘 {row.get('close')}  涨跌 {row.get('pct_chg')}%  "
              f"溢价 {row.get('premium')}%  成交 {row.get('amount')}万  "
              f"主力 {row.get('main_in')}万  [{source}]")

    conn.close()
    print(f"\n快照已写入 {DB}")


# ---------------------------------------------------------------- 报告
def show_report():
    import pandas as pd

    pd.set_option("display.width", 200)
    conn = get_db()
    df = pd.read_sql(
        "SELECT * FROM snapshot WHERE trade_date = "
        "(SELECT MAX(trade_date) FROM snapshot) ORDER BY main_in DESC", conn)
    conn.close()
    if df.empty:
        print("暂无快照数据, 先运行: python etf_tracker.py snapshot")
        return
    cols = ["code", "name", "close", "pct_chg", "premium", "amount",
            "main_in", "shares", "source"]
    df = df[[c for c in cols if c in df.columns]]
    df.columns = ["代码", "名称", "最新价", "涨跌幅%", "溢价率%", "成交额(万)",
                  "主力净流入(万)", "份额(亿)", "来源"][: len(df.columns)]
    print(df.to_string(index=False))


def show_history(code):
    import pandas as pd

    pd.set_option("display.width", 200)
    conn = get_db()
    df = pd.read_sql(
        "SELECT trade_date, close, pct_chg, premium, amount, main_in, shares, source "
        "FROM snapshot WHERE code = ? ORDER BY trade_date DESC LIMIT 20",
        conn, params=(code,))
    conn.close()
    if df.empty:
        print(f"{code} 无历史快照")
        return
    df.columns = ["日期", "收盘", "涨跌%", "溢价%", "成交额(万)", "主力净流入(万)", "份额(亿)", "来源"]
    print(df.to_string(index=False))


def add_etf(code, name, note=""):
    etfs = load_etfs()
    if any(e["code"] == code for e in etfs):
        print(f"{code} 已存在")
        return
    etfs.append({"code": code, "name": name, "note": note})
    CONFIG.write_text(json.dumps(etfs, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已添加 {code} {name} -> {CONFIG}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="ETF 跟踪器 (akshare主 + tushare备)")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("snapshot", help="采集当日快照并落库")
    s.add_argument("--only", nargs="*", help="仅采集指定代码")
    sub.add_parser("report", help="显示最新一日报告")
    h = sub.add_parser("history", help="单只 ETF 历史")
    h.add_argument("--code", required=True)
    a = sub.add_parser("add", help="添加 ETF")
    a.add_argument("code")
    a.add_argument("name")
    a.add_argument("note", nargs="?", default="")
    args = ap.parse_args()

    if args.cmd == "snapshot":
        run_snapshot(set(args.only) if args.only else None)
    elif args.cmd == "history":
        show_history(args.code)
    elif args.cmd == "add":
        add_etf(args.code, args.name, args.note)
    else:
        show_report()
