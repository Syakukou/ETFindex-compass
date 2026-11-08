# -*- coding: utf-8 -*-
"""跨平台运行环境: 数据目录、中文字体、应用标识。

同一份代码要在 Windows 和 macOS 上跑, 两边的差异全集中在这里, 别处不再判断平台:

- 数据目录: Windows 写 exe 同级; macOS 写 ~/Library/Application Support/。
  macOS 的 .app 通常放在 /Applications 下, 拷进去的 .app 内部不可写, 而且每次
  覆盖安装都会把包内数据一起换掉, 用户的库和自选列表会丢。所以必须落到用户目录。
- 中文字体: Windows 是微软雅黑, macOS 是苹方。字体名对不上时 matplotlib 会退回
  DejaVu Sans, 表现是满屏方块, 不是报错, 很容易误判成"图表坏了"。
"""
import os
import sys
from pathlib import Path

IS_MAC = sys.platform == "darwin"
IS_WIN = os.name == "nt"

APP_ID = "IndexCompass"        # 用户数据目录名 / bundle id 中段
APP_DISPLAY = "指数罗盘"


def is_frozen():
    return bool(getattr(sys, "frozen", False))


def data_dir():
    """可写的运行数据目录: 数据库、自选列表、日志都放这里。"""
    if IS_MAC:
        base = Path.home() / "Library" / "Application Support" / APP_ID
    elif is_frozen():
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parent
    try:
        base.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return base


def bundle_dir():
    """只读资源目录: 冻结后是 _MEIPASS, 源码运行是脚本所在目录。"""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


# matplotlib 的 font.sans-serif 候选链, 顺序即优先级
CJK_FONTS = (
    ["PingFang SC", "Hiragino Sans GB", "Heiti SC", "STHeiti", "Songti SC", "Arial Unicode MS"]
    if IS_MAC else
    ["Microsoft YaHei", "SimHei", "DengXian", "SimSun"]
)

# Qt 样式表里的字体族, 写成 fallback 链, 两个平台都不会落到无衬线默认字体上
CJK_CSS = (
    '"PingFang SC", "Hiragino Sans GB", "Heiti SC", sans-serif'
    if IS_MAC else
    '"Microsoft YaHei", "SimHei", sans-serif'
)

# 窗口图标优先用哪个格式: macOS 对 .ico 支持不稳, 优先 png
ICON_EXTS = ("png", "ico") if IS_MAC else ("ico", "png")
