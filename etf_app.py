# -*- coding: utf-8 -*-
"""
ETF 跟踪面板 - 本地独立软件
- 双击 start_etf_app.bat 启动, 自动打开浏览器
- 服务端: 标准库 http.server + SQLite, 无额外依赖
- 数据源: 公开行情接口(东财), 行情快照 + 日K历史
- 指标: 前端计算 MA / MACD / BOLL, 后端供原始K线与快照
"""
import json
import shutil
import sqlite3

import brand
import threading
import time
import webbrowser
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import sys as _sys
BASE = (Path(_sys.executable).resolve().parent if getattr(_sys, "frozen", False)
        else Path(__file__).resolve().parent)
DB = BASE / "etf_tracker.db"
CONFIG = BASE / "etf_list.json"
KLINE_DAYS = 300  # 保留约 14 个月日K

COLUMNS = """CREATE TABLE IF NOT EXISTS snapshot (
    trade_date TEXT NOT NULL, code TEXT NOT NULL, name TEXT,
    close REAL, pct_chg REAL, amount REAL, turnover REAL, vol_ratio REAL,
    iopv REAL, premium REAL, main_in REAL, shares REAL, source TEXT,
    fetched_at TEXT, PRIMARY KEY (trade_date, code)
)"""
KLINE = """CREATE TABLE IF NOT EXISTS kline (
    code TEXT NOT NULL, trade_date TEXT NOT NULL,
    open REAL, close REAL, high REAL, low REAL,
    volume REAL, amount REAL, pct_chg REAL,
    PRIMARY KEY (code, trade_date)
)"""

DEFAULT_ETFS = [
    {"code": "510300", "name": "沪深300ETF", "category": "宽基指数", "note": "华泰柏瑞"},
    {"code": "510500", "name": "中证500ETF", "category": "宽基指数", "note": "南方"},
    {"code": "512100", "name": "中证1000ETF", "category": "宽基指数", "note": "南方"},
    {"code": "588000", "name": "科创50ETF", "category": "科技成长", "note": "华夏"},
    {"code": "512890", "name": "红利低波ETF", "category": "红利稳健", "note": "华泰柏瑞"},
    {"code": "510880", "name": "红利ETF", "category": "红利稳健", "note": "华泰柏瑞"},
    {"code": "513100", "name": "纳指ETF", "category": "跨境QDII", "note": "国泰, 跟踪纳斯达克100"},
    {"code": "513500", "name": "标普500ETF", "category": "跨境QDII", "note": "博时"},
    {"code": "518880", "name": "黄金ETF", "category": "商品避险", "note": "华安"},
]


def load_etfs():
    if CONFIG.exists():
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    # 打包后资源落在 _internal, 而 CONFIG 找的是 exe 同级目录。不搬一次的话,
    # 首次启动只能拿到 9 只内置标的, 看起来像"全池没拉出来"。
    bundled = Path(getattr(_sys, "_MEIPASS", BASE)) / "etf_list.json"
    if bundled.exists() and bundled.resolve() != CONFIG.resolve():
        try:
            shutil.copyfile(str(bundled), str(CONFIG))
            if CONFIG.exists():
                return json.loads(CONFIG.read_text(encoding="utf-8"))
        except Exception:
            pass
    CONFIG.write_text(json.dumps(DEFAULT_ETFS, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    return DEFAULT_ETFS


def get_db():
    conn = sqlite3.connect(DB)
    conn.execute(COLUMNS)
    conn.execute(KLINE)
    return conn


def _f(v):
    try:
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------ 数据更新(后台线程)
def ensure_stdio():
    """窗口程序没有控制台时 stdout/stderr 是 None, 写一下就会抛 AttributeError。

    行情接口内部有进度条, 一抛就整批失败。这里在取数入口再兜一次底:
    不依赖主程序模块的导入顺序, 谁先调用都安全。
    """
    import os
    for name in ("stdout", "stderr"):
        if getattr(_sys, name) is None:
            try:
                setattr(_sys, name, open(os.devnull, "w", encoding="utf-8",
                                         errors="ignore"))
            except Exception:
                pass


def refresh_data(codes=None):
    """拉取全部/指定 ETF 的实时快照 + 日K, 写库。返回 (ok_count, errors)"""
    ensure_stdio()
    try:
        import akshare as ak
    except ImportError as e:
        return 0, [f"行情数据接口库未安装: {e}"]

    etfs = load_etfs()
    errors, ok = [], 0
    today = date.today().isoformat()
    now = time.strftime("%Y-%m-%d %H:%M:%S")

    # 1) 全市场快照(一次接口)
    try:
        spot = ak.fund_etf_spot_em()
    except Exception as e:
        spot = None
        errors.append(f"行情接口失败: {type(e).__name__}: {e}")

    # 1.5) 按成交额门槛维护标的池(>=5亿纳入, <2亿剔除, 宽基与货币豁免)
    if spot is not None and not codes:
        try:
            from etf_meta import ensure_list
            pooled, chg = ensure_list(spot, str(BASE))
            if chg["added"] or chg["dropped"]:
                errors.append(f"[标的池] 新增{len(chg['added'])}只 "
                              f"剔除{len(chg['dropped'])}只, 当前{len(pooled)}只")
            etfs = load_etfs()
        except Exception as e:
            errors.append(f"标的池维护失败: {type(e).__name__}: {e}")

    if codes:
        etfs = [e for e in etfs if e["code"] in codes]
    codes = [e["code"] for e in etfs]
    # 当日没有场内行情的标的(新成立未上市/停牌)不必再去逐只拉日K, 省一半时间
    live = {str(x).zfill(6) for x in spot["代码"]} if spot is not None else None

    conn = get_db()
    if spot is not None:
        for etf in etfs:
            hit = spot[spot["代码"] == etf["code"]]
            if hit.empty:
                continue
            r = hit.iloc[0]
            close = _f(r.get("最新价"))
            if not close:
                continue
            iopv = _f(r.get("IOPV实时估值"))
            conn.execute(
                """INSERT OR REPLACE INTO snapshot VALUES
                   (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (today, etf["code"], str(r.get("名称", etf["name"])), close,
                 _f(r.get("涨跌幅")), round(_f(r.get("成交额")) / 1e4, 1),
                 _f(r.get("换手率")), _f(r.get("量比")), iopv,
                 round((close - iopv) / iopv * 100, 3) if iopv else None,
                 round(_f(r.get("主力净流入-净额")) / 1e4, 1),
                 round(_f(r.get("最新份额")) / 1e8, 2), brand.SOURCE_TAG, now))
            ok += 1
        conn.commit()

    # 2) 逐只日K
    end = today.replace("-", "")
    start = (datetime.now().replace(year=datetime.now().year - 2)).strftime("%Y%m%d")
    for etf in etfs:
        if live is not None and etf["code"] not in live:
            continue
        try:
            df = ak.fund_etf_hist_em(symbol=etf["code"], period="daily",
                                     start_date=start, end_date=end, adjust="")
            if df is None or df.empty:
                continue
            rows = []
            for t in df.tail(KLINE_DAYS).itertuples(index=False):
                d = str(getattr(t, "日期", getattr(t, "date", "")))
                rows.append((etf["code"], d, _f(t.开盘), _f(t.收盘),
                             _f(t.最高), _f(t.最低), _f(t.成交量), _f(t.成交额), None))
            if rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO kline VALUES (?,?,?,?,?,?,?,?,?)", rows)
                conn.commit()
        except Exception as e:
            errors.append(f"{etf['code']} 日K失败: {type(e).__name__}: {e}")
        time.sleep(0.4)
    conn.close()
    return ok, errors


# ------------------------------------------------------------ API
def api_etfs():
    etfs = load_etfs()
    conn = get_db()
    out = []
    for e in etfs:
        row = conn.execute(
            """SELECT trade_date, close, pct_chg, amount, turnover, vol_ratio,
                      iopv, premium, main_in, shares, fetched_at
               FROM snapshot WHERE code=? ORDER BY trade_date DESC LIMIT 1""",
            (e["code"],)).fetchone()
        days = conn.execute(
            "SELECT COUNT(*) FROM kline WHERE code=?", (e["code"],)).fetchone()[0]
        item = dict(e)
        item["kline_bars"] = days
        if row:
            item["snap"] = {"trade_date": row[0], "close": row[1], "pct_chg": row[2],
                            "amount": row[3], "turnover": row[4], "vol_ratio": row[5],
                            "iopv": row[6], "premium": row[7], "main_in": row[8],
                            "shares": row[9], "fetched_at": row[10]}
        else:
            item["snap"] = None
        out.append(item)
    conn.close()
    return out


def api_kline(code):
    conn = get_db()
    rows = conn.execute(
        """SELECT trade_date, open, close, high, low, volume, amount
           FROM kline WHERE code=? ORDER BY trade_date""", (code,)).fetchall()
    conn.close()
    return [{"date": r[0], "open": r[1], "close": r[2], "high": r[3],
             "low": r[4], "volume": r[5], "amount": r[6]} for r in rows]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静默访问日志
        pass

    def _send(self, obj, code=200, ctype="application/json; charset=utf-8"):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = urlparse(self.path).path
        if p in ("/", "/index.html"):
            html = (BASE / "dashboard.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
        elif p == "/api/etfs":
            self._send(api_etfs())
        elif p == "/api/kline":
            code = parse_qs(urlparse(self.path).query).get("code", [""])[0]
            self._send(api_kline(code))
        elif p == "/api/refresh":
            threading.Thread(target=refresh_data, daemon=True).start()
            self._send({"status": "refreshing"})
        elif p == "/api/status":
            conn = get_db()
            r = conn.execute("SELECT MAX(trade_date), MAX(fetched_at) FROM snapshot").fetchone()
            n = conn.execute("SELECT COUNT(DISTINCT code) FROM kline").fetchone()[0]
            conn.close()
            self._send({"trade_date": r[0], "fetched_at": r[1], "kline_codes": n})
        else:
            self._send({"error": "not found"}, 404)


def main():
    port = 8765
    for try_port in range(port, port + 10):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", try_port), Handler)
            port = try_port
            break
        except OSError:
            continue

    # 后台预刷新数据
    threading.Thread(target=refresh_data, daemon=True).start()

    url = f"http://127.0.0.1:{port}"
    print(f"ETF 跟踪面板已启动: {url}  (Ctrl+C 退出)")
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
