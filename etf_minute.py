# -*- coding: utf-8 -*-
"""ETF 分时与分钟K线取数。

- 分时(period="1"): 接口自带「均价」列, 直接画均价线, 取最近一个交易日
- 5/15/30/60 分钟: 无均价列, 用成交额/(成交量*100) 自行还原
- 进程内缓存, 同一标的同一周期在 TTL 内不重复请求, 切来切去不卡
"""
import sqlite3
import time

import numpy as np

# 分时变动快, TTL 短一点; 60 分钟线一天才 4 根, 缓存久一些
_TTL = {"1": 45, "5": 90, "15": 150, "30": 240, "60": 300}
_CACHE = {}


def _f(v, d=0.0):
    try:
        x = float(v)
        return x if x == x else d
    except Exception:
        return d


def clear_cache(code=None):
    if code is None:
        _CACHE.clear()
        return
    for k in [k for k in _CACHE if k[0] == code]:
        _CACHE.pop(k, None)


def prev_close(code, db, before_date):
    """分时基准线用的昨收。取不到返回 None(图上不画基准)。"""
    try:
        conn = sqlite3.connect(str(db))
        r = conn.execute(
            """SELECT close FROM kline WHERE code=? AND trade_date<?
               ORDER BY trade_date DESC LIMIT 1""",
            (code, before_date)).fetchone()
        conn.close()
        return float(r[0]) if r and r[0] else None
    except Exception:
        return None


def fetch(code, period="1", db=None):
    """返回 dict(times, o, c, h, l, v, avg, prev_close, date, updated)。

    取不到抛异常, 由调用方决定怎么提示。
    """
    period = str(period)
    key = (code, period)
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _TTL.get(period, 90):
        return hit[1]

    import akshare as ak
    df = ak.fund_etf_hist_min_em(symbol=code, period=period, adjust="")
    if df is None or len(df) == 0:
        raise RuntimeError("该标的暂无分钟行情")

    tcol = "时间" if "时间" in df.columns else df.columns[0]
    times = [str(x) for x in df[tcol].tolist()]
    o = np.array([_f(x) for x in df["开盘"]], float)
    c = np.array([_f(x) for x in df["收盘"]], float)
    h = np.array([_f(x) for x in df["最高"]], float)
    l = np.array([_f(x) for x in df["最低"]], float)
    v = np.array([_f(x) for x in df["成交量"]], float)
    amt = np.array([_f(x) for x in df["成交额"]], float) \
        if "成交额" in df.columns else np.zeros(len(c))

    avg = None
    if "均价" in df.columns:
        avg = np.array([_f(x) for x in df["均价"]], float)

    day = times[-1][:10]
    if period == "1":
        # 接口一次给 5 个交易日, 分时只画最后一个交易日
        keep = [i for i, t in enumerate(times) if t[:10] == day]
        times = [times[i] for i in keep]
        o, c, h, l, v = o[keep], c[keep], h[keep], l[keep], v[keep]
        amt = amt[keep]
        if avg is not None:
            avg = avg[keep]
        # 集合竞价那一根的开盘价常是 0, 会画出一条到地上的影线, 用收盘价顶掉
        o = np.where(o <= 0, c, o)
        h = np.maximum.reduce([h, o, c])
        l = np.minimum.reduce([np.where(l <= 0, c, l), o, c])
        # 均价缺失时用 VWAP 兜底(成交量单位是手)
        if avg is None or not np.isfinite(avg).any() or (avg <= 0).all():
            cum_v = np.cumsum(v) * 100
            avg = np.where(cum_v > 0, np.cumsum(amt) / np.maximum(cum_v, 1), c)
    else:
        n = 400
        times, o, c, h, l, v = times[-n:], o[-n:], c[-n:], h[-n:], l[-n:], v[-n:]
        avg = None

    pc = prev_close(code, db, day) if db is not None else None
    if pc is None and period == "1" and len(c):
        pc = float(c[0])

    data = dict(times=times, o=o, c=c, h=h, l=l, v=v, avg=avg,
                prev_close=pc, date=day, period=period,
                updated=time.strftime("%H:%M:%S"))
    _CACHE[key] = (time.time(), data)
    return data


def resample(rows, mode):
    """日K行 (date, o, c, h, l, v, amount) 合成周/月K。

    分组键用 ISO 周与自然月, 首日开盘、末日收盘、区间最高最低、成交量求和。
    """
    if not rows:
        return []
    import datetime as _dt

    def _key(d):
        if mode != "W":
            return d[:7]
        try:
            y, w, _ = _dt.date(*[int(x) for x in d[:10].split("-")]).isocalendar()
            return "%04d-W%02d" % (y, w)
        except Exception:
            return d

    out, cur, key = [], None, None
    for r in rows:
        d = str(r[0])
        k = _key(d)
        if k != key:
            if cur:
                out.append(cur)
            key = k
            cur = [d, r[1], r[2], r[3], r[4], r[5], r[6]]
        else:
            cur[0] = d
            cur[2] = r[2]
            cur[3] = max(cur[3], r[3])
            cur[4] = min(cur[4], r[4])
            cur[5] += r[5]
            cur[6] += r[6]
    if cur:
        out.append(cur)
    return [tuple(x) for x in out]
