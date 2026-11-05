# -*- coding: utf-8 -*-
"""生成程序图标。

同一套形状, 两套配色, 对应两个构建:
- icon_a    : 深墨绿 + 琥珀, 发布版(指数罗盘)
- icon_self : 深灰蓝 + 亮蓝, 本机自用版(ETF 量化分析终端)

另有 icon_b 上行折线方案, 保留备选, 未接进任何构建。
超采样后缩小, 边缘抗锯齿。最终输出多尺寸 .ico。
"""
import os
from PIL import Image, ImageDraw

SS = 1024            # 绘制尺寸
C = SS // 2          # 中心
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
os.makedirs(OUT, exist_ok=True)

# 发布版: 深墨绿底 + 琥珀
PUB = dict(
    bg_top=(28, 46, 39), bg_bot=(9, 14, 12), edge=(46, 62, 55),
    accent=(203, 152, 64), accent_soft=(203, 152, 64, 175),
    white=(234, 242, 238), dim=(86, 104, 97),
)

# 自用版: 深灰蓝底 + 亮蓝, 与界面主题 BG/ACCENT 一致
SELF = dict(
    bg_top=(31, 38, 54), bg_bot=(10, 12, 19), edge=(52, 60, 82),
    accent=(74, 140, 255), accent_soft=(74, 140, 255, 165),
    white=(223, 232, 246), dim=(84, 94, 116),
)


def vgrad(size, top, bot):
    w, h = size
    img = Image.new("RGBA", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / (h - 1)
        c = tuple(int(top[i] * (1 - t) + bot[i] * t) for i in range(3)) + (255,)
        d.line([(0, y), (w, y)], fill=c)
    return img


def base(p):
    """圆角方形底, 内描一圈淡边, 避免在深色任务栏上糊成一片。"""
    grad = vgrad((SS, SS), p["bg_top"], p["bg_bot"])
    mask = Image.new("L", (SS, SS), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, SS - 1, SS - 1], radius=int(SS * 0.23), fill=255)
    out = Image.new("RGBA", (SS, SS), (0, 0, 0, 0))
    out.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(out)
    d.rounded_rectangle([6, 6, SS - 7, SS - 7], radius=int(SS * 0.225),
                        outline=p["edge"] + (255,), width=6)
    return out


def ring(img, p, width=30, radius=330):
    d = ImageDraw.Draw(img)
    d.ellipse([C - radius, C - radius, C + radius, C + radius],
              outline=p["accent_soft"], width=width)
    return img


def ticks(img, p, radius=330, length=54, width=12):
    """环上四个方位的刻度, 强化罗盘感。"""
    d = ImageDraw.Draw(img)
    for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0)):
        x1, y1 = C + dx * (radius - length // 2), C + dy * (radius - length // 2)
        x2, y2 = C + dx * (radius + length // 2), C + dy * (radius + length // 2)
        d.line([(x1, y1), (x2, y2)], fill=p["accent"], width=width)
    return img


def variant_needle(p):
    """A: 罗盘指针。远端主色, 近端暗色, 菱形收尖。"""
    img = base(p)
    ring(img, p)
    ticks(img, p)
    d = ImageDraw.Draw(img)
    d_ = 0.7071
    far = (C + int(242 * d_), C - int(242 * d_))
    near = (C - int(158 * d_), C + int(158 * d_))
    side1 = (C + int(64 * d_), C + int(64 * d_))
    side2 = (C - int(64 * d_), C - int(64 * d_))
    d.polygon([far, side1, side2], fill=p["accent"])    # 指向端
    d.polygon([near, side1, side2], fill=p["dim"])      # 配重端
    d.ellipse([C - 27, C - 27, C + 27, C + 27], fill=p["bg_bot"] + (255,))
    d.ellipse([C - 13, C - 13, C + 13, C + 13], fill=p["accent"])
    return img


def variant_trend(p):
    """B: 上行折线, 末端一个主色点。备选, 未接入构建。"""
    img = base(p)
    ring(img, p)
    d = ImageDraw.Draw(img)
    pts = [(374, 646), (458, 552), (549, 596), (664, 410)]
    d.line(pts, fill=p["white"], width=50, joint="curve")
    for pt in (pts[0], pts[1], pts[2]):
        d.ellipse([pt[0] - 25, pt[1] - 25, pt[0] + 25, pt[1] + 25], fill=p["white"])
    d.ellipse([664 - 40, 410 - 40, 664 + 40, 410 + 40], fill=p["accent"])
    return img


def save_all(img, name):
    img.resize((256, 256), Image.LANCZOS).save(os.path.join(OUT, name + ".png"))
    img.resize((512, 512), Image.LANCZOS).save(os.path.join(OUT, name + "_preview.png"))
    for s in (256, 128, 64, 48, 32, 16):
        img.resize((s, s), Image.LANCZOS).save(os.path.join(OUT, "%s_%d.png" % (name, s)))
    img.save(os.path.join(OUT, name + ".ico"), format="ICO",
             sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])


if __name__ == "__main__":
    save_all(variant_needle(PUB), "icon_a")
    save_all(variant_needle(SELF), "icon_self")
    save_all(variant_trend(PUB), "icon_b")
    print("已生成到", OUT)
    for f in sorted(os.listdir(OUT)):
        if f.endswith((".ico", "_preview.png")):
            p = os.path.join(OUT, f)
            print(" ", f, round(os.path.getsize(p) / 1024, 1), "KB")
