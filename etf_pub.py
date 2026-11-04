# -*- coding: utf-8 -*-
"""指数罗盘 · 公开发布版入口。

与自用版共用同一套分析逻辑, 只切换品牌与主题:
- 名称、页签文案、配色取自 brand.PUBLISH 分支
- 不写调试日志, 界面与提示中不出现数据接口库名称
"""
import os
import sys
from pathlib import Path

os.environ["ETF_PUBLISH"] = "1"

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import etf_pro  # noqa: E402

if __name__ == "__main__":
    etf_pro.main()
