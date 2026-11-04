# -*- coding: utf-8 -*-
"""指数对照: 把宽基指数与跟踪它的 ETF 放在一起, 判断资金是不是真进场。

口径说明
--------
指数成交额  : 成分股二级市场成交额, 元 -> 亿元
ETF成交亿   : 该指数下所有跟踪 ETF 的成交额合计
占指数%     : ETF成交亿 / 指数成交亿, 反映这个指数的定价有多大程度依赖 ETF
ETF净流入亿 : 该指数下所有跟踪 ETF 的主力净流入合计(东财口径, 二级市场大单)
资金强度%   : ETF净流入亿 / ETF成交亿, 剔除规模差异后可横向比较
均溢价%     : 组内 ETF 溢价率均值, 偏离 0 说明场内价格与净值脱节

主力净流入是二级市场大单撮合, 不等于份额净申赎。指数涨而 ETF 净流出, 常见于
存量资金换手; 指数涨且 ETF 净流入, 才是场外资金借道进场。
"""
import json
import urllib.request

# (东财代码, secid, 指数名, 名称关键词) —— 关键词匹配有先后, 具体的放前面
IDX = [
    ("000016", "1.000016", "上证50",   ["上证50"]),
    ("000010", "1.000010", "上证180",  ["上证180"]),
    ("000300", "1.000300", "沪深300",  ["沪深300"]),
    ("000510", "1.000510", "中证A500", ["中证A500", "A500"]),
    ("000905", "1.000905", "中证500",  ["中证500"]),
    ("000852", "1.000852", "中证1000", ["中证1000"]),
    ("932000", "2.932000", "中证2000", ["中证2000"]),
    ("000688", "1.000688", "科创50",   ["科创50", "科创板50"]),
    ("000698", "1.000698", "科创100",  ["科创100"]),
    ("399006", "0.399006", "创业板指", ["创业板50", "创业板ETF", "创业板指"]),
    ("399330", "0.399330", "深证100",  ["深证100"]),
    ("000922", "1.000922", "中证红利", ["中证红利"]),
    ("000985", "1.000985", "中证全指", ["中证全指"]),
]

# 名称里带这些词就不是"纯宽基", 避免创业板人工智能被算进创业板指
EXCL = ("人工智能", "科技", "医药", "医疗", "新能源", "半导体", "芯片",
        "信息", "数字", "传媒", "游戏", "军工", "机器人", "碳中和")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
      "Referer": "https://quote.eastmoney.com/"}


def fetch_quotes(timeout=12):
    """一次请求取全部指数行情, 返回 {code: {pct, amt, name}}。失败抛异常。"""
    ids = ",".join(x[1] for x in IDX)
    url = ("https://push2.eastmoney.com/api/qt/ulist.np/get?secids=" + ids +
           "&fields=f2,f3,f6,f12,f14&fltt=2&invt=2")
    req = urllib.request.Request(url, headers=UA)
    d = json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())
    out = {}
    for it in (d.get("data") or {}).get("diff") or []:
        out[str(it["f12"])] = {"pct": it.get("f3"),
                               "amt": it.get("f6") or 0,
                               "name": it.get("f14")}
    if not out:
        raise RuntimeError("指数接口返回空")
    return out


def match_index(name):
    """按名称判断这只 ETF 跟踪哪个宽基指数, 认不出返回 None。"""
    for code, _secid, _iname, kws in IDX:
        for k in kws:
            if k in name:
                if k == "创业板" and any(e in name for e in EXCL):
                    continue
                return code
    return None


def build(rows, quotes=None):
    """rows: [(code, name, pct, amount万, main_in万, premium, shares亿)]
    返回 (groups, others)
    groups: [{code,name,pct,iamt,n,eamt,emi,ratio,strength,avgp,members,tag}]
    """
    quotes = quotes or {}
    buckets, others = {}, []
    for r in rows:
        if isinstance(r, dict):
            code, name, pct = r.get("code"), r.get("name"), r.get("pct")
            amt, mi, prem, shares = (r.get("amount"), r.get("main_in"),
                                     r.get("premium"), r.get("shares"))
        else:
            code, name, pct, amt, mi, prem = r[0], r[1], r[2], r[3], r[4], r[5]
            shares = r[6] if len(r) > 6 else None
        amt = float(amt or 0) / 1e4                     # 万元 -> 亿
        mi = float(mi) / 1e4 if mi is not None else None  # 万元 -> 亿
        item = {"code": code, "name": name, "pct": pct, "amt": amt,
                "mi": mi, "prem": prem, "shares": shares,
                "turnover": r.get("turnover") if isinstance(r, dict) else None,
                "vol_ratio": r.get("vol_ratio") if isinstance(r, dict) else None}
        k = match_index(name)
        if k:
            buckets.setdefault(k, []).append(item)
        else:
            others.append(item)

    groups = []
    for code, _secid, iname, _kws in IDX:
        g = buckets.get(code) or []
        if not g:
            continue
        q = quotes.get(code) or {}
        ipct = q.get("pct")
        iamt = (q.get("amt") or 0) / 1e8
        eamt = sum(x["amt"] for x in g)
        emi = sum(x["mi"] for x in g if x["mi"] is not None)
        prems = [x["prem"] for x in g if x["prem"] is not None]
        groups.append({
            "code": code, "name": iname, "pct": ipct, "iamt": iamt,
            "n": len(g), "eamt": eamt, "emi": emi,
            "ratio": (eamt / iamt * 100) if iamt else None,
            "strength": (emi / eamt * 100) if eamt else None,
            "avgp": (sum(prems) / len(prems)) if prems else None,
            "members": sorted(g, key=lambda x: -(x["mi"] if x["mi"] is not None else -9e9)),
            "tag": verdict(ipct, emi),
        })
    return groups, others


def verdict(ipct, emi):
    if ipct is None:
        return "无指数行情"
    if ipct > 1 and emi > 0:
        return "同向进场: 指数涨 + ETF 净流入"
    if ipct > 1 and emi <= 0:
        return "背离: 指数涨但 ETF 净流出"
    if ipct < -1 and emi > 0:
        return "逆向: 指数跌但 ETF 在买"
    if ipct < -1 and emi <= 0:
        return "同步撤离: 指数跌 + ETF 流出"
    return "横盘: 方向不明"


def totals(groups, others):
    eamt = sum(g["eamt"] for g in groups) + sum(x["amt"] for x in others)
    emi = sum(g["emi"] for g in groups) + sum(x["mi"] for x in others if x["mi"] is not None)
    return eamt, emi
