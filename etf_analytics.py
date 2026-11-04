# -*- coding: utf-8 -*-
"""
ETF 量化分析引擎 (纯 numpy/pandas, 与界面解耦)
- 指标: MA/BOLL/MACD/RSI/KDJ/ATR/CCI/威廉%R + 收益风险
- 分析: 四因子量化评分 / 收益率相关性矩阵 / 风险收益散点 / 板块轮动
"""
import sqlite3
import sys
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
from etf_app import get_db, load_etfs, refresh_data  # noqa: E402,F401

# ============================================================ 基础指标
def sma(a, n):
    out = np.full(len(a), np.nan)
    if len(a) >= n:
        c = np.cumsum(np.insert(np.asarray(a, float), 0, 0))
        out[n-1:] = (c[n:] - c[:-n]) / n
    return out

def ema(a, n):
    out = np.empty(len(a)); a = np.asarray(a, float)
    k = 2 / (n + 1); out[0] = a[0]
    for i in range(1, len(a)):
        out[i] = a[i] * k + out[i-1] * (1 - k)
    return out

def stdev(a, n):
    out = np.full(len(a), np.nan); a = np.asarray(a, float)
    for i in range(n - 1, len(a)):
        out[i] = a[i-n+1:i+1].std()
    return out

def rsi(close, n=14):
    out = np.full(len(close), np.nan); c = np.asarray(close, float)
    if len(c) <= n:
        return out
    g = np.clip(np.diff(c), 0, None); l = np.clip(-np.diff(c), 0, None)
    ga, la = g[:n].mean(), l[:n].mean()
    out[n] = 100 - 100 / (1 + ga / (la or 1e-9))
    for i in range(n + 1, len(c)):
        ga = (ga * (n - 1) + g[i-1]) / n; la = (la * (n - 1) + l[i-1]) / n
        out[i] = 100 - 100 / (1 + ga / (la or 1e-9))
    return out

def kdj(o, c, h, l, n=9):
    N = len(c)
    rsv = np.zeros(N)
    for i in range(N):
        lo = l[max(0, i-n+1):i+1].min(); hi = h[max(0, i-n+1):i+1].max()
        rsv[i] = 50 if hi == lo else (c[i] - lo) / (hi - lo) * 100
    Kv = np.zeros(N); Dv = np.zeros(N); Jv = np.zeros(N)
    Kv[0] = Dv[0] = Jv[0] = 50
    for i in range(1, N):
        Kv[i] = Kv[i-1] * 2/3 + rsv[i] / 3
        Dv[i] = Dv[i-1] * 2/3 + Kv[i] / 3
        Jv[i] = 3 * Kv[i] - 2 * Dv[i]
    return Kv, Dv, Jv

def macd(close):
    c = np.asarray(close, float)
    dif = ema(c, 12) - ema(c, 26); dea = ema(dif, 9)
    return dif, dea, (dif - dea) * 2

def full_ind(k):
    """k: rows of (date, open, close, high, low, volume, amount)"""
    a = np.array([(x[1], x[2], x[3], x[4], x[5]) for x in k], float)
    o, c, h, l, v = a.T
    n = len(c)
    ret = lambda d: (c[-1] / c[-1-d] - 1) * 100 if n > d else None
    seg = c[max(0, n-120):]
    rets = np.diff(seg) / seg[:-1]
    vol = float(rets.std() * np.sqrt(252) * 100) if len(rets) > 5 else None
    s2 = c[max(0, n-250):]
    peak = np.maximum.accumulate(s2)
    mdd = float((s2 / peak - 1).min()) * 100 if len(s2) else None
    y = c[max(0, n-244):]; hi, lo = float(y.max()), float(y.min())
    yr = k[n-1][0][:4] + "-01-01"
    idx = next((i for i, x in enumerate(k) if x[0] >= yr), None)
    ytd = (c[-1] / c[idx] - 1) * 100 if idx is not None and idx < n - 1 else None
    ma20, ma60 = sma(c, 20), sma(c, 60)
    mid = sma(c, 20); sd = stdev(c, 20)
    bup, blow = mid + 2 * sd, mid - 2 * sd
    pb = (c[-1] - blow[-1]) / (bup[-1] - blow[-1]) * 100 if not np.isnan(bup[-1]) else None
    dif, dea, hist = macd(c)
    r14 = rsi(c)
    Kv, Dv, Jv = kdj(o, c, h, l)
    tr = np.maximum(h - l, np.maximum(abs(h - np.r_[c[0], c[:-1]]),
                                      abs(l - np.r_[c[0], c[:-1]])))
    atr = sma(tr, 14)
    bias20 = (c[-1] / ma20[-1] - 1) * 100 if not np.isnan(ma20[-1]) else None
    ma5_, ma10 = sma(c, 5), sma(c, 10)
    bull = bool(ma5_[-1] > ma10[-1] > ma20[-1] > ma60[-1]) if not np.isnan(ma60[-1]) else False
    bear = bool(ma5_[-1] < ma10[-1] < ma20[-1] < ma60[-1]) if not np.isnan(ma60[-1]) else False
    def sig(f):
        for i in range(n - 3, n):
            if i > 0:
                lab = f(i)
                if lab:
                    return lab
        return None
    macd_sig = sig(lambda i: "金叉" if dif[i-1] <= dea[i-1] and dif[i] > dea[i]
                   else ("死叉" if dif[i-1] >= dea[i-1] and dif[i] < dea[i] else None))
    kdj_sig = sig(lambda i: "金叉" if Kv[i-1] <= Dv[i-1] and Kv[i] > Dv[i]
                  else ("死叉" if Kv[i-1] >= Dv[i-1] and Kv[i] < Dv[i] else None))
    ann_ret = float((c[-1] / c[-min(n, 250)] - 1) * 100) if n > 60 else None
    # 波动率 < 2% 视为现金管理类(货币/短融), 夏普分母趋零会给出 -100 这类伪值, 直接置空
    sharpe = ((ann_ret / 100 - 0.015) / (vol / 100)) \
        if (ann_ret is not None and vol and vol >= 2.0) else None
    return dict(dates=[x[0] for x in k], o=o, c=c, h=h, l=l, v=v,
                ma5=ma5_, ma10=ma10, ma20=ma20, ma60=ma60,
                boll_up=bup, boll_mid=mid, boll_low=blow,
                dif=dif, dea=dea, hist=hist, rsi=r14, K=Kv, D=Dv, J=Jv, atr=atr,
                sum=dict(rsi=float(r14[-1]) if not np.isnan(r14[-1]) else None,
                         kdjK=float(Kv[-1]), kdjD=float(Dv[-1]), kdjJ=float(Jv[-1]),
                         dif=float(dif[-1]), dea=float(dea[-1]), pb=pb,
                         bias20=bias20, macdSig=macd_sig, kdjSig=kdj_sig,
                         bull=bull, bear=bear,
                         r5=ret(5), r20=ret(20), r60=ret(60), r120=ret(120), ytd=ytd,
                         vol=vol, mdd=mdd, ann_ret=ann_ret, sharpe=sharpe,
                         hi52=hi, lo52=lo,
                         pos52=(c[-1] - lo) / (hi - lo or 1e-9) * 100,
                         offHi=(c[-1] / hi - 1) * 100))

# ============================================================ 数据载入
class Analyzer:
    def __init__(self):
        self.etfs = []
        self.ind = {}       # code -> full_ind
        self.snaps = {}     # code -> dict
        self.kline = {}     # code -> rows
        self.corr = None    # (names, matrix)
        self.scores = []    # 评分排名列表
        self.rot = None     # 轮动数据
        self.meta = {}

    def load(self):
        etfs = load_etfs()
        conn = get_db()
        self.etfs, self.ind, self.snaps, self.kline = [], {}, {}, {}
        for e in etfs:
            rows = conn.execute(
                """SELECT trade_date, open, close, high, low, volume, amount
                   FROM kline WHERE code=? ORDER BY trade_date""", (e["code"],)).fetchall()
            snap = conn.execute(
                """SELECT trade_date, close, pct_chg, amount, turnover, vol_ratio,
                          iopv, premium, main_in, shares, fetched_at
                   FROM snapshot WHERE code=? ORDER BY trade_date DESC LIMIT 1""",
                (e["code"],)).fetchone()
            item = dict(e)
            item["snap"] = dict(zip(["trade_date", "close", "pct_chg", "amount",
                "turnover", "vol_ratio", "iopv", "premium", "main_in", "shares",
                "fetched_at"], snap)) if snap else None
            if len(rows) >= 30:
                self.ind[e["code"]] = full_ind(rows)
            self.kline[e["code"]] = rows
            self.etfs.append(item)
        conn.close()
        self.snaps = {e["code"]: e["snap"] for e in self.etfs}
        self._corr()
        self._score()
        self._rotation()
        r = get_db().execute("SELECT MAX(trade_date), MAX(fetched_at) FROM snapshot").fetchone()
        self.meta = {"trade_date": r[0], "fetched_at": r[1], "count": len(self.etfs)}
        return self

    # ---------- 相关性矩阵(近120日日收益率) ----------
    def _corr(self, days=120):
        series, names = [], []
        for e in self.etfs:
            k = self.kline.get(e["code"]) or []
            if len(k) > days:
                c = np.array([x[2] for x in k][-days-1:], float)
                series.append(np.diff(c) / c[:-1])
                names.append(e["name"])
        if len(series) >= 2:
            m = np.corrcoef(np.array(series))
            self.corr = (names, m)

    # ---------- 四因子评分 ----------
    @staticmethod
    def _z2pct(vals):
        """把原始值转成 0-100 分位分(缺失给50)"""
        v = np.array([x if x is not None and not np.isnan(x) else np.nan for x in vals], float)
        out = np.full(len(v), 50.0)
        good = ~np.isnan(v)
        if good.sum() > 1:
            order = v[good].argsort().argsort() / (good.sum() - 1) * 100
            out[good] = order
        return out

    def _score(self):
        rows = []
        for e in self.etfs:
            k = self.kline.get(e["code"]) or []
            d = self.ind.get(e["code"], {}).get("sum", {})
            s = e["snap"] or {}
            rows.append(dict(
                code=e["code"], name=e["name"], cat=e["category"],
                close=s.get("close"), pct=s.get("pct_chg"), premium=s.get("premium"),
                main_in=s.get("main_in"), amount=s.get("amount"),
                shares=s.get("shares"), turnover=s.get("turnover"),
                vol_ratio=s.get("vol_ratio"),
                mom20=d.get("r20"), mom60=d.get("r60"),
                trend=(d.get("bias20")), pos52=d.get("pos52"),
                vol=d.get("vol"), mdd=d.get("mdd"), sharpe=d.get("sharpe"),
                rsi=d.get("rsi"), kdjJ=d.get("kdjJ"), pb=d.get("pb"),
                ytd=d.get("ytd"), ann_ret=d.get("ann_ret"),
                macdSt=("多头" if d.get("dif", 0) > d.get("dea", 0) else "空头")
                       if d.get("dif") is not None else None,
                inflow_ratio=(s["main_in"] / s["amount"]) if s.get("main_in") is not None
                             and s.get("amount") else None))
        def col(key):
            return self._z2pct([r.get(key) for r in rows])
        mom = col("mom20") * 0.6 + col("mom60") * 0.4
        trend = col("trend") * 0.5 + col("pos52") * 0.5
        flow = col("inflow_ratio")
        risk = 100 - col("vol")   # 波动越低分越高
        for i, r in enumerate(rows):
            if r["code"] not in self.ind:      # 新成立/尚未上市: 没有场内K线就不给分
                r["f_mom"] = r["f_trend"] = r["f_flow"] = r["f_risk"] = None
                r["score"] = None
                r["no_data"] = True
                continue
            r["f_mom"], r["f_trend"], r["f_flow"], r["f_risk"] = mom[i], trend[i], flow[i], risk[i]
            r["score"] = round(mom[i] * 0.40 + trend[i] * 0.30 + flow[i] * 0.20 + risk[i] * 0.10, 1)
        rows.sort(key=lambda r: (r["score"] is not None, r["score"] or 0), reverse=True)
        for i, r in enumerate(rows):
            r["rank"] = i + 1 if r["score"] is not None else None
        self.scores = rows

    # ---------- 板块轮动(分类等权指数的多周期动量) ----------
    WINDOWS = (0, 3, 5, 10, 15, 20, 60)   # 0 = 今日(当日涨跌), 其余为滚动累计
    @classmethod
    def rot_label(cls, w):
        return "今日" if int(w) == 0 else "%d日" % int(w)

    # ---------- 区间收益 / 区间波动 (风险收益页的多周期) ----------
    RISK_W = (1, 3, 5, 10, 15, 20, 60)

    def risk_pts(self, w=5):
        """某一窗口下每只 ETF 的 区间涨跌 vs 区间波动幅度。

        x = 区间波动幅度(%), 即窗口内日收益标准差 × √天数; 这样不同窗口的
        x 与 y 都是「这段时间内的百分数」, 不用年化也能横向比。
        1 日窗口没有足够样本算标准差, 用近 20 日的日波动代替 (图上会注明)。
        """
        out = []
        for e in self.etfs:
            k = self.kline.get(e["code"]) or []
            if len(k) < max(30, w + 2):
                continue
            c = np.array([x[2] for x in k if x[2] and x[2] > 0], float)
            if len(c) < max(30, w + 2):
                continue
            ret = float((c[-1] / c[-1 - w] - 1) * 100)
            if w > 1:
                seg = c[-(w + 1):]
                r1 = np.diff(seg) / seg[:-1]
                amp = float(r1.std(ddof=1) * np.sqrt(len(r1)) * 100) if len(r1) > 2 else None
                note = ""
            else:
                r1 = np.diff(c[-21:]) / c[-21:-1]
                amp = float(r1.std(ddof=1) * 100) if len(r1) > 5 else None
                note = "波动取近20日"
            if amp is not None and amp < 0.15:     # 货币/短融: 波动趋零, 比值会炸
                amp = None
            s = e["snap"] or {}
            out.append(dict(code=e["code"], name=e["name"], cat=e["category"],
                            ret=ret, amp=amp,
                            ratio=(ret / amp) if amp else None,
                            amount=(s.get("amount") or 0), note=note,
                            pct=s.get("pct_chg")))
        return out

    def _rotation(self, days=150):
        """每个分类的等权指数, 再按 今日/5/10/20/60 日算动量。

        做法: 先把每只 ETF 的收盘价按公共交易日轴归一(以轴上首个可得价作基期),
        分类内等权平均得到分类指数, 再对指数取 w 日涨幅。这样不同窗口都是同一
        条曲线算出来的, 换个窗口不会因为成分缺失而对不上。
        """
        px, cat_of = {}, {}
        for e in self.etfs:
            if e["category"] == "货币现金":
                continue          # 货币基金净值几乎不动, 混进排名只会挤掉真板块
            k = self.kline.get(e["code"]) or []
            if len(k) < 30:
                continue
            ser = {r[0]: float(r[2]) for r in k if r[2]}
            if len(ser) < 30:
                continue
            px[e["code"]] = ser
            cat_of[e["code"]] = e["category"]
        if len(px) < 3:
            self.rot = None
            return

        # 公共日期轴: 过半标的都有数据的交易日
        from collections import Counter
        dc = Counter()
        for s in px.values():
            dc.update(s.keys())
        floor = max(3, int(len(px) * 0.5))
        axis = sorted(d for d, c in dc.items() if c >= floor)
        if len(axis) < 26:
            self.rot = None
            return
        axis = axis[-max(days, 70):]

        # 逐只归一
        norm = {}
        for code, s in px.items():
            base, arr = None, np.full(len(axis), np.nan)
            for i, d in enumerate(axis):
                v = s.get(d)
                if v is None:
                    continue
                if base is None:
                    base = v
                arr[i] = v / base
            norm[code] = arr

        cats = sorted({cat_of[c] for c in px})
        n_mem = {c: 0 for c in cats}
        for c in px:
            n_mem[cat_of[c]] += 1

        # 分类指数(等权)
        idx, keep = {}, []
        for cat in cats:
            mem = [norm[c] for c in px if cat_of[c] == cat]
            M = np.vstack(mem)
            cnt = np.sum(np.isfinite(M), axis=0)
            tot = np.where(cnt > 0, np.nansum(M, axis=0), np.nan)
            with np.errstate(invalid="ignore", divide="ignore"):
                s = np.where(cnt > 0, tot / np.maximum(cnt, 1), np.nan)
            if np.isfinite(s).sum() < 26:
                continue
            idx[cat] = s
            keep.append(cat)
        if len(keep) < 3:
            self.rot = None
            return
        cats = keep

        # 多窗口动量
        mom, cur_by_w, rank_by_w, chg_by_w = {}, {}, {}, {}
        for w in self.WINDOWS:
            if w == 0:
                continue
            per = {}
            for cat in cats:
                s = idx[cat]
                arr = np.full(len(s), np.nan)
                with np.errstate(invalid="ignore"):
                    arr[w:] = (s[w:] / s[:-w] - 1) * 100
                per[cat] = arr
                v = arr[np.isfinite(arr)]
                cur_by_w.setdefault(w, {})[cat] = float(v[-1]) if v.size else np.nan
            mom.update({(cat, w): per[cat] for cat in cats})

        # 当日涨跌: 优先快照, 缺则用指数最后一日变动
        pct_today, avg = {}, {}
        for e in self.etfs:
            s0 = e["snap"]
            if s0 and s0.get("pct_chg") is not None:
                pct_today.setdefault(e["category"], []).append(s0["pct_chg"])
        pct_today = {k: sum(v) / len(v) for k, v in pct_today.items()}
        for cat in cats:
            s = idx[cat]
            v = s[np.isfinite(s)]
            avg[cat] = float((v[-1] / v[-2] - 1) * 100) if v.size > 1 else np.nan
            cur_by_w.setdefault(0, {})[cat] = float(pct_today.get(cat, avg[cat]))

        # 各窗口排名 / 排名变化(近10个交易日)
        for w in self.WINDOWS:
            cur = cur_by_w[w]
            ok = [c for c in cats if cur.get(c) == cur.get(c)]
            rank_by_w[w] = {c: i + 1 for i, c in enumerate(
                sorted(ok, key=lambda c: -cur[c]))}
            chg_by_w[w] = {c: 0 for c in cats}
            if w == 0:
                continue
            past = {}
            for cat in cats:
                arr = mom.get((cat, w))
                val = np.nan
                if arr is not None and len(arr) > 11:
                    seg = arr[:-10]
                    seg = seg[np.isfinite(seg)]
                    if seg.size:
                        val = float(seg[-1])
                past[cat] = val
            ok2 = [c for c in cats if past[c] == past[c]]
            prank = {c: i + 1 for i, c in enumerate(sorted(ok2, key=lambda c: -past[c]))}
            for cat in ok2:
                now_rank = rank_by_w[w].get(cat)
                if now_rank:
                    chg_by_w[w][cat] = int(prank[cat] - now_rank)

        # 资金流按分类汇总(当日快照, 万元)
        flow = {}
        for e in self.etfs:
            s0 = e["snap"]
            if s0 and s0.get("main_in") is not None:
                flow[e["category"]] = flow.get(e["category"], 0) + s0["main_in"]

        # 分日资金流(亿元, CLV 口径): 分类每日 = 成分合计, 再按窗口拆成流入/流出/净额。
        # 快照只有今天一个数, 想看「这 5 天到底进多少出多少」只能自己按日累。
        # 流入/流出按「成分 x 交易日」逐笔拆, 不能先汇总到分类再拆: 一天一个数字,
        # 汇总后就没有正负可分了, 今日窗口会全是流入。
        dpos, dneg = {}, {}
        for code, k in self.kline.items():
            cat = cat_of.get(code)
            if not cat:
                continue
            for r in k:
                d, cl, h, l, am = r[0], r[2], r[3], r[4], r[6]
                if not h or not l or not cl or not am or float(h) <= float(l):
                    continue
                v = (float(am) * (2 * float(cl) - float(h) - float(l))
                     / (float(h) - float(l)) / 1e8)
                if v >= 0:
                    dpos[(d, cat)] = dpos.get((d, cat), 0.0) + v
                else:
                    dneg[(d, cat)] = dneg.get((d, cat), 0.0) - v
        fin, fout, fnet = {}, {}, {}
        for w in self.WINDOWS:
            ds = axis[-max(1, int(w)):]
            for cat in cats:
                pos = sum(dpos.get((d, cat), 0.0) for d in ds)
                neg = sum(dneg.get((d, cat), 0.0) for d in ds)
                fin[(cat, w)] = round(pos, 2)
                fout[(cat, w)] = round(-neg, 2)
                fnet[(cat, w)] = round(pos - neg, 2)

        leader = max(cats, key=lambda c: (cur_by_w[20].get(c, -1e9)
                                          if cur_by_w[20].get(c) == cur_by_w[20].get(c) else -1e9))
        self.rot = dict(cats=cats, dates=axis, n_mem=n_mem, mom=mom, idx=idx,
                        cur_by_w=cur_by_w, rank_by_w=rank_by_w, chg_by_w=chg_by_w,
                        flow=flow, pct_today=pct_today, leader=leader,
                        fin=fin, fout=fout, fnet=fnet,
                        windows=self.WINDOWS,
                        notes=self._rot_notes(cats, cur_by_w[20], rank_by_w[20],
                                              chg_by_w[20], flow, 20))

    @staticmethod
    def _rot_notes(cats, cur, cur_rank, chg, flow=None, w=20):
        notes = []
        lab = "今日" if w == 0 else "%d日" % w
        ok = [c for c in cats if cur.get(c) == cur.get(c)]
        order = sorted(ok, key=lambda c: -cur[c])
        if len(order) >= 2:
            notes.append("%s动量最强 %s(%+.1f%%), 最弱 %s(%+.1f%%), 强弱差 %.1f 个点"
                         % (lab, order[0], cur[order[0]], order[-1], cur[order[-1]],
                            cur[order[0]] - cur[order[-1]]))
        chg = chg or {}
        up = sorted([c for c in ok if chg.get(c, 0) > 0], key=lambda c: -chg[c])[:3]
        dn = sorted([c for c in ok if chg.get(c, 0) < 0], key=lambda c: chg[c])[:2]
        if up:
            notes.append("近10日排名上升 " + "、".join(
                "%s(%d→%d)" % (c, cur_rank.get(c, 0) + chg[c], cur_rank.get(c, 0)) for c in up))
        if dn:
            notes.append("排名下滑 " + "、".join(
                "%s(%d→%d)" % (c, cur_rank.get(c, 0) + chg[c], cur_rank.get(c, 0)) for c in dn))
        # 动量与资金背离: 动量在前1/3但资金净流出, 或动量在后1/3但资金净流入
        flow = flow or {}
        n = len(order)
        cut = max(1, n // 3)
        bad, good = [], []
        for c in order[:cut]:
            v = flow.get(c)
            if v is not None and v < -5000:
                bad.append("%s(%+.1f亿)" % (c, v / 1e4))
        for c in order[-cut:]:
            v = flow.get(c)
            if v is not None and v > 5000:
                good.append("%s(%+.1f亿)" % (c, v / 1e4))
        if bad:
            notes.append("动量靠前但资金在撤 " + "、".join(bad) + ", 涨的是存量博弈")
        if good:
            notes.append("动量靠后但资金在进 " + "、".join(good) + ", 有人在低位接")
        return notes

    # ---------- 每日自动结论 ----------
    def daily_brief(self):
        if not self.scores:
            return "无数据"
        top = self.scores[:5]
        bot = self.scores[-3:]
        lines = [f"数据日期 {self.meta.get('trade_date')} · 覆盖 {len(self.scores)} 只ETF",
                 "综合评分TOP5: " + " | ".join(f"{r['rank']}.{r['name']} {r['score']}分" for r in top),
                 "垫底: " + " | ".join(f"{r['name']}({r['score']}分)" for r in bot)]
        if self.rot:
            lines.append("轮动: " + "; ".join(self.rot["notes"]))
            fl = sorted(self.rot["flow"].items(), key=lambda x: -x[1])
            if fl:
                lines.append("当日资金: " + " | ".join(f"{c} {v/1e4:+.2f}亿" for c, v in fl[:3])
                             + " ← 流入前3; " + f"{fl[-1][0]} {fl[-1][1]/1e4:+.2f}亿 ← 流出最大")
        sigs = [f"{r['name']}({r['code']})" for r in self.scores[:10]
                if r["code"] in self.ind and
                (self.ind[r["code"]]["sum"].get("macdSig") or self.ind[r["code"]]["sum"].get("kdjSig"))]
        if sigs:
            lines.append("近3日出现金叉/死叉信号(评分前10内): " + ", ".join(sigs))
        return "\n".join(lines)


if __name__ == "__main__":
    a = Analyzer().load()
    print(a.daily_brief())
    print("\n评分TOP10:")
    for r in a.scores[:10]:
        print(f"  {r['rank']:>2}. {r['name']:<14} {r['score']:>5}  动量{r['f_mom']:.0f} 趋势{r['f_trend']:.0f} 资金{r['f_flow']:.0f} 风险{r['f_risk']:.0f}")
    if a.corr:
        names, m = a.corr
        print(f"\n相关性矩阵: {len(names)}x{len(names)}")
        i, j = np.unravel_index(np.argmin(m + np.eye(len(m)) * 2), m.shape)
        print(f"  最低相关对: {names[i]} <-> {names[j]} = {m[i,j]:.2f}")
    if a.rot:
        print("\n轮动:", "; ".join(a.rot["notes"]))
