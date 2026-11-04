# -*- coding: utf-8 -*-
"""ETF 标的池维护: 按成交额门槛自动纳入/剔除, 名称关键词自动归类

规则:
  1. 当日成交额 >= ENTER(5亿)  -> 纳入
  2. 已在池内且当日成交额 >= HOLD(2亿) -> 保留(防止单日缩量反复增删)
  3. 已在池内但当日 < HOLD -> 剔除, 除非属于核心宽基/货币(基准品种, 保留)
"""
import json
import os

ENTER = 5.0   # 亿元, 纳入门槛
HOLD = 2.0    # 亿元, 保留门槛

# 分类优先级: 货币/债券/贵金属/商品 > 主动管理 > 强行业词 > 宽基 > 跨境(QDII) > 风格兜底。
# 行业优先于市场: "港股创新药" 归医药医疗而不是港股, "纳指生物科技" 归医药医疗,
# 因为决定它们走势的是产业景气度, 上市地只影响计价与流动性。
# 只有综合型(恒生科技/中概互联网/纳指100/标普500/日经/亚太)才按市场切成跨境类,
RULES = [
    ("货币现金", ["日利", "添益", "货币", "现金", "快线", "保证金", "理财"]),
    ("债券", ["国债", "政金债", "信用债", "可转债", "转债", "债券", "短融", "地方债", "城投"]),
    ("黄金白银", ["黄金", "白银", "贵金属", "金ETF"]),
    ("商品期货", ["原油", "油气", "石油", "天然气", "能源化工", "豆粕", "商品"]),
    # ---- 主动管理型(2026年新批, 名字里带"主动"就先归这一档, 免得被红利/价值吃掉)
    ("主动管理", ["主动管理", "主动ETF", "主动型"]),
    # ---- 强行业词
    ("半导体芯片", ["半导体", "芯片", "集成电路"]),
    ("人工智能", ["人工智能", "AI", "云计算", "软件", "计算机", "数字经济", "信创", "算力", "数据"]),
    ("通信电子", ["通信", "5G", "电子", "光模块", "PCB"]),
    ("传媒游戏", ["传媒", "游戏", "文娱", "影视", "动漫", "文化"]),
    ("新能源车", ["新能源车", "新能车", "电池", "锂电", "汽车"]),
    ("光伏储能", ["光伏", "储能", "碳中和", "环保"]),
    ("军工航天", ["军工", "国防", "卫星", "航天", "低空"]),
    ("机器人机械", ["机器人", "机械", "工业母机", "智能制造"]),
    ("医药医疗", ["医药", "医疗", "生物", "创新药", "疫苗", "生医", "药"]),
    ("食品饮料", ["食品", "饮料", "酒"]),
    ("消费", ["消费", "家电", "美容", "旅游", "零售", "纺织"]),
    ("农业养殖", ["养殖", "农业", "畜牧", "种业", "农牧"]),
    ("银行", ["银行"]),
    ("券商保险", ["证券", "券商", "保险", "非银", "金融"]),
    ("地产建材", ["房地产", "地产", "建材", "建筑", "REIT", "基建"]),
    ("有色资源", ["有色", "铜", "铝", "稀土", "矿业", "资源", "金属", "锂"]),
    ("煤炭钢铁", ["煤炭", "钢铁", "焦炭"]),
    ("化工", ["化工", "化学"]),
    ("电力公用", ["电力", "公用", "水务", "电网", "核电"]),
    # ---- 宽基细分 (A500 必须先于 A50)
    ("宽基·A系列", ["A500", "中证A50", "中证A100", "中证A", "MSCI"]),
    ("宽基·沪深300", ["沪深300", "300ETF", "300指数"]),
    ("宽基·上证50", ["上证50", "上证180", "上证380", "A50", "上证综指", "上证指数"]),
    ("宽基·中证500/1000", ["中证500", "中证1000", "中证2000", "中证100", "中证800", "中证全指", "全指"]),
    ("宽基·双创", ["创业板", "科创50", "科创100", "科创综", "科创板", "双创", "科创"]),
    ("宽基·深证", ["深证成指", "深证100", "深成指"]),
    # ---- 跨境(QDII): 按跟踪的市场拆开, 纳指/标普/日欧/全球各有各的时区与汇率
    #      "中韩半导体" 不在此列, 它是半导体主题, 归行业词
    ("跨境·纳指", ["纳指", "纳斯达克"]),
    ("跨境·标普", ["标普", "道琼斯", "美国50", "美国"]),
    ("跨境·日欧", ["日经", "日本", "东证", "德国", "法国", "英国", "欧洲"]),
    ("跨境·全球", ["全球", "海外", "世界", "国际", "亚太", "东南亚", "沙特", "印度", "越南"]),
    ("跨境·港股", ["恒生", "港股", "香港", "H股", "中概", "恒指"]),
    # ---- 风格兜底
    ("红利低波", ["红利", "低波", "价值", "股息", "央企", "国企"]),
    ("科技综合", ["科技", "创新", "成长", "龙头", "主题"]),
]
CODE_HINT = {"518": "黄金白银", "511": "货币现金"}

# 核心基准: 缩量也不剔除
KEEP_CATS = {"宽基·沪深300", "宽基·上证50", "宽基·中证500/1000",
             "宽基·双创", "货币现金", "主动管理",
             # QDII 通道少、同标的替代品不多, 缩量也留着
             "跨境·纳指", "跨境·标普", "跨境·日欧", "跨境·全球", "跨境·港股"}


def classify(name, old=None, code=""):
    # "标普A股红利" "MSCI中国A股" 名字带海外指数名, 但投的是 A 股, 不能算跨境
    a_share = ("A股" in name) or ("中国A" in name)
    for cat, kws in RULES:
        if a_share and cat.startswith("跨境"):
            continue
        for k in kws:
            if k in name:
                return cat
    if code[:3] in CODE_HINT:
        return CODE_HINT[code[:3]]
    return old or "其他"


def is_qdii(cat):
    """跨境品种: 涨跌幅跟着海外市场走, 溢价率口径也和 A 股 ETF 不同。"""
    return bool(cat) and str(cat).startswith("跨境")


# 全部跨境关键词, 用于给归到行业分类的海外品种(纳指生物科技/港股创新药)打 Q 标
_CROSS_KWS = [k for cat, kws in RULES if cat.startswith("跨境") for k in kws]


def looks_qdii(name):
    """按名称判断是不是跨境(QDII)。分类归行业时也要能认出来, 溢价口径要提示。"""
    n = str(name or "")
    if "A股" in n or "中国A" in n:
        return False
    return any(k in n for k in _CROSS_KWS)


def ensure_list(spot, base_dir):
    """spot: 行情快照接口返回的 DataFrame。返回 (清单, 变动说明)"""
    path = os.path.join(base_dir, "etf_list.json")
    old = {}
    if os.path.exists(path):
        try:
            for e in json.load(open(path, encoding="utf-8")):
                old[e["code"]] = e
        except Exception:
            old = {}

    import pandas as pd  # noqa: F401  (spot 已是 DataFrame)

    df = spot.copy()
    df["代码"] = df["代码"].astype(str).str.zfill(6)
    df["amt"] = df["成交额"].astype(float) / 1e8

    picked, added, dropped = [], [], []
    for _, r in df.iterrows():
        code, name, amt = r["代码"], str(r["名称"]), float(r["amt"])
        prev = old.get(code)
        if prev is not None:
            if amt >= HOLD or prev.get("category") in KEEP_CATS:
                keep = True
            else:
                keep = False
                dropped.append(f"{code} {name}({amt:.1f}亿)")
        else:
            keep = amt >= ENTER
            if keep:
                added.append(f"{code} {name}({amt:.1f}亿)")
        if not keep:
            continue
        item = {
            "code": code, "name": name,
            # 分类每次按最新规则重算, 规则调整后旧池子能自动迁移
            "category": classify(name, prev["category"] if prev else None, code),
            "amount": round(amt, 2),
            "note": f"成交{amt:.1f}亿",
        }
        if prev and prev.get("pin"):
            item["pin"] = True
        picked.append(item)

    picked.sort(key=lambda x: -(x.get("amount") or 0))

    # 手工锁定(pin)的标的: 还没上市/没有行情也保留, 上市后自动接上
    have = {x["code"] for x in picked}
    for code, prev in old.items():
        if prev.get("pin") and code not in have:
            picked.append(prev)
            have.add(code)

    # 类别兜底: 每个分类至少留成交额最大的一只, 否则轮动/分类对比会缺类
    have_cats = {x["category"] for x in picked}
    have_codes = {x["code"] for x in picked}
    best_by_cat = {}
    for _, r in df.iterrows():
        code, name, amt = r["代码"], str(r["名称"]), float(r["amt"])
        cat = classify(name, None, code)
        if cat == "其他":
            continue
        if cat not in best_by_cat or amt > best_by_cat[cat]["amount"]:
            best_by_cat[cat] = {"code": code, "name": name, "category": cat,
                                "amount": round(amt, 2), "note": f"成交{amt:.1f}亿"}
    for cat, item in best_by_cat.items():
        if cat in have_cats or item["code"] in have_codes:
            continue
        picked.append(item)
        have_codes.add(item["code"])
        have_cats.add(cat)
        added.append(f"{item['code']} {item['name']}({item['amount']}亿, 类别兜底)")
        dropped = [d for d in dropped if not d.startswith(item["code"] + " ")]

    picked.sort(key=lambda x: -(x.get("amount") or 0))
    json.dump(picked, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return picked, {"added": added, "dropped": dropped}
