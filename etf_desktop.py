# -*- coding: utf-8 -*-
"""
ETF 跟踪面板 - 桌面版(Tkinter 原生窗口, 无浏览器)
- 启动: start_etf_desktop.bat (pythonw, 无控制台)
- 布局: 左侧标的树 + 右侧五页签(行情K线/指标看板/标的对比/资金流/相对走势)
- 数据: 复用 etf_app.py 的 SQLite 与刷新逻辑, 与网页版共用 etf_tracker.db
- 指标: MA/BOLL/MACD/RSI/KDJ/ATR/CCI/威廉%R/BIAS + 收益风险 + 自动信号
"""
import json
import queue
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import ttk

import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure

matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
matplotlib.rcParams["axes.unicode_minus"] = False

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
from etf_app import get_db, load_etfs, refresh_data  # noqa: E402

UP, DOWN, ACCENT, SUB = "#d93a3a", "#1a9e5c", "#2f6fed", "#6b7686"

# ============================================================ 指标计算
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

def stdev(a, n, mid):
    out = np.full(len(a), np.nan); a = np.asarray(a, float)
    for i in range(n - 1, len(a)):
        out[i] = a[i-n+1:i+1].std()
    return out

def rsi(close, n=14):
    out = np.full(len(close), np.nan); c = np.asarray(close, float)
    g = np.clip(np.diff(c), 0, None); l = np.clip(-np.diff(c), 0, None)
    ga, la = g[:n].mean(), l[:n].mean()
    if n < len(c):
        out[n] = 100 - 100 / (1 + ga / (la or 1e-9))
    for i in range(n + 1, len(c)):
        ga = (ga * (n - 1) + g[i-1]) / n; la = (la * (n - 1) + l[i-1]) / n
        out[i] = 100 - 100 / (1 + ga / (la or 1e-9))
    return out

def kdj(K, n=9):
    k = np.array(K, float); N = len(k)
    rsv = np.zeros(N)
    for i in range(N):
        lo = k[max(0, i-n+1):i+1, 2].min(); hi = k[max(0, i-n+1):i+1, 1].max()
        rsv[i] = 50 if hi == lo else (k[i, 3] - lo) / (hi - lo) * 100
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
    """k: list of (date, open, close, high, low, volume, amount)"""
    a = np.array([(x[1], x[2], x[3], x[4], x[5]) for x in k], float)  # o c h l v
    o, c, h, l, v = a.T
    n = len(c)
    ret = lambda d: (c[-1] / c[-1-d] - 1) * 100 if n > d else None
    seg = c[max(0, n-120):]
    rets = np.diff(seg) / seg[:-1]
    vol = rets.std() * np.sqrt(252) * 100 if len(rets) else None
    s2 = c[max(0, n-250):]
    peak = np.maximum.accumulate(s2)
    mdd = ((s2 / peak - 1).min()) * 100 if len(s2) else None
    y = c[max(0, n-244):]; hi, lo = y.max(), y.min()
    yr = k[n-1][0][:4] + "-01-01"
    idx = next((i for i, x in enumerate(k) if x[0] >= yr), None)
    ytd = (c[-1] / c[idx] - 1) * 100 if idx is not None and idx < n - 1 else None
    ma5, ma10, ma20, ma60 = sma(c, 5), sma(c, 10), sma(c, 20), sma(c, 60)
    mid = sma(c, 20); sd = stdev(c, 20, mid)
    bup, blow = mid + 2 * sd, mid - 2 * sd
    pb = (c[-1] - blow[-1]) / (bup[-1] - blow[-1]) * 100 if not np.isnan(bup[-1]) else None
    dif, dea, hist = macd(c)
    r14 = rsi(c)
    Kv, Dv, Jv = kdj(a[:, :4] * [1, 0, 1, 1]) if False else kdj(np.column_stack([o, c, h, l]))
    tr = np.maximum(h - l, np.maximum(abs(h - np.r_[c[0], c[:-1]]),
                                      abs(l - np.r_[c[0], c[:-1]])))
    atr = sma(tr, 14)
    tp = (h + l + c) / 3
    cci = np.full(n, np.nan)
    for i in range(19, n):
        s = tp[i-19:i+1]; m = s.mean()
        md = np.abs(s - m).mean()
        cci[i] = 0 if md == 0 else (tp[i] - m) / (0.015 * md)
    hi14, lo14 = h[-14:].max(), l[-14:].min()
    wr = (hi14 - c[-1]) / (hi14 - lo14 or 1e-9) * -100
    bias20 = (c[-1] / ma20[-1] - 1) * 100 if not np.isnan(ma20[-1]) else None
    bull = ma5[-1] > ma10[-1] > ma20[-1] > ma60[-1]
    bear = ma5[-1] < ma10[-1] < ma20[-1] < ma60[-1]
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
    return dict(ma=dict(ma5=ma5, ma10=ma10, ma20=ma20, ma60=ma60),
                boll=dict(up=bup, mid=mid, low=blow),
                dif=dif, dea=dea, hist=hist, rsi=r14, K=Kv, D=Dv, J=Jv,
                sum=dict(rsi=r14[-1], kdjK=Kv[-1], kdjD=Dv[-1], kdjJ=Jv[-1],
                         dif=dif[-1], dea=dea[-1], atr=atr[-1],
                         atrPct=atr[-1] / c[-1] * 100, cci=cci[-1], wr=wr, pb=pb,
                         bias20=bias20, macdSig=macd_sig, kdjSig=kdj_sig,
                         bull=bool(bull), bear=bool(bear),
                         r5=ret(5), r20=ret(20), r60=ret(60), r120=ret(120), ytd=ytd,
                         vol=vol, mdd=mdd, hi52=hi, lo52=lo,
                         pos52=(c[-1] - lo) / (hi - lo or 1e-9) * 100,
                         offHi=(c[-1] / hi - 1) * 100))

# ============================================================ 数据层
def db_etfs():
    etfs = load_etfs()
    conn = get_db()
    out = []
    for e in etfs:
        row = conn.execute(
            """SELECT trade_date, close, pct_chg, amount, turnover, vol_ratio,
                      iopv, premium, main_in, shares, fetched_at
               FROM snapshot WHERE code=? ORDER BY trade_date DESC LIMIT 1""",
            (e["code"],)).fetchone()
        item = dict(e)
        if row:
            item["snap"] = dict(zip(["trade_date", "close", "pct_chg", "amount",
                "turnover", "vol_ratio", "iopv", "premium", "main_in", "shares",
                "fetched_at"], row))
        else:
            item["snap"] = None
        out.append(item)
    conn.close()
    return out

def db_kline(code):
    conn = get_db()
    rows = conn.execute(
        """SELECT trade_date, open, close, high, low, volume, amount
           FROM kline WHERE code=? ORDER BY trade_date""", (code,)).fetchall()
    conn.close()
    return rows

class Store:
    """线程安全的数据缓存 + 后台刷新"""
    def __init__(self):
        self.etfs = db_etfs()
        self.kline = {}          # code -> rows
        self.kcache = {}         # code -> full_ind 结果
        self.msgq = queue.Queue()
        self.refreshing = False

    def ind(self, code):
        if code not in self.kcache:
            self.kcache[code] = full_ind(self.kline[code])
        return self.kcache[code]

    def invalidate(self):
        self.kcache.clear()

    def start_refresh(self, done_cb):
        if self.refreshing:
            return
        self.refreshing = True
        def work():
            try:
                ok, errs = refresh_data()
                self.msgq.put(("refresh_done", ok, errs))
            except Exception as e:
                self.msgq.put(("refresh_done", 0, [str(e)]))
        threading.Thread(target=work, daemon=True).start()
        self._done = done_cb

    def poll(self):
        try:
            while True:
                msg = self.msgq.get_nowait()
                if msg[0] == "refresh_done":
                    self.refreshing = False
                    self.etfs = db_etfs()
                    self.invalidate()
                    if self._done:
                        self._done(msg[1], msg[2])
        except queue.Empty:
            pass

# ============================================================ 界面
class App(tk.Tk):
    def __init__(self, selftest=False):
        super().__init__()
        self.title("ETF 跟踪面板")
        self.geometry("1380x860")
        self.store = Store()
        self.cur_code = None
        self.sub_mode = tk.StringVar(value="MACD")
        self.sort_state = (None, False)
        self.selftest = selftest
        self._build()
        self.after(200, self._poll)
        self.after(100, self._boot)
        if not selftest:
            self.mainloop()

    # ---------- 构建 ----------
    def _build(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        top = ttk.Frame(self, padding=6)
        top.pack(fill="x")
        ttk.Label(top, text="ETF 跟踪面板", font=("Microsoft YaHei", 13, "bold")).pack(side="left")
        self.status_var = tk.StringVar(value="加载中…")
        ttk.Label(top, textvariable=self.status_var, foreground=SUB).pack(side="left", padx=14)
        self.cat_var = tk.StringVar(value="全部")
        self.cat_cb = ttk.Combobox(top, textvariable=self.cat_var, state="readonly", width=12)
        self.cat_cb.pack(side="right", padx=4)
        self.cat_cb.bind("<<ComboboxSelected>>", lambda e: self.reload_list())
        ttk.Label(top, text="分类:").pack(side="right")
        self.btn_refresh = ttk.Button(top, text="刷新数据", command=self.on_refresh)
        self.btn_refresh.pack(side="right", padx=4)

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=8, pady=(0, 6))

        # --- 页1: 行情K线 ---
        self.tab_chart = ttk.Frame(self.nb)
        self.nb.add(self.tab_chart, text=" 行情K线 ")
        left = ttk.Frame(self.tab_chart, padding=4)
        left.pack(side="left", fill="y")
        self.tree = ttk.Treeview(left, columns=("code", "name", "chg", "main"),
                                 show="headings", height=26, selectmode="browse")
        for cid, txt, w, anc in (("code", "代码", 62, "w"), ("name", "名称", 130, "w"),
                                 ("chg", "涨跌%", 56, "e"), ("main", "主力(万)", 76, "e")):
            self.tree.heading(cid, text=txt)
            self.tree.column(cid, width=w, anchor=anc)
        self.tree.pack(fill="y", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.on_pick)
        self.tree.tag_configure("up", foreground=UP)
        self.tree.tag_configure("down", foreground=DOWN)

        right = ttk.Frame(self.tab_chart)
        right.pack(side="left", fill="both", expand=True)
        rb = ttk.Frame(right, padding=3)
        rb.pack(fill="x")
        ttk.Label(rb, text="副图:").pack(side="left")
        for m in ("MACD", "RSI", "KDJ"):
            ttk.Radiobutton(rb, text=m, value=m, variable=self.sub_mode,
                            command=self.draw_chart).pack(side="left", padx=4)
        self.fig = Figure(figsize=(9.4, 6.4), dpi=96)
        self.canvas = FigureCanvasTkAgg(self.fig, master=right)
        self.toolbar = NavigationToolbar2Tk(self.canvas, right, pack_toolbar=False)
        self.toolbar.pack(side="bottom", fill="x")
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        self._axes()

        # --- 页2: 指标看板 ---
        self.tab_ind = ttk.Frame(self.nb)
        self.nb.add(self.tab_ind, text=" 指标看板 ")
        self.ind_text = tk.Text(self.tab_ind, font=("Microsoft YaHei", 11),
                                wrap="word", padx=14, pady=10, relief="flat",
                                background="#fafbfc")
        self.ind_text.pack(fill="both", expand=True)

        # --- 页3: 标的对比 ---
        self.tab_cmp = ttk.Frame(self.nb)
        self.nb.add(self.tab_cmp, text=" 标的对比 ")
        wrap = ttk.Frame(self.tab_cmp, padding=4)
        wrap.pack(fill="both", expand=True)
        self.cmp_cols = [("name", "名称", 130), ("cat", "分类", 80),
            ("close", "最新价", 66), ("pct", "涨跌%", 60), ("premium", "溢价%", 60),
            ("main_in", "主力(万)", 80), ("rsi", "RSI14", 56), ("kdjJ", "KDJ-J", 56),
            ("macdSt", "MACD", 56), ("pb", "布林%B", 60), ("r5", "5日%", 56),
            ("r20", "20日%", 56), ("ytd", "年内%", 62), ("vol", "年化波动", 66),
            ("mdd", "最大回撤", 66), ("pos52", "52周位置", 66)]
        self.cmp_tree = ttk.Treeview(wrap, columns=[c[0] for c in self.cmp_cols],
                                     show="headings", height=28)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.cmp_tree.yview)
        self.cmp_tree.configure(yscrollcommand=vsb.set)
        self.cmp_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        for cid, txt, w in self.cmp_cols:
            self.cmp_tree.heading(cid, text=txt,
                                  command=lambda c=cid: self.sort_cmp(c))
            self.cmp_tree.column(cid, width=w, anchor="e" if cid != "name" else "w")
        self.cmp_tree.tag_configure("up", foreground=UP)
        self.cmp_tree.tag_configure("down", foreground=DOWN)

        # --- 页4: 资金流 ---
        self.tab_flow = ttk.Frame(self.nb)
        self.nb.add(self.tab_flow, text=" 资金流 ")
        self.flow_fig = Figure(figsize=(9.4, 6), dpi=96)
        self.flow_canvas = FigureCanvasTkAgg(self.flow_fig, master=self.tab_flow)
        self.flow_canvas.get_tk_widget().pack(fill="both", expand=True, padx=6, pady=6)

        # --- 页5: 相对走势 ---
        self.tab_rel = ttk.Frame(self.nb)
        self.nb.add(self.tab_rel, text=" 相对走势 ")
        self.rel_fig = Figure(figsize=(9.4, 6), dpi=96)
        self.rel_canvas = FigureCanvasTkAgg(self.rel_fig, master=self.tab_rel)
        self.rel_canvas.get_tk_widget().pack(fill="both", expand=True, padx=6, pady=6)

    def _axes(self):
        self.fig.clear()
        self.ax_k = self.fig.add_axes((0.055, 0.40, 0.92, 0.55))
        self.ax_v = self.fig.add_axes((0.055, 0.26, 0.92, 0.12), sharex=self.ax_k)
        self.ax_s = self.fig.add_axes((0.055, 0.07, 0.92, 0.16), sharex=self.ax_k)

    # ---------- 数据流 ----------
    def _poll(self):
        self.store.poll()
        self.after(300, self._poll)

    def _boot(self):
        self.load_all()
        self.store.start_refresh(lambda ok, errs: self.after(0, lambda: self.on_refreshed(ok, errs)))

    def on_refresh(self):
        self.btn_refresh.config(state="disabled", text="刷新中…")
        self.status_var.set("后台拉取行情与日K中(52只约2.5分钟)…")
        self.store.start_refresh(lambda ok, errs: self.after(0, lambda: self.on_refreshed(ok, errs)))

    def on_refreshed(self, ok, errs):
        self.btn_refresh.config(state="normal", text="刷新数据")
        note = f"刷新完成: {ok} 只行情" + (f", {len(errs)} 项失败" if errs else "")
        if errs:
            note += " | " + "; ".join(errs[:2])
        self.status_var.set(note)
        self.load_all()

    def load_all(self):
        self.reload_list()
        self.render_cmp()
        self.render_flow()
        self.render_rel()
        self.render_ind_panel()
        if not self.cur_code and self.store.etfs:
            self.cur_code = self.store.etfs[0]["code"]
        if self.cur_code:
            self.select(self.cur_code)

    def shown(self):
        cat = self.cat_var.get()
        e = self.store.etfs
        return e if cat == "全部" else [x for x in e if x["category"] == cat]

    def reload_list(self):
        cats = ["全部"] + sorted({e["category"] for e in self.store.etfs})
        self.cat_cb.config(values=cats)
        self.tree.delete(*self.tree.get_children())
        for e in self.shown():
            s = e["snap"] or {}
            chg = s.get("pct_chg")
            mi = s.get("main_in")
            self.tree.insert("", "end", iid=e["code"], tags=("up" if (chg or 0) > 0 else "down",),
                values=(e["code"], e["name"],
                        f"{chg:+.2f}%" if chg is not None else "—",
                        f"{mi:+,.0f}" if mi is not None else "—"))
        self.render_cmp(); self.render_flow(); self.render_rel(); self.render_ind_panel()

    def on_pick(self, _e=None):
        sel = self.tree.selection()
        if sel:
            self.select(sel[0])

    def select(self, code):
        self.cur_code = code
        if code not in self.store.kline:
            self.store.kline[code] = db_kline(code)
        self.draw_chart()
        self.render_ind_panel()
        # 同步对比页选中行
        kids = self.cmp_tree.get_children()
        if code in kids:
            self.cmp_tree.see(code)
            self.cmp_tree.selection_set(code)

    # ---------- 页1: K线 ----------
    def draw_chart(self):
        code = self.cur_code
        k = self.store.kline.get(code) or []
        e = next((x for x in self.store.etfs if x["code"] == code), None)
        self._axes()
        if not k:
            self.ax_k.text(0.5, 0.5, "暂无K线数据, 请点右上角刷新",
                           ha="center", va="center", transform=self.ax_k.transAxes,
                           color=SUB, fontsize=13)
            self.canvas.draw_idle()
            return
        d = np.array([(x[1], x[2], x[3], x[4], x[5]) for x in k], float)
        o, c, h, l, v = d.T
        dates = [x[0] for x in k]
        ind = self.store.ind(code)
        n = len(k)
        up = c >= o
        colors = np.where(up, UP, DOWN)
        x = np.arange(n)
        self.ax_k.vlines(x, l, h, color=colors, lw=0.7)
        self.ax_k.bar(x, np.abs(c - o) + 1e-9, np.where(up, 0.65, 0.65),
                      bottom=np.minimum(o, c), color=colors, zorder=3)
        for key, val, colr in (("ma5", ind["ma"]["ma5"], "#f5a623"),
                               ("ma20", ind["ma"]["ma20"], ACCENT),
                               ("ma60", ind["ma"]["ma60"], "#9b59b6")):
            self.ax_k.plot(x, val, lw=1, color=colr, label=key.upper())
        self.ax_k.plot(x, ind["boll"]["up"], lw=0.8, ls="--", color="#b0b8c4", label="BOLL上")
        self.ax_k.plot(x, ind["boll"]["low"], lw=0.8, ls="--", color="#b0b8c4", label="BOLL下")
        self.ax_k.legend(loc="upper left", fontsize=8, ncol=5, frameon=False)
        self.ax_k.set_title(f"{e['name']} ({code})  日K · 近{n}个交易日", loc="left", fontsize=11)
        self.ax_k.grid(color="#eef1f5", lw=0.6)
        self.ax_k.tick_params(labelsize=8, labelbottom=False)

        self.ax_v.bar(x, v, color=colors, width=0.65)
        self.ax_v.set_ylabel("成交量", fontsize=8)
        self.ax_v.tick_params(labelsize=7, labelbottom=False)

        ax = self.ax_s
        mode = self.sub_mode.get()
        if mode == "MACD":
            ax.bar(x, ind["hist"], color=np.where(ind["hist"] >= 0, UP, DOWN), width=0.65)
            ax.plot(x, ind["dif"], lw=1, color="#f5a623", label="DIF")
            ax.plot(x, ind["dea"], lw=1, color=ACCENT, label="DEA")
            ax.legend(loc="upper left", fontsize=8, ncol=2, frameon=False)
            ax.set_ylabel("MACD", fontsize=8)
        elif mode == "RSI":
            ax.plot(x, ind["rsi"], lw=1.1, color="#9b59b6", label="RSI14")
            ax.axhline(70, color=UP, ls="--", lw=0.7)
            ax.axhline(30, color=DOWN, ls="--", lw=0.7)
            ax.set_ylim(0, 100)
            ax.legend(loc="upper left", fontsize=8, frameon=False)
            ax.set_ylabel("RSI", fontsize=8)
        else:
            ax.plot(x, ind["K"], lw=1, color="#f5a623", label="K")
            ax.plot(x, ind["D"], lw=1, color=ACCENT, label="D")
            ax.plot(x, ind["J"], lw=1, color="#9b59b6", label="J")
            ax.legend(loc="upper left", fontsize=8, ncol=3, frameon=False)
            ax.set_ylabel("KDJ", fontsize=8)
        ax.grid(color="#eef1f5", lw=0.6)
        ax.tick_params(labelsize=7)
        step = max(1, n // 10)
        ticks = list(range(0, n, step))
        for a in (self.ax_k, self.ax_v, ax):
            a.set_xlim(-1, n)
        ax.set_xticks(ticks)
        ax.set_xticklabels([dates[i] for i in ticks], rotation=0, fontsize=7)
        self.canvas.draw_idle()

    # ---------- 页2: 指标看板 ----------
    def render_ind_panel(self):
        t = self.ind_text
        t.config(state="normal")
        t.delete("1.0", "end")
        code = self.cur_code
        e = next((x for x in self.store.etfs if x["code"] == code), None)
        k = self.store.kline.get(code) or []
        if not e or not k:
            t.insert("end", "请选择标的(行情K线页左侧列表)\n")
            return
        s = e["snap"] or {}
        d = self.store.ind(code)["sum"]
        def line(sec):
            t.insert("end", f"\n【{sec}】\n", "sec")
        def row(*vals):
            t.insert("end", "  ".join(f"{vals[i]}: {vals[i+1]}"
                     for i in range(0, len(vals) - 1, 2)) + "\n")
        def f(v, n=2):
            return "—" if v is None else f"{v:.{n}f}"
        t.tag_configure("sec", font=("Microsoft YaHei", 11, "bold"),
                        foreground=ACCENT, spacing1=6)
        line(f"{e['name']} ({e['code']}) · {e['category']}")
        row("最新价", f(s.get('close'), 3), "涨跌幅", f"{s.get('pct_chg', 0):+.2f}%" if s.get('pct_chg') is not None else "—",
            "IOPV", f(s.get('iopv'), 3), "溢价率", f"{s.get('premium', 0):+.3f}%" if s.get('premium') is not None else "—")
        row("主力净流入", f"{s.get('main_in', 0):+,.0f}万" if s.get('main_in') is not None else "—",
            "成交额", f"{s.get('amount', 0):,.0f}万", "换手", f(s.get('turnover')) + "%",
            "量比", f(s.get('vol_ratio')), "份额", f(s.get('shares'), 2) + "亿份")
        line("技术指标")
        rsi_st = "超买" if (d["rsi"] or 50) > 70 else ("超卖" if (d["rsi"] or 50) < 30 else "中性")
        row("RSI(14)", f(d['rsi'], 1), f"({rsi_st})",
            "KDJ", f"{f(d['kdjK'],0)}/{f(d['kdjD'],0)}/{f(d['kdjJ'],0)}",
            "MACD DIF/DEA", f"{d['dif']:+.3f}/{d['dea']:+.3f}",
            "状态", "多头" if d['dif'] > d['dea'] else "空头")
        row("布林%B", f(d['pb'], 1) + "%", "CCI(20)", f(d['cci'], 0),
            "威廉%R", f(d['wr'], 1), "BIAS(20)", f"{d['bias20']:+.2f}%" if d['bias20'] is not None else "—",
            "ATR(14)", f(d['atr'], 3), f"({f(d['atrPct'])}%)")
        line("收益与风险")
        row("近5日", f"{d['r5']:+.2f}%" if d['r5'] is not None else "—",
            "近20日", f"{d['r20']:+.2f}%" if d['r20'] is not None else "—",
            "近60日", f"{d['r60']:+.2f}%" if d['r60'] is not None else "—",
            "年内", f"{d['ytd']:+.2f}%" if d['ytd'] is not None else "—")
        row("年化波动率", f(d['vol'], 1) + "%", "最大回撤(250日)", f(d['mdd'], 1) + "%",
            "52周位置", f(d['pos52'], 0) + "%", "距52周高点", f(d['offHi'], 1) + "%")
        row("52周区间", f"{d['lo52']:.3f} ~ {d['hi52']:.3f}")
        line("信号")
        sigs = []
        if d["bull"]: sigs.append("均线多头排列")
        if d["bear"]: sigs.append("均线空头排列")
        if d["macdSig"]: sigs.append(f"MACD{d['macdSig']}(3日内)")
        if d["kdjSig"]: sigs.append(f"KDJ{d['kdjSig']}(3日内)")
        if (d["rsi"] or 50) > 70: sigs.append("RSI超买")
        if (d["rsi"] or 50) < 30: sigs.append("RSI超卖")
        if (d["pb"] or 50) > 100: sigs.append("突破布林上轨")
        if (d["pb"] or 50) < 0: sigs.append("跌破布林下轨")
        t.insert("end", ("  |  ".join(sigs) if sigs else "无显著信号") + "\n")
        t.insert("end", "\n口径: 日K · RSI14 · KDJ(9,3,3) · BOLL(20,2) · MACD(12,26,9) · 不构成投资建议\n", "sec")
        t.tag_config("sec", font=("Microsoft YaHei", 9), foreground=SUB)
        t.config(state="disabled")

    # ---------- 页3: 对比表 ----------
    def sort_cmp(self, key):
        _, asc = self.sort_state
        self.sort_state = (key, not asc if key == self.sort_state[0] else False)
        self.render_cmp()

    def render_cmp(self):
        rows = []
        for e in self.shown():
            s = e["snap"] or {}
            k = self.store.kline.get(e["code"]) or db_kline(e["code"])
            self.store.kline[e["code"]] = k
            d = self.store.ind(e["code"])["sum"] if len(k) >= 30 else {}
            rows.append(dict(code=e["code"], name=e["name"], cat=e["category"],
                close=s.get("close"), pct=s.get("pct_chg"), premium=s.get("premium"),
                main_in=s.get("main_in"), rsi=d.get("rsi"), kdjJ=d.get("kdjJ"),
                macdSt=("多头" if d.get("dif", 0) > d.get("dea", 0) else "空头") if d else None,
                pb=d.get("pb"), r5=d.get("r5"), r20=d.get("r20"), ytd=d.get("ytd"),
                vol=d.get("vol"), mdd=d.get("mdd"), pos52=d.get("pos52")))
        key, asc = self.sort_state
        if key:
            rows.sort(key=lambda r: (r[key] is None, r[key] if not asc else -r[key] if isinstance(r[key], (int, float)) else r[key]), reverse=not asc and key is not None)
            rows.sort(key=lambda r: (r[key] is not None, r[key] if isinstance(r[key], (int, float)) else str(r[key])), reverse=not asc)
        self.cmp_tree.delete(*self.cmp_tree.get_children())
        for r in rows:
            tag = "up" if (r["pct"] or 0) > 0 else "down"
            def f(v, n=2, sign=False):
                if v is None: return "—"
                return (f"{v:+.{n}f}" if sign else f"{v:.{n}f}")
            self.cmp_tree.insert("", "end", iid=r["code"], tags=(tag,), values=(
                r["name"], r["cat"], f(r["close"], 3), f(r["pct"], 2, True),
                f(r["premium"], 3, True),
                f"{r['main_in']:+,.0f}" if r["main_in"] is not None else "—",
                f(r["rsi"], 1), f(r["kdjJ"], 1), r["macdSt"] or "—", f(r["pb"], 1),
                f(r["r5"], 2, True), f(r["r20"], 2, True), f(r["ytd"], 2, True),
                f(r["vol"], 1), f(r["mdd"], 1), f(r["pos52"], 0)))

    # ---------- 页4: 资金流 ----------
    def render_flow(self):
        fig = self.flow_fig
        fig.clear()
        ax = fig.add_axes((0.18, 0.08, 0.78, 0.88))
        data = sorted([(e["name"], e["snap"]["main_in"]) for e in self.store.etfs
                       if e["snap"] and e["snap"].get("main_in") is not None],
                      key=lambda x: x[1])
        if not data:
            ax.text(0.5, 0.5, "暂无资金流数据", ha="center", va="center", color=SUB)
        else:
            names = [d[0] for d in data]; vals = np.array([d[1] for d in data])
            ax.barh(names, vals / 1e4, color=[UP if x >= 0 else DOWN for x in vals])
            for i, x in enumerate(vals):
                ax.text(x / 1e4 + (0.01 if x >= 0 else -0.01), i,
                        f"{x/1e4:+.2f}亿", va="center",
                        ha="left" if x >= 0 else "right", fontsize=8, color=SUB)
            ax.set_title("当日主力净流入 (亿元)", loc="left", fontsize=11)
            ax.grid(axis="x", color="#eef1f5", lw=0.6)
            ax.tick_params(labelsize=9)
            xmin, xmax = ax.get_xlim(); rng = xmax - xmin
            ax.set_xlim(xmin - rng * 0.12, xmax + rng * 0.12)
            ax.axvline(0, color="#c8cfd9", lw=0.8)
        self.flow_canvas.draw_idle()

    # ---------- 页5: 相对走势 ----------
    def render_rel(self):
        fig = self.rel_fig
        fig.clear()
        ax = fig.add_axes((0.06, 0.1, 0.85, 0.82))
        lists = [(e["code"], e["name"], self.store.kline.get(e["code"]) or db_kline(e["code"]))
                 for e in self.shown()]
        lists = [(c, n, k) for c, n, k in lists if len(k) > 20]
        if not lists:
            ax.text(0.5, 0.5, "暂无数据", ha="center", va="center", color=SUB)
            self.rel_canvas.draw_idle()
            return
        N = min(len(k) for _, _, k in lists)
        N = min(N, 250)
        palette = ["#2f6fed", "#d93a3a", "#1a9e5c", "#f5a623", "#9b59b6",
                   "#00b8d4", "#e67e22", "#7f8c9b", "#c2185b", "#5d4037"]
        for i, (code, name, k) in enumerate(lists):
            closes = [x[2] for x in k][-N:]
            base = closes[0]
            colr = palette[i % len(palette)]
            ax.plot(range(N), np.array(closes) / base * 100, lw=1.3, color=colr, label=name)
            ax.annotate(f"{(closes[-1]/base-1)*100:+.1f}%", (N - 1, closes[-1] / base * 100),
                        fontsize=8, color=colr, xytext=(4, 0),
                        textcoords="offset points", va="center")
        ax.axhline(100, color="#c8cfd9", lw=0.8)
        ax.set_title(f"相对走势对比 (基期=100, 近{N}个交易日, 当前分类)", loc="left", fontsize=11)
        ax.grid(color="#eef1f5", lw=0.6)
        ax.tick_params(labelsize=8)
        step = max(1, N // 8)
        ax.set_xticks(range(0, N, step))
        ax.set_xticklabels([lists[0][2][-N + i][0] for i in range(0, N, step)], fontsize=7)
        ax.set_xlim(0, N + N * 0.06)
        ax.legend(loc="upper left", fontsize=8, ncol=2, frameon=False)
        self.rel_canvas.draw_idle()


if __name__ == "__main__":
    App(selftest="--selftest" in sys.argv)
