# -*- coding: utf-8 -*-
"""进场时点: 宽基指数 x ETF 资金 x 股指期货基差 x 期权 PCR, 四维对账。

四个维度回答同一个问题 —— 钱是在进还是在出, 是现在进还是早就进完了:

1. ETF 二级市场资金(STrength): 跟踪该指数的 ETF 主力净流入 / ETF 成交额。
2. ETF 五日资金动能(Mom5): 用 K 线的 CLV 加权成交额做代理, 5 日有符号累计。
   份额申赎数据拿不到历史, 这是二级市场口径的量价代理, 只能看方向强度。
3. 股指期货基差(basis): 主力合约收盘 - 现货指数收盘, 除以现货。
   贴水收窄说明对冲盘撤退或投机多头进场; 贴水加深相反。
4. 期权 PCR 与平值隐波: 认沽持仓 / 认购持仓。PCR 高位回落是恐慌释放,
   PCR 极低是拥挤。隐波由 BS 反解, 无风险利率取 1.8%。

期货与期权的映射关系(中金所):
    IH -> 上证50      HO 期权
    IF -> 沪深300     IO 期权
    IC -> 中证500     无场内期权
    IM -> 中证1000    MO 期权

注意: 主力合约会换月, 基差序列在换月日有跳空, 所以基差比较用"当日值 vs 近 20 日
均值"的偏离, 而不是简单差分。
"""
import json
import math
import urllib.request

from etf_meta import looks_qdii

try:                                   # 窗口程序没有控制台, 进度条一写就崩
    from etf_app import ensure_stdio as _ensure_stdio
    _ensure_stdio()
except Exception:
    pass

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
      "Referer": "https://quote.eastmoney.com/"}
RF = 0.018          # 无风险利率, BS 反解用
BASIS_WIN = 20      # 基差均值窗口


class Idx(object):
    __slots__ = ("code", "secid", "name", "kws", "future", "option")

    def __init__(self, code, secid, name, kws, future=None, option=None):
        self.code, self.secid, self.name = code, secid, name
        self.kws, self.future, self.option = kws, future, option


# 用户点名的核心指数排前面: 上证 / 深证 / 沪深300 / 科创 / 创业板 / 北证 / 中证
IDX = [
    Idx("000001", "1.000001", "上证指数", ["上证指数", "上证综指"]),
    Idx("399001", "0.399001", "深证成指", ["深证成指"]),
    Idx("000300", "1.000300", "沪深300", ["沪深300"], "IF", "IO"),
    Idx("000016", "1.000016", "上证50", ["上证50"], "IH", "HO"),
    Idx("000688", "1.000688", "科创50", ["科创50", "科创板50"]),
    Idx("000698", "1.000698", "科创100", ["科创100"]),
    Idx("000680", "1.000680", "科创综指", ["科创综指", "科创板综指"]),
    Idx("399006", "0.399006", "创业板指", ["创业板50", "创业板ETF", "创业板指"]),
    Idx("899050", "0.899050", "北证50", ["北证50"]),
    Idx("000905", "1.000905", "中证500", ["中证500"], "IC", None),
    Idx("000852", "1.000852", "中证1000", ["中证1000"], "IM", "MO"),
    Idx("932000", "2.932000", "中证2000", ["中证2000"]),
    Idx("000510", "1.000510", "中证A500", ["中证A500", "A500"]),
    # ---- 跨境指数: QDII 的进场时点要对着它跟踪的那个市场看, 不能拿 A 股指数替代
    #      code 必须与东财 f12 返回值一致(美股/港股返回的是字母代码), 否则报价取不到
    Idx("NDX", "100.NDX", "纳斯达克", ["纳指", "纳斯达克"]),
    Idx("SPX", "100.SPX", "标普500", ["标普500"]),
    Idx("N225", "100.N225", "日经225", ["日经"]),
    Idx("HSTECH", "124.HSTECH", "恒生科技", ["恒生科技"]),
    Idx("HSI", "100.HSI", "恒生指数", ["恒生ETF", "恒指"]),
]

EXCL = ("人工智能", "科技", "医药", "医疗", "新能源", "半导体", "芯片",
        "信息", "数字", "传媒", "游戏", "军工", "机器人", "碳中和",
        "光伏", "酒", "证券", "银行", "有色", "稀土", "电池", "汽车",
        "消费", "地产", "基建", "央企", "国企", "红利低波", "价值", "成长")

# 跨境指数: UI 里用蓝字区分, 它们的涨跌幅跟着隔夜外盘走, 不能和 A 股指数混着读
CROSS_IDX = {"NDX", "SPX", "N225", "HSTECH", "HSI"}

FUT = {"IH": "上证50", "IF": "沪深300", "IC": "中证500", "IM": "中证1000"}
OPT = {"HO": ("上证50", "option_cffex_sz50"),
       "IO": ("沪深300", "option_cffex_hs300"),
       "MO": ("中证1000", "option_cffex_zz1000")}


# ---------------------------------------------------------------- 指数
def _num(v):
    """东财对海外指数的成交额字段返回 '-', 直接参与运算会炸, 统一转成数字或 None"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_idx_quotes(timeout=12):
    """{f12: {pct, close, amt, name}}"""
    ids = ",".join(x.secid for x in IDX)
    url = ("https://push2.eastmoney.com/api/qt/ulist.np/get?secids=" + ids +
           "&fields=f2,f3,f6,f12,f14&fltt=2&invt=2")
    d = json.loads(urllib.request.urlopen(
        urllib.request.Request(url, headers=UA), timeout=timeout).read().decode())
    out = {}
    for it in (d.get("data") or {}).get("diff") or []:
        out[str(it["f12"])] = {"pct": _num(it.get("f3")),
                               "close": _num(it.get("f2")),
                               "amt": _num(it.get("f6")) or 0.0,
                               "name": it.get("f14")}
    if not out:
        raise RuntimeError("指数行情返回空")
    return out


def fetch_idx_kline(secid, lmt=420, timeout=15):
    """[(date, close, high, low, amount亿, open, volume)] 升序

    末尾多带开盘价与成交量, 供进出页画蜡烛; 前五位语义不变,
    原来按 k[1]=收盘 / k[4]=成交额 取值的调用不受影响。
    """
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=" + secid +
           "&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57&klt=101&fqt=1&lmt=" +
           str(lmt) + "&end=20500101")
    d = json.loads(urllib.request.urlopen(
        urllib.request.Request(url, headers=UA), timeout=timeout).read().decode())
    out = []
    for s in ((d.get("data") or {}).get("klines") or []):
        p = s.split(",")
        try:
            close = float(p[2])
            hi, lo = float(p[3]), float(p[4])
            op = float(p[1]) if len(p) > 1 and p[1] not in ("", "-") else close
            vol = float(p[5]) if len(p) > 5 and p[5] not in ("", "-") else 0.0
            amt = float(p[6]) / 1e8 if len(p) > 6 and p[6] else 0.0
        except (ValueError, IndexError):
            continue
        out.append((p[0], close, hi, lo, amt, op, vol))
    return out


# ---------------------------------------------------------------- 期货
def fetch_futures():
    """{IH: {...}} 主力连续, 含基差。公开接口给的即持仓最大合约。"""
    import akshare as ak
    out = {}
    for sym in FUT:
        try:
            df = ak.futures_main_sina(symbol=sym + "0")
        except Exception as e:
            out[sym] = {"err": "%s: %s" % (type(e).__name__, e)}
            continue
        if df is None or df.empty:
            out[sym] = {"err": "空数据"}
            continue
        df = df.tail(40)
        close = [float(x) for x in df["收盘价"].tolist()]
        hold = [float(x) for x in df["持仓量"].tolist()]
        vol = [float(x) for x in df["成交量"].tolist()]
        date = [str(x) for x in df["日期"].tolist()]
        out[sym] = {"date": date[-1], "close": close[-1],
                    "chg": (close[-1] / close[-2] - 1) * 100 if len(close) > 1 else None,
                    "hold": hold[-1],
                    "oi_chg": (hold[-1] / hold[-2] - 1) * 100 if len(hold) > 1 and hold[-2] else None,
                    "vol": vol[-1],
                    "vr": vol[-1] / (sum(vol[-6:-1]) / 5) if len(vol) > 6 else None,
                    "hist": list(zip(date, close))}
    return out


def add_basis(fut, idx_close_hist):
    """把主力收盘与现货对齐, 算基差率与 20 日均值偏离。
    idx_close_hist: {date: close}
    """
    for sym, f in fut.items():
        if f.get("err"):
            continue
        ser = []
        for d, c in f["hist"]:
            s = idx_close_hist.get(d)
            if s:
                ser.append((d, (c - s) / s * 100))
        if not ser:
            f["basis"] = None
            continue
        cur = ser[-1][1]
        prev = [x[1] for x in ser[-BASIS_WIN - 1:-1]]
        ma = sum(prev) / len(prev) if prev else None
        f["basis"] = cur
        f["basis_ma"] = ma
        f["basis_dev"] = (cur - ma) if ma is not None else None
        f["basis_ser"] = ser[-BASIS_WIN:]
    return fut


# ---------------------------------------------------------------- 期权
def _third_friday(ym):
    """'2611' -> (year, month, day) 第三个周五"""
    y, m = 2000 + int(ym[:2]), int(ym[2:])
    import datetime
    d = datetime.date(y, m, 1)
    fri = []
    for i in range(1, 32):
        try:
            x = d.replace(day=i)
        except ValueError:
            break
        if x.month == m and x.weekday() == 4:
            fri.append(x)
    return fri[2] if len(fri) >= 3 else None


def _nd(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _bs(S, K, T, sig, call=True):
    if T <= 0 or sig <= 0:
        return max(S - K, 0) if call else max(K - S, 0)
    d1 = (math.log(S / K) + (RF + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    if call:
        return S * _nd(d1) - K * math.exp(-RF * T) * _nd(d2)
    return K * math.exp(-RF * T) * _nd(-d2) - S * _nd(-d1)


def implied_vol(price, S, K, T, call=True):
    if not price or price <= 0 or T <= 0:
        return None
    lo, hi = 1e-4, 3.0
    if _bs(S, K, T, hi, call) < price:
        return None
    for _ in range(40):
        mid = (lo + hi) / 2
        if _bs(S, K, T, mid, call) < price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2 * 100


def fetch_option(prod, spot, timeout=12):
    """中金所期权 T 型报价 -> PCR + 平值隐波"""
    import akshare as ak
    import datetime
    fn = {"HO": "option_cffex_sz50_spot_sina",
          "IO": "option_cffex_hs300_spot_sina",
          "MO": "option_cffex_zz1000_spot_sina"}[prod]
    lst = {"HO": "option_cffex_sz50_list_sina",
           "IO": "option_cffex_hs300_list_sina",
           "MO": "option_cffex_zz1000_list_sina"}[prod]
    try:
        near = list(getattr(ak, lst)().values())[0][0]
        df = getattr(ak, fn)(symbol=near)
    except Exception as e:
        return {"prod": prod, "err": type(e).__name__}
    if df is None or df.empty:
        return {"prod": prod, "err": "空数据"}
    ci, pi = df["看涨合约-持仓量"].tolist(), df["看跌合约-持仓量"].tolist()
    c_last, p_last = df["看涨合约-最新价"].tolist(), df["看跌合约-最新价"].tolist()
    ks = [float(x) for x in df["行权价"].tolist()]
    call_oi = sum(float(x or 0) for x in ci)
    put_oi = sum(float(x or 0) for x in pi)
    exp = _third_friday(near[2:6])
    T = max((exp - datetime.date.today()).days, 1) / 365.0 if exp else 0
    # 平值: 取离现价最近的两个执行价, 认购认沽各算一次, 取均值
    order = sorted(range(len(ks)), key=lambda i: abs(ks[i] - spot))[:2]
    ivs = []
    for i in order:
        for last, is_call in ((c_last[i], True), (p_last[i], False)):
            v = implied_vol(float(last or 0), spot, ks[i], T, is_call)
            if v:
                ivs.append(v)
    iv = sum(ivs) / len(ivs) if ivs else None
    # 近月平值上下 5% 区间内的 PCR, 比全合约更敏感
    near_oi_p = sum(float(pi[i] or 0) for i in range(len(ks)) if abs(ks[i] - spot) / spot < 0.05)
    near_oi_c = sum(float(ci[i] or 0) for i in range(len(ks)) if abs(ks[i] - spot) / spot < 0.05)
    return {"prod": prod, "exp": near, "exp_date": exp.isoformat() if exp else None,
            "pcr": put_oi / call_oi if call_oi else None,
            "pcr_near": near_oi_p / near_oi_c if near_oi_c else None,
            "call_oi": call_oi, "put_oi": put_oi,
            "iv": iv, "n": len(ks), "spot": spot}


# ---------------------------------------------------------------- ETF 侧
def match_index(name):
    for x in IDX:
        for k in x.kws:
            if k in name:
                if k == "创业板" and any(e in name for e in EXCL):
                    continue
                return x.code
    return None


def etf_groups(rows):
    """rows: analyzer.scores 的 list[dict] -> {idx_code: [items]}"""
    out = {}
    for r in rows:
        k = match_index(r.get("name") or "")
        if not k:
            continue
        amt = float(r.get("amount") or 0) / 1e4          # 万元 -> 亿
        mi = r.get("main_in")
        mi = float(mi) / 1e4 if mi is not None else None
        out.setdefault(k, []).append({
            "code": r.get("code"), "name": r.get("name"), "pct": r.get("pct"),
            "cat": r.get("cat"), "qdii": looks_qdii(r.get("name")),
            "amt": amt, "mi": mi, "prem": r.get("premium"),
            "shares": r.get("shares"), "turnover": r.get("turnover"),
            "vol_ratio": r.get("vol_ratio"), "mom5": r.get("_mom5"),
            "amt5": r.get("_amt5"), "mom_ser": r.get("_mom_ser")})
    return out


def load_mom(db_path, codes, days=260):
    """从本地 K 线算 5 日 CLV 加权资金流(亿元)序列与 5 日成交额。

    CLV = (2*close-high-low)/(high-low), 乘成交额后再滚动 5 日累加, 是二级市场
    口径的有符号资金强度代理, 不等于份额申赎。返回 {code: (ser, amt5)}, 
    ser = [(date, 5日累计亿)]。
    """
    import sqlite3
    out = {}
    try:
        con = sqlite3.connect(db_path)
    except Exception:
        return out
    try:
        for c in codes:
            rs = con.execute(
                "select trade_date,high,low,close,amount from kline where code=? "
                "order by trade_date desc limit ?", (c, days)).fetchall()
            if len(rs) < 6:
                continue
            rs.reverse()
            vals, amts = [], []
            for d, h, l, cl, am in rs:
                if not h or not l or not cl or not am or float(h) <= float(l):
                    vals.append((d, 0.0))
                    amts.append(0.0)
                    continue
                h, l, cl, am = float(h), float(l), float(cl), float(am)
                vals.append((d, am * (2 * cl - h - l) / (h - l) / 1e8))
                amts.append(am / 1e8)
            ser = []
            for i in range(4, len(vals)):
                ser.append((vals[i][0], round(sum(v[1] for v in vals[i - 4:i + 1]), 2)))
            if ser:
                out[c] = (ser, sum(amts[-5:]))
    finally:
        con.close()
    return out


def etf_klines(db_path, codes, n=250):
    """单只 ETF 的日线, 供页签里点选某只后单独画图。
    {code: [(date, close, high, low, amount亿, open, volume)]} 升序
    """
    import sqlite3
    out = {}
    try:
        con = sqlite3.connect(db_path)
    except Exception:
        return out
    try:
        for c in codes:
            rs = con.execute(
                "select trade_date,close,high,low,amount,open,volume from kline "
                "where code=? order by trade_date desc limit ?", (c, n)).fetchall()
            rs.reverse()
            ser = []
            for row in rs:
                d, cl, h, l, am = row[0], row[1], row[2], row[3], row[4]
                op = row[5] if len(row) > 5 else None
                vol = row[6] if len(row) > 6 else None
                if not cl:
                    continue
                cl = float(cl)
                ser.append((d, cl,
                            float(h) if h else cl,
                            float(l) if l else cl,
                            (float(am) / 1e8) if am else 0.0,
                            float(op) if op else cl,
                            float(vol) if vol else 0.0))
            out[c] = ser
    finally:
        con.close()
    return out


# ---------------------------------------------------------------- 评分
def _clip(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


def score(r):
    """四维加权, 50 为中性; 缺失维度按已具备权重归一化, 不让缺口把分数压低。
    返回 (score, parts), parts 为 [(维度, 因子 -1..1, 权重)]。
    """
    parts = []
    tot = [0.0, 0.0]

    def add(name, f, w):
        if w <= 0:
            return
        parts.append((name, f, w))
        tot[0] += w
        tot[1] += w * f

    # 1 当日 ETF 资金强度: 净流入 / ETF 成交额。
    #   绝对额也要过关, 一只小 ETF 几千万进出就能做出高比例, 那是噪声不是信号。
    st, eamt, emi, n = r.get("strength"), r.get("eamt") or 0, r.get("emi"), r.get("n") or 0
    if st is not None:
        w = 24.0
        w *= min(1.0, abs(emi or 0) / 2.0)          # 绝对额门槛: 2 亿封顶
        w *= 1.0 if n >= 2 else 0.6                 # 单只 ETF 代表性弱
        if eamt < 5:
            w *= max(0.3, eamt / 5.0)
        add("ETF净流入", _clip(st / 6.0), w)
    # 2 五日资金动能: CLV 加权成交额的 5 日累计, 相对同期成交额
    mo, a5 = r.get("mom5"), r.get("amt5")
    if mo is not None and a5:
        add("5日动能", _clip(mo / (a5 * 0.8)), 14 * min(1.0, a5 / 30.0))
    # 3 基差偏离 20 日均值: 贴水修复为正
    dv = r.get("basis_dev")
    if dv is not None:
        add("基差修复", _clip(dv / 0.5), 20)
    # 4 持仓与价格方向: 增仓上涨新多进场最强, 减仓上涨为空头回补
    oc, pc = r.get("oi_chg"), r.get("pct")
    if oc is not None and pc is not None:
        up = pc >= 0
        f = (1.0 if up else -1.0) if oc >= 0 else (0.3 if up else -0.3)
        add("增仓方向", f * min(1.0, abs(oc) / 4.0), 15)
    # 5 期权 PCR: 1.0 中性, 越高越防御
    pcr = r.get("pcr")
    if pcr is not None:
        add("期权PCR", _clip((1.0 - pcr) / 0.6), 15)
    # 6 现货量能: 成交额相对 5 日均值的放大倍数, 与方向结合
    vr = r.get("vol_ratio_idx")
    if vr is not None and pc is not None:
        add("现货量能", _clip((vr - 1.0) / 0.35) * (1 if pc >= 0 else -1), 10)
    # 7 趋势位置: 站上 MA20 + 处在 20 日区间上部
    ma, pos, pc0 = r.get("ma_dev"), r.get("pos"), r.get("pct")
    if ma is not None:
        f = _clip(ma / 5.0) * 0.5
        if pos is not None:
            f += _clip((pos - 50) / 50.0) * 0.3
        if pc0 is not None:
            f += _clip(pc0 / 3.0) * 0.2
        add("趋势位置", _clip(f), 12)
    if not tot[0]:
        return 50.0, parts
    # 维度越全, 交叉验证越充分, 分数越敢给; 只有一两个维度的指数压低幅度
    conf = 0.55 + 0.45 * min(1.0, tot[0] / 100.0)
    sc = 50.0 + (tot[1] / tot[0]) * 50.0 * conf
    return round(max(0.0, min(100.0, sc)), 1), parts


def state(sc):
    if sc >= 70:
        return "进场确认"
    if sc >= 58:
        return "启动中"
    if sc >= 44:
        return "观望"
    if sc >= 34:
        return "退潮"
    return "撤离"


def reason(r, parts):
    """把四维读数翻译成一句人话, 优先说资金和背离。"""
    seg = []
    emi, st, n = r.get("emi"), r.get("strength"), r.get("n") or 0
    mo = r.get("mom5")
    bd, oc = r.get("basis_dev"), r.get("oi_chg")
    pcr = r.get("pcr")
    if emi is not None and abs(emi) >= 0.5:
        if n:
            seg.append("ETF %+.1f亿(%+.1f%%)" % (emi, st or 0))
        else:
            seg.append("ETF %+.1f亿" % emi)
    if bd is not None and oc is not None:
        f = "贴水收窄" if bd > 0.05 else ("贴水加深" if bd < -0.05 else "基差平")
        seg.append("%s %+.2fpp, 持仓%+.1f%%" % (f, bd, oc))
    if pcr is not None:
        seg.append("PCR %.2f%s" % (pcr, "(防御)" if pcr > 1.0 else ""))
    if not seg:
        if r.get("ma_dev") is not None:
            seg.append("无ETF样本, 仅看指数: MA20 %+.1f%%" % r["ma_dev"])
        else:
            seg.append("数据不足")
    # 背离提示: ETF 与期货方向相反
    if emi is not None and bd is not None:
        if emi < -0.5 and bd > 0.1:
            seg.append("→ 背离: ETF流出但贴水修复, 期货资金先动")
        elif emi > 0.5 and bd < -0.1:
            seg.append("→ 背离: ETF流入但贴水加深, 对冲盘在压")
    return ", ".join(seg)


# ---------------------------------------------------------------- 汇总
def build(rows, idx_klines, fut, opt, quotes):
    """rows: analyzer.scores(list[dict]); idx_klines: {code: [(date,close,hi,lo)]}
    fut: fetch_futures()+add_basis 的结果; opt: {prod: dict}
    返回 list[dict], 每项一个指数。
    """
    groups = etf_groups(rows)
    out = []
    for x in IDX:
        g = groups.get(x.code) or []
        q = quotes.get(x.code) or {}
        kl = idx_klines.get(x.code) or []
        closes = [k[1] for k in kl]
        close = q.get("close") or (closes[-1] if closes else None)
        pct = q.get("pct")
        ma20 = sum(closes[-20:]) / len(closes[-20:]) if len(closes) >= 20 else None
        hi = max(k[2] for k in kl[-20:]) if kl else None
        lo = min(k[3] for k in kl[-20:]) if kl else None
        pos = ((close - lo) / (hi - lo) * 100) if (hi and lo and hi > lo) else None
        pct5 = ((close / closes[-6] - 1) * 100) if len(closes) >= 6 else None
        amts = [k[4] for k in kl if len(k) > 4 and k[4]]
        vr_idx = None
        if amts and len(amts) >= 6:
            base = sum(amts[-6:-1]) / 5.0
            vr_idx = (amts[-1] / base) if base else None

        eamt = sum(i["amt"] for i in g)
        mis = [i["mi"] for i in g if i["mi"] is not None]
        emi = sum(mis) if mis else None
        prems = [i["prem"] for i in g if i["prem"] is not None]
        moms = [i["mom5"] for i in g if i.get("mom5") is not None]
        mo5 = sum(moms) if moms else None
        agg = {}
        for i in g:
            for d, v in (i.get("mom_ser") or []):
                agg[d] = agg.get(d, 0.0) + v
        mom_ser = sorted(agg.items())[-260:] if agg else []
        a5 = sum(i["amt5"] or 0 for i in g if i.get("amt5"))

        f = fut.get(x.future) if x.future else None
        o = opt.get(x.option) if x.option else None
        r = {
            "code": x.code, "name": x.name, "secid": x.secid,
            "pct": pct, "pct5": pct5, "pos": pos, "vol_ratio_idx": vr_idx,
            "ma_dev": ((close / ma20 - 1) * 100) if (close and ma20) else None,
            "close": close, "iamt": (q.get("amt") or 0) / 1e8,
            "n": len(g), "eamt": eamt, "emi": emi,
            "ratio": (eamt / ((q.get("amt") or 0) / 1e8) * 100) if q.get("amt") else None,
            "strength": (emi / eamt * 100) if (emi is not None and eamt) else None,
            "avgp": (sum(prems) / len(prems)) if prems else None,
            "mom5": mo5, "amt5": a5, "mom_ser": mom_ser,
            "basis_ser": (f.get("basis_ser") if f and not f.get("err") else None),
            "future": x.future, "option": x.option,
            "basis": f.get("basis") if f and not f.get("err") else None,
            "basis_dev": f.get("basis_dev") if f and not f.get("err") else None,
            "basis_ma": f.get("basis_ma") if f and not f.get("err") else None,
            "oi_chg": f.get("oi_chg") if f and not f.get("err") else None,
            "fut_chg": f.get("chg") if f and not f.get("err") else None,
            "fut_hold": f.get("hold") if f and not f.get("err") else None,
            "pcr": o.get("pcr") if o and not o.get("err") else None,
            "pcr_near": o.get("pcr_near") if o and not o.get("err") else None,
            "iv": o.get("iv") if o and not o.get("err") else None,
            "members": sorted(g, key=lambda i: -(i["mi"] if i["mi"] is not None else -9e9)),
            "kline": kl[-420:],
        }
        sc, parts = score(r)
        r["score"], r["parts"] = sc, parts
        r["state"] = state(sc)
        r["why"] = reason(r, parts)
        out.append(r)
    out.sort(key=lambda z: -z["score"])
    # 跨境指数统一排到 A 股之后: 两类看的是不同市场的开盘时间, 混在一列容易读串
    out.sort(key=lambda z: z["code"] in CROSS_IDX)
    return out


# ---------------------------------------------------------------- 装配
def snapshot(rows, db_path, log=None):
    """一次性取齐四维数据。返回 dict:
    {rows, fut, opt, quotes, err, date}
    """
    import concurrent.futures as cf
    log = log or (lambda *_: None)
    err = []

    log("取指数行情…")
    try:
        quotes = fetch_idx_quotes()
    except Exception as e:
        quotes, _ = {}, err.append("指数行情 " + type(e).__name__)

    log("取指数日线…")
    klines = {}

    def _one(x):
        try:
            return x.code, fetch_idx_kline(x.secid)
        except Exception:
            return x.code, []

    try:
        with cf.ThreadPoolExecutor(max_workers=6) as ex:
            for code, kl in ex.map(_one, IDX):
                klines[code] = kl
    except Exception as e:
        err.append("指数日线 " + type(e).__name__)

    log("取股指期货…")
    try:
        fut = fetch_futures()
    except Exception as e:
        fut = {}
        err.append("期货 %s: %s" % (type(e).__name__, e))
    fmap = {"IH": "000016", "IF": "000300", "IC": "000905", "IM": "000852"}
    # 逐个补基差
    for sym, code in fmap.items():
        f = fut.get(sym)
        if not f or f.get("err"):
            continue
        hist = {k[0]: k[1] for k in klines.get(code) or []}
        ser = []
        for d, c in f["hist"]:
            s = hist.get(d)
            if s:
                ser.append((d, (c - s) / s * 100))
        if ser:
            cur = ser[-1][1]
            prev = [v[1] for v in ser[-BASIS_WIN - 1:-1]]
            ma = sum(prev) / len(prev) if prev else None
            f["basis"] = cur
            f["basis_ma"] = ma
            f["basis_dev"] = (cur - ma) if ma is not None else None
            f["basis_ser"] = ser[-BASIS_WIN:]

    log("取期权…")
    opt = {}
    for prod, (iname, _) in OPT.items():
        code = {"上证50": "000016", "沪深300": "000300", "中证1000": "000852"}[iname]
        spot = (quotes.get(code) or {}).get("close")
        if not spot:
            kl = klines.get(code) or []
            spot = kl[-1][1] if kl else None
        if not spot:
            opt[prod] = {"prod": prod, "err": "无标的价"}
            continue
        try:
            r = fetch_option(prod, spot)
        except Exception as e:
            r = {"prod": prod, "err": type(e).__name__}
        opt[prod] = r
        log("期权 %s · PCR %s · IV %s" % (
            prod, "—" if r.get("pcr") is None else round(r["pcr"], 2),
            "—" if r.get("iv") is None else round(r["iv"], 1)))

    mom = load_mom(db_path, [r.get("code") for r in rows if r.get("code")])
    for r in rows:
        v = mom.get(r.get("code"))
        if v:
            r["_mom_ser"], r["_amt5"] = v
            r["_mom5"] = v[0][-1][1] if v[0] else None

    out = build(rows, klines, fut, opt, quotes)
    date = None
    for x in IDX:
        kl = klines.get(x.code) or []
        if kl:
            date = kl[-1][0]
            break
    return {"rows": out, "fut": fut, "opt": opt, "quotes": quotes,
            "err": err, "date": date}
