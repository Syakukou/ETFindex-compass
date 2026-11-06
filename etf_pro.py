# -*- coding: utf-8 -*-
"""
ETF 量化分析终端 (PySide6 专业桌面软件, 暗色终端风格)
- 六页签: 评分排名 / K线图表 / 相关性矩阵 / 风险收益 / 板块轮动 / 进场时点
- 进场时点: 宽基指数 x ETF资金 x 股指期货基差持仓 x 期权PCR隐波, 四维对账
- 分析引擎: etf_analytics.py (四因子评分/相关性/轮动/每日结论)
- 数据: 复用 etf_app.py 的 SQLite 与刷新逻辑
"""
import os
import sys
import threading
from pathlib import Path

import matplotlib
matplotlib.use("QtAgg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg, NavigationToolbar2QT
from matplotlib.figure import Figure
from matplotlib.patches import Rectangle
from matplotlib.ticker import FuncFormatter

from PySide6.QtCore import Qt, QTimer, Signal, QThread
from PySide6.QtGui import QColor, QFont, QAction, QIcon
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QTabWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QLabel, QPushButton, QComboBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QTextEdit, QSplitter, QGroupBox, QStatusBar,
    QToolBar, QSizePolicy, QListWidget, QListWidgetItem, QLineEdit, QCheckBox, QDialog,
    QMessageBox)

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))


def _restore_stdio():
    """打包成窗口程序后 stdout/stderr 是 None。

    行情接口内部用进度条往 stderr 写, 一写就抛 AttributeError, 整个快照接口
    整批失败: 表现为价格和份额全是「—」, 而日K、期货因为不走进度条反而正常。
    挂到 devnull 上即可, 窗口程序不需要这些输出。
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8",
                                        errors="ignore"))
            except Exception:
                pass


_restore_stdio()
from etf_analytics import Analyzer, full_ind  # noqa: E402
from etf_meta import looks_qdii  # noqa: E402
import etf_app  # noqa: E402
from etf_app import refresh_data    # noqa: E402
import etf_flow  # noqa: E402
import etf_hold  # noqa: E402
import etf_inst  # noqa: E402
import etf_minute  # noqa: E402

# 主题与品牌: 自用版 / 发布版共用一套逻辑, 外观由 brand 决定
import brand  # noqa: E402

BG = brand.BG; PANEL = brand.PANEL; PANEL2 = brand.PANEL2
TXT = brand.TXT; SUB = brand.SUB; UP = brand.UP; DOWN = brand.DOWN
ACCENT = brand.ACCENT; GRID = brand.GRID; WARN = brand.WARN

matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
matplotlib.rcParams["axes.unicode_minus"] = False

DARK_RC = {
    "figure.facecolor": PANEL, "axes.facecolor": PANEL,
    "axes.edgecolor": GRID, "axes.labelcolor": TXT,
    "xtick.color": SUB, "ytick.color": SUB,
    "grid.color": GRID, "text.color": TXT,
    "axes.titlesize": 11, "xtick.labelsize": 8, "ytick.labelsize": 8,
}
plt.rcParams.update(DARK_RC)

# 走势页的周期选项 → 分钟接口的 period 参数
PERIOD_MIN = {"5分钟": "5", "15分钟": "15", "30分钟": "30", "60分钟": "60"}

# 周期档位: 从细到粗。滚轮与上下键沿这条序列连续缩放, 缩到本档最小/最大再走一步就跨档,
# 和券商软件的手感一致(往上推变细, 往下推变粗)。
PERIODS = ["分时", "5分钟", "15分钟", "30分钟", "60分钟", "日K", "周K", "月K"]
ZOOM_MIN_BARS = 30      # 同一周期内最多放大到只看 30 根
ZOOM_RATIO = 1.25       # 每格滚轮改变可见根数的比例
AUTO_VIS = 120          # 滚轮跨到更细的周期时先给这么多根, 不至于一次摊开几百根
FLOW_PERIODS = ["日K", "周K", "月K"]   # 进出页只有日频资金流, 最细到日K
FLOW_DEF_VIS = 60
FLOW_ZOOM_RATIO = 1.6   # 进出页横轴更短, 用更大的步长, 三格就能从近60根看到周K


def _bucket_key(d, mode):
    """日期 -> 分组键: 周K 用 ISO 周, 月K 用自然月, 其余原样。"""
    d = str(d)
    if mode != "W":
        return d[:7]
    import datetime as _dt
    try:
        y, w, _ = _dt.date(*[int(x) for x in d[:10].split("-")]).isocalendar()
        return "%04d-W%02d" % (y, w)
    except Exception:
        return d


def _fmt_vol(y, _pos=None):
    """成交量刻度: 用万/亿, 免得出现 1e7 那种科学计数。"""
    try:
        y = float(y)
    except Exception:
        return ""
    if abs(y) >= 1e8:
        return "%.1f亿" % (y / 1e8)
    if abs(y) >= 1e4:
        return "%.0f万" % (y / 1e4)
    return "%.0f" % y


def _fmt_bar_label(s, label):
    """分钟线横轴带时分, 月K只留年月, 其余用完整日期。"""
    s = str(s)
    if "分钟" in label and len(s) >= 16:
        return s[5:16]
    if label == "月K":
        return s[:7]
    return s


def _intraday_ticks(times):
    """分时横轴刻度: 09:30 / 10:30 / 11:30-13:00 / 14:00 / 15:00。

    用分钟数就近取命中位置, 5分钟线的时间戳是 09:35 这种, 不能按字符串相等匹配。
    """
    targets = [(9, 30, "09:30"), (10, 30, "10:30"), (11, 30, "11:30/13:00"),
               (14, 0, "14:00"), (15, 0, "15:00")]

    def _m(t):
        try:
            return int(str(t)[11:13]) * 60 + int(str(t)[14:16])
        except Exception:
            return None

    ms = [_m(t) for t in times]
    idx, labs = [], []
    for hh, mm, lab in targets:
        want = hh * 60 + mm
        hit = next((i for i, m in enumerate(ms) if m is not None and m >= want), None)
        if hit is not None and hit not in idx:
            idx.append(hit)
            labs.append(lab)
    return idx, labs

QSS = f"""
QMainWindow, QWidget {{ background: {BG}; color: {TXT}; font-size: 12px; }}
QTabWidget::pane {{ border: 1px solid {GRID}; background: {PANEL}; }}
QTabBar::tab {{ background: {PANEL2}; color: {SUB}; padding: 8px 18px;
    border: 1px solid {GRID}; border-bottom: none; }}
QTabBar::tab:selected {{ color: {TXT}; background: {PANEL};
    border-top: 2px solid {ACCENT}; }}
QTableWidget {{ background: {PANEL}; gridline-color: {GRID}; color: {TXT};
    border: 1px solid {GRID}; }}
QHeaderView::section {{ background: {PANEL2}; color: {SUB};
    border: 1px solid {GRID}; padding: 5px; font-weight: bold; }}
QTableWidget::item:selected {{ background: {ACCENT}; color: white; }}
QPushButton {{ background: {ACCENT}; color: white; border: none;
    padding: 6px 16px; border-radius: 4px; }}
QPushButton:disabled {{ background: {GRID}; color: {SUB}; }}
QComboBox {{ background: {PANEL2}; color: {TXT}; border: 1px solid {GRID};
    padding: 4px 8px; border-radius: 3px; }}
QTextEdit {{ background: {PANEL}; border: 1px solid {GRID};
    font-family: "Microsoft YaHei"; font-size: 13px; padding: 8px; }}
QStatusBar {{ background: {PANEL2}; color: {SUB}; }}
QToolBar {{ background: {PANEL2}; border-bottom: 1px solid {GRID}; spacing: 8px; }}
QLabel#title {{ font-size: 15px; font-weight: bold; color: {TXT}; }}
QLabel#meta {{ color: {SUB}; font-size: 11px; }}
QGroupBox {{ border: 1px solid {GRID}; border-radius: 5px; margin-top: 10px;
    color: {SUB}; font-weight: bold; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; }}
"""


def color_item(text, v, fmt="{:+.2f}", colorize=True):
    it = QTableWidgetItem(text)
    it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
    if colorize and v is not None:
        it.setForeground(QColor(UP if v > 0 else DOWN if v < 0 else SUB))
    return it


def color_of(v):
    """数值 -> 涨跌色, 空值取次级灰"""
    if v is None:
        return QColor(SUB)
    return QColor(UP if v > 0 else DOWN if v < 0 else SUB)


def n0(v, d=2):
    return "—" if v is None else format(v, ".%df" % d)


def short_name(n, k=8):
    """图轴/窄表用的短名: 去掉 ETF 后缀, 过长截断"""
    s = str(n).replace("ETF", "").replace("指数", "")
    return s if len(s) <= k else s[:k - 1] + "…"


def hit_axs(ev, axs):
    """点击是否落在这几个轴的地盘上, 用像素框判定。

    不能用 ev.inaxes: ax.twinx() 会盖在主轴上, 事件命中的是最上层那个
    (比如资金柱上的基差轴、K线上的均线轴), inaxes 不等于主轴, 点哪都没反应。
    """
    if ev.x is None or ev.y is None:
        return False
    for a in axs:
        try:
            if a.bbox.contains(ev.x, ev.y):
                return True
        except Exception:
            continue
    return False


def _ffill(vals):
    """按日期对齐后补空值: 跨境指数比 A 股少一个交易日, 缺的那天沿用前收。

    开头若还缺(标的上市晚于窗口), 用第一个有效值回填, 避免前段断线。
    """
    out = list(vals)
    last = None
    for i, v in enumerate(out):
        if v is None:
            out[i] = last
        else:
            last = v
    first = next((v for v in out if v is not None), None)
    for i in range(len(out)):
        if out[i] is None:
            out[i] = first
    return out


class RefreshThread(QThread):
    done = Signal(int, list)

    def run(self):
        try:
            ok, errs = refresh_data()
        except Exception as e:
            ok, errs = 0, [str(e)]
        self.done.emit(ok, errs)


class FlowThread(QThread):
    """四维数据取数线程: 指数日线 + 期货 + 期权, 合计约 6 秒"""
    done = Signal(dict)

    def __init__(self, scores, db):
        super().__init__()
        self.scores = scores
        self.db = db

    def run(self):
        try:
            import etf_flow
            d = etf_flow.snapshot(self.scores, self.db)
        except Exception as e:
            d = {"rows": [], "err": ["%s: %s" % (type(e).__name__, e)]}
        # 打包成窗口程序后 stderr 看不见, 取数失败只会表现为"整页空白";
        # 自用版落一行日志到 exe 同级, 出问题能直接翻原因。发布版不写。
        if not brand.PUBLISH:
            try:
                import time as _t
                with open(str(Path(etf_app.DB).parent / "flow_debug.log"), "a",
                          encoding="utf-8") as f:
                    f.write("[%s] rows=%d fut=%d opt=%d err=%s\n" % (
                        _t.strftime("%Y-%m-%d %H:%M:%S"), len(d.get("rows") or []),
                        len(d.get("fut") or {}), len(d.get("opt") or {}), d.get("err")))
            except Exception:
                pass
        self.done.emit(d)


class InstThread(QThread):
    """拉季度机构持仓(社保/QFII/保险/券商/信托/基金) + 陆股通持股。"""
    tick = Signal(str)
    done = Signal(int, int, list)

    def __init__(self, db):
        super().__init__()
        self.db = db

    def run(self):
        errs = []
        n1 = n2 = 0
        try:
            q = etf_inst.quarters(1)[0]
        except Exception as e:
            self.done.emit(0, 0, ["%s: %s" % (type(e).__name__, e)])
            return
        try:
            self.tick.emit("拉机构持仓 %s…" % q)
            n1, e1 = etf_inst.refresh_inst(self.db, q, force=True)
            errs += e1
            self.tick.emit("机构 %d 行, 拉陆股通…" % n1)
            n2 = etf_inst.refresh_hsgt(self.db, q, force=True)
            self.tick.emit("陆股通 %d 只" % n2)
        except Exception as e:
            errs.append("%s: %s" % (type(e).__name__, e))
        self.done.emit(n1, n2, errs)


class MinuteThread(QThread):
    """分钟/分时取数。网络慢时不至于把界面卡住。"""
    done = Signal(str, str, object)

    def __init__(self, code, period, db):
        super().__init__()
        self.code = code
        self.period = str(period)
        self.db = db

    def run(self):
        try:
            d = etf_minute.fetch(self.code, self.period, self.db)
        except Exception as e:
            d = {"err": "%s: %s" % (type(e).__name__, e)}
        self.done.emit(self.code, self.period, d)


class ChartWidget(QWidget):
    """matplotlib 画布 + 工具栏封装"""
    def __init__(self, figsize=(9, 6), toolbar=True):
        super().__init__()
        self.fig = Figure(figsize=figsize, dpi=96)
        self.fig.patch.set_facecolor(PANEL)
        self.canvas = FigureCanvasQTAgg(self.fig)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 0)
        lay.setSpacing(0)
        if toolbar:
            self.toolbar = NavigationToolbar2QT(self.canvas, self)
            lay.addWidget(self.toolbar)
        lay.addWidget(self.canvas)

    def clear(self):
        self.fig.clear()

    def draw(self):
        self.canvas.draw_idle()


class ProWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{brand.TITLE} · {brand.VERSION}")
        self.resize(1460, 980)
        self.analyzer = None
        self.refreshing = False
        self._sort_key = "score"
        self._sort_desc = True
        self._view_rows = []
        self._sort_def = {}
        self._hdr_base = []
        self._build()
        self._load()

    # ---------------- UI 构建 ----------------
    def _build(self):
        tb = QToolBar()
        self.addToolBar(tb)
        self.btn_refresh = QPushButton(" 刷新数据 ")
        self.btn_refresh.clicked.connect(self.on_refresh)
        tb.addWidget(self.btn_refresh)
        self.meta_lbl = QLabel(" 加载中… ")
        self.meta_lbl.setObjectName("meta")
        tb.addWidget(self.meta_lbl)
        tb.addWidget(QLabel("    口径: 日K · 评分=动量40%+趋势30%+资金20%+风险10% · 不构成投资建议 "))

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self._tab_overview()
        self._tab_kline()
        self._tab_corr()
        self._tab_risk()
        self._tab_rot()
        self._tab_index()
        self._tab_hold()
        # 成分股表第一次显示时按真实窗口宽度压一次列宽(渲染时窗口可能还没成型)
        self._hold_tab_idx = self.tabs.count() - 1
        self.tabs.currentChanged.connect(self._on_tab_changed)

        sb = QStatusBar()
        self.setStatusBar(sb)
        self.status = QLabel("就绪")
        sb.addWidget(self.status)

    def _tab_overview(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        # 顶部: 每日结论
        self.brief_box = QTextEdit()
        self.brief_box.setReadOnly(True)
        self.brief_box.setMaximumHeight(142)
        lay.addWidget(QLabel("每日分析结论"))
        lay.addWidget(self.brief_box)
        # 过滤条
        frow = QHBoxLayout()
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("过滤: 输入代码 / 名称 / 分类, 如 科创、588、黄金")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(lambda _: self.render_scores())
        frow.addWidget(self.filter_edit, 1)
        self.jump_chk = QCheckBox("点名称跳K线")
        self.jump_chk.setChecked(True)
        self.jump_chk.setToolTip("只有点「名称」格才跳到 K线图; 点其它数字列只做左侧联动, 不切页。\n"
                                 "双击任意一格始终跳转。")
        frow.addWidget(self.jump_chk)
        self.count_lbl = QLabel("")
        self.count_lbl.setObjectName("meta")
        frow.addWidget(self.count_lbl)
        lay.addLayout(frow)
        # 评分表
        self.score_tbl = QTableWidget()
        self.score_tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.score_tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.score_tbl.setAlternatingRowColors(True)
        cols = [("rank", "排名", 44, False), ("name", "名称", 176, False),
                ("cat", "分类", 72, False), ("score", "综合评分", 74, True),
                ("f_mom", "动量分", 56, True), ("f_trend", "趋势分", 56, True),
                ("f_flow", "资金分", 56, True), ("f_risk", "风险分", 56, True),
                ("pct", "今日涨跌%", 74, True),
                ("mom20", "20日%", 66, True), ("mom60", "60日%", 66, True),
                ("ytd", "年内%", 60, True), ("vol", "年化波动%", 74, True),
                ("sharpe", "夏普", 56, True), ("mdd", "最大回撤%", 74, False),
                ("main_in", "主力(万)", 76, True), ("amount", "成交额(亿)", 84, True),
                ("premium", "溢价%*", 64, True),
                ("rsi", "RSI", 50, True), ("macdSt", "MACD", 50, True)]
        self.score_tbl.setColumnCount(len(cols))
        self.score_tbl.setHorizontalHeaderLabels([c[1] for c in cols])
        for i, c in enumerate(cols):
            self.score_tbl.setColumnWidth(i, c[2])
        self.score_cols = [c[0] for c in cols]
        self._sort_def = {c[0]: c[3] for c in cols}
        self._hdr_base = [c[1] for c in cols]
        self.score_tbl.setSortingEnabled(False)
        hh = self.score_tbl.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionsClickable(True)
        hh.setToolTip("点击任意列标题排序, 再点同一列切换升/降序")
        hh.sectionClicked.connect(self._on_sort)
        self.score_tbl.verticalHeader().setVisible(False)
        self.score_tbl.clicked.connect(self._pick_from_score)
        self.score_tbl.doubleClicked.connect(self._on_score_dbl)
        lay.addWidget(self.score_tbl)
        self.tabs.addTab(w, brand.TABS["rank"])

    def _tab_kline(self):
        w = QWidget()
        lay = QHBoxLayout(w)
        left = QVBoxLayout()
        self.cat_cb = QComboBox()
        self.cat_cb.currentIndexChanged.connect(lambda _: self._fill_list())
        left.addWidget(self.cat_cb)
        from PySide6.QtWidgets import QListWidget
        self.etf_list = QListWidget()
        self.etf_list.currentItemChanged.connect(self._pick_from_list)
        left.addWidget(self.etf_list, 1)
        lw = QWidget(); lw.setLayout(left); lw.setMaximumWidth(270)
        lay.addWidget(lw)
        right = QVBoxLayout()
        self.period_cb = QComboBox()
        self.period_cb.addItems(PERIODS)
        self.period_cb.setCurrentText("日K")
        self.period_cb.setToolTip("也可以直接用滚轮或 ↑ ↓ 缩放切换, 手感跟券商软件一样")
        self.period_cb.currentTextChanged.connect(self._on_period_change)
        self.sub_mode = QComboBox()
        self.sub_mode.addItems(["MACD", "RSI", "KDJ"])
        self.sub_mode.currentTextChanged.connect(lambda _: self.draw_kline())
        top = QHBoxLayout()
        top.addWidget(QLabel("周期:"))
        top.addWidget(self.period_cb)
        top.addSpacing(10)
        top.addWidget(QLabel("副图:"))
        top.addWidget(self.sub_mode)
        self.zoom_hint = QLabel("滚轮 / ↑↓ 缩放切周期")
        self.zoom_hint.setObjectName("meta")
        top.addWidget(self.zoom_hint)
        top.addStretch(1)
        self.kinfo = QLabel("")
        self.kinfo.setObjectName("meta")
        top.addWidget(self.kinfo)
        right.addLayout(top)
        self.kchart = ChartWidget(figsize=(10, 7))
        self.kchart.canvas.mpl_connect("button_press_event", self._kline_click)
        self.kchart.canvas.mpl_connect("button_press_event", self._intraday_click)
        self.kchart.canvas.mpl_connect("scroll_event", self._kline_scroll)
        self.kchart.canvas.mpl_connect("key_press_event", self._kline_key)
        self.kchart.canvas.setFocusPolicy(Qt.StrongFocus)
        self.kchart.setToolTip("滚轮放大缩小切周期, ↑↓ 同效; 单击看某个交易日的明细")
        self._k_ctx = None
        self._i_ctx = None
        self._mdata = {}       # (code, period) -> 已取到的分钟数据
        self._mthread = None   # 只保留最新一次请求, 防止连点刷屏
        self._vis = None       # 当前周期下可见根数, None = 全部
        self._bar_total = 0
        right.addWidget(self.kchart, 1)
        rw = QWidget(); rw.setLayout(right)
        lay.addWidget(rw, 1)
        self.tabs.addTab(w, brand.TABS["kline"])

        # 分时是活的: 停在这一页且选着分时时, 每分钟重取一次
        self._live = QTimer(self)
        self._live.setInterval(60000)
        self._live.timeout.connect(self._live_tick)
        self._live.start()

    def _tab_corr(self):
        w = QWidget()
        lay = QHBoxLayout(w)
        ctrl = QVBoxLayout()
        g1 = QGroupBox("范围")
        v1 = QVBoxLayout(g1)
        self.corr_scope = QComboBox()
        self.corr_scope.addItems(["全部", "各分类龙头", "成交额前25", "成交额前40",
                                  "评分前20", "当前分类"])
        self.corr_scope.setToolTip("默认「全部」= 池里所有有 K 线的标的都进矩阵。\n"
                                   "同指数不同管理人的 ETF 相关性恒接近 1, 全表会一片红; "
                                   "看分散化价值请用「各分类龙头」")
        v1.addWidget(self.corr_scope)
        hb0 = QHBoxLayout()
        hb0.setSpacing(4)
        self.corr_cnt = QLabel("参与计算 — 只")
        self.corr_cnt.setObjectName("meta")
        self.corr_cnt.setWordWrap(True)
        hb0.addWidget(self.corr_cnt, 1)
        bc = QPushButton(" 名单 ")
        bc.setToolTip("列出这次参与计算的每一只, 以及被排除的标的与原因")
        bc.clicked.connect(self._corr_names_dialog)
        hb0.addWidget(bc)
        v1.addLayout(hb0)
        g2 = QGroupBox("窗口(交易日)")
        v2 = QVBoxLayout(g2)
        self.corr_days = QComboBox()
        self.corr_days.addItems(["60", "120", "250"])
        v2.addWidget(self.corr_days)
        btn = QPushButton(" 重算相关性 ")
        btn.clicked.connect(self.draw_corr)
        v2.addWidget(btn)
        ctrl.addWidget(g1)
        ctrl.addWidget(g2)
        self.corr_info = QLabel(
            "怎么读: 一格=两只ETF日涨跌的同步程度。深红(≈1)同涨同跌, 同时买两只等于\n"
            "把仓位压在一个方向上; 偏绿或发暗(≈0)各走各的, 组合才能真的降波动。\n"
            "行序按相似度聚过, 红块连成一片 = 那一团是同一个风险源。\n"
            "点矩阵里任意一格(或下面配对表任一行), 图下方立刻出这两只的\n"
            "归一走势 + 滚动20日相关, 看它们到底是哪天开始分岔的。\n"
            "配对表选「全部配对」= 列出所有 n(n−1)/2 对, 点列头排序、搜索框按代码筛。")
        self.corr_info.setWordWrap(True)
        self.corr_info.setObjectName("meta")
        ctrl.addWidget(self.corr_info)
        self.corr_pair_lbl = QLabel("配对清单")
        self.corr_pair_lbl.setObjectName("meta")
        ctrl.addWidget(self.corr_pair_lbl)
        self.corr_pair_mode = QComboBox()
        self.corr_pair_mode.addItems(["全部配对", "同涨 前40", "分散 前40", "同涨+分散 各40"])
        self.corr_pair_mode.setToolTip("矩阵里每一对都能点; 这里是把极端配对直接列出来, 免得靠眼睛找。\n"
                                       "选「全部配对」会把 n×(n−1)/2 对全部列出来, 再多也能靠搜索和排序找到")
        self.corr_pair_mode.currentIndexChanged.connect(lambda _: self._fill_corr_pairs())
        ctrl.addWidget(self.corr_pair_mode)
        self.corr_pair_search = QLineEdit()
        self.corr_pair_search.setPlaceholderText("筛配对: 代码或名称")
        self.corr_pair_search.setClearButtonEnabled(True)
        self.corr_pair_search.textChanged.connect(self._corr_filter_later)
        ctrl.addWidget(self.corr_pair_search)
        pt = QTableWidget()
        pt.setColumnCount(4)
        pt.setHorizontalHeaderLabels(["关系", "标的A", "标的B", "相关"])
        pt.verticalHeader().setVisible(False)
        pt.verticalHeader().setDefaultSectionSize(21)
        pt.setEditTriggers(QAbstractItemView.NoEditTriggers)
        pt.setSelectionBehavior(QAbstractItemView.SelectRows)
        pt.setAlternatingRowColors(True)
        pt.setColumnWidth(0, 40)
        pt.setColumnWidth(1, 110)
        pt.setColumnWidth(2, 110)
        pt.setColumnWidth(3, 48)
        ph = pt.horizontalHeader()
        ph.setSectionResizeMode(2, QHeaderView.Stretch)
        ph.setSectionResizeMode(3, QHeaderView.Fixed)
        ph.setSectionResizeMode(0, QHeaderView.Fixed)
        pt.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        pt.clicked.connect(self._on_corr_pair_pick)
        ph.setSectionsClickable(True)
        ph.sectionClicked.connect(self._corr_sort_by)
        ph.setSortIndicatorShown(True)
        ph.setSortIndicator(3, Qt.DescendingOrder)
        self.corr_pairs = pt
        ctrl.addWidget(pt, 1)
        cw = QWidget()
        cw.setLayout(ctrl)
        cw.setFixedWidth(384)   # 再窄配对表 4 列就被裁掉了(横滚是关掉的)
        lay.addWidget(cw)
        self.corr_chart = ChartWidget(figsize=(9, 7.6))
        lay.addWidget(self.corr_chart, 1)
        self.corr_chart.canvas.mpl_connect("button_press_event", self._corr_click)
        self.corr_chart.canvas.mpl_connect("motion_notify_event", self._corr_hover)
        self._corr_pair = None
        self._corr_ctx = None
        self._corr_show = []
        self._corr_sort = (3, True)          # (列, 是否降序)
        self._corr_filter = QTimer(self)
        self._corr_filter.setSingleShot(True)
        self._corr_filter.setInterval(260)
        self._corr_filter.timeout.connect(lambda: self._fill_corr_pairs())
        self._corr_pairs_all = []
        self._corr_names_all = []
        # 换范围/换窗口立刻重算: 以前只认「重算」按钮, 选了「全部」看着还是那几个
        self.corr_scope.currentIndexChanged.connect(lambda _: self._corr_reset())
        self.corr_days.currentIndexChanged.connect(lambda _: self._corr_reset())
        self.tabs.addTab(w, brand.TABS["corr"])

    def _corr_reset(self):
        """换范围/窗口后旧的那对标的已不在同一张表里, 先清掉再重算。"""
        self._corr_pair = None
        self.draw_corr()

    def _corr_names_dialog(self):
        """把这次真正参与计算的和被排除的名单摊开, 免得只看到一个 n。"""
        a = self.analyzer
        part = getattr(self, "_corr_part", []) or []
        used = {c for c, _ in part}
        lines = ["参与计算 %d 只 · 近 %s 个交易日" % (len(part), self.corr_days.currentText()),
                 "", "代码      名称"]
        for c, nm in sorted(part, key=lambda x: x[1]):
            lines.append("%s  %s" % (c, nm))
        out = [e for e in a.etfs if e["code"] not in used]
        if out:
            lines += ["", "未参与 %d 只 · 代码      名称" % len(out)]
            for e in out:
                k = a.kline.get(e["code"]) or []
                why = "无K线(未上市)" if not k else "K线 %d 日, 不足窗口" % len(k)
                lines.append("%s  %s  ·  %s" % (e["code"], e["name"], why))
        d = QDialog(self)
        d.setWindowTitle("参与相关性计算的标的")
        d.resize(540, 640)
        v = QVBoxLayout(d)
        te = QTextEdit()
        te.setReadOnly(True)
        te.setPlainText("\n".join(lines))
        v.addWidget(te)
        b = QPushButton(" 复制名单 ")
        b.clicked.connect(lambda: QApplication.clipboard().setText("\n".join(lines)))
        v.addWidget(b)
        d.exec()

    def _tab_risk(self):
        w = QWidget()
        lay = QHBoxLayout(w)
        left = QVBoxLayout()
        lbl = QLabel(
            "怎么读\n"
            "横轴=这段时间抖了多少(区间波动幅度), 纵轴=这段涨跌。\n"
            "左上=涨得多、抖得少; 右下=又抖又亏。颜色=收益÷波动,\n"
            "红(≥1)=赚的比抖的多, 绿(负)=白担风险。\n"
            "点圆点 → 下表自动滚到该行并高亮; 点表格行 → 图上圈出该点, 两边随时对得上。\n"
            "双击圆点或表格行 = 跳去K线。下表列的是散点里的全部标的。")
        lbl.setWordWrap(True)
        lbl.setObjectName("meta")
        left.addWidget(lbl)
        row = QHBoxLayout()
        row.addWidget(QLabel("区间"))
        self.risk_win = QComboBox()
        self.risk_win.addItems(["今日", "3日", "5日", "10日", "15日", "20日", "60日"])
        self.risk_win.setCurrentText("5日")
        self.risk_win.setToolTip("换区间后横纵轴一起重算, 点位的相对位置会变")
        self.risk_win.currentIndexChanged.connect(lambda _: self.draw_risk())
        row.addWidget(self.risk_win, 1)
        left.addLayout(row)

        self.risk_scope = QComboBox()
        self.risk_scope.addItems(["全部", "成交额前40", "当前分类"])
        self.risk_scope.setToolTip("默认全部(155 只左右, 圆点大小=成交额); 嫌挤可只看成交额前 40")
        self.risk_scope.currentIndexChanged.connect(lambda _: self.draw_risk())
        left.addWidget(self.risk_scope)

        self.risk_sel_lbl = QLabel("点图中圆点看名称")
        self.risk_sel_lbl.setWordWrap(True)
        self.risk_sel_lbl.setStyleSheet(
            "color:%s; background:%s; border-left:3px solid %s; padding:4px 8px;"
            % (TXT, PANEL2, ACCENT))
        left.addWidget(self.risk_sel_lbl)

        rl = QLabel("全部标的按收益/波动排序 · 点列头换排序")
        rl.setObjectName("meta")
        self.risk_cnt = rl
        left.addWidget(rl)
        rt = QTableWidget()
        rt.setColumnCount(6)
        rt.setHorizontalHeaderLabels(["名称", "代码", "分类", "区间%", "波动%", "收益/波动"])
        rt.verticalHeader().setVisible(False)
        rt.verticalHeader().setDefaultSectionSize(22)
        rt.setEditTriggers(QAbstractItemView.NoEditTriggers)
        rt.setSelectionBehavior(QAbstractItemView.SelectRows)
        rt.setAlternatingRowColors(True)
        for i, wd in enumerate((118, 56, 78, 54, 54, 62)):
            rt.setColumnWidth(i, wd)
        rh0 = rt.horizontalHeader()
        rh0.setSectionResizeMode(0, QHeaderView.Stretch)
        for i in range(1, 6):
            rh0.setSectionResizeMode(i, QHeaderView.Fixed)
        rh0.setSectionsClickable(True)
        rh0.sectionClicked.connect(self._on_risk_sort)
        rt.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        rt.clicked.connect(self._on_risk_pick)
        rt.doubleClicked.connect(self._on_risk_dbl)
        self.risk_tbl = rt
        self._risk_sort = (5, True)      # (列, 是否降序)
        left.addWidget(rt, 1)
        self._risk_pts = []
        self._risk_all = []
        self._risk_ranked = []
        self._risk_ax = None
        self._risk_sel = None
        self._risk_no_scroll = False
        lw = QWidget()
        lw.setLayout(left)
        lw.setFixedWidth(438)      # 6 列要放得下, 太窄右侧列会被裁掉
        lay.addWidget(lw)
        self.risk_chart = ChartWidget(figsize=(9.4, 7))
        lay.addWidget(self.risk_chart, 1)
        self.risk_chart.canvas.mpl_connect("button_press_event", self._risk_click)
        self.risk_chart.canvas.mpl_connect("motion_notify_event", self._risk_hover)
        self.risk_chart.canvas.mpl_connect("resize_event", self._risk_resize)
        self.tabs.addTab(w, brand.TABS["risk"])

    # ---- 散点交互: 点选看名称, 双击跳 K线
    def _risk_hit(self, ev, tol=13):
        try:
            r = self.risk_chart.canvas.devicePixelRatio() or 1.0
        except Exception:
            r = 1.0
        tol = tol * max(1.0, r)
        ax = self._risk_ax
        xy = getattr(self, "_risk_xy", None)
        if ax is None or ev.x is None or xy is None or not len(xy):
            return None
        d = (xy[:, 0] - ev.x) ** 2 + (xy[:, 1] - ev.y) ** 2
        i = int(np.argmin(d))
        return i if d[i] < tol * tol else None

    def _risk_sync_xy(self):
        """画布尺寸变了, 显示坐标要重算, 否则鼠标总是点不中圆点。"""
        d = getattr(self, "_risk_data", None)
        if self._risk_ax is None or not d:
            self._risk_xy = None
            return
        try:
            self._risk_xy = self._risk_ax.transData.transform(
                np.column_stack([d[0], d[1]]))
        except Exception:
            self._risk_xy = None

    def _risk_resize(self, _ev):
        self._risk_sync_xy()

    def _risk_click(self, ev):
        i = self._risk_hit(ev)
        if i is None:
            return
        p = self._risk_all[i]
        if getattr(ev, "dblclick", False):
            self._select_code(p["code"])
            self.tabs.setCurrentIndex(1)
            return
        self._risk_sel = p["code"]
        self.draw_risk()
        self.status.setText("%s (%s) · %s %+.2f%% · 波动 %.2f%% · 收益/波动 %s · 已定位到下表"
                            % (p["name"], p["code"], self.risk_win.currentText(),
                               p["ret"], p["amp"] or 0, n0(p["ratio"], 2)))

    def _risk_hover(self, ev):
        if ev.inaxes is None or self._risk_ax is None:
            return
        i = self._risk_hit(ev)
        if i is None:
            return
        p = self._risk_all[i]
        self.risk_sel_lbl.setText(
            "%s (%s)\n%s %+.2f%% · 波动 %.2f%% · 收益/波动 %s"
            % (p["name"], p["code"], self.risk_win.currentText(), p["ret"],
               p["amp"] or 0, n0(p["ratio"], 2)))

    def _on_risk_pick(self, idx):
        """右边表点行 -> 左边散点高亮 + 十字线。行是用户自己点的, 不要把它滚走。"""
        if 0 <= idx.row() < len(self._risk_pts):
            p = self._risk_pts[idx.row()]
            self._risk_sel = p["code"]
            self._risk_no_scroll = True
            self.draw_risk()
            self.status.setText("%s (%s) · %s %+.2f%% · 波动 %.2f%% · 收益/波动 %s · 已在散点图定位"
                                % (p["name"], p["code"], self.risk_win.currentText(),
                                   p["ret"], p["amp"] or 0, n0(p["ratio"], 2)))

    def _on_risk_dbl(self, idx):
        if 0 <= idx.row() < len(self._risk_pts):
            self._select_code(self._risk_pts[idx.row()]["code"])
            self.tabs.setCurrentIndex(1)

    def _tab_rot(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(6, 4, 6, 4)
        v.setSpacing(4)

        self.rot_concl = QLabel("")
        self.rot_concl.setWordWrap(True)
        self.rot_concl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.rot_concl.setStyleSheet(
            "color:%s; background:%s; border-left:3px solid %s; padding:5px 9px; font-size:12px;"
            % (TXT, PANEL2, ACCENT))
        v.addWidget(self.rot_concl)

        body = QHBoxLayout()
        body.setSpacing(7)

        # ---- 左: 窗口 + 分类排名表
        left = QWidget()
        left.setFixedWidth(468)
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(3)
        wrow = QHBoxLayout()
        wrow.setSpacing(4)
        wl = QLabel("动量窗口")
        wl.setObjectName("meta")
        wrow.addWidget(wl)
        self.rot_win = QComboBox()
        for lab, wv in (("今日", 0), ("3日", 3), ("5日", 5), ("10日", 10),
                        ("15日", 15), ("20日", 20), ("60日", 60)):
            self.rot_win.addItem(lab, wv)
        self.rot_win.setCurrentIndex(5)
        self.rot_win.setFixedWidth(78)
        self.rot_win.currentIndexChanged.connect(lambda _: self.draw_rot())
        wrow.addWidget(self.rot_win)
        hint = QLabel("流入/流出按日逐只累加, 非席位主力资金")
        hint.setObjectName("meta")
        hint.setToolTip("资金 = 成交额 × CLV, CLV=(2×收-高-低)/(高-低), 收在当日区间上半为流入、\n"
                        "下半为流出。它是二级市场买卖压力的代理, 不是席位主力净买额。")
        wrow.addWidget(hint, 1)
        lv.addLayout(wrow)

        rcols = [("name", "分类", 152, False), ("pct", "今日%", 52, True),
                 ("cur", "动量%", 56, True), ("fin", "流入亿", 54, True),
                 ("fout", "流出亿", 54, True), ("fnet", "净额亿", 56, True)]
        self.rot_cols = [c[0] for c in rcols]
        self.rot_hdr = [c[1] for c in rcols]
        rt = QTableWidget()
        rt.setEditTriggers(QAbstractItemView.NoEditTriggers)
        rt.setSelectionBehavior(QAbstractItemView.SelectRows)
        rt.setSelectionMode(QAbstractItemView.SingleSelection)
        rt.setAlternatingRowColors(True)
        rt.setColumnCount(len(rcols))
        rt.setHorizontalHeaderLabels([c[1] for c in rcols])
        rt.verticalHeader().setVisible(False)
        rt.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
        rt.verticalHeader().setDefaultSectionSize(24)
        for i, c in enumerate(rcols):
            rt.setColumnWidth(i, c[2])
        rh = rt.horizontalHeader()
        rh.setSectionResizeMode(0, QHeaderView.Stretch)
        for i in range(1, len(rcols)):
            rh.setSectionResizeMode(i, QHeaderView.Fixed)
        rt.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        rh.setSectionsClickable(True)
        rh.setToolTip("点列标题排序, 再点一次切升降")
        rh.setDefaultAlignment(Qt.AlignRight | Qt.AlignVCenter)
        rh.sectionClicked.connect(self._on_rot_sort)
        rt.clicked.connect(self._on_rot_pick)
        hf = QFont()
        hf.setPointSize(9)
        rt.horizontalHeader().setFont(hf)
        rt.horizontalHeaderItem(0).setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.rot_tbl = rt
        self.rot_sort, self.rot_desc = "cur", True
        self.rot_view = []
        lv.addWidget(rt, 1)
        body.addWidget(left)

        # ---- 右: 上=柱图, 下=多周期对照
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(3)
        self.rot_bar = ChartWidget(figsize=(9.6, 7.4), toolbar=False)
        rv.addWidget(self.rot_bar, 1)
        body.addWidget(right, 1)
        v.addLayout(body, 1)
        self.rot_bar.canvas.mpl_connect("button_press_event", self._rot_click)
        self._rot_names = []
        self._rot_axes = []
        self.tabs.addTab(w, brand.TABS["rot"])

    # ---- 排序 / 点选
    @property
    def rot_w(self):
        return int(self.rot_win.currentData() or 0)

    def _on_rot_sort(self, col):
        if not (0 <= col < len(self.rot_cols)):
            return
        k = self.rot_cols[col]
        if k in ("name", "n"):
            return
        if k == self.rot_sort:
            self.rot_desc = not self.rot_desc
        else:
            self.rot_sort, self.rot_desc = k, True
        self.draw_rot()

    def _on_rot_pick(self, idx):
        r = idx.row()
        if 0 <= r < len(self.rot_view):
            self.rot_hl = self.rot_view[r]["name"]
            self._draw_rot_bars(self.rot_view, self.rot_w, hl=self.rot_hl)

    def _rot_click(self, ev):
        """点柱图任一根柱 = 选中左侧同一行; 数据用当前窗口的口径。"""
        if getattr(ev, "button", 1) != 1:
            return
        if not hit_axs(ev, getattr(self, "_rot_axes", [])):
            return
        names = getattr(self, "_rot_names", [])
        if not names or ev.ydata is None:
            return
        i = int(round(ev.ydata))
        if not (0 <= i < len(names)) or i >= len(self.rot_view):
            return
        self.rot_hl = names[i]
        t = self.rot_tbl
        t.blockSignals(True)
        t.selectRow(i)
        t.blockSignals(False)
        it = t.item(i, 0)
        if it is not None:
            t.scrollToItem(it, QAbstractItemView.PositionAtCenter)
        self._draw_rot_bars(self.rot_view, self.rot_w, hl=self.rot_hl)
        r = self.rot_view[i]
        self.status.setText(
            "%s · 今日 %s%% · %s %s%% · 流入 %.1f 亿 · 流出 %.1f 亿 · 净额 %+.1f 亿"
            % (r["name"], n0(r["pct"]), self.analyzer.rot_label(self.rot_w), n0(r["cur"], 1),
               r["fin"] or 0, abs(r["fout"] or 0), r["fnet"] or 0))

    def _rot_rows(self, w=None):
        """把 rot 整理成可排序的行"""
        rot = self.analyzer.rot
        w = self.rot_w if w is None else w
        cur_map = rot["cur_by_w"].get(w, {})
        chg_map = rot["chg_by_w"].get(w, {})
        out = []
        for c in rot["cats"]:
            cur = cur_map.get(c)
            out.append({"name": c, "n": rot["n_mem"].get(c, 0),
                        "pct": rot["pct_today"].get(c),
                        "cur": None if cur != cur else cur,
                        "chg": chg_map.get(c, 0),
                        "fin": rot["fin"].get((c, w), 0.0),
                        "fout": rot["fout"].get((c, w), 0.0),
                        "fnet": rot["fnet"].get((c, w), 0.0),
                        "flow": (rot["flow"].get(c) or 0) / 1e4,
                        "rank": rot["rank_by_w"].get(w, {}).get(c)})
        k, desc = self.rot_sort, self.rot_desc
        if k:
            ok = [r for r in out if isinstance(r.get(k), (int, float)) and r.get(k) == r.get(k)]
            no = [r for r in out if r not in ok]
            ok.sort(key=lambda r: r[k], reverse=desc)
            out = ok + no
        return out

    # ---------------- 页6: 指数 x ETF 进出 ----------------
    STATE_BG = {"进场确认": "#8c2f2f", "启动中": "#5a2929",
                "观望": "#2b2f3a", "退潮": "#1f3a33", "撤离": "#1b5344"}
    STATE_FG = {"进场确认": "#ffd9d9", "启动中": "#f0b9b9",
                "观望": SUB, "退潮": "#8fd4bd", "撤离": "#d9fff1"}

    def _lbl_meta(self, txt):
        lb = QLabel(txt)
        lb.setObjectName("meta")
        return lb

    def _tab_index(self):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(6, 4, 6, 4)
        h.setSpacing(7)

        # ---- 左: 指数清单
        lw = QWidget()
        lw.setFixedWidth(212)
        lv = QVBoxLayout(lw)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(3)
        lv.addWidget(self._lbl_meta("点选切换　红底=进 绿底=撤　蓝字=跨境"))

        self.idx_rest = QLabel("")
        self.idx_rest.setObjectName("meta")
        self.idx_rest.setWordWrap(True)
        lv.addWidget(self.idx_rest)
        lt = QTableWidget()
        lt.setEditTriggers(QAbstractItemView.NoEditTriggers)
        lt.setSelectionBehavior(QAbstractItemView.SelectRows)
        lt.setSelectionMode(QAbstractItemView.SingleSelection)
        lt.setShowGrid(False)
        lt.setColumnCount(3)
        lt.setHorizontalHeaderLabels(["指数", "今日%", "评分"])
        lt.verticalHeader().setVisible(False)
        lt.verticalHeader().setDefaultSectionSize(27)
        lh = lt.horizontalHeader()
        lh.setSectionResizeMode(0, QHeaderView.Stretch)
        lh.setSectionResizeMode(1, QHeaderView.Fixed)
        lh.setSectionResizeMode(2, QHeaderView.Fixed)
        lt.setColumnWidth(1, 62)
        lt.setColumnWidth(2, 46)
        lh.setSectionsClickable(False)
        lt.clicked.connect(self._on_idx_pick)
        self.idx_list = lt
        lv.addWidget(lt, 1)
        h.addWidget(lw)

        # ---- 右: 图形 + ETF 明细
        rw = QWidget()
        rv = QVBoxLayout(rw)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(6)
        self.flow_title = QLabel("—")
        top.addWidget(self.flow_title)
        self.flow_plbl = QLabel("日K · 近%d根" % FLOW_DEF_VIS)
        self.flow_plbl.setObjectName("meta")
        top.addWidget(self.flow_plbl)
        self.flow_meta = QLabel("加载中…")
        self.flow_meta.setObjectName("meta")
        top.addWidget(self.flow_meta, 1)
        self.btn_back = QPushButton(" ← 回到全部ETF ")
        self.btn_back.clicked.connect(self._back_to_group)
        self.btn_back.setVisible(False)
        top.addWidget(self.btn_back)
        self.btn_flow = QPushButton(" 刷新数据 ")
        self.btn_flow.clicked.connect(self.reload_index)
        top.addWidget(self.btn_flow)
        rv.addLayout(top)

        self.flow_concl = QLabel("")
        self.flow_concl.setWordWrap(True)
        self.flow_concl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.flow_concl.setMinimumHeight(24)
        self.flow_concl.setStyleSheet(
            "color:%s; background:%s; border-left:3px solid %s; padding:5px 9px; font-size:12px;"
            % (TXT, PANEL2, ACCENT))
        rv.addWidget(self.flow_concl)

        cw = QWidget()
        cv = QVBoxLayout(cw)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(2)
        self.flow_chart = ChartWidget(figsize=(9, 4.2))
        self.flow_chart.canvas.mpl_connect("button_press_event", self._flow_click)
        self.flow_chart.canvas.mpl_connect("scroll_event", self._flow_scroll)
        self.flow_chart.canvas.mpl_connect("key_press_event", self._flow_key)
        self.flow_chart.canvas.setFocusPolicy(Qt.StrongFocus)
        self.flow_chart.setToolTip(
            "滚轮放大缩小: 日K → 周K → 月K; 同一档内继续滚会改看的根数\n"
            "↑↓ 同效, 单击看当天的收盘与资金动能")
        self._flow_ctx = None
        self._flow_p = "日K"
        self._flow_vis = FLOW_DEF_VIS
        self._flow_total = 0
        self._flow_args = None
        cv.addWidget(self.flow_chart, 1)
        self.flow_note = QLabel("")
        self.flow_note.setObjectName("meta")
        self.flow_note.setWordWrap(True)
        cv.addWidget(self.flow_note)
        rv.addWidget(cw, 3)

        tw = QWidget()
        tv = QVBoxLayout(tw)
        tv.setContentsMargins(0, 0, 0, 0)
        tv.setSpacing(2)
        self.idx_detail_lbl = QLabel("跟踪该指数的场内 ETF · 点列标题排序 · 双击跳 K 线")
        self.idx_detail_lbl.setObjectName("meta")
        tv.addWidget(self.idx_detail_lbl)
        dcols = [("name", "ETF 名称", 186, False), ("code", "代码", 64, False),
                 ("pct", "涨跌%", 62, True), ("amt", "成交亿", 70, True),
                 ("mi", "净流入亿", 82, True), ("stg", "强度%", 64, True),
                 ("mom5", "5日动能亿", 78, True), ("prem", "溢价%*", 66, True),
                 ("turnover", "换手%", 60, True), ("vol_ratio", "量比", 54, True)]
        self.det_cols = [c[0] for c in dcols]
        self.det_hdr = [c[1] for c in dcols]
        d = QTableWidget()
        d.setEditTriggers(QAbstractItemView.NoEditTriggers)
        d.setSelectionBehavior(QAbstractItemView.SelectRows)
        d.setSelectionMode(QAbstractItemView.SingleSelection)
        d.setAlternatingRowColors(True)
        d.setColumnCount(len(dcols))
        d.setHorizontalHeaderLabels([c[1] for c in dcols])
        d.verticalHeader().setVisible(False)
        d.verticalHeader().setDefaultSectionSize(22)
        for i, c in enumerate(dcols):
            d.setColumnWidth(i, c[2])
        dh = d.horizontalHeader()
        for i in range(len(dcols)):
            dh.setSectionResizeMode(i, QHeaderView.Stretch)
        dh.setSectionResizeMode(0, QHeaderView.Fixed)
        dh.setSectionResizeMode(1, QHeaderView.Fixed)
        d.setColumnWidth(0, 208)
        d.setColumnWidth(1, 70)
        dh.setSectionsClickable(True)
        dh.setToolTip("点列标题排序, 再点一次切升降")
        dh.sectionClicked.connect(self._on_det_sort)
        d.clicked.connect(self._on_det_pick)
        d.doubleClicked.connect(self._on_idx_detail_pick)
        self.idx_det = d
        tv.addWidget(d, 1)
        rv.addWidget(tw)
        h.addWidget(rw, 1)

        self.tabs.addTab(w, brand.TABS["flow"])
        self.idx_groups = []
        self.cur_idx = None
        self.det_sort, self.det_desc = None, True
        self._det_rows, self._det_code = [], []


    # ---------------- 页7: ETF 成分股 ----------------
    def _tab_hold(self):
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(7)

        left = QVBoxLayout()
        left.setSpacing(3)
        self.hold_meta = self._lbl_meta("标的 · 点选查看重仓股")
        left.addWidget(self.hold_meta)
        self.hold_cat = QComboBox()
        self.hold_cat.addItem("全部")
        self.hold_cat.currentIndexChanged.connect(lambda _: self._fill_hold_list())
        left.addWidget(self.hold_cat)
        self.hold_search = QLineEdit()
        self.hold_search.setPlaceholderText("搜代码 / 名称")
        self.hold_search.setClearButtonEnabled(True)
        self.hold_search.textChanged.connect(lambda _: self._fill_hold_list())
        left.addWidget(self.hold_search)
        self.hold_list = QListWidget()
        self.hold_list.currentItemChanged.connect(self._on_hold_pick)
        # 再点当前已选中的那行不会触发 currentItemChanged, 所以额外接点击:
        # 点左边任何一只都算"放弃看右边那只股票", 橙底必须当场清掉
        self.hold_list.itemClicked.connect(self._on_hold_click)
        left.addWidget(self.hold_list, 1)
        hb = QHBoxLayout()
        b1 = QPushButton(" 重取该只 ")
        b1.setToolTip("忽略本地缓存, 重新拉这一只的季报持仓(会顺带补上一季度, 用于算环比)")
        b1.clicked.connect(lambda: self._load_hold(force=True))
        hb.addWidget(b1)
        b2 = QPushButton(" 复制表格 ")
        b2.setToolTip("把当前表格按屏幕顺序复制成制表符分隔文本, 可直接贴进 Excel / 文档")
        b2.clicked.connect(self._copy_hold)
        hb.addWidget(b2)
        self.hold_inst = QPushButton(" 刷新机构 ")
        self.hold_inst.setToolTip("重新拉季度机构持仓(社保/QFII/保险/券商/信托/基金)与陆股通持股,\n"
                                  "一次覆盖全市场, 约 40 秒。已有缓存时直接读本地库, 不必点它")
        self.hold_inst.clicked.connect(self._refresh_inst)
        hb.addWidget(self.hold_inst)
        left.addLayout(hb)
        hb3 = QHBoxLayout()
        b3 = QPushButton(" 列宽缩到一屏 ")
        b3.setToolTip("把 18 列按比例压进当前窗口宽度, 一屏看完不用拖;\n"
                      "想给某列更多位置, 直接拖表头的分隔线")
        b3.clicked.connect(self._fit_hold_cols)
        hb3.addWidget(b3)
        left.addLayout(hb3)
        lw = QWidget()
        lw.setLayout(left)
        lw.setFixedWidth(262)
        lay.addWidget(lw)

        right = QVBoxLayout()
        right.setSpacing(4)
        self.hold_title = QLabel("选左侧任一 ETF")
        self.hold_title.setWordWrap(True)
        self.hold_title.setStyleSheet(
            "color:%s; background:%s; border-left:3px solid %s; padding:5px 9px;"
            % (TXT, PANEL2, ACCENT))
        right.addWidget(self.hold_title)
        self.hold_src = QLabel("机构/北向数据: 读取中…")
        self.hold_src.setObjectName("meta")
        self.hold_src.setWordWrap(True)
        right.addWidget(self.hold_src)
        qrow = QHBoxLayout()
        qrow.setSpacing(5)
        ql = QLabel("季度")
        ql.setObjectName("meta")
        qrow.addWidget(ql)
        self.hold_q = QComboBox()
        self.hold_q.setToolTip("季报(1季/3季)只披露前十大, 中报/年报才是全部持仓。\n"
                               "库里每一期都存着, 这里按当期行数标出来, 想看全量就切到中报那期。")
        self.hold_q.currentIndexChanged.connect(self._on_hold_q)
        qrow.addWidget(self.hold_q, 1)
        right.addLayout(qrow)
        self._hold_q_guard = False
        ht = QTableWidget()
        hcols = [("rank", "排名", 40), ("stock", "代码", 74), ("sname", "名称", 96),
                 ("pct", "占净值%", 62), ("wchg", "环比pp", 56),
                 ("cum", "累计%", 54),
                 ("shares", "持股万股", 76), ("value", "市值万元", 76),
                 ("price", "现价", 58), ("pct_today", "当日%", 58),
                 ("turnover", "换手%", 54), ("amt", "成交亿", 58),
                 ("pe", "PE", 46), ("mcap", "流通亿", 60),
                 ("hsgt", "北向%", 56), ("mflow", "主力万", 66),
                 ("inst", "社保/险/QFII", 146), ("ind", "所属行业", 96)]
        self.hold_cols = [c[0] for c in hcols]
        ht.setColumnCount(len(hcols))
        ht.setHorizontalHeaderLabels([c[1] for c in hcols])
        ht.verticalHeader().setVisible(False)
        ht.verticalHeader().setDefaultSectionSize(22)
        ht.setEditTriggers(QAbstractItemView.NoEditTriggers)
        ht.setSelectionBehavior(QAbstractItemView.SelectRows)
        ht.setAlternatingRowColors(True)
        for i, c in enumerate(hcols):
            ht.setColumnWidth(i, c[2])
        hh = ht.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)   # 列宽自己拖, 名称不再吃掉全部余量
        hh.setMinimumSectionSize(34)
        hh.setSectionsClickable(True)
        hh.setSortIndicatorShown(True)
        hh.setSortIndicator(0, Qt.AscendingOrder)
        hh.setToolTip("点列标题排序, 再点一次切升降")
        hh.sectionClicked.connect(self._sort_hold)
        ht.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        ht.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        ht.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)   # 18 列总宽超过窗口时必须能拖出来
        self.hold_tbl = ht
        ht.clicked.connect(self._hold_stock_click)
        ht.doubleClicked.connect(self._hold_stock_dbl)
        right.addWidget(ht, 1)
        self.hold_note = QLabel("")
        self.hold_note.setWordWrap(True)
        self.hold_note.setObjectName("meta")
        right.addWidget(self.hold_note)
        lay.addLayout(right, 1)
        self._hold_code = None
        self._hold_hit = None
        self._inst_thread = None
        self._inst_cache = None
        self._hold_vrows = []
        self._hold_ctx = {}
        self._hold_mark = None
        self._hold_meta_txt = ""
        self._hold_fitted = False
        self._hold_sort = (0, False)      # (列, 是否降序), 默认按排名升序
        self.tabs.addTab(w, brand.TABS["hold"])

    def _fill_hold_list(self):
        a = self.analyzer
        if not a:
            return
        cats = sorted({e["category"] for e in a.etfs})
        cur = self.hold_cat.currentText()
        if self.hold_cat.count() <= 1:
            self.hold_cat.blockSignals(True)
            self.hold_cat.addItems(cats)
            self.hold_cat.blockSignals(False)
        try:
            has = etf_hold.cached_codes(str(etf_app.DB))
            cnt = etf_hold.cached_counts(str(etf_app.DB))
        except Exception:
            has, cnt = set(), {}
        kw = self.hold_search.text().strip()
        self.hold_list.blockSignals(True)
        self.hold_list.clear()
        n_all = n_has = 0
        for e in sorted(a.etfs, key=lambda x: (x["category"], x["code"])):
            if cur not in ("全部", "") and e["category"] != cur:
                continue
            if kw and kw not in e["code"] and kw not in e["name"]:
                continue
            n_all += 1
            ok = e["code"] in has
            n_has += 1 if ok else 0
            nrow = cnt.get(e["code"]) or 0
            # ○ 的项要写明"为什么没有": 待上市 / 季报里本来就不含股票
            if ok:
                tail = "  %d" % nrow
            elif e.get("listed") is False:
                tail = "  · 待上市"
            else:
                tail = "  · 无股票持仓"
            it = QListWidgetItem("%s %s %s%s" % ("●" if ok else "○", e["code"], e["name"], tail))
            it.setData(Qt.UserRole, e["code"])
            it.setForeground(QColor(TXT if ok else SUB))
            if ok:
                tip = "%s\n已有季报持仓明细, 最新季度 %d 只" % (e["category"], nrow)
            elif e.get("listed") is False:
                tip = "%s\n%s\n还没上市, 没有行情也没有持仓, 等它上市后会自己出现在这里" % (
                    e["category"], e.get("note") or "")
            else:
                tip = ("%s\n这只不披露股票持仓(货币/黄金/商品/跨境类), 季报里只有基金资产构成, 不是抓漏"
                       % e["category"])
            it.setToolTip(tip)
            it.setData(Qt.UserRole + 1, tip)     # 原始 tooltip, 标橙底时在其后追加持有明细
            self.hold_list.addItem(it)
        pool = len(a.etfs)
        n_listed = sum(1 for e in a.etfs if e.get("listed") is False)
        n_nostock = pool - len(has & {e["code"] for e in a.etfs}) - n_listed
        self._hold_meta_txt = ("当前列表 %d 只 · 有股票明细 %d 只 ｜ 池 %d = 有明细 %d + "
                               "待上市 %d + 无股票持仓 %d"
                               % (n_all, n_has, pool,
                                  len(has & {e["code"] for e in a.etfs}), n_listed, n_nostock))
        self.hold_meta.setText(self._hold_meta_txt)
        self.hold_meta.setToolTip(
            "无股票持仓的 %d 只是货币/黄金/白银/商品期货/跨境类, 基金季报只披露资产构成, "
            "本来就没有股票明细, 不是抓取失败。待上市的 %d 只东财还没有行情与持仓。\n"
            "两只都没有可补的东西, 所以这里不设批量补全按钮; 想重抓当前这只, 点「重取该只」。" %
            (n_nostock, n_listed))
        self._hold_mark = None
        code = self._hold_code
        hit = -1
        for i in range(self.hold_list.count()):
            if self.hold_list.item(i).data(Qt.UserRole) == code:
                hit = i
                break
        if self.hold_list.count():
            if hit < 0 and self._hold_code is None:
                for i in range(self.hold_list.count()):
                    if self.hold_list.item(i).data(Qt.UserRole) in ("510300", "510500", "159915"):
                        hit = i
                        break
            self.hold_list.setCurrentRow(hit if hit >= 0 else 0)
        self.hold_list.blockSignals(False)
        # 输入过滤时不要每次都重新拉持仓: 只有选中的标的真的换了才取数
        cur_item = self.hold_list.currentItem()
        if cur_item is not None and cur_item.data(Qt.UserRole) != code:
            self._load_hold(cur_item.data(Qt.UserRole))

    def _clear_hold_mark(self):
        """撤掉右侧选股票留下的橙底, 并把汇总行恢复成池内统计。"""
        if not getattr(self, "_hold_mark", None):
            return
        self._hold_mark = None
        for i in range(self.hold_list.count()):
            it = self.hold_list.item(i)
            it.setBackground(QColor(0, 0, 0, 0))
            base = it.data(Qt.UserRole + 1)
            if base:
                it.setToolTip(base)      # 去掉追加的「持有 N 期 / 占净值」，别留残影
        self.hold_meta.setText(getattr(self, "_hold_meta_txt", ""))

    def _on_hold_click(self, item):
        if item is None:
            return
        self._clear_hold_mark()
        code = item.data(Qt.UserRole)
        if code and code != self._hold_code:
            self._load_hold(code)

    def _on_hold_pick(self, cur, _prev=None):
        if cur:
            self._clear_hold_mark()      # 换标的 = 不再看刚才那只股票
            self._load_hold(cur.data(Qt.UserRole))

    def _holders_of(self, stock):
        """反查: 哪些 ETF 的披露持仓里有这只票, 按占净值比降序。

        注意这里扫的是库内全部季度, 不是当前看的那一期。季报(Q1/Q3)只披露前十大,
        中报/年报才是全量; 只按当前季查会漏掉一大堆。返回 [(code, quarter, pct)]。
        """
        import sqlite3
        try:
            con = sqlite3.connect(str(etf_app.DB))
            try:
                rows = con.execute(
                    "SELECT code, quarter, pct FROM holding WHERE stock=?",
                    (stock,)).fetchall()
            finally:
                con.close()
        except Exception:
            return []
        best = {}
        qset = {}
        for code, q, pct in rows:
            pct = pct or 0.0
            qset.setdefault(code, set()).add(q)
            cur = best.get(code)
            if cur is None or pct > cur[1]:     # 同一只 ETF 多期持有, 取占比最高的那期
                best[code] = (q, pct)
        return sorted(((c, q, p, qset[c]) for c, (q, p) in best.items()),
                      key=lambda x: -x[2])

    def _hold_stock_click(self, idx):
        """点右边某只股票, 左边列表把持有它的 ETF 标成橙底; 再点一次取消。"""
        vrows = self._hold_vrows or []
        if not (0 <= idx.row() < len(vrows)):
            return
        r = vrows[idx.row()]
        stock = r["code"]
        if getattr(self, "_hold_mark", None) == stock:
            self._clear_hold_mark()
            return
        hits = self._holders_of(stock)
        if not hits:
            self.hold_meta.setText(
                "%s %s · 池内 134 只有披露的 ETF 里没有别家持有它 —— 不是没显示, "
                "是它们的季报里确实没有这只票(未持有, 或本季只披露前十大)"
                % (stock, r["name"]))
            return
        codes = {c for c, _, _, _ in hits}
        # 当前筛选只露出其中几只(或一只都不露)时, 橙底标了也看不出"被这么多家持有",
        # 直接放开筛选再标 —— 用户点这只票就是要看全套持有人
        vis = {self.hold_list.item(i).data(Qt.UserRole)
               for i in range(self.hold_list.count())}
        n_vis = len(codes & vis)
        dropped = bool(self.hold_search.text().strip()) or \
            self.hold_cat.currentText() not in ("全部", "")
        if dropped and n_vis < len(codes):
            self.hold_cat.blockSignals(True)
            self.hold_search.blockSignals(True)
            self.hold_cat.setCurrentText("全部")
            self.hold_search.clear()
            self.hold_cat.blockSignals(False)
            self.hold_search.blockSignals(False)
            self._fill_hold_list()      # 保住当前正在看的那只, 不被重建带走
            self._auto_clear = n_vis
        self._hold_mark = stock
        detail = {c: (q, p) for c, q, p, _ in hits}
        cur_q = (self._hold_ctx or {}).get("hit", {}).get("quarter") or ""
        n_cur = sum(1 for _, _, _, qs in hits if cur_q in qs)
        label = "%s %s" % (stock, r["name"])
        self._mark_holders(codes, label, len(hits), detail, n_cur, cur_q)
        if getattr(self, "_auto_clear", None) is not None:
            self.hold_meta.setText(self.hold_meta.text() +
                                   " · 原筛选只露出 %d 只, 已自动切回全部" % self._auto_clear)
            self._auto_clear = None

    def _mark_holders(self, codes, label, n_all=0, detail=None, n_cur=0, cur_q=""):
        n_vis = 0
        first = -1
        for i in range(self.hold_list.count()):
            it = self.hold_list.item(i)
            code = it.data(Qt.UserRole)
            hot = code in codes
            it.setBackground(QColor("#5a4320") if hot else QColor(0, 0, 0, 0))
            base = it.data(Qt.UserRole + 1) or ""
            if hot:
                n_vis += 1
                if first < 0:
                    first = i
                if detail and code in detail:
                    q, pct = detail[code]
                    it.setToolTip("%s\n这只票在本库的持有: %s, 占净值 %.2f%%"
                                  % (base, self._qlab(q), pct or 0))
                else:
                    it.setToolTip(base)
            elif base:
                it.setToolTip(base)
        if not codes:
            self.hold_meta.setText(getattr(self, "_hold_meta_txt", "标的 · 点选查看重仓股"))
            return
        # 橙底那一堆可能在列表很下面, 不滚过去等于没标
        if first >= 0:
            self.hold_list.scrollToItem(self.hold_list.item(first),
                                        QAbstractItemView.PositionAtCenter)
        extra = ""
        if cur_q:
            lab = self._qlab(cur_q)
            if n_cur == n_all:
                extra = " (本季 %s 这 %d 只全部都在)" % (lab, n_all)
            elif n_cur == 0:
                extra = (" (本季 %s 只披露前十大, 这 %d 只全部来自中报/年报全量, "
                         "不是界面少显示了)" % (lab, n_all))
            else:
                extra = (" (本季 %s 只有 %d 只披露到它, 其余 %d 只来自中报/年报全量, "
                         "不是界面少显示了)" % (lab, n_cur, n_all - n_cur))
        self.hold_meta.setText(
            "%s 被 %d 只 ETF 披露持有%s · 橙底标出, 悬停看是哪一期持有、占净值多少 · 双击行跳到仓位最重的"
            % (label, n_all, extra))

    def _hold_stock_dbl(self, idx):
        """双击右边股票 = 跳到持有它仓位最重的那只 ETF。"""
        vrows = self._hold_vrows or []
        if not (0 <= idx.row() < len(vrows)):
            return
        hits = self._holders_of(vrows[idx.row()]["code"])
        if not hits:
            return
        code = hits[0][0]
        self._clear_hold_mark()      # 双击会先触发一次 clicked 标橙底, 换 ETF 前先撤掉
        self._load_hold(code)
        for i in range(self.hold_list.count()):
            if self.hold_list.item(i).data(Qt.UserRole) == code:
                self.hold_list.setCurrentRow(i)
                break

    @staticmethod
    def _qlab(q):
        """'2026年3季度股票投资明细' -> '2026Q3'"""
        s = str(q)
        if "股票投资明细" in s:
            return s.replace("年", "Q").replace("季度股票投资明细", "")
        return s or "—"

    def _fill_hold_q(self, hit):
        """季度下拉。每期都标出当期行数, 季报 15 只 / 中报 100 只一眼能看出来。"""
        qs = hit.get("quarters") or []
        cnt = hit.get("qcount") or {}
        cur = hit.get("quarter") or ""
        self._hold_q_guard = True
        self.hold_q.blockSignals(True)
        self.hold_q.clear()
        for q in reversed(qs):          # 新期在前
            self.hold_q.addItem("%s · %d 只" % (self._qlab(q), cnt.get(q, 0)), q)
        i = self.hold_q.findData(cur)
        if i >= 0:
            self.hold_q.setCurrentIndex(i)
        self.hold_q.blockSignals(False)
        self._hold_q_guard = False
        self.hold_q.setEnabled(self.hold_q.count() > 1)

    def _on_hold_q(self, _i=0):
        if getattr(self, "_hold_q_guard", False):
            return
        q = self.hold_q.currentData()
        if q and self._hold_code:
            self._load_hold(self._hold_code, quarter=q)

    def _load_hold(self, code=None, force=False, quarter=None):
        a = self.analyzer
        if not a:
            return
        code = code or self._hold_code
        if not code:
            return
        self._hold_code = code
        e = next((x for x in a.etfs if x["code"] == code), None)
        name = e["name"] if e else code
        label = "%s %s" % (code, name)
        try:
            hit = etf_hold.fetch(code, str(etf_app.DB), force=force, quarter=quarter)
        except Exception as ex:
            self.hold_title.setText("%s · 取持仓失败: %s: %s" % (label, type(ex).__name__, ex))
            self._hold_vrows = []
            self.hold_tbl.setRowCount(0)
            return
        self._fill_hold_q(hit)
        self._hold_hit = dict(hit, name=name)
        rows = hit.get("rows") or []
        if not rows:
            self.hold_title.setText("%s · 暂无股票持仓披露" % label)
            self._hold_vrows = []
            self.hold_tbl.setRowCount(0)
            if hit.get("err"):
                self.hold_note.setText(
                    "这次没取到(%s)。季报接口偶发超时, 点「刷新该只」重试。" % hit["err"])
            else:
                self.hold_note.setText(
                    "货币/债券类 ETF 的资产在债券与存单里, 没有股票明细; "
                    "跨境 ETF 持有的是海外股票, 也常常不披露到明细; "
                    "新成立未披露季报的同样查不到。")
            return
        q = hit.get("quarter") or ""
        pq = (hit.get("prev_quarter") or "").replace("股票投资明细", "")
        self.hold_title.setText(
            "%s · %s　前十大合计 <b>%.2f%%</b>　共 %d 只%s"
            % (label, q.replace("股票投资明细", ""), hit.get("top10") or 0, len(rows),
               ("　环比基准 %s" % pq) if pq else ""))
        self.hold_title.setTextFormat(Qt.RichText)
        qs = etf_hold.stock_quotes([r["stock"] for r in rows])
        idate, inst_map, hsgt_map = self._inst_maps()
        vrows = []
        cum = 0.0
        n_new = 0
        for r in rows:
            cum += r["pct"] or 0
            if r.get("new"):
                n_new += 1
            q0 = qs.get(r["stock"]) or {}
            # 北向: 季度末香港中央结算持股 / 当前流通股本
            ratio = None
            hg = hsgt_map.get(r["stock"])
            if hg and q0.get("price") and q0.get("mcap"):
                float_shares = q0["mcap"] * 1e8 / q0["price"]
                if float_shares > 0:
                    ratio = (hg["shares"] or 0) / float_shares * 100
            txt, long = etf_inst.summary(r["stock"], inst_map)
            d = inst_map.get(r["stock"]) or {}
            i_n = sum((v.get("n") or 1) for k, v in d.items() if k != "基金")
            vrows.append(dict(
                rank=r["rank"], code=r["stock"], name=r["sname"], pct=r["pct"],
                wchg=r.get("delta"), new=bool(r.get("new")), prev=r.get("prev"),
                cum=cum, shares=r["shares"], value=r["value"],
                price=q0.get("price"), chg=q0.get("pct"), turnover=q0.get("turnover"),
                amt=q0.get("amt"), pe=q0.get("pe"), mcap=q0.get("mcap"),
                hsgt=ratio, mflow=q0.get("mflow"), has_q=bool(q0),
                inst=txt, inst_tip=long, inst_n=i_n, ind=q0.get("ind")))
        self._hold_vrows = vrows
        cnt = hit.get("qcount") or {}
        # 全量期优先取最近的一期(中报/年报都是全量, 别把去年中报当参考)
        bestq = max(cnt, key=lambda x: (cnt[x], etf_hold._qkey(x))) if cnt else ""
        self._hold_ctx = dict(hit=hit, idate=idate, n_new=n_new, idate_ok=bool(idate),
                              q_best=(cnt.get(bestq, 0), bestq), q_cur=q,
                              foreign=sum(1 for r in rows
                                          if not (len(r["stock"]) == 6 and r["stock"].isdigit())))
        self._apply_hold_sort(render=False)
        self._render_hold()

    def _render_hold(self):
        t = self.hold_tbl
        vrows = self._hold_vrows or []
        t.setRowCount(len(vrows))
        for i, r in enumerate(vrows):
            t.setItem(i, 0, self._cell("%d" % r["rank"]))
            itc = self._lcell(r["code"])
            itc.setToolTip(r["code"])
            t.setItem(i, 1, itc)
            nm = self._lcell(r["name"])
            nm.setToolTip(r["name"])
            t.setItem(i, 2, nm)
            t.setItem(i, 3, color_item("%.2f" % (r["pct"] or 0), r["pct"]))
            # 环比: 与上一季度同一只票的占净值比之差(百分点)
            dl = r["wchg"]
            if dl is not None:
                it = color_item("%+.2f" % dl, dl)
                it.setToolTip("上季 %.2f%% → 本季 %.2f%%" % (r["prev"] or 0, r["pct"] or 0))
            elif r["new"]:
                it = self._cell("新")
                it.setForeground(QColor(WARN))
                it.setToolTip("上一季度明细里没有这只: 可能是新进, 也可能上季没披露到它")
            else:
                it = self._cell("—")
                it.setForeground(QColor(SUB))
                it.setToolTip("没有上一季度明细可比")
            t.setItem(i, 4, it)
            ic = self._cell("%.2f" % r["cum"])
            ic.setForeground(QColor(SUB))
            ic.setToolTip("按权重从大到小累加, 不随排序变化")
            t.setItem(i, 5, ic)
            t.setItem(i, 6, self._cell("%.0f" % (r["shares"] or 0)))   # 东财持股数就是万股
            t.setItem(i, 7, self._cell("%.0f" % (r["value"] or 0)))
            if not r["has_q"]:
                for c in (8, 9, 10, 11, 12, 13, 15, 17):
                    it = self._cell("—")
                    it.setForeground(QColor(SUB))
                    t.setItem(i, c, it)
            else:
                t.setItem(i, 8, self._cell(n0(r["price"], 2)))
                t.setItem(i, 9, color_item("%+.2f" % (r["chg"] or 0), r["chg"]))
                tv = r["turnover"]
                t.setItem(i, 10, self._cell("—" if tv is None else "%.2f" % tv))
                av = r["amt"]
                t.setItem(i, 11, self._cell("—" if av is None else "%.2f" % av))
                pe = r["pe"]
                ipe = self._cell("—" if pe is None else ("亏" if pe < 0 else "%.1f" % pe))
                if pe is not None and pe < 0:
                    ipe.setForeground(QColor(WARN))
                t.setItem(i, 12, ipe)
                mc = r["mcap"]
                t.setItem(i, 13, self._cell("—" if mc is None else "%.0f" % mc))
                mf = r["mflow"]
                t.setItem(i, 15, color_item("—" if mf is None else "%+.0f" % mf, mf))
                ind = self._lcell(r["ind"] or "—")
                ind.setForeground(QColor(SUB))
                t.setItem(i, 17, ind)
            rt = r["hsgt"]
            if rt is None:
                ih = self._cell("—")
                ih.setForeground(QColor(SUB))
                ih.setToolTip("十大流通股东里没有香港中央结算(北向每日持股已停止披露)")
            else:
                ih = self._cell("%.2f" % rt)
                ih.setToolTip("陆股通(香港中央结算)持股, 按当前流通股本折算\n"
                              "口径: 十大流通股东里的香港中央结算, 未进前十大的看不到")
            t.setItem(i, 14, ih)
            ii = self._lcell(r["inst"] or "—")
            if r["inst"]:
                ii.setForeground(QColor(ACCENT if ("社保" in r["inst"] or "险" in r["inst"]) else TXT))
                ii.setToolTip(r["inst_tip"])
            else:
                ii.setForeground(QColor(SUB))
                ii.setToolTip(r["inst_tip"] or "没有机构持仓披露")
            t.setItem(i, 16, ii)
        ctx = self._hold_ctx or {}
        hit = ctx.get("hit") or {}
        idate = ctx.get("idate")
        note = ("口径: 东财基金持仓(季报披露, 滞后于最新持仓); 环比=与上一季度占净值比之差(百分点); "
                "北向=十大流通股东里的香港中央结算 ÷ 当前流通股本(近似); "
                "机构=社保险资等家数, 取自东财机构持仓, 报告期 %s。" % (idate or "—"))
        bn, bq = ctx.get("q_best") or (0, "")
        if bq and bn > len(vrows):
            note += (" 本期 %s 只披露前十大, 共 %d 只; %s 是全量持仓 %d 只, "
                     "上方「季度」切过去就能看全部。"
                     % (self._qlab(ctx.get("q_cur") or ""), len(vrows),
                        self._qlab(bq), bn))
        if ctx.get("n_new"):
            note += (" 标「新」的 %d 只: 上季明细里没有它 —— 可能真新进, 也可能上季披露不到它"
                     "(上季披露 %d 只 / 本季 %d 只)。"
                     % (ctx["n_new"], hit.get("prev_rows") or 0, len(vrows)))
        if ctx.get("foreign"):
            note += " 其中 %d 只海外代码(QDII), 不取实时行情。" % ctx["foreign"]
        if hit.get("err"):
            note += " 本次取数异常(%s), 显示的是本地缓存。" % hit["err"]
        note += " 点表头可排序。"
        self.hold_note.setText(note)

    _HOLD_KEYS = (("rank", 0), ("code", 1), ("name", 2), ("pct", 3), ("wchg", 4),
                  ("cum", 5), ("shares", 6), ("value", 7), ("price", 8), ("chg", 9),
                  ("turnover", 10), ("amt", 11), ("pe", 12), ("mcap", 13),
                  ("hsgt", 14), ("mflow", 15), ("inst_n", 16), ("ind", 17))

    def _hold_key(self, r, col):
        for k, c in self._HOLD_KEYS:
            if c == col:
                return r.get(k)
        return None

    def _sort_hold(self, col):
        """点表头排序: 数字列首点降序, 文本列首点升序, 再点切反; 空值恒排最后。"""
        if not self._hold_vrows:
            return
        old_col, old_desc = getattr(self, "_hold_sort", (0, False))
        if old_col == col:
            desc = not old_desc
        else:
            desc = col not in (1, 2, 17)
        self._hold_sort = (col, desc)
        self._apply_hold_sort(render=True)

    def _apply_hold_sort(self, render=True):
        col, desc = getattr(self, "_hold_sort", (0, False))
        hv = list(self._hold_vrows or [])
        if hv:
            has, non = [], []
            for r in hv:
                v = self._hold_key(r, col)
                (non if v is None or v == "" else has).append(r)
            try:
                has.sort(key=lambda r: self._hold_key(r, col), reverse=desc)
            except TypeError:
                has.sort(key=lambda r: str(self._hold_key(r, col)), reverse=desc)
            self._hold_vrows = has + non
        hh = self.hold_tbl.horizontalHeader()
        hh.setSortIndicator(col, Qt.DescendingOrder if desc else Qt.AscendingOrder)
        if render:
            pos = self.hold_tbl.verticalScrollBar().value()
            self._render_hold()
            self.hold_tbl.verticalScrollBar().setValue(pos)

    def _on_tab_changed(self, idx):
        if idx == getattr(self, "_hold_tab_idx", -1):
            QTimer.singleShot(60, self._fit_hold_once)

    def _fit_hold_once(self):
        """首次进成分股页: 18 列装不下就按窗口压一次, 之后尊重用户自己拖的宽度。"""
        if getattr(self, "_hold_fitted", False):
            return
        t = self.hold_tbl
        if t.viewport().width() < 400:
            return
        self._hold_fitted = True
        if sum(t.columnWidth(i) for i in range(t.columnCount())) > t.viewport().width():
            self._fit_hold_cols()

    def _fit_hold_cols(self):
        """把 18 列按当前比例压进窗口: 一屏看完, 不用横向拖。"""
        t = self.hold_tbl
        base = [t.columnWidth(i) for i in range(t.columnCount())]
        if len(base) != 18 or sum(base) <= 0:
            base = [40, 64, 84, 50, 46, 44, 60, 64, 48, 48, 44, 46, 38, 50, 46, 50, 104, 66]
        avail = t.viewport().width() - 6
        if avail < 300:
            return
        tot = sum(base)
        if tot <= avail:
            w = [int(x * avail / tot) for x in base]
        else:
            w = [max(34, int(x * avail / tot)) for x in base]
        for i, x in enumerate(w):
            t.setColumnWidth(i, x)
        self.status.setText("列宽已缩到一屏 (%d px)" % sum(w))


    def _inst_maps(self):
        """机构/北向映射, 进程内缓存; 点「刷新机构」更新后清空。"""
        d = getattr(self, "_inst_cache", None)
        if d:
            return d
        try:
            date, inst, hs = etf_inst.load(str(etf_app.DB))
        except Exception:
            date, inst, hs = None, {}, {}
        self._inst_cache = (date, inst, hs)
        if hasattr(self, "hold_src"):
            self._set_inst_src(date, inst, hs)
        return self._inst_cache

    def _set_inst_src(self, date, inst, hs, extra=""):
        if not date:
            self.hold_src.setText(
                "机构/北向: 本地还没有数据 —— 点左边「刷新机构」拉一次(约 40 秒), "
                "拉完这两列才有数。")
            return
        self.hold_src.setText(
            "机构/北向: 报告期 <b>%s</b> · 覆盖 %d 只股票 · 陆股通 %d 只%s"
            % (date, len(inst), len(hs), (" · " + extra) if extra else ""))
        self.hold_src.setTextFormat(Qt.RichText)

    def _copy_hold(self):
        if not self._hold_hit:
            return
        hit = dict(self._hold_hit)
        if self._hold_vrows:
            by = {r["code"]: r for r in hit.get("rows") or []}
            seq = [by[r["code"]] for r in self._hold_vrows if r["code"] in by]
            if seq:
                hit["rows"] = seq      # 按当前排序导出, 和屏幕顺序一致
        txt = etf_hold.copy_text(hit, hit.get("name", ""))
        QApplication.clipboard().setText(txt)
        self.hold_note.setText("已复制 %d 行到剪贴板(按当前排序)。" % len(hit.get("rows") or []))

    def _refresh_inst(self):
        th = getattr(self, "_inst_thread", None)
        if th and th.isRunning():
            self.status.setText("机构/北向还在拉, 稍等")
            return
        self._inst_cache = None
        th = InstThread(str(etf_app.DB))
        th.tick.connect(lambda s: self.status.setText(s))
        th.done.connect(self._inst_done)
        self._inst_thread = th
        self.hold_inst.setEnabled(False)
        self.hold_inst.setText(" 拉取中… ")
        self.hold_note.setText("正在拉机构持仓与陆股通持股, 全市场一次, 约 40 秒…")
        th.start()

    def _inst_done(self, n1, n2, errs):
        self.hold_inst.setEnabled(True)
        self.hold_inst.setText(" 刷新机构 ")
        self._inst_cache = None
        date, inst, hs = self._inst_maps()
        extra = ("异常: " + "; ".join(errs[:2])) if errs else "刚刚更新"
        self._set_inst_src(date, inst, hs, extra)
        if self._hold_code:
            self._load_hold(self._hold_code)     # 先重画表(它会改 hold_note), 再写提示
        if errs:
            self.hold_note.setText(
                "机构/北向部分失败(%s)。已拿到的部分照样写进库里并显示, 可再点一次重试。"
                % "; ".join(errs[:2]))
        else:
            self.hold_note.setText(
                "机构/北向更新完成: 机构 %d 条、陆股通 %d 只, 报告期 %s。"
                "表里「北向%%」「社保/险/QFII」两列已按新数据重算。" % (n1, n2, date or "—"))
        self.status.setText("机构 %d 条 · 陆股通 %d 只%s"
                            % (n1, n2, (" · 异常: " + "; ".join(errs[:2])) if errs else ""))
        if self._hold_code:
            self._load_hold(self._hold_code)
    # ---- 左表 + 明细渲染
    def render_index(self):
        rows = self.idx_groups
        lt = self.idx_list
        lt.setRowCount(len(rows))
        fb = QFont()
        fb.setBold(True)
        for i, g in enumerate(rows):
            it = QTableWidgetItem(g["name"])
            # 跨境指数用蓝字: 底色仍表示评分状态, 蓝字说明它跟的是隔夜外盘
            if g["code"] in etf_flow.CROSS_IDX:
                it.setForeground(QColor(ACCENT))
            else:
                it.setForeground(QColor(self.STATE_FG.get(g["state"], TXT)))
            it.setBackground(QColor(self.STATE_BG.get(g["state"], PANEL2)))
            it.setFont(fb)
            lt.setItem(i, 0, it)
            p = QTableWidgetItem(n0(g["pct"]))
            p.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            p.setBackground(QColor(self.STATE_BG.get(g["state"], PANEL2)))
            p.setForeground(color_of(g["pct"]))
            lt.setItem(i, 1, p)
            sc = QTableWidgetItem(format(g["score"], ".0f"))
            sc.setTextAlignment(Qt.AlignCenter)
            sc.setBackground(QColor(self.STATE_BG.get(g["state"], PANEL2)))
            sc.setForeground(QColor(self.STATE_FG.get(g["state"], TXT)))
            sc.setFont(fb)
            lt.setItem(i, 2, sc)
        if rows:
            tot = len(self.analyzer.scores) if self.analyzer else 0
            na = sum(g["n"] for g in rows if g["code"] not in etf_flow.CROSS_IDX)
            nq = sum(g["n"] for g in rows if g["code"] in etf_flow.CROSS_IDX)
            self.idx_rest.setText("上=A股 %d只 · 下=跨境 %d只 · 其余 %d只行业主题不进此表"
                                  % (na, nq, max(0, tot - na - nq)))
            sel = 0
            if self.cur_idx:
                for i, g in enumerate(rows):
                    if g["code"] == self.cur_idx.get("code"):
                        sel = i
                        break
            else:
                want = ["000300", "000001", "000905", "000852", "399006"]
                sel = max(range(len(rows)), key=lambda i: (rows[i]["n"], rows[i]["score"]))
                for w0 in want:
                    hit = [i for i, g in enumerate(rows) if g["code"] == w0 and g["n"] >= 3]
                    if hit:
                        sel = hit[0]
                        break
            lt.selectRow(sel)
            self._fill_idx_detail(rows[sel])

    def _on_idx_pick(self, idx):
        r = idx.row()
        if 0 <= r < len(self.idx_groups):
            self._fill_idx_detail(self.idx_groups[r])

    def _on_det_sort(self, col):
        if not (0 <= col < len(self.det_cols)):
            return
        k = self.det_cols[col]
        if k in ("name", "code"):
            return
        if self.det_sort == k:
            self.det_desc = not self.det_desc
        else:
            self.det_sort, self.det_desc = k, True
        self._render_det()

    def _render_det(self):
        rows = list(self._det_rows)
        k = self.det_sort
        if k:
            ok = lambda r: isinstance(r.get(k), (int, float)) and r.get(k) == r.get(k)
            have = [r for r in rows if ok(r)]
            none = [r for r in rows if not ok(r)]
            have.sort(key=lambda r: r[k], reverse=self.det_desc)
            rows = have + none
        t = self.idx_det
        t.clearSpans()
        self._det_view = rows
        self._det_code = []
        if not rows:
            t.setRowCount(1)
            t.setSpan(0, 0, 1, len(self.det_cols))
            it = QTableWidgetItem(
                "该指数旗下没有成交额过 5 亿的场内 ETF，资金只能看指数自身的量价")
            it.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            it.setForeground(QColor(SUB))
            t.setItem(0, 0, it)
            t.setFixedHeight(96)
            return
        t.setRowCount(len(rows))
        for i, m in enumerate(rows):
            t.setItem(i, 0, self._lcell(m["name"]))
            t.setItem(i, 1, self._lcell(m["code"]))
            t.setItem(i, 2, color_item(n0(m["pct"]), m["pct"]))
            t.setItem(i, 3, self._cell("%.1f" % m["amt"] if m["amt"] else "—"))
            t.setItem(i, 4, color_item(n0(m["mi"]), m["mi"]))
            t.setItem(i, 5, color_item(n0(m["stg"], 1), m["stg"]))
            t.setItem(i, 6, color_item(n0(m["mom5"], 0), m["mom5"]))
            # QDII 的溢价按 T-1 净值估算, 跨时区品种(纳指/日经)里含着昨夜海外涨跌,
            # 数值天然偏大, 打 * 提醒不能当真实溢价读
            itp = color_item(n0(m["prem"]), m["prem"])
            if m.get("qdii"):
                itp.setText(("⁎" + n0(m["prem"])) if m["prem"] is not None else "—")
                itp.setToolTip("QDII 溢价 = 现价 / 东财 IOPV − 1。IOPV 是 T-1 净值估算,"
                               "跨时区品种还含昨夜海外涨跌与汇率变动, 不是真实溢价;"
                               "要判断限购溢价, 看它相对同类其它 QDII 的高低差。")
            t.setItem(i, 7, itp)
            t.setItem(i, 8, self._cell(n0(m["turnover"])))
            t.setItem(i, 9, self._cell(n0(m["vol_ratio"])))
            self._det_code.append(m["code"])
            if getattr(self, "_sel_etf", None) and m["code"] == self._sel_etf:
                fb = QFont()
                fb.setBold(True)
                for j in range(len(self.det_cols)):
                    it2 = t.item(i, j)
                    if it2 is not None:
                        it2.setFont(fb)
                        it2.setBackground(QColor(PANEL2))
        for i, txt in enumerate(self.det_hdr):
            it = t.horizontalHeaderItem(i)
            if it is not None:
                it.setText(txt + (" ▼" if self.det_desc else " ▲") if self.det_cols[i] == k else txt)
        # 行少就收表, 把空间让给上面那张图
        hgt = len(rows) * 22 + t.horizontalHeader().height() + 8
        t.setFixedHeight(max(96, min(384, hgt)))

    # ---- 选中指数填充
    def _fill_idx_detail(self, g):
        self.cur_idx = g
        self._sel_etf = None
        if hasattr(self, "btn_back"):
            self.btn_back.setVisible(False)
        pc = g.get("pct")
        self.flow_title.setText(
            "<span style='font-size:15px;font-weight:600;'>%s</span>"
            "<span style='font-size:13px;color:%s;'> %s %s</span>"
            % (g["name"], UP if (pc or 0) >= 0 else DOWN, n0(g.get("close")),
               "—" if pc is None else "%+.2f%%" % pc))
        seg = ["评分 <b>%d</b> %s" % (round(g["score"]), g["state"])]
        if g.get("ma_dev") is not None:
            seg.append("MA20 %+.2f%%" % g["ma_dev"])
        if g.get("pos") is not None:
            seg.append("20日位置 %.0f" % g["pos"])
        if g.get("pct5") is not None:
            seg.append("5日 %+.2f%%" % g["pct5"])
        self.flow_meta.setText(" · ".join(seg))

        if not g["n"]:
            self.idx_detail_lbl.setText(
                "%s · 无 5 亿以上场内 ETF，只展示指数走势与现货量能　"
                "点列标题可排序" % g["name"])
        else:
            self.idx_detail_lbl.setText(
                "%s · ETF %d 只 · 合计成交 %.1f 亿 · 合计净流入 %s · 占指数成交 %s　"
                "单击行看单只走势，双击跳 K 线"
                % (g["name"], g["n"], g["eamt"],
                   "—" if g["emi"] is None else "%+.2f 亿" % g["emi"],
                   "—" if g["ratio"] is None else "%.1f%%" % g["ratio"]))

        ms = []
        for m in g["members"]:
            d2 = dict(m)
            d2["stg"] = (m["mi"] / m["amt"] * 100) if (m["mi"] is not None and m["amt"]) else None
            ms.append(d2)
        self._det_rows = ms
        if self.det_sort is None:
            self.det_sort, self.det_desc = "mi", True
        self._render_det()
        self.draw_flow(g)
        self._fill_note(g)

    def _fut_opt_text(self, g):
        d = getattr(self, "flow_data", None) or {}
        fut, opt = d.get("fut") or {}, d.get("opt") or {}
        p, o = g.get("future"), g.get("option")
        seg = []
        f2 = fut.get(p) or {}
        if f2.get("basis") is not None:
            seg.append("对应期货 %s 基差 %+.2f%% · 较20日均 %+.2fpp · 持仓 %+.1f%%"
                       % (p, f2["basis"], f2.get("basis_dev") or 0, f2.get("oi_chg") or 0))
        allf = ["%s %+.2f%%" % (q, (fut.get(q) or {}).get("basis"))
                for q in ("IF", "IH", "IC", "IM")
                if (fut.get(q) or {}).get("basis") is not None]
        if allf:
            seg.append("四品种基差 " + " / ".join(allf))
        o2 = opt.get(o) or {}
        if o2.get("pcr") is not None:
            seg.append("对应期权 %s PCR %.2f · 平值IV %.1f%% · 到期 %s"
                       % (o, o2["pcr"], o2.get("iv") or 0, o2.get("exp_date") or "—"))
        return "　".join(seg) if seg else "期货/期权无数据"

    def _fill_note(self, g):
        self.flow_note.setText(self._fut_opt_text(g))

    # ---- 结论条
    def _flow_ready(self, d):
        self._flow_busy = False
        if getattr(self, "_flow_wd", None):
            self._flow_wd.stop()
        self.btn_flow.setEnabled(True)
        self.btn_flow.setText(" 刷新数据 ")
        if not d or not d.get("rows"):
            err = " / ".join((d or {}).get("err") or ["未知"])
            self.flow_meta.setText("取数失败: " + err)
            self.btn_flow.setText(" 重试 ")
            self.flow_concl.setText(
                "<span style='color:%s'>这一页要联网取指数/期货/期权行情, 这次没取到。</span>"
                "　原因: %s　·　点右上「重试」再来一次; 反复失败就看 exe 同级目录的 "
                "flow_debug.log" % (WARN, err))
            self.flow_note.setText("")
            self._draw_flow_placeholder("取数失败")
            return
        self.flow_data = d
        self.idx_groups = d["rows"]
        fut_n = sum(1 for v in (d.get("fut") or {}).values() if v.get("basis") is not None)
        opt_n = sum(1 for v in (d.get("opt") or {}).values() if v.get("pcr") is not None)
        rows = d["rows"]
        up = [r for r in rows if r["score"] >= 70]
        dn = [r for r in rows if r["score"] < 44]
        div = [r for r in rows if "背离" in r["why"]]
        seg = ["<b>%s</b>　指数 %d · 期货 %d · 期权 %d%s" % (
            d.get("date") or "", len(rows), fut_n, opt_n,
            ("　" + " / ".join(d["err"])) if d.get("err") else "")]
        if up:
            seg.append("确认进场 <b>%s</b>" % "、".join(
                "%s %d" % (r["name"], round(r["score"])) for r in up))
        if dn:
            seg.append("仍在撤 %s" % "、".join(
                "%s %d" % (r["name"], round(r["score"])) for r in dn[:2]))
        if div:
            seg.append("<span style='color:%s'>口径打架 %s</span>" % (
                WARN, "、".join(r["name"] for r in div)))
        self.flow_concl.setText("　·　".join(seg))
        self.render_index()

    # ---- 取数
    def reload_index(self):
        if not self.analyzer:
            return
        if getattr(self, "_flow_busy", False):
            return
        self._flow_busy = True
        self._kl_cache = {}
        self.btn_flow.setEnabled(False)
        self.btn_flow.setText(" 取数中… ")
        self.flow_meta.setText("正在取指数/期货/期权…")
        self._draw_flow_placeholder("正在取数…", "指数/期货/期权行情要联网, 通常 5 到 20 秒")
        self._arm_flow_watchdog()
        th = FlowThread(list(self.analyzer.scores), str(etf_app.DB))
        th.done.connect(self._flow_ready)
        th.done.connect(th.deleteLater)
        self._flow_thread = th
        th.start()

    def _arm_flow_watchdog(self):
        """取数卡死时页面会一直是空的, 到点必须给个说法而不是继续假装在加载。"""
        t = getattr(self, "_flow_wd", None)
        if t is None:
            t = self._flow_wd = QTimer(self)
            t.setSingleShot(True)
            t.timeout.connect(self._flow_timeout)
        t.start(90000)

    def _flow_timeout(self):
        if not getattr(self, "_flow_busy", False):
            return
        self._flow_busy = False
        self.btn_flow.setEnabled(True)
        self.btn_flow.setText(" 重试 ")
        self.flow_meta.setText("取数超过 90 秒没有返回")
        self.flow_concl.setText(
            "<span style='color:%s'>这一页要联网取指数/期货/期权行情, 90 秒没回来。</span>"
            "　点右上「重试」再来一次; 一直不动就看 exe 同级目录的 flow_debug.log" % WARN)
        self._draw_flow_placeholder("取数超时", "网络卡住或被拦截, 点「重试」")

    def _draw_flow_placeholder(self, title, sub=""):
        """没数据时别留一片纯黑, 至少写清楚在等什么。"""
        fig = self.flow_chart.fig
        fig.clear()
        ax = fig.add_subplot(111)
        ax.set_axis_off()
        ax.text(0.5, 0.56, title, ha="center", va="center", color=TXT, fontsize=13,
                transform=ax.transAxes)
        if sub:
            ax.text(0.5, 0.44, sub, ha="center", va="center", color=SUB, fontsize=9.5,
                    transform=ax.transAxes)
        self.flow_chart.draw()

    # ---- 图形
    def _etf_kl(self, code):
        cache = getattr(self, "_kl_cache", None)
        if cache is None:
            cache = self._kl_cache = {}
        if code in cache:
            return cache[code]
        kl = []
        try:
            import etf_flow
            kl = etf_flow.etf_klines(str(etf_app.DB), [code], 250).get(code) or []
        except Exception:
            kl = []
        cache[code] = kl
        return kl

    def _on_det_pick(self, idx):
        """点明细行 -> 上面那张图换成这单只 ETF 自己的资金柱"""
        r = idx.row()
        view = getattr(self, "_det_view", [])
        if not (0 <= r < len(view)):
            return
        m = view[r]
        g = self.cur_idx
        if not g:
            return
        kl = self._etf_kl(m["code"])
        if not kl:
            return
        self._sel_etf = m["code"]
        self.btn_back.setVisible(True)
        self._render_det()
        self.idx_det.selectRow(r)
        pc = m.get("pct")
        self.flow_title.setText(
            "<span style='font-size:12px;color:%s;'>%s ▸ </span>"
            "<span style='font-size:15px;font-weight:600;'>%s</span>"
            "<span style='font-size:12px;color:%s;'> %s</span>"
            "<span style='font-size:13px;color:%s;'> %s %s</span>"
            % (SUB, g["name"], m["name"], SUB, m["code"],
               UP if (pc or 0) >= 0 else DOWN, n0(kl[-1][1], 3),
               "—" if pc is None else "%+.2f%%" % pc))
        seg = []
        if m.get("mi") is not None:
            t0 = "净流入 %+.2f 亿" % m["mi"]
            if m.get("stg") is not None:
                t0 += "(强度 %.1f%%)" % m["stg"]
            seg.append(t0)
        if m.get("amt"):
            seg.append("成交 %.1f 亿" % m["amt"])
        if m.get("mom5") is not None:
            seg.append("5日动能 %+.1f 亿" % m["mom5"])
        if m.get("prem") is not None:
            seg.append("溢价 %+.2f%%" % m["prem"])
        if m.get("turnover") is not None:
            seg.append("换手 %.2f%%" % m["turnover"])
        if m.get("vol_ratio") is not None:
            seg.append("量比 %.2f" % m["vol_ratio"])
        self.flow_meta.setText(" · ".join(seg) if seg else "无当日资金明细")
        self.draw_flow(g, m, kl)
        self.flow_note.setText(
            "该ETF 的资金柱为二级市场买卖压力代理，与指数合计口径不同，"
            "拿单只和合计对照能看出是谁在动　" + self._fut_opt_text(g))

    def _back_to_group(self):
        if self.cur_idx:
            self._fill_idx_detail(self.cur_idx)

    # ---- 进出页的周期与缩放
    @staticmethod
    def _flow_bars(raw, period):
        """原始日线 -> 画图用的 bar 列表(d/o/c/h/l/a/v); 周月K在本地聚合, 不额外发请求。"""
        pts = []
        for r in raw or []:
            try:
                d = str(r[0])
                cl = float(r[1])
                hi = float(r[2]) if r[2] else cl
                lo = float(r[3]) if r[3] else cl
            except (TypeError, ValueError, IndexError):
                continue
            amt = float(r[4]) if len(r) > 4 and r[4] else 0.0
            op = float(r[5]) if len(r) > 5 and r[5] else cl
            vol = float(r[6]) if len(r) > 6 and r[6] else 0.0
            pts.append(dict(d=d, o=op or cl, c=cl, h=max(hi, cl),
                            l=min(lo, cl), a=amt, v=vol))
        if period == "日K" or len(pts) < 3:
            return pts
        mode = "W" if period == "周K" else "M"
        out, key, cur = [], None, None
        for p in pts:
            k = _bucket_key(p["d"], mode)
            if k != key:
                if cur:
                    out.append(cur)
                key = k
                cur = dict(p)
                cur["k"] = k
            else:
                cur["d"] = p["d"]          # 用分组末个交易日做横轴标签
                cur["c"] = p["c"]
                cur["h"] = max(cur["h"], p["h"])
                cur["l"] = min(cur["l"], p["l"])
                cur["a"] += p["a"]
                cur["v"] += p["v"]
        if cur:
            out.append(cur)
        return out

    def _flow_zoom(self, d):
        """d>0 放大(更少根/更细周期), d<0 缩小。日K 是最细一档, 资金流本来就只有日频。"""
        i = FLOW_PERIODS.index(getattr(self, "_flow_p", "日K"))
        total = int(getattr(self, "_flow_total", 0) or 0)
        vis = getattr(self, "_flow_vis", None)
        if vis == "auto":
            vis = min(total, AUTO_VIS) if total else None
        if d > 0:
            if vis is None and total:
                vis = total
            if vis:
                nv = max(min(ZOOM_MIN_BARS, vis),
                         int(round(vis / ZOOM_RATIO)))
                if nv < vis:
                    self._flow_vis = nv
                    self._flow_redraw()
                    return
            if i > 0:
                self._flow_p = FLOW_PERIODS[i - 1]
                self._flow_vis = "auto"
                self._flow_redraw()
            else:
                self.status.setText("进出页最细就是日K: 资金动能只有日频")
            return
        if vis is None:
            if i < len(FLOW_PERIODS) - 1:
                self._flow_p = FLOW_PERIODS[i + 1]
                self._flow_vis = None
                self._flow_redraw()
            else:
                self.status.setText("已经是最大周期: 月K")
            return
        nv = int(round(vis * FLOW_ZOOM_RATIO))
        if nv >= max(1, int(total * 0.55)):
            if i < len(FLOW_PERIODS) - 1:
                self._flow_p = FLOW_PERIODS[i + 1]
                self._flow_vis = None
            else:
                self._flow_vis = None
            self._flow_redraw()
        else:
            self._flow_vis = nv
            self._flow_redraw()

    def _flow_redraw(self):
        if not getattr(self, "_flow_args", None):
            return
        self.draw_flow(*self._flow_args)

    def _flow_scroll(self, ev):
        if ev.button == "up":
            self._flow_zoom(1)
        elif ev.button == "down":
            self._flow_zoom(-1)

    def _flow_key(self, ev):
        if ev.key in ("up", "+", "=", "pageup"):
            self._flow_zoom(1)
        elif ev.key in ("down", "-", "_", "pagedown"):
            self._flow_zoom(-1)

    def draw_flow(self, g, m=None, kl=None):
        """m/kl 有值 = 画单只 ETF; 否则画该指数全部 ETF 的合计。

        上图是蜡烛(日/周/月K), 下图是 5 日资金动能; 滚轮或 ↑↓ 缩放切周期,
        同一档内继续缩就改看的根数, 单击定位到某一天。
        """
        self._flow_args = (g, m, kl)
        c = self.flow_chart
        c.fig.clear()
        one = bool(m and kl)
        period = getattr(self, "_flow_p", "日K")
        raw = (kl if one else (g.get("kline") or [])) or []
        bars = self._flow_bars(raw, period)
        self._flow_total = len(bars)
        if len(bars) < 8:
            self.flow_plbl.setText("%s · 数据不足" % period)
            self._flow_ctx = None
            c.draw()
            return

        # 均线在全序列上算, 再跟着截尾, 否则缩到近 30 根时前段全是空值
        closes = [b["c"] for b in bars]

        def _ma(k):
            out = []
            for i in range(len(closes)):
                seg = closes[max(0, i - k + 1):i + 1]
                out.append(sum(seg) / len(seg))
            return out

        ma5, ma20 = _ma(5), _ma(20)
        vis = getattr(self, "_flow_vis", None)
        if vis == "auto":
            vis = min(len(bars), AUTO_VIS)
            self._flow_vis = vis
        st = 0
        if vis and len(bars) > vis:
            st = len(bars) - int(vis)
        bars, ma5, ma20 = bars[st:], ma5[st:], ma20[st:]
        n = len(bars)
        xs = list(range(n))
        dates = [b["d"] for b in bars]
        closes = [b["c"] for b in bars]
        mode = "" if period == "日K" else ("W" if period == "周K" else "M")
        keys = [(_bucket_key(d, mode) if mode else d) for d in dates]

        # 5 日动能: 日频取当日值; 周/月取该分组末日的值(滚动5日累计不能相加, 会重叠加总)
        src = dict(((m or {}).get("mom_ser") if one else g.get("mom_ser")) or {})
        if mode:
            mom = {}
            for d0, v0 in src.items():
                mom[_bucket_key(d0, mode)] = v0
        else:
            mom = src
        has_flow = bool(mom)
        vals = [mom.get(k) for k in keys]

        if has_flow:
            gs = c.fig.add_gridspec(2, 1, height_ratios=[1.28, 1], hspace=0.14)
            ax1 = c.fig.add_subplot(gs[0])
            ax2 = c.fig.add_subplot(gs[1], sharex=ax1)
        else:
            ax1 = c.fig.add_subplot(111)
            ax2 = None

        o = np.array([b["o"] for b in bars], float)
        cl = np.array(closes, float)
        hi = np.array([b["h"] for b in bars], float)
        lo = np.array([b["l"] for b in bars], float)
        colr = np.where(cl >= o, UP, DOWN)
        ax1.vlines(xs, lo, hi, color=colr, lw=0.7)
        ax1.bar(xs, np.abs(cl - o) + 1e-12, 0.68, bottom=np.minimum(o, cl),
                color=colr, zorder=3)
        ax1.plot(xs, ma5, lw=0.9, color="#f5a623", label="MA5")
        ax1.plot(xs, ma20, lw=0.9, color=ACCENT, ls="--", label="MA20")
        if one:
            ax1.set_title("%s · %s %s · %s %d根" % (
                g["name"], m["name"], m["code"], period, n),
                loc="left", fontsize=10, color=TXT)
            imap = {k[0]: k[1] for k in (g.get("kline") or [])}
            iy = [imap.get(d3) for d3 in dates]
            if any(y is not None for y in iy):
                iy = _ffill(iy)
                ax1b = ax1.twinx()
                li, = ax1b.plot(xs, iy, color=SUB, lw=0.9, alpha=0.6, label="指数(右轴)")
                ax1b.tick_params(labelsize=7, colors=SUB, pad=1)
                ax1b.spines["top"].set_visible(False)
                h1, l1 = ax1.get_legend_handles_labels()
                ax1.legend(h1 + [li], l1 + ["指数(右轴)"], loc="upper left",
                           fontsize=7, frameon=False, ncol=4)
            else:
                ax1.legend(loc="upper left", fontsize=7, frameon=False, ncol=3)
        else:
            ax1.set_title("%s · %s %d根 · 评分 %d %s" % (
                g["name"], period, n, round(g["score"]), g["state"]),
                loc="left", fontsize=10, color=TXT)
            ax1.legend(loc="upper left", fontsize=7, frameon=False, ncol=3)
        ax1.grid(alpha=0.22, ls=":")
        ax1.tick_params(labelbottom=not has_flow, labelsize=7)
        for sp in ("top", "right"):
            ax1.spines[sp].set_visible(False)

        if has_flow:
            lab = "该ETF 5日资金动能(亿)" if one else "ETF 5日资金动能(亿)"
            px = [x for x, v in zip(xs, vals) if v is not None]
            pv = [v for v in vals if v is not None]
            if pv:
                ax2.bar(px, pv, color=[UP if v >= 0 else DOWN for v in pv],
                        width=0.7, alpha=0.85, label=lab)
                # 前段没柱子是本地库没有那段资金记录, 不是零流入, 得说明白
                if px[0] > max(2, int(n * 0.3)):
                    ax2.text(0.99, 0.9, "资金动能自 %s 起有记录" % dates[px[0]],
                             transform=ax2.transAxes, ha="right", va="top",
                             fontsize=6.8, color=SUB,
                             bbox=dict(facecolor=PANEL, alpha=0.7, edgecolor="none",
                                       pad=1.6))
            ax2.axhline(0, color=GRID, lw=0.8)
            ax2.grid(alpha=0.2, ls=":")
            ax2.tick_params(labelsize=7)
            for sp in ("top", "right"):
                ax2.spines[sp].set_visible(False)
            bs = dict(g.get("basis_ser") or {})
            # 基差是日频, 切到周/月K 就不硬套, 免得看着像周度基差
            if bs and not one and period == "日K":
                ax3 = ax2.twinx()
                bys = [bs.get(d3) for d3 in dates]
                bx = [x for x, y in zip(xs, bys) if y is not None]
                by = [y for y in bys if y is not None]
                l3, = ax3.plot(bx, by, color=WARN, lw=1.2, label="期货基差率%(右轴)")
                ax3.tick_params(labelsize=7, colors=WARN, pad=1)
                ax3.spines["right"].set_color(WARN)
                ax3.spines["top"].set_visible(False)
                h1, l1 = ax2.get_legend_handles_labels()
                ax2.legend(h1 + [l3], l1 + ["期货基差率%(右轴)"], loc="upper left",
                           fontsize=7, frameon=False, ncol=2)
            else:
                ax2.legend(loc="upper left", fontsize=7, frameon=False)
            # 最近一次资金方向翻转, 就是图上能看到的进场时点
            mark = None
            for j in range(len(pv) - 1, 0, -1):
                if pv[j - 1] < 0 <= pv[j]:
                    mark = (px[j], "资金转正", UP)
                    break
            if mark is None:
                for j in range(len(pv) - 1, 0, -1):
                    if pv[j - 1] > 0 >= pv[j]:
                        mark = (px[j], "资金转负", DOWN)
                        break
            if mark:
                i0, lab2, cl2 = mark
                right = i0 > n * 0.78
                ax2.axvline(i0, color=cl2, lw=0.9, ls="--", alpha=0.75)
                ax2.annotate(lab2 + " " + dates[i0][5:], xy=(i0, 0),
                             xytext=(-4 if right else 4, 7), textcoords="offset points",
                             fontsize=7, color=cl2, ha="right" if right else "left",
                             bbox=dict(facecolor=PANEL, alpha=0.72, edgecolor="none", pad=1.2))
            step = max(1, n // 8)
            ax2.set_xticks(xs[::step])
            ax2.set_xticklabels([dates[i][5:10] for i in xs[::step]], fontsize=7)
        else:
            step = max(1, n // 8)
            ax1.set_xticks(xs[::step])
            ax1.set_xticklabels([dates[i][5:10] for i in xs[::step]], fontsize=7)
            ax1.set_xlabel("该指数无 5 亿以上场内 ETF，资金与基差维度缺失", fontsize=7, color=SUB)
        c.fig.subplots_adjust(left=0.055, right=0.922, top=0.925, bottom=0.115, hspace=0.14)
        self.flow_plbl.setText("%s · 近%d根 · 滚轮缩放" % (period, n))
        self._flow_ctx = dict(
            dates=dates, close=closes, o=o.tolist(), h=hi.tolist(), l=lo.tolist(),
            mom=vals if has_flow else None,
            axs=[ax1] + ([ax2] if ax2 is not None else []),
            name=(m or {}).get("name") or g.get("name") or "", period=period)
        self._flow_marks = []
        c.draw()

    def _flow_click(self, ev):
        """点走势图/资金柱任一处: 竖线定位 + 当日收盘、涨跌、资金动能。"""
        ctx = getattr(self, "_flow_ctx", None)
        if not ctx or ev.xdata is None:
            return
        if not hit_axs(ev, ctx["axs"]):
            return
        i = int(round(ev.xdata))
        if not (0 <= i < len(ctx["dates"])):
            return
        for art in getattr(self, "_flow_marks", []):
            try:
                art.remove()
            except Exception:
                pass
        self._flow_marks = []
        ax0 = ctx["axs"][0]
        # 点在哪块就用哪块的坐标放框: 点资金柱贴柱子, 点收盘线贴收盘点
        hit = ax0
        for a in ctx["axs"]:
            try:
                if a.bbox.contains(ev.x, ev.y):
                    hit = a
                    break
            except Exception:
                continue
        ln = ax0.axvline(i, color=ACCENT, lw=0.9, alpha=0.9, zorder=8)
        close = ctx["close"][i]
        prev = ctx["close"][i - 1] if i > 0 else close
        chg = (close / prev - 1) * 100 if (prev and close) else 0.0
        per = ctx.get("period") or "日K"
        word = {"日K": "日", "周K": "周", "月K": "月"}.get(per, "")
        txt = "%s\n开 %.3f  高 %.3f  低 %.3f\n收 %s   %s涨跌 %+.2f%%" % (
            ctx["dates"][i], ctx["o"][i], ctx["h"][i], ctx["l"][i],
            "—" if close is None else "%.3f" % close, word, chg)
        mv = ctx["mom"][i] if ctx.get("mom") else None
        if mv is not None:
            txt += "\n5日资金动能 %+.2f 亿" % mv
            # 5日动能是滚动的, 再加一句相对上一根的增减, 看的是加速还是减速
            mi = i - 1
            while mi >= 0 and ctx["mom"][mi] is None:
                mi -= 1
            if mi >= 0:
                txt += " (较前一%s %+.2f)" % (word, mv - ctx["mom"][mi])
        right = i > len(ctx["dates"]) * 0.66
        yv = close if close else 0
        if hit is not ax0 and mv is not None:
            yv = mv
        box = hit.annotate(txt, xy=(i, yv),
                           xytext=(-9 if right else 9, 4), textcoords="offset points",
                           fontsize=7.6, color=TXT, va="top",
                           ha="right" if right else "left",
                           bbox=dict(boxstyle="round,pad=0.4", fc=PANEL2, ec=ACCENT,
                                     lw=0.8, alpha=0.95), zorder=9)
        self._flow_marks = [ln, box]
        self.flow_chart.draw()
        self.status.setText("%s %s · 收 %s · %+.2f%%%s"
                            % (ctx["name"], ctx["dates"][i],
                               "—" if close is None else "%.3f" % close, chg,
                               (" · 动能 %+.2f 亿" % mv) if mv is not None else ""))

    def _on_idx_detail_pick(self, idx):
        if 0 <= idx.row() < len(self._det_code):
            self._select_code(self._det_code[idx.row()])
            self.tabs.setCurrentIndex(1)

    # ---------------- 数据加载 ----------------
    def _load(self):
        self.analyzer = Analyzer().load()
        self._after_load()

    def _after_load(self):
        a = self.analyzer
        m = a.meta
        self.meta_lbl.setText(f"  数据日期 {m['trade_date']} · 更新 {m['fetched_at']} · 覆盖 {m['count']} 只 ")
        brief = a.daily_brief()
        pend = [e for e in a.etfs if e["code"] not in a.ind]
        if pend:
            cats = sorted({e["category"] for e in pend})
            brief += ("\n待上市/数据不足 %d 只(已入池): %s · 有连续30个交易日行情后自动纳入评分"
                      % (len(pend), "、".join(cats)))
        self.brief_box.setPlainText(brief)
        self._fill_cats()
        self._fill_list()
        self.render_scores()
        self.draw_kline()
        self.draw_corr()
        self.draw_risk()
        self.draw_rot()
        self.reload_index()
        self._fill_hold_list()

    def _fill_cats(self):
        cats = ["全部"] + sorted({e["category"] for e in self.analyzer.etfs})
        self.cat_cb.blockSignals(True)
        self.cat_cb.clear()
        self.cat_cb.addItems(cats)
        self.cat_cb.blockSignals(False)

    def shown(self):
        cat = self.cat_cb.currentText() if self.cat_cb.count() else "全部"
        if cat == "全部":
            return self.analyzer.scores
        return [r for r in self.analyzer.scores if r["cat"] == cat]

    def _fill_list(self):
        self.etf_list.blockSignals(True)
        self.etf_list.clear()
        for r in self.shown():
            it = QListWidgetItem(f"{r['name']}  {r['pct']:+.2f}%" if r["pct"] is not None
                                 else r["name"])
            it.setData(Qt.UserRole, r["code"])
            it.setForeground(QColor(UP if (r["pct"] or 0) > 0 else DOWN if (r["pct"] or 0) < 0 else TXT))
            self.etf_list.addItem(it)
        self.etf_list.blockSignals(False)
        if self.etf_list.count():
            self.etf_list.setCurrentRow(0)

    # ---------------- 页1: 评分排名 ----------------
    @staticmethod
    def _sort_val(r, key):
        """取用于排序的值; 返回 None 表示该行无此数据(统一排到最后)。"""
        if key == "macdSt":
            s = r.get("macdSt")
            return None if not s else (1 if s == "多头" else 0)
        if key in ("name", "cat"):
            s = str(r.get(key) or "")
            if not s:
                return None
            try:
                from pypinyin import lazy_pinyin
                return "".join(lazy_pinyin(s)).lower()
            except Exception:
                return s.lower()
        v = r.get(key)
        return v if isinstance(v, (int, float)) and v == v else None

    def _sort_rows(self, rows, key, desc):
        have, none = [], []
        for r in rows:
            (have if self._sort_val(r, key) is not None else none).append(r)
        have.sort(key=lambda r: self._sort_val(r, key), reverse=desc)
        return have + none

    def _on_sort(self, col):
        if not (0 <= col < len(self.score_cols)):
            return
        key = self.score_cols[col]
        if key == self._sort_key:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_key = key
            self._sort_desc = self._sort_def.get(key, True)
        self.render_scores()

    def _update_header(self):
        for i, k in enumerate(self.score_cols):
            base = self._hdr_base[i]
            if i == 0 and self._sort_key not in ("score", "rank"):
                base = "原排名"
            if k == self._sort_key:
                base += " ▼" if self._sort_desc else " ▲"
            it = self.score_tbl.horizontalHeaderItem(i)
            if it is not None:
                it.setText(base)

    def render_scores(self):
        rows = self.shown()
        total = len(rows)
        kw = self.filter_edit.text().strip() if hasattr(self, "filter_edit") else ""
        if kw:
            rows = [r for r in rows
                    if kw in str(r["code"]) or kw in str(r["name"]) or kw in str(r["cat"])]
        if hasattr(self, "count_lbl"):
            self.count_lbl.setText(
                f"显示 {len(rows)} / {total} 只" + ("（已过滤）" if kw else ""))
        if self._sort_key:
            rows = self._sort_rows(rows, self._sort_key, self._sort_desc)
        self._view_rows = rows
        t = self.score_tbl
        t.setRowCount(len(rows))
        for i, r in enumerate(rows):
            vals = {}
            for k in self.score_cols:
                vals[k] = r.get(k)
            t.setItem(i, 0, self._cell("—" if r.get("rank") is None else str(r["rank"])))
            nm = self._lcell(r["name"])
            nm.setToolTip("点名称 → 跳到 K线图")
            if r.get("no_data"):
                nm.setToolTip("%s · 上市不足30个交易日, 暂不评分" % (r.get("note") or ""))
                nm.setForeground(QColor(SUB))
            t.setItem(i, 1, nm)
            t.setItem(i, 2, self._cell(r["cat"]))
            sc = vals["score"]
            it = QTableWidgetItem("待上市" if sc is None else f"{sc:.1f}")
            it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            if sc is None:
                it.setForeground(QColor(SUB))
            else:
                it.setForeground(QColor(UP if sc >= 70 else WARN if sc >= 45 else DOWN))
            t.setItem(i, 3, it)
            for j, k in enumerate(["f_mom", "f_trend", "f_flow", "f_risk"], start=4):
                v = vals[k]
                it = QTableWidgetItem(f"{v:.0f}" if v is not None else "—")
                it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                it.setForeground(QColor(UP if (v or 0) >= 70 else DOWN if (v or 0) < 30 else TXT))
                t.setItem(i, j, it)
            t.setItem(i, 8, color_item(f"{vals['pct']:+.2f}" if vals["pct"] is not None else "—", vals["pct"]))
            for j, k, sign in ((9, "mom20", True), (10, "mom60", True), (11, "ytd", True)):
                v = vals[k]
                t.setItem(i, j, color_item(f"{v:+.1f}" if v is not None else "—", v))
            t.setItem(i, 12, self._cell(f"{vals['vol']:.1f}" if vals["vol"] is not None else "—"))
            t.setItem(i, 13, color_item(f"{vals['sharpe']:.2f}" if vals["sharpe"] is not None else "—", vals["sharpe"]))
            t.setItem(i, 14, self._cell(f"{vals['mdd']:.1f}" if vals["mdd"] is not None else "—"))
            mi = vals["main_in"]
            t.setItem(i, 15, color_item(f"{mi:+,.0f}" if mi is not None else "—", mi))
            amt = vals.get("amount")
            t.setItem(i, 16, self._cell(f"{amt/1e4:,.1f}" if amt else "—"))
            pv = vals["premium"]
            itp = self._cell(f"{pv:+.2f}" if pv is not None else "—")
            if pv is not None and looks_qdii(r.get("name")):
                itp.setText("⁎%+.2f" % pv)
                itp.setForeground(QColor(WARN))
                itp.setToolTip("QDII: 现价 / 东财 IOPV(T-1 净值估算) − 1。"
                               "跨时区品种含着昨夜海外涨跌与汇率, 数值偏大属正常, "
                               "要判断限购溢价, 比同类 QDII 之间的高低差。")
            t.setItem(i, 17, itp)
            t.setItem(i, 18, self._cell(f"{vals['rsi']:.0f}" if vals["rsi"] is not None else "—"))
            t.setItem(i, 19, self._cell(vals.get("macdSt") or "—"))
        self._update_header()

    @staticmethod
    def _cell(text):
        it = QTableWidgetItem(text)
        it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        return it

    @staticmethod
    def _lcell(text):
        it = QTableWidgetItem(str(text))
        it.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        return it

    def _pick_from_score(self, idx):
        if not (0 <= idx.row() < len(self._view_rows)):
            return
        r = self._view_rows[idx.row()]
        if idx.column() != 1:
            # 点数字列只是想核对那一格, 一选中就翻页会把表带走; 只做左侧联动
            self.cur_code = r["code"]
            self._sync_selection(r["code"])
            return
        self._select_code(r["code"])
        if self.jump_chk.isChecked():
            self.tabs.setCurrentIndex(1)

    def _on_score_dbl(self, idx):
        if 0 <= idx.row() < len(self._view_rows):
            self._select_code(self._view_rows[idx.row()]["code"])
            self.tabs.setCurrentIndex(1)

    def _pick_from_list(self, cur, _prev=None):
        if cur:
            self._select_code(cur.data(Qt.UserRole))

    def _sync_selection(self, code):
        """两处列表与评分表互相点亮, 免得点了一只却在别处找不到。"""
        lst = getattr(self, "etf_list", None)
        if lst is not None and lst.count():
            lst.blockSignals(True)
            hit = -1
            for i in range(lst.count()):
                if lst.item(i).data(Qt.UserRole) == code:
                    hit = i
                    break
            if hit >= 0:
                lst.setCurrentRow(hit)
            lst.blockSignals(False)
        t = getattr(self, "score_tbl", None)
        if t is not None:
            for i, r in enumerate(self._view_rows):
                if r["code"] == code:
                    t.blockSignals(True)
                    t.selectRow(i)
                    it = t.item(i, 0)
                    if it is not None:
                        t.scrollToItem(it)
                    t.blockSignals(False)
                    break

    def _select_code(self, code):
        self.cur_code = code
        self._sync_selection(code)
        self.draw_kline()

    # ---------------- 页2: K线 ----------------
    def _on_period_change(self, txt):
        # 分时没有 K 线, 副图指标无处可放, 直接置灰免得误以为坏了
        self._vis = None          # 手动换周期就从全图重新看起
        self.sub_mode.setEnabled(txt != "分时")
        self.draw_kline()

    # ---- 缩放: 滚轮/上下键沿 PERIODS 连续切档, 与券商软件一致
    def _period_idx(self):
        try:
            return PERIODS.index(self.period_cb.currentText())
        except (ValueError, AttributeError):
            return PERIODS.index("日K")

    def _set_period(self, i):
        i = max(0, min(len(PERIODS) - 1, i))
        # 跨档时别把更细周期的几百根一次全摊开: 先给 120 根当过渡, 继续滚再缩
        self._vis = "auto" if i < self._period_idx() else None
        txt = PERIODS[i]
        self.period_cb.blockSignals(True)
        self.period_cb.setCurrentText(txt)
        self.period_cb.blockSignals(False)
        self.sub_mode.setEnabled(txt != "分时")
        self.draw_kline()
        self.status.setText("周期 → %s" % txt)

    def _zoom(self, d):
        """d>0 放大(更细的周期/更少的根数), d<0 缩小。

        同一周期内先改可见根数, 到 30 根还不够细就切更细的周期;
        反过来把本周期全看完还继续缩, 就切更粗的周期。分时已是最细, 不再切。
        """
        if not hasattr(self, "period_cb"):
            return
        i = self._period_idx()
        total = int(getattr(self, "_bar_total", 0) or 0)
        vis = getattr(self, "_vis", None)
        if vis == "auto":
            vis = min(total, AUTO_VIS) if total else None
        if d > 0:
            if i == 0:                      # 分时无根数可缩, 也不存在更细的档
                self.status.setText("已经是最细周期: 分时")
                return
            if vis is None and total:
                vis = total
            if vis:
                nv = max(min(ZOOM_MIN_BARS, vis),
                         int(round(vis / ZOOM_RATIO)))
                if nv < vis:
                    self._vis = nv
                    self.draw_kline()
                    return
            self._set_period(i - 1)
            return
        if vis is None:
            if i < len(PERIODS) - 1:
                self._set_period(i + 1)
            else:
                self.status.setText("已经是最大周期: 月K")
            return
        nv = int(round(vis * ZOOM_RATIO))
        if nv >= total:
            if i < len(PERIODS) - 1:
                self._set_period(i + 1)
            else:
                self._vis = None
                self.draw_kline()
        else:
            self._vis = nv
            self.draw_kline()

    def _kline_scroll(self, ev):
        if ev.button == "up":
            self._zoom(1)
        elif ev.button == "down":
            self._zoom(-1)

    def _kline_key(self, ev):
        if ev.key in ("up", "+", "=", "pageup"):
            self._zoom(1)
        elif ev.key in ("down", "-", "_", "pagedown"):
            self._zoom(-1)

    def _live_tick(self):
        """停在分时页时每分钟自己重取一次, 跟行情软件一样会动。"""
        code = getattr(self, "cur_code", None)
        if not code or self.period_cb.currentText() != "分时":
            return
        if self.tabs.tabText(self.tabs.currentIndex()) != brand.TABS["kline"]:
            return
        key = (code, "1")
        if key not in self._mdata:      # 上次就没取到, 别反复打接口
            return
        etf_minute.clear_cache(code)
        self._request_minute(code, "1", None, quiet=True, force=True)

    def draw_kline(self):
        """按周期分发: 分时 / 分钟K / 日K / 周K / 月K。"""
        code = getattr(self, "cur_code", None)
        if not code or not getattr(self, "analyzer", None):
            return
        e = next((x for x in self.analyzer.etfs if x["code"] == code), None)
        p = self.period_cb.currentText() if hasattr(self, "period_cb") else "日K"
        if p == "分时":
            self._request_minute(code, "1", e)
        elif p in ("日K", "周K", "月K"):
            self._draw_candles(code, e, p)
        else:
            self._request_minute(code, PERIOD_MIN.get(p, "5"), e)

    # ---------- 通用提示页 ----------
    def _placeholder(self, title, sub=""):
        fig = self.kchart.fig
        fig.clear()
        ax = fig.add_subplot(111)
        ax.axis("off")
        ax.text(0.5, 0.56, title, ha="center", va="center", fontsize=13, color=TXT)
        if sub:
            ax.text(0.5, 0.46, sub[:120], ha="center", va="center", fontsize=9, color=SUB)
        self._bar_total = 0
        self._k_ctx = None
        self._i_ctx = None
        self.kchart.draw()

    # ---------- 日/周/月 K ----------
    def _draw_candles(self, code, e, label):
        rows = self.analyzer.kline.get(code) or []
        if label == "日K":
            ind = self.analyzer.ind.get(code)
            if ind is None and len(rows) >= 30:
                ind = full_ind(rows)
        else:
            merged = etf_minute.resample(rows, "W" if label == "周K" else "M")
            need = 12 if label == "周K" else 6
            ind = full_ind(merged) if len(merged) >= need else None
        if not ind:
            fig = self.kchart.fig
            fig.clear()
            ax = fig.add_subplot(111)
            ax.axis("off")
            nm = e["name"] if e else code
            note = (e.get("note") or "") if e else ""
            if rows and label != "日K":
                # 有日K但不够合成周/月K, 别说成"待上市"
                ax.text(0.5, 0.60, nm, ha="center", va="center", fontsize=15, color=TXT)
                ax.text(0.5, 0.50, "%s  ·  %s 历史不足" % (code, label),
                        ha="center", va="center", fontsize=10, color=SUB)
                ax.text(0.5, 0.38, "库里只有 %d 根日K，合成%s至少需要 %d 根。\n"
                                  "点「刷新数据」补历史，或先看日K。"
                        % (len(rows), label, need),
                        ha="center", va="center", fontsize=9, color=SUB, linespacing=1.7)
                self.kinfo.setText("  %s (%s) · %s 历史不足" % (nm, code, label))
            else:
                ax.text(0.5, 0.62, nm, ha="center", va="center", fontsize=15, color=TXT)
                ax.text(0.5, 0.52, "%s  ·  暂无场内 K 线" % code, ha="center", va="center",
                        fontsize=10, color=SUB)
                ax.text(0.5, 0.40, "新成立/未上市的基金，交易所还没有连续行情。\n"
                                  "%s\n上市后点「刷新数据」，日K与资金流会自动补上。" % note,
                        ha="center", va="center", fontsize=9, color=SUB, linespacing=1.7)
                self.kinfo.setText("  %s (%s) · 待上市" % (nm, code))
            self._k_ctx = None
            self.kchart.draw()
            return
        self._render_candles(ind, code, e, label,
                             "%d根" % len(ind["c"]) if label != "日K"
                             else "%d个交易日" % len(ind["c"]))

    def _render_candles(self, ind, code, e, label, unit):
        d = ind.get("sum") or {}
        nm = e["name"] if e else code
        # 缩放到某一档时只画最后 n 根; 指标在长序列上已经算好, 直接截尾不会错位
        n_all = len(ind["c"]) if ind.get("c") is not None else 0
        self._bar_total = n_all
        vis = getattr(self, "_vis", None)
        if vis == "auto":
            vis = min(n_all, AUTO_VIS)
            self._vis = vis
        n_show = n_all if not vis else max(ZOOM_MIN_BARS, min(n_all, int(vis)))
        if n_show < n_all:
            st = n_all - n_show
            ind = {k: (v[st:] if isinstance(v, (list, np.ndarray)) and len(v) == n_all
                       else v) for k, v in ind.items()}
            unit = "近%d/%d" % (n_show, n_all)
        if label == "日K" and d.get("rsi") is not None:
            self.kinfo.setText(
                f"  {nm} ({code}) · RSI {d['rsi']:.0f} · 20日 {d['r20']:+.1f}% · "
                f"年内 {d['ytd']:+.1f}% · 距52周高点 {d['offHi']:+.1f}%"
                + ("　%s" % unit if n_show < n_all else ""))
        else:
            last = ind["c"][-1]
            prev = ind["c"][-2] if len(ind["c"]) > 1 else last
            self.kinfo.setText("  %s (%s) · %s · %s · 最新 %.3f (%+.2f%%)"
                               % (nm, code, label, unit, last,
                                  (last / prev - 1) * 100 if prev else 0.0))
        fig = self.kchart.fig
        fig.clear()
        ax_k = fig.add_axes((0.055, 0.40, 0.92, 0.56))
        ax_v = fig.add_axes((0.055, 0.26, 0.92, 0.12), sharex=ax_k)
        ax_s = fig.add_axes((0.055, 0.07, 0.92, 0.16), sharex=ax_k)
        o, c, h, l, v = ind["o"], ind["c"], ind["h"], ind["l"], ind["v"]
        n = len(c)
        x = np.arange(n)
        colors = np.where(c >= o, UP, DOWN)
        ax_k.vlines(x, l, h, color=colors, lw=0.7)
        ax_k.bar(x, np.abs(c - o) + 1e-9, 0.65, bottom=np.minimum(o, c),
                 color=colors, zorder=3)
        for arr, colr, lab in ((ind["ma5"], "#f5a623", "MA5"),
                               (ind["ma20"], ACCENT, "MA20"),
                               (ind["ma60"], "#9b59b6", "MA60")):
            # 月K 只有十几根, MA60 全是空值, 别在图例里挂一条不存在的线
            if np.isfinite(arr).any():
                ax_k.plot(x, arr, lw=1, color=colr, label=lab)
        ax_k.plot(x, ind["boll_up"], lw=0.8, ls="--", color="#5a6070", label="BOLL上")
        ax_k.plot(x, ind["boll_low"], lw=0.8, ls="--", color="#5a6070", label="BOLL下")
        ax_k.legend(loc="upper left", fontsize=8, ncol=5, frameon=False)
        ax_k.set_title(f"{nm} ({code})  {label} · {unit}", loc="left", fontsize=11)
        ax_k.grid(color=GRID, lw=0.5)
        ax_k.tick_params(labelbottom=False)
        ax_v.bar(x, v, color=colors, width=0.65)
        ax_v.yaxis.set_major_formatter(FuncFormatter(_fmt_vol))
        ax_v.set_ylabel("成交量", fontsize=8)
        ax_v.tick_params(labelbottom=False)
        ax = ax_s
        mode = self.sub_mode.currentText()
        if mode == "MACD":
            ax.bar(x, ind["hist"], color=np.where(ind["hist"] >= 0, UP, DOWN), width=0.65)
            ax.plot(x, ind["dif"], lw=1, color="#f5a623", label="DIF")
            ax.plot(x, ind["dea"], lw=1, color=ACCENT, label="DEA")
            ax.legend(loc="upper left", fontsize=8, ncol=2, frameon=False)
            ax.set_ylabel("MACD", fontsize=8)
        elif mode == "RSI":
            ax.plot(x, ind["rsi"], lw=1.1, color="#9b59b6")
            ax.axhline(70, color=UP, ls="--", lw=0.7)
            ax.axhline(30, color=DOWN, ls="--", lw=0.7)
            ax.set_ylim(0, 100)
            ax.set_ylabel("RSI14", fontsize=8)
        else:
            ax.plot(x, ind["K"], lw=1, color="#f5a623", label="K")
            ax.plot(x, ind["D"], lw=1, color=ACCENT, label="D")
            ax.plot(x, ind["J"], lw=1, color="#9b59b6", label="J")
            ax.legend(loc="upper left", fontsize=8, ncol=3, frameon=False)
            ax.set_ylabel("KDJ", fontsize=8)
        ax.grid(color=GRID, lw=0.5)
        if "分钟" in label:
            want = 8
        elif label in ("周K", "月K"):
            want = 6
        else:
            want = 10
        step = max(1, n // want)
        ticks = list(range(0, n, step))
        ax.set_xticks(ticks)
        ax.set_xticklabels([_fmt_bar_label(ind["dates"][i], label) for i in ticks],
                           fontsize=7)
        if "分钟" in label:
            for lab in ax.get_xticklabels():
                lab.set_rotation(0)
                lab.set_ha("center")
        for a_ in (ax_k, ax_v, ax):
            a_.set_xlim(-1, n)
        self._k_ctx = dict(dates=ind["dates"], o=o, c=c, h=h, l=l, v=v, n=n,
                           ax=ax_k, axs=(ax_k, ax_v, ax), code=code, name=nm,
                           ma5=ind["ma5"], ma20=ind["ma20"], ma60=ind["ma60"])
        self._i_ctx = None
        self._k_marks = []
        self.kchart.draw()

    # ---------- 分时 / 分钟线取数 ----------
    def _request_minute(self, code, period, e, quiet=False, force=False):
        key = (code, period)
        if not force:
            d = self._mdata.get(key)
            if d:
                self._show_minute(code, e, period, d)
                return
        if not quiet:
            self._placeholder("正在取分钟行情…",
                              "%s · %s" % (code, "分时" if period == "1"
                                           else period + "分钟K"))
        th = MinuteThread(code, period, str(etf_app.DB))
        th.done.connect(self._on_minute_done)
        self._mthread = th
        th.start()

    def _on_minute_done(self, code, period, d):
        key = (code, period)
        if "err" in d:
            # 自动刷新失败时保留原来那张图, 不要把画面换成错误页
            if key not in self._mdata:
                self._placeholder("分钟行情取不到", d["err"])
            self.status.setText("分钟行情失败: %s" % d["err"])
            return
        self._mdata[(code, period)] = d
        if code != getattr(self, "cur_code", None):
            return
        p = self.period_cb.currentText()
        if period == "1" and p != "分时":
            return
        if period != "1" and PERIOD_MIN.get(p) != period:
            return
        e = next((x for x in self.analyzer.etfs if x["code"] == code), None)
        self._show_minute(code, e, period, d)

    def _show_minute(self, code, e, period, d):
        if e is None and getattr(self, "analyzer", None):
            e = next((x for x in self.analyzer.etfs if x["code"] == code), None)
        if period == "1":
            self._render_intraday(code, e, d)
        else:
            rows = [(t, o_, c_, h_, l_, v_, 0.0) for t, o_, c_, h_, l_, v_
                    in zip(d["times"], d["o"], d["c"], d["h"], d["l"], d["v"])]
            if len(rows) < 12:
                self._placeholder("分钟K太少", "该标的成交清淡, 暂无足够的分钟数据")
                return
            self._render_candles(full_ind(rows), code, e, period + "分钟K",
                                 "%d根" % len(rows))

    # ---------- 分时图 ----------
    def _render_intraday(self, code, e, d):
        nm = e["name"] if e else code
        c = d["c"]
        n = len(c)
        self._bar_total = n
        if n < 2:
            self._placeholder("分时数据为空", "%s 当日没有成交" % code)
            return
        pc = d["prev_close"] or float(c[0])
        avg = d["avg"]
        x = np.arange(n)
        up = c[-1] >= pc
        line = UP if up else DOWN
        pct = (c[-1] / pc - 1) * 100 if pc else 0.0
        self.kinfo.setText("  %s (%s) · 分时 · 现价 %.3f (%+.2f%%) · 均价 %.3f · %s"
                           % (nm, code, c[-1], pct,
                              float(avg[-1]) if avg is not None else c[-1],
                              d.get("updated", "")))

        fig = self.kchart.fig
        fig.clear()
        ax_p = fig.add_axes((0.075, 0.31, 0.845, 0.64))
        ax_v = fig.add_axes((0.075, 0.11, 0.845, 0.16), sharex=ax_p)

        # 上下对称, 昨收压在正中间, 涨跌幅刻度才不会偏
        dev = float(np.max(np.abs(c - pc)))
        if avg is not None:
            dev = max(dev, float(np.max(np.abs(avg - pc))))
        dev = max(dev, pc * 0.002)
        lo, hi = pc - dev * 1.14, pc + dev * 1.14
        ax_p.set_ylim(lo, hi)
        ax_p.set_xlim(-1, max(n, 240))

        ax_p.plot(x, c, lw=1.1, color=line, label="价格", zorder=3)
        # 填到画布底部, 别用昨收做基线, 否则一半画布糊成一整块
        ax_p.fill_between(x, c, lo, color=line, alpha=0.10, lw=0, zorder=2)
        if avg is not None:
            ax_p.plot(x, avg, lw=1.0, color="#f5a623", label="均价", zorder=4)
        ax_p.axhline(pc, color=SUB, ls="--", lw=0.8, zorder=1)
        ax_p.grid(color=GRID, lw=0.5)
        ax_p.tick_params(labelbottom=False)
        ax_p.legend(loc="upper left", fontsize=8, ncol=2, frameon=False)
        ax_p.set_title("%s (%s)  分时 · %s" % (nm, code, d.get("date", "")),
                       loc="left", fontsize=11)

        # 右轴换成涨跌幅, 和券商软件一致。刻度必须落在可视范围内,
        # 否则 get_yticks() 多出来的两端刻度会和左轴错位。
        ax_r = ax_p.twinx()
        ax_r.set_ylim(lo, hi)
        yticks = [t for t in ax_p.get_yticks() if lo <= t <= hi]
        ax_p.set_yticks(yticks)
        ax_r.set_yticks(yticks)
        ax_r.set_yticklabels(["%+.2f%%" % ((y / pc - 1) * 100) for y in yticks], fontsize=8)
        ax_r.tick_params(colors=SUB, length=0)
        for sp in ("top", "right"):
            ax_r.spines[sp].set_visible(False)

        # 成交量: 按这一分钟相对上一分钟的涨跌染色
        dc = np.diff(c, prepend=c[0])
        ax_v.bar(x, d["v"], width=0.7,
                 color=np.where(dc >= 0, UP, DOWN), alpha=0.85)
        ax_v.yaxis.set_major_formatter(FuncFormatter(_fmt_vol))
        ax_v.set_ylabel("成交量", fontsize=8)
        ax_v.grid(color=GRID, lw=0.5)
        idx, labs = _intraday_ticks(d["times"])
        ax_v.set_xticks(idx)
        ax_v.set_xticklabels(labs, fontsize=7.5)

        self._i_ctx = dict(code=code, name=nm, times=d["times"], c=c,
                           avg=avg, v=d["v"], n=n, pc=pc,
                           ax=ax_p, axs=(ax_p, ax_v))
        self._k_ctx = None
        self._k_marks = []
        self.kchart.draw()

    def _intraday_click(self, ev):
        """点分时任一点: 竖线 + 时间/价格/涨跌幅/均价/成交量。"""
        ctx = getattr(self, "_i_ctx", None)
        if not ctx or ev.xdata is None:
            return
        if not hit_axs(ev, ctx["axs"]):
            return
        i = int(round(ev.xdata))
        if not (0 <= i < ctx["n"]):
            return
        for art in getattr(self, "_k_marks", []):
            try:
                art.remove()
            except Exception:
                pass
        self._k_marks = []
        ax = ctx["ax"]
        ln = ax.axvline(i, color=ACCENT, lw=0.9, alpha=0.9, zorder=8)
        pc = ctx["pc"] or 1.0
        pct = (ctx["c"][i] / pc - 1) * 100 if pc else 0.0
        avg = ctx["avg"]
        a_ = "—" if avg is None else "%.3f" % float(avg[i])
        txt = ("%s\n价 %.3f   涨跌 %+.2f%%\n均价 %s   量 %.0f"
               % (ctx["times"][i][11:16], ctx["c"][i], pct, a_, ctx["v"][i]))
        right = i > ctx["n"] * 0.66
        box = ax.annotate(txt, xy=(i, ctx["c"][i]),
                          xytext=(-10 if right else 10, 12), textcoords="offset points",
                          fontsize=7.8, color=TXT, va="bottom",
                          ha="right" if right else "left",
                          bbox=dict(boxstyle="round,pad=0.42", fc=PANEL2, ec=ACCENT,
                                    lw=0.8, alpha=0.96), zorder=9)
        self._k_marks = [ln, box]
        self.kchart.draw()
        self.status.setText("%s %s · %.3f (%+.2f%%) · 均价 %s · 量 %.0f"
                            % (ctx["code"], ctx["times"][i][11:16],
                               ctx["c"][i], pct, a_, ctx["v"][i]))

    def _kline_click(self, ev):
        """点 K 线任意位置: 竖线定位 + 当日开高低收/涨跌/量/均线数值。"""
        ctx = getattr(self, "_k_ctx", None)
        if not ctx or ev.xdata is None:
            return
        if not hit_axs(ev, ctx["axs"]):
            return
        i = int(round(ev.xdata))
        if not (0 <= i < ctx["n"]):
            return
        for art in getattr(self, "_k_marks", []):
            try:
                art.remove()
            except Exception:
                pass
        self._k_marks = []
        ax = ctx["ax"]
        ln = ax.axvline(i, color=ACCENT, lw=0.9, alpha=0.9, zorder=8)

        def _ma(arr):
            try:
                val = float(arr[i])
            except Exception:
                return "—"
            return "—" if val != val else "%.2f" % val

        prev = ctx["c"][i - 1] if i > 0 else ctx["o"][i]
        chg = (ctx["c"][i] / prev - 1) * 100 if prev else 0.0
        txt = ("%s\n开 %.3f  高 %.3f  低 %.3f  收 %.3f\n涨跌 %+.2f%%   量 %.0f\nMA5 %s  MA20 %s  MA60 %s"
               % (ctx["dates"][i], ctx["o"][i], ctx["h"][i], ctx["l"][i], ctx["c"][i],
                  chg, ctx["v"][i], _ma(ctx["ma5"]), _ma(ctx["ma20"]), _ma(ctx["ma60"])))
        # 越靠右的柱子, 框子摆到左边, 免得跑出画布
        right = i > ctx["n"] * 0.66
        box = ax.annotate(txt, xy=(i, ctx["h"][i]),
                          xytext=(-10 if right else 10, 4), textcoords="offset points",
                          fontsize=7.8, color=TXT, va="top", ha="right" if right else "left",
                          bbox=dict(boxstyle="round,pad=0.42", fc=PANEL2, ec=ACCENT,
                                    lw=0.8, alpha=0.96), zorder=9)
        self._k_marks = [ln, box]
        self.kchart.draw()
        self.status.setText("%s %s · 收 %.3f (%+.2f%%) · 量 %.0f"
                            % (ctx["code"], ctx["dates"][i], ctx["c"][i], chg, ctx["v"][i]))

    # ---------------- 页3: 相关性 ----------------
    @staticmethod
    def _cluster_order(m):
        """贪心聚拢: 从平均相关最高的那只出发, 每次接上跟当前最同步的一只,
        同风险源的标的会连成块, 矩阵看得出一团一团。"""
        n = len(m)
        mm = np.where(np.isfinite(m), m, -1.0).astype(float)
        np.fill_diagonal(mm, -9.0)
        avg = mm.mean(axis=1)
        order = [int(np.argmax(avg))]
        used = [False] * n
        used[order[0]] = True
        while len(order) < n:
            last = order[-1]
            bi, best = -1, -9.0
            for j in range(n):
                if used[j]:
                    continue
                if mm[last, j] > best:
                    best, bi = mm[last, j], j
            order.append(bi)
            used[bi] = True
        return order

    # ---- 点矩阵格子 / 配对表 → 出这两只的对比图
    def _corr_click(self, ev):
        ctx = self._corr_ctx
        if not ctx or ev.xdata is None or ev.ydata is None:
            return
        if not hit_axs(ev, [ctx.get("ax")]):
            return
        i, j = int(round(ev.ydata)), int(round(ev.xdata))
        n = len(ctx["names"])
        if not (0 <= i < n and 0 <= j < n) or i == j:
            return
        self._corr_pair = (i, j) if i < j else (j, i)
        self.draw_corr()
        self._corr_focus_row()
        self.status.setText("%s vs %s · 近 %s 日相关 %.3f · 已定位到左侧配对表"
                            % (ctx["names"][j], ctx["names"][i], ctx["days"],
                               ctx["m"][i, j]))

    def _corr_focus_row(self):
        """矩阵点完, 左侧配对表滚到同那一对并选中, 两边互相对得上。"""
        ctx, pair = self._corr_ctx, self._corr_pair
        if not ctx or not pair:
            return
        want = {ctx["codes"][pair[0]], ctx["codes"][pair[1]]}
        t = self.corr_pairs
        for r, (x, y) in enumerate(self._corr_show or []):
            if {x, y} == want:
                t.blockSignals(True)
                t.selectRow(r)
                t.blockSignals(False)
                it = t.item(r, 0)
                if it is not None:
                    t.scrollToItem(it, QAbstractItemView.PositionAtCenter)
                return
        self.corr_pair_lbl.setText(
            "已选 %s vs %s, 但当前%s里没列出这一对, 换「全部配对」或清掉搜索框"
            % (short_name(ctx["names"][pair[0]], 8),
               short_name(ctx["names"][pair[1]], 8),
               self.corr_pair_mode.currentText()))

    def _corr_hover(self, ev):
        """鼠标停在哪一格, 状态栏就报那两只的名字与相关系数。"""
        ctx = self._corr_ctx
        if not ctx or ev.xdata is None or ev.ydata is None:
            return
        if not hit_axs(ev, [ctx.get("ax")]):
            return
        i, j = int(round(ev.ydata)), int(round(ev.xdata))
        n = len(ctx["names"])
        if not (0 <= i < n and 0 <= j < n):
            return
        self.status.setText("%s vs %s · 近 %s 日相关 %.3f"
                            % (ctx["names"][j], ctx["names"][i], ctx["days"],
                               ctx["m"][i, j]))

    def _on_corr_pair_pick(self, idx):
        """左边配对表点一行 -> 右上矩阵框出这两行两列 + 下方出对比走势。"""
        ctx = self._corr_ctx
        if not ctx or not (0 <= idx.row() < len(self._corr_show)):
            return
        ca, cb_ = self._corr_show[idx.row()]
        codes = ctx["codes"]
        if ca in codes and cb_ in codes:
            i, j = codes.index(ca), codes.index(cb_)
            self._corr_pair = (i, j) if i < j else (j, i)
            self.draw_corr()
            # 重算会重建配对表并滚回顶部, 不把这一对重新选回来, 看着就像点了没反应
            self._corr_focus_row()
            self.corr_pair_lbl.setText(
                "已选 %s vs %s · 相关 %.2f · 矩阵里已框出这两行两列, 下方是对比走势"
                % (short_name(ctx["names"][i], 9), short_name(ctx["names"][j], 9),
                   ctx["m"][self._corr_pair[0], self._corr_pair[1]]))
            self.status.setText("%s vs %s · 近 %s 日相关 %.2f"
                                % (ctx["names"][i], ctx["names"][j], ctx["days"],
                                   ctx["m"][i, j]))

    def _corr_filter_later(self):
        """搜索框每敲一个字就重建一万行会卡, 停顿一下再建。"""
        self._corr_filter.start()

    def _corr_sort_by(self, col):
        if not (0 <= col <= 3):
            return
        old_col, old_desc = self._corr_sort
        desc = not old_desc if col == old_col else (col == 3)
        self._corr_sort = (col, desc)
        self.corr_pairs.horizontalHeader().setSortIndicator(
            col, Qt.DescendingOrder if desc else Qt.AscendingOrder)
        self._fill_corr_pairs()

    def _fill_corr_pairs(self):
        """配对清单: 前40/各40 仍只列极端对; 「全部配对」把 n(n-1)/2 对全列出来。"""
        t = self.corr_pairs
        pairs = getattr(self, "_corr_pairs_all", []) or []
        if not pairs:
            t.setRowCount(0)
            self._corr_show = []
            self.corr_pair_lbl.setText("配对清单 · 先选范围重算")
            return
        mode = self.corr_pair_mode.currentText()
        n = len(self._corr_names_all)
        tot = n * (n - 1) // 2
        if mode.startswith("同涨+分散"):
            top = pairs[:40]
            bot = sorted(pairs, key=lambda x: x[0])[:40]
            show = [("同涨", an, bn, v, ac, bc) for v, an, bn, ac, bc in top]
            show += [("分散", an, bn, v, ac, bc) for v, an, bn, ac, bc in bot]
        elif mode.startswith("分散"):
            show = [("分散", an, bn, v, ac, bc)
                    for v, an, bn, ac, bc in sorted(pairs, key=lambda x: x[0])[:40]]
        elif mode.startswith("全部"):
            show = [(("同涨" if v >= 0.9 else "分散" if v < 0.6 else "中性"),
                     an, bn, v, ac, bc) for v, an, bn, ac, bc in pairs]
        else:
            show = [("同涨", an, bn, v, ac, bc) for v, an, bn, ac, bc in pairs[:40]]
        kw = self.corr_pair_search.text().strip().upper()
        if kw:
            show = [p for p in show
                    if kw in p[1].upper() or kw in p[2].upper()
                    or kw in str(p[4]) or kw in str(p[5])]
        col, desc = getattr(self, "_corr_sort", (3, True))
        def _k(p):
            if col == 0:
                return "0" if p[0] == "同涨" else "1" if p[0] == "分散" else "2"
            if col == 1:
                return p[1]
            if col == 2:
                return p[2]
            return p[3]
        show.sort(key=_k, reverse=desc)
        self._corr_show = [(p[4], p[5]) for p in show]
        shown = "全部列出" if mode.startswith("全部") and not kw and len(show) == tot else (
            "筛出 %d 对" % len(show) if kw else "列 %d 行" % len(show))
        self.corr_pair_lbl.setText("%d 只 · 共 %d 对 · %s · 点列头排序"
                                   % (n, tot, shown))
        t.setSortingEnabled(False)
        t.setRowCount(len(show))
        for r, (rel, an, bn, v, ac, bc) in enumerate(show):
            it0 = QTableWidgetItem(rel)
            it0.setTextAlignment(Qt.AlignCenter)
            it0.setForeground(QColor(UP if rel == "同涨" else DOWN if rel == "分散" else SUB))
            t.setItem(r, 0, it0)
            ia = QTableWidgetItem(short_name(an, 8))
            ia.setToolTip("%s %s · 点这行看对比图" % (ac, an))
            t.setItem(r, 1, ia)
            ib = QTableWidgetItem(short_name(bn, 8))
            ib.setToolTip("%s %s" % (bc, bn))
            t.setItem(r, 2, ib)
            it = QTableWidgetItem("%.2f" % v)
            it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            it.setForeground(QColor(UP if v >= 0.9 else DOWN if v < 0.6 else TXT))
            t.setItem(r, 3, it)
        t.scrollToTop()

    def draw_corr(self):
        days = int(self.corr_days.currentText())
        scope = self.corr_scope.currentText()
        names, codes, m, info = self._corr_subset(scope, days)
        self._corr_part = list(zip(codes, names))
        fig = self.corr_chart.fig
        fig.clear()
        self.corr_pairs.setRowCount(0)
        self._corr_show = []
        if m is None or len(names) < 2:
            self._corr_ctx = None
            self.corr_cnt.setText("参与计算 0 只 · %s" % (info.get("why") or "样本不足"))
            ax = fig.add_subplot(111)
            ax.text(0.5, 0.5, "样本不足", ha="center", va="center", color=SUB,
                    transform=ax.transAxes)
            self.corr_chart.draw()
            return
        txt = "参与计算 <b>%d</b> 只 / 池 %d 只" % (len(names), info["pool"])
        if info["short"]:
            txt += "<br>%d 只K线不足 %d 日已跳过" % (info["short"], days)
        if info["unlisted"]:
            txt += "<br>%d 只未上市无K线" % info["unlisted"]
        self.corr_cnt.setText(txt)
        self.corr_cnt.setTextFormat(Qt.RichText)
        # 配对表(先存全部配对, 再按模式取子集; 排序在矩阵聚簇之前做)
        pairs = []
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                v = m[i, j]
                if v == v:
                    pairs.append((float(v), names[i], names[j], codes[i], codes[j]))
        pairs.sort(key=lambda x: -x[0])
        self._corr_pairs_all = pairs
        self._corr_names_all = list(names)
        self._fill_corr_pairs()

        order = self._cluster_order(m)
        names2 = [names[i] for i in order]
        codes2 = [codes[i] for i in order]
        m2 = m[np.ix_(order, order)]
        n = len(names2)
        pair = self._corr_pair
        if pair and (pair[0] >= n or pair[1] >= n):
            pair = self._corr_pair = None
        self._corr_ctx = dict(names=names2, codes=codes2, m=m2, days=days, ax=None)

        if pair:
            gs = fig.add_gridspec(2, 1, height_ratios=[1.85, 1.05],
                                  left=0.148, right=0.895, top=0.952, bottom=0.072,
                                  hspace=0.16)
            ax = fig.add_subplot(gs[0])
            has_xtick = False
        else:
            # 标的很多时横轴标签挤不下, 干脆不标, 省出来的高度给矩阵
            bottom = 0.19 if n <= 60 else 0.095
            ax = fig.add_axes((0.135, bottom, 0.775, 0.935 - bottom))
            has_xtick = n <= 60
        self._corr_ctx["ax"] = ax
        from matplotlib.colors import LinearSegmentedColormap
        cmap = LinearSegmentedColormap.from_list("rg", [DOWN, "#232834", UP])
        # 有下方配对图或标的很多时, 保持正方格会留大片空白, 让它填满
        im = ax.imshow(m2, cmap=cmap, vmin=-1, vmax=1,
                       aspect="auto" if (pair or n > 48) else None)
        # 分类分隔线只在块真的连续时才有意义, 聚簇后同分类未必相邻, 因此不画
        ax.set_xticks(range(n))
        ax.set_yticks(range(n))
        fs = 6.6 if n <= 26 else 5.8 if n <= 36 else 5.0
        # n 大时全标会糊成黑带, 每 k 个标一次; 名字仍然都算得出来(点格子看标题)
        step = 1 if n <= 48 else int(np.ceil(n / 40))
        if has_xtick:
            ax.set_xticklabels([short_name(x, 7) if i % step == 0 else ""
                                for i, x in enumerate(names2)],
                               rotation=90, fontsize=fs)
        else:
            ax.set_xticklabels([])
            ax.tick_params(axis="x", length=0)
            fs = fs + 0.6
        ax.set_yticklabels([short_name(x, 9) if i % step == 0 else ""
                            for i, x in enumerate(names2)], fontsize=fs)
        if n <= 22:
            for i in range(n):
                for j in range(n):
                    if i == j:
                        continue
                    ax.text(j, i, "%.2f" % m2[i, j], ha="center", va="center",
                            fontsize=5.4, color=("#ffffff" if abs(m2[i, j]) > 0.7 else SUB))
        # 选中: 两格描边 + 贯穿的十字线(155 只时那两格只有几个像素, 不画线根本找不到)
        if pair:
            i, j = pair
            # 155×155 时一个格子只有几个像素, 描边根本看不见, 先铺两条色带把行列拉出来
            for idx in (i, j):
                ax.axhspan(idx - 0.5, idx + 0.5, color=ACCENT, alpha=0.22, zorder=5)
                ax.axvspan(idx - 0.5, idx + 0.5, color=ACCENT, alpha=0.22, zorder=5)
            for (a_, b_) in ((i, j), (j, i)):
                ax.add_patch(Rectangle((b_ - 0.5, a_ - 0.5), 1, 1, fill=False,
                                       edgecolor=ACCENT, lw=2.0, zorder=6))
            for idx in (i, j):
                ax.axhline(idx + 0.5, color=ACCENT, lw=1.0, alpha=0.85, zorder=7)
                ax.axvline(idx + 0.5, color=ACCENT, lw=1.0, alpha=0.85, zorder=7)
        if pair:
            title = (f"日收益率相关性 · 近{days}日 · {n}×{n} 格 / {n*(n-1)//2} 对 · 已选: "
                     f"{short_name(names2[pair[0]], 10)} vs {short_name(names2[pair[1]], 10)} "
                     f"= {m2[pair[0], pair[1]]:.2f}")
        else:
            title = (f"日收益率相关性 · 近{days}日 · {n}×{n} 格 / {n*(n-1)//2} 对 · "
                     f"行序按相似度聚拢, 色块成团=同一风险源")
        ax.set_title(title, loc="left", fontsize=10)
        cb = fig.colorbar(im, fraction=0.03, pad=0.02)
        cb.ax.tick_params(labelsize=7, colors=SUB)
        for spine in ax.spines.values():
            spine.set_visible(False)

        if pair:
            self._draw_corr_pair(fig, gs, pair, names2, codes2, m2, days)
        self.corr_chart.draw()

    def _draw_corr_pair(self, fig, gs, pair, names2, codes2, m2, days):
        """两只标的: 归一走势(左轴) + 滚动20日相关(右轴)。"""
        a = self.analyzer
        ax = fig.add_subplot(gs[1])
        i, j = pair
        ca, cb_ = codes2[i], codes2[j]
        ser = []
        for code in (ca, cb_):
            k = (a.kline.get(code) or [])[-(days + 1):]
            if len(k) < 8:
                return
            ser.append([(r[0], float(r[2])) for r in k if r[2]])
        d0 = {d for d, _ in ser[0]}
        common = [d for d, _ in ser[1] if d in d0]
        if len(common) < 8:
            return
        N1 = {d: v for d, v in ser[0]}
        N2 = {d: v for d, v in ser[1]}
        x = list(range(len(common)))
        y1 = np.array([N1[d] for d in common], float)
        y2 = np.array([N2[d] for d in common], float)
        y1 = y1 / y1[0] * 100
        y2 = y2 / y2[0] * 100
        ax.plot(x, y1, color=UP, lw=1.35, label=short_name(names2[i], 10))
        ax.plot(x, y2, color=ACCENT, lw=1.35, label=short_name(names2[j], 10))
        ax.axhline(100, color=SUB, lw=0.6, ls=":")
        ax.set_ylabel("归一=100", fontsize=7.5)
        ax.tick_params(labelsize=7)
        ax.grid(color=GRID, lw=0.5)
        step = max(1, len(common) // 8)
        ax.set_xticks(list(range(0, len(common), step)))
        ax.set_xticklabels([common[k][5:] for k in range(0, len(common), step)], fontsize=6.5)
        ax.set_xlim(-1, len(common))
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
        ax.legend(loc="upper left", fontsize=7, frameon=False, ncol=2, handlelength=1.2)
        # 滚动 20 日相关
        best = float(m2[i, j])
        ax2 = ax.twinx()
        w = 20
        if len(common) > w + 2:
            r1 = np.diff(y1) / y1[:-1]
            r2 = np.diff(y2) / y2[:-1]
            roll = []
            for t in range(w, len(r1) + 1):
                s1, s2 = r1[t - w:t], r2[t - w:t]
                c = np.corrcoef(s1, s2)[0, 1] if s1.std() > 0 and s2.std() > 0 else np.nan
                roll.append(c)
            xs = list(range(w, w + len(roll)))
            ax2.plot(xs, roll, color=WARN, lw=1.0, ls="--", alpha=0.9,
                     label="滚动20日相关")
            ax2.axhline(best, color=SUB, lw=0.7, ls=":")
            ax2.set_ylim(-1.05, 1.05)
            ax2.set_ylabel("滚动相关", fontsize=7, color=WARN)
            ax2.tick_params(labelsize=6.5, colors=WARN)
            ax2.legend(loc="lower right", fontsize=6.6, frameon=False)
            for sp in ("top", "left"):
                ax2.spines[sp].set_visible(False)
            ax.set_title("归一走势(今日=100) · 虚线=滚动20日相关(右轴) · 区间相关 %.2f"
                         % best, loc="left", fontsize=8.6, pad=3)
        else:
            ax.set_title("归一走势(起点=100) · 区间相关 %.2f" % best,
                         loc="left", fontsize=8.6, pad=3)

    def _corr_subset(self, scope, days):
        a = self.analyzer
        codes = [e["code"] for e in a.etfs]
        names_all = [e["name"] for e in a.etfs]
        if scope.startswith("成交额前"):
            topn = int(scope.replace("成交额前", "").strip() or 25)
            ranked = sorted(range(len(a.etfs)),
                            key=lambda i: -(((a.snaps.get(codes[i]) or {}).get("amount")) or 0))
            keep = ranked[:topn]
        elif scope.startswith("各分类"):
            # 每个分类里成交额最大的那只, 避免同指数重复把矩阵刷成一片红
            best = {}
            for i, e in enumerate(a.etfs):
                amt = (a.snaps.get(codes[i]) or {}).get("amount") or 0
                cat = e["category"]
                if cat not in best or amt > best[cat][0]:
                    best[cat] = (amt, i)
            keep = [i for _, i in sorted(best.values(), key=lambda x: -x[0])]
        elif scope.startswith("当前分类"):
            cat = self.cat_cb.currentText()
            if cat != "全部":
                keep = [i for i, e in enumerate(a.etfs) if e["category"] == cat]
            else:
                keep = list(range(len(a.etfs)))
        elif scope.startswith("评分前20"):
            keep = [next(i for i, e in enumerate(a.etfs) if e["code"] == r["code"])
                    for r in a.scores[:20]]
        else:
            keep = list(range(len(a.etfs)))
        series, names, keep_codes = [], [], []
        short = 0
        for i in keep:
            k = a.kline.get(codes[i]) or []
            if len(k) > days:
                c = np.array([x[2] for x in k][-days-1:], float)
                series.append(np.diff(c) / c[:-1])
                names.append(names_all[i])
                keep_codes.append(codes[i])
            elif k:
                short += 1
        info = dict(pool=len(a.etfs), sel=len(keep), short=short,
                    unlisted=sum(1 for e in a.etfs if not a.kline.get(e["code"])),
                    why="池内 %d 只, 选中范围内 %d 只, 可用样本 %d 只"
                        % (len(a.etfs), len(keep), len(series)))
        if len(series) < 2:
            return names, keep_codes, None, info
        return names, keep_codes, np.corrcoef(np.array(series)), info

    # ---------------- 页4: 风险收益 ----------------
    def draw_risk(self):
        a = self.analyzer
        wlab = self.risk_win.currentText()
        w = int(wlab.replace("今日", "1").replace("日", ""))
        mode = self.risk_scope.currentText()
        pts = a.risk_pts(w)
        if mode.startswith("成交额前"):
            topn = int(mode.replace("成交额前", "").strip() or 40)
            keep = {r["code"] for r in sorted(pts, key=lambda r: -r["amount"])[:topn]}
            pts = [r for r in pts if r["code"] in keep]
        elif mode == "当前分类":
            keep = {r["code"] for r in self.shown()}
            pts = [r for r in pts if r["code"] in keep]
        pts = [r for r in pts if r["amp"] is not None]
        fig = self.risk_chart.fig
        fig.clear()
        self._risk_all = pts
        ax = fig.add_subplot(111)
        self._risk_ax = ax
        if not pts:
            ax.text(0.5, 0.5, "样本不足", ha="center", va="center", color=SUB,
                    transform=ax.transAxes)
            self._risk_ax = None
            self.risk_tbl.setRowCount(0)
            self._risk_pts = []
            self._risk_ranked = []
            self.risk_cnt.setText("样本不足, 该区间没有可比的标的")
            self.risk_chart.draw()
            return
        xv = np.array([p["amp"] for p in pts], float)
        yv = np.array([p["ret"] for p in pts], float)
        amt = np.array([p["amount"] or 0 for p in pts], float)
        rat = np.array([p["ratio"] if p["ratio"] is not None else 0 for p in pts], float)
        sizes = 22 + 380 * np.sqrt(amt / max(amt.max(), 1e-9))
        from matplotlib.colors import LinearSegmentedColormap
        cmap = LinearSegmentedColormap.from_list("rg", [DOWN, "#e6a23c", UP])
        sc = ax.scatter(xv, yv, s=sizes, c=np.clip(rat, -2, 2), cmap=cmap,
                        vmin=-2, vmax=2, alpha=0.85,
                        edgecolors=GRID, linewidths=0.7, zorder=3)
        cb = fig.colorbar(sc, ax=ax, fraction=0.028, pad=0.012)
        cb.set_label("收益 ÷ 波动", fontsize=8)
        cb.ax.tick_params(labelsize=7, colors=SUB)
        cb.outline.set_visible(False)

        x0, x1 = float(xv.min()), float(xv.max())
        y0, y1 = float(yv.min()), float(yv.max())
        px = max((x1 - x0) * 0.08, 0.12)
        py = max((y1 - y0) * 0.10, 0.35)
        ax.axhline(0, color=SUB, lw=0.9, zorder=2)
        ax.axvline(float(xv.mean()), color=SUB, ls="--", lw=0.8, zorder=2)
        ax.axhline(float(yv.mean()), color=SUB, ls="--", lw=0.8, zorder=2)
        ax.text(float(xv.mean()), y0 - py * 0.72, " 均值波动 %.2f%%" % xv.mean(),
                fontsize=7, color=SUB, va="bottom")
        ax.text(x0 - px * 0.1, y1 + py * 0.5, "左上: 涨得多 · 抖得少", fontsize=8.2,
                color=ACCENT, va="top", ha="left")
        ax.text(x1 + px * 0.1, y0 - py * 0.4, "右下: 又抖又亏", fontsize=8.2,
                color=WARN, ha="right", va="bottom")

        # 默认标两端(前10/后5) + 选中项, 中间那堆靠点选看, 免得名字叠成一团
        order = np.argsort(-rat)
        pick = list(order[:10]) + list(order[-5:])
        sel = self._risk_sel
        placed = []
        for rank_i, i in enumerate(pick):
            if sel and pts[i]["code"] == sel:
                continue
            r = pts[i]
            try:
                dx, dy = ax.transData.transform((xv[i], yv[i]))
            except Exception:
                dx = dy = 0
            # 前 5 个必标, 其余跟已标过的靠太近就跳过, 免得左上角糊成一团
            if rank_i >= 5 and any((dx - ox) ** 2 + (dy - oy) ** 2 < 2100
                                   for ox, oy in placed):
                continue
            placed.append((dx, dy))
            ax.annotate(short_name(r["name"], 9), (xv[i], yv[i]), fontsize=6.4,
                        color=TXT, xytext=(6, 5), textcoords="offset points",
                        alpha=0.95,
                        bbox=dict(boxstyle="round,pad=0.12", fc=BG, ec="none", alpha=0.72))
        if sel:
            hit = next((i for i, p in enumerate(pts) if p["code"] == sel), None)
            if hit is not None:
                # 十字线: 155 个点里一个描边圈根本找不到, 拉起两条线才看得见"它在哪一档"
                ax.axvline(xv[hit], color=ACCENT, ls="--", lw=0.9, alpha=0.55, zorder=4)
                ax.axhline(yv[hit], color=ACCENT, ls="--", lw=0.9, alpha=0.55, zorder=4)
                ax.scatter([xv[hit]], [yv[hit]], s=[sizes[hit] + 260], facecolors="none",
                           edgecolors=ACCENT, linewidths=1.9, alpha=0.55, zorder=5)
                ax.scatter([xv[hit]], [yv[hit]], s=[sizes[hit] + 100], facecolors="none",
                           edgecolors="#ffffff", linewidths=1.5, zorder=5)
                r = pts[hit]
                ax.annotate("%s \n%s %+.2f%%  波动%.2f%%  比值%s"
                            % (r["name"][:14], wlab, r["ret"], r["amp"], n0(r["ratio"], 2)),
                            (xv[hit], yv[hit]), fontsize=7.4, color=TXT,
                            xytext=(10, -16), textcoords="offset points", zorder=6,
                            bbox=dict(boxstyle="round,pad=0.28", fc=PANEL2, ec=ACCENT,
                                      alpha=0.95))
                self.risk_sel_lbl.setText(
                    "%s (%s)\n%s %+.2f%% · 波动 %.2f%% · 收益/波动 %s"
                    % (r["name"], r["code"], wlab, r["ret"], r["amp"], n0(r["ratio"], 2)))
        ax.set_xlim(x0 - px, x1 + px)
        ax.set_ylim(y0 - py, y1 + py)
        ax.set_xlabel("区间波动幅度 %% (%s, 日收益标准差×√天数)" % wlab)
        ax.set_ylabel("区间涨跌 %% (%s)" % wlab)
        ax.set_title("风险收益散点 · n=%d · 颜色=区间收益÷区间波动(≥1 才没白担风险) · 点圆点看名 · 双击跳K线" % len(pts),
                     loc="left", fontsize=11)
        ax.grid(color=GRID, lw=0.5, zorder=1)
        self._risk_data = (xv, yv)
        self._risk_sync_xy()
        self.risk_chart.draw()

        # 榜单: 参与散点的全部标的, 点列头可换排序
        self._risk_ranked = [p for p in pts if p["ratio"] is not None]
        self.risk_cnt.setText(
            "共 %d 只（散点里的全部）· 点列头换排序 · 点行在图上定位" % len(self._risk_ranked))
        self._render_risk_tbl()

    def _on_risk_sort(self, col):
        keymap = {0: "name", 1: "code", 2: "cat", 3: "ret", 4: "amp", 5: "ratio"}
        key = keymap.get(col)
        if not key:
            return
        old, desc = self._risk_sort
        self._risk_sort = (col, (not desc) if old == col else key not in ("name", "code", "cat"))
        self._render_risk_tbl()

    def _render_risk_tbl(self):
        col, desc = self._risk_sort
        keymap = {0: "name", 1: "code", 2: "cat", 3: "ret", 4: "amp", 5: "ratio"}
        key = keymap.get(col, "ratio")
        rows = sorted(self._risk_ranked,
                      key=lambda p: (p.get(key) is None, p.get(key) if p.get(key) is not None
                                     else 0), reverse=desc)
        self._risk_pts = rows
        t = self.risk_tbl
        t.blockSignals(True)
        pos = t.verticalScrollBar().value()
        t.setRowCount(len(rows))
        for i, r in enumerate(rows):
            nm = self._lcell(r["name"])
            nm.setToolTip("%s · %s\n区间 %+.2f%% · 波动 %.2f%% · 收益/波动 %s"
                          % (r["name"], r.get("cat") or "", r["ret"], r["amp"] or 0,
                             n0(r["ratio"], 2)))
            if r["code"] == self._risk_sel:
                nm.setForeground(QColor(ACCENT))
            t.setItem(i, 0, nm)
            ic = self._lcell(r["code"])
            ic.setForeground(QColor(SUB))
            t.setItem(i, 1, ic)
            ca = self._lcell(r.get("cat") or "—")
            ca.setForeground(QColor(SUB))
            t.setItem(i, 2, ca)
            t.setItem(i, 3, color_item("%+.2f" % r["ret"], r["ret"]))
            t.setItem(i, 4, self._cell("%.2f" % (r["amp"] or 0)))
            it = QTableWidgetItem(n0(r["ratio"], 2))
            it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            v = r["ratio"] or 0
            it.setForeground(QColor(UP if v >= 1 else WARN if v >= 0 else DOWN))
            t.setItem(i, 5, it)
        no_scroll = bool(getattr(self, "_risk_no_scroll", False))
        self._risk_no_scroll = False
        if self._risk_sel:
            for i, r in enumerate(rows):
                if r["code"] == self._risk_sel:
                    t.selectRow(i)
                    it = t.item(i, 0)
                    # 点图上圆点时选中行常常在视野外, 不滚过去看着就像没反应;
                    # 但如果是用户自己点表里的行, 再滚一次等于把他点的位置丢掉
                    if it is not None and not no_scroll:
                        t.scrollToItem(it, QAbstractItemView.PositionAtCenter)
                    break
        else:
            t.verticalScrollBar().setValue(pos)
        t.blockSignals(False)

    # ---------------- 页5: 轮动 ----------------
    def draw_rot(self):
        rot = self.analyzer.rot if self.analyzer else None
        if not rot:
            self.rot_concl.setText("样本不足, 无法计算轮动")
            self.rot_tbl.setRowCount(0)
            return
        w = self.rot_w
        rows = self._rot_rows(w)
        self.rot_view = rows
        self._render_rot_table(rows, w)
        hl = getattr(self, "rot_hl", None)
        self._draw_rot_bars(rows, w, hl=hl)
        lab = self.analyzer.rot_label(w)
        head = "%s　%d 个板块 · %s动量（成分等权）" % (
            self.analyzer.meta.get("trade_date") or "", len(rot["cats"]), lab)
        if w == 20:
            notes = rot.get("notes") or []
        else:
            notes = self.analyzer._rot_notes(rot["cats"], rot["cur_by_w"][w],
                                             rot["rank_by_w"][w], rot["chg_by_w"][w],
                                             rot["flow"], w)[:2]
        self.rot_concl.setText(head + ("　·　" + "　·　".join(notes[:3]) if notes else ""))

    def _render_rot_table(self, rows, w):
        t = self.rot_tbl
        lab = self.analyzer.rot_label(w)
        for i, h in enumerate(self.rot_hdr):
            it = t.horizontalHeaderItem(i)
            if it is None:
                continue
            k = self.rot_cols[i]
            if k == "cur":
                # 今日窗口下 "今日%" 会和前一列重名, 改成「涨跌%」
                txt = "涨跌%" if w == 0 else "%s%%" % lab
            else:
                txt = h
            it.setText(txt + (" ▼" if self.rot_desc else " ▲") if k == self.rot_sort else txt)
        t.setRowCount(len(rows))
        fb = QFont()
        fb.setBold(True)
        for i, r in enumerate(rows):
            nm = QTableWidgetItem(r["name"])
            nm.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            nm.setToolTip("%s · %d 只成分" % (r["name"], r["n"]))
            t.setItem(i, 0, nm)
            t.setItem(i, 1, color_item(n0(r["pct"]), r["pct"]))
            it = color_item(n0(r["cur"], 1), r["cur"])
            it.setFont(fb)
            t.setItem(i, 2, it)
            i3 = color_item(n0(r["fin"], 1), r["fin"])
            i3.setToolTip("窗口内所有成分、所有交易日里 CLV>0 的部分之和")
            t.setItem(i, 3, i3)
            i4 = color_item(n0(r["fout"], 1), r["fout"])
            i4.setToolTip("窗口内 CLV<0 的部分之和(负值)")
            t.setItem(i, 4, i4)
            fn = color_item(n0(r["fnet"], 1), r["fnet"])
            fn.setFont(fb)
            fn.setToolTip("净额 = 流入 + 流出（同日同口径，不是主力资金）")
            t.setItem(i, 5, fn)

    @staticmethod
    def _bar_cap(vals, q=88):
        """资金额跨度能到两个数量级(小板块几亿, 宽基几千亿), 线性轴会把小板块压成一条线。
        按分位截断, 超出的柱子顶到边界并把真实数字写在柱子里。"""
        v = np.abs(np.asarray(vals, float))
        v = v[np.isfinite(v)]
        if v.size == 0:
            return 1.0
        return max(float(np.percentile(v, q)), 1e-6)

    def _draw_rot_bars(self, rows, w, hl=None):
        """左: 区间流入/流出分两侧画; 右: 净额(=流入+流出)并标横截面均值。"""
        import numpy as np2
        c = self.rot_bar
        c.fig.clear()
        if not rows:
            c.draw()
            return
        names = [r["name"] for r in rows]
        fin = np2.array([r.get("fin") or 0.0 for r in rows], float)
        fout = np2.array([r.get("fout") or 0.0 for r in rows], float)
        fnet = np2.array([r.get("fnet") or 0.0 for r in rows], float)
        y = np2.arange(len(rows))
        nrow = len(rows)
        fs_y, fs_v = (7.4, 6.8) if nrow <= 26 else (6.7, 6.1)
        lab = self.analyzer.rot_label(w)

        gs = c.fig.add_gridspec(1, 2, width_ratios=[2.3, 1], wspace=0.05,
                                left=0.122, right=0.976, top=0.905, bottom=0.152)
        ax = c.fig.add_subplot(gs[0])
        ax.barh(y, fin, height=0.7, color=UP, alpha=0.88, zorder=3)
        ax.barh(y, fout, height=0.7, color=DOWN, alpha=0.88, zorder=3)
        ax.set_yticks(y)
        ax.set_yticklabels(names, fontsize=fs_y)
        ax.invert_yaxis()
        ax.axvline(0, color=SUB, lw=0.9, zorder=4)
        cap = self._bar_cap(np2.concatenate([fin, -fout]))
        ax.set_xlim(-cap * 1.34, cap * 1.34)
        ax.set_ylim(len(rows) - 0.5, -0.5)
        for i in range(nrow):
            for val, sgn in ((fin[i], 1), (fout[i], -1)):
                if val == 0:
                    continue
                big = abs(val) > cap
                if sgn > 0:
                    ax.text(cap * 0.96 if big else val + cap * 0.02, i,
                            "%.0f" % val, va="center",
                            ha="right" if big else "left", fontsize=fs_v,
                            color="white" if big else UP, zorder=5)
                else:
                    ax.text(-cap * 0.96 if big else val - cap * 0.02, i,
                            "%.0f" % abs(val), va="center",
                            ha="left" if big else "right", fontsize=fs_v,
                            color="white" if big else DOWN, zorder=5)
        if hl and hl in names:
            ax.axhspan(names.index(hl) - 0.5, names.index(hl) + 0.5,
                       color=ACCENT, alpha=0.14, zorder=0)
        ax.grid(axis="x", color=GRID, lw=0.5, zorder=0)
        ax.tick_params(axis="x", labelsize=7)
        for sp in ("top", "right", "left"):
            ax.spines[sp].set_visible(False)
        ax.set_title("%s流入 / 流出 (亿元, 成交额×CLV 代理, 成分合计)　红=流入 绿=流出　"
                     "超 %.0f 亿的柱子截断标数" % (lab, cap), loc="left", fontsize=9.2, pad=5)

        ax2 = c.fig.add_subplot(gs[1], sharey=ax)
        ax2.barh(y, fnet, height=0.7,
                 color=[UP if v >= 0 else DOWN for v in fnet], alpha=0.82, zorder=3)
        ax2.axvline(0, color=SUB, lw=0.9, zorder=4)
        fcap = self._bar_cap(fnet)
        ax2.set_xlim(-fcap * 1.38, fcap * 1.38)
        ax2.tick_params(labelleft=False, axis="x", labelsize=7)
        for i, v in enumerate(fnet):
            if v == 0:
                continue
            big = abs(v) > fcap
            ax2.text(fcap * 0.94 * (1 if v >= 0 else -1) if big
                     else v + fcap * 0.05 * (1 if v >= 0 else -1), i, "%+.0f" % v,
                     va="center", ha=("right" if v >= 0 else "left") if big
                     else ("left" if v >= 0 else "right"),
                     fontsize=fs_v, color="white" if big else (UP if v >= 0 else DOWN),
                     zorder=5)
        mean = float(fnet.mean())
        ax2.axvline(mean, color=ACCENT, ls=":", lw=1.1, zorder=5)
        if hl and hl in names:
            ax2.axhspan(names.index(hl) - 0.5, names.index(hl) + 0.5,
                        color=ACCENT, alpha=0.14, zorder=0)
        ax2.grid(axis="x", color=GRID, lw=0.5, zorder=0)
        for sp in ("top", "right", "left"):
            ax2.spines[sp].set_visible(False)
        ax2.set_title("净额(亿元)　点线=板块均值 %+.0f" % mean,
                      loc="left", fontsize=9.4, pad=5)
        # 点击联动用的映射: 点第 i 根柱 = 第 i 行
        self._rot_names = list(names)
        self._rot_axes = [ax, ax2]
        if hl and hl in names:
            r = rows[names.index(hl)]
            info = ("%s · %d 只成分　今日 %s%%　%s动量 %s%%　流入 %.1f 亿　流出 %.1f 亿　净额 %+.1f 亿"
                    % (r["name"], r["n"], n0(r["pct"]), lab, n0(r["cur"], 1),
                       r["fin"] or 0, abs(r["fout"] or 0), r["fnet"] or 0))
            color, ec = TXT, ACCENT
        else:
            info = "点柱子或左侧任一行 → 这里给出该板块同一窗口下的具体数值"
            color, ec = SUB, GRID
        c.fig.text(0.122, 0.018, info, fontsize=8.2, color=color, va="bottom", ha="left",
                   bbox=dict(boxstyle="round,pad=0.35", fc=PANEL2, ec=ec, lw=0.8))
        c.draw()

    # ---------------- 刷新 ----------------
    def on_refresh(self):
        if self.refreshing:
            return
        self.refreshing = True
        self.btn_refresh.setEnabled(False)
        self.btn_refresh.setText("刷新中…")
        n = len(self.analyzer.etfs) if self.analyzer else 0
        self.status.setText(f"后台拉取行情与日K({n}只, 约 {max(1, round(n * 1.2 / 60))} 分钟)…")
        self._rt = RefreshThread()
        self._rt.done.connect(self._on_refreshed)
        self._rt.start()

    def _on_refreshed(self, ok, errs):
        self.refreshing = False
        self.btn_refresh.setEnabled(True)
        self.btn_refresh.setText(" 刷新数据 ")
        pool = [e for e in errs if e.startswith("[标的池]")]
        real = [e for e in errs if not e.startswith("[标的池]")]
        note = f"刷新完成: {ok} 只行情"
        if pool:
            note += " · " + pool[0]
        if real:
            note += f" · {len(real)} 项失败: {real[0][:50]}"
        self.status.setText(note)
        self._log_refresh(ok, errs)
        # 整批失败才弹窗: 单只日K偶发失败不该每次刷新都打扰
        if ok == 0 or any(e.startswith("行情接口失败") for e in real):
            head = "\n".join(_scrub(e)[:160] for e in real[:5])
            more = f"\n… 另有 {len(real) - 5} 项" if len(real) > 5 else ""
            QMessageBox.warning(
                self, f"{brand.NAME} {brand.VERSION}",
                f"拉取到 {ok} 只行情, 有 {len(real)} 项失败。\n\n{head}{more}\n\n"
                f"完整记录见程序目录下的 refresh.log")
        self._load()

    def _log_refresh(self, ok, errs):
        """每次刷新落一行到程序目录, 出问题能直接翻, 不用靠用户复述状态栏。"""
        try:
            import time as _t
            p = Path(etf_app.DB).parent / "refresh.log"
            with open(str(p), "a", encoding="utf-8") as f:
                f.write("[%s] v=%s ok=%d errs=%d %s\n" % (
                    _t.strftime("%Y-%m-%d %H:%M:%S"), brand.VERSION, ok, len(errs),
                    " | ".join(_scrub(e)[:200] for e in errs[:6])))
        except Exception:
            pass


def _scrub(msg):
    """发布版不出现数据接口库的名字, 报错文本里也要盖掉。"""
    s = str(msg)
    if brand.PUBLISH:
        s = s.replace("akshare", "行情接口").replace("AKShare", "行情接口")
    return s


def _app_icon():
    """窗口与任务栏图标。打包后资源在 _internal, 源码运行时在项目目录。

    两个构建用不同配色: 发布版墨绿琥珀, 自用版深灰蓝亮蓝。
    """
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    names = ("icon_a.ico", "icon_self.ico") if brand.PUBLISH else ("icon_self.ico", "icon_a.ico")
    for name in names:
        for rel in ("icons/" + name, name):
            p = root / rel
            if p.exists():
                return QIcon(str(p))
    return QIcon()


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(QSS)
    icon = _app_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)
    win = ProWindow()
    if not icon.isNull():
        win.setWindowIcon(icon)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
