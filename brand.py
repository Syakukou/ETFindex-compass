# -*- coding: utf-8 -*-
"""品牌与主题。

同一套代码两个外观:
- 默认: 本机自用版(深灰蓝 + 亮蓝), 窗口标题与页签保持原样
- ETF_PUBLISH=1: 公开发布版(深墨绿 + 琥珀), 独立名称与页签文案

发布版不出现数据接口库的名字, 界面与文档统一说"公开行情接口"。
"""
import os

PUBLISH = os.environ.get("ETF_PUBLISH") == "1"

if PUBLISH:
    NAME = "指数罗盘"
    NAME_EN = "IndexCompass"
    TITLE = "指数罗盘 · ETF 资金与持仓观察台"
    SOURCE_TAG = "public"
    SOURCE_TEXT = "公开行情接口"
    TABS = {
        "rank": " 综合评分 ",
        "kline": " 行情走势 ",
        "corr": " 相关性 ",
        "risk": " 风险收益 ",
        "rot": " 板块轮动 ",
        "flow": " 指数资金流 ",
        "hold": " 持仓穿透 ",
    }
    # 深墨绿 + 琥珀, 和自用版的深灰蓝 + 亮蓝区分开
    BG = "#0f1614"; PANEL = "#151f1c"; PANEL2 = "#1b2723"
    TXT = "#dbe5e1"; SUB = "#7f918b"; UP = "#d65151"; DOWN = "#3fa87e"
    ACCENT = "#c8963e"; GRID = "#26332f"; WARN = "#d9a441"
else:
    NAME = "ETF 量化分析终端"
    NAME_EN = "ETFQuant"
    TITLE = "ETF 量化分析终端"
    SOURCE_TAG = "local"
    SOURCE_TEXT = "本地行情库"
    TABS = {
        "rank": " 评分排名 ",
        "kline": " K线图表 ",
        "corr": " 相关性矩阵 ",
        "risk": " 风险收益 ",
        "rot": " 板块轮动 ",
        "flow": " 指数·ETF进出 ",
        "hold": " ETF成分股 ",
    }
    BG = "#161a23"; PANEL = "#1e222d"; PANEL2 = "#232834"
    TXT = "#d1d4dc"; SUB = "#787b86"; UP = "#e05252"; DOWN = "#3fb68b"
    ACCENT = "#4a8cff"; GRID = "#2a2e39"; WARN = "#e6a23c"
