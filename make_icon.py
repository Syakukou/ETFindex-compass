# -*- coding: utf-8 -*-
"""生成程序图标。

两套方案，同一套配色（深墨绿底 + 琥珀）：A 罗盘指针，B 上行折线。
超采样 4 倍后缩小，边缘抗锯齿。最终输出多尺寸 .ico。
"""
import os
from PIL import Image, ImageDraw

SS = 1024            # 绘制尺寸
C = SS // 2          # 中心
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
os.makedirs(OUT, exist_ok=True)

BG_TOP = (28, 46, 39)
BG_BOT = (9, 14, 12)
EDGE = (46, 62, 55)
AMBER = (203, 152, 64)
AMBER_SOFT = (203, 152, 64, 175)
WHITE = (234, 242, 238)
DIM = (86, 104, 97)


def vgrad(size, top, bot):
    w, h = size
    img = Image.new("RGBA", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / (h - 1)
        c = tuple(int(top[i] * (1 - t) + bot[i] * t) for i in range(3)) + (255,)
        d.line([(0, y), (w, y)], fill=c)
    return img


def base():
    """圆角方形底，内描一圈淡边，避免在深色任务栏上糊成一片。"""
    grad = vgrad((SS, SS), BG_TOP, BG_BOT)
    mask = Image.new("L", (SS, SS), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, SS - 1, SS - 1], radius=int(SS * 0.23), fill=255)
    out = Image.new("RGBA", (SS, SS), (0, 0, 0, 0))
    out.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(out)
    d.rounded_rectangle([6, 6, SS - 7, SS - 7], radius=int(SS * 0.225),
                        outline=EDGE + (255,), width=6)
    return out


def ring(img, width=30, radius=330):
    d = ImageDraw.Draw(img)
    d.ellipse([C - radius, C - radius, C + radius, C + radius],
              outline=AMBER_SOFT, width=width)
    return img


def ticks(img, radius=330, length=54, width=12):
    """环上四个方位的刻度，强化罗盘感。"""
    d = ImageDraw.Draw(img)
    for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0)):
        x1, y1 = C + dx * (radius - length // 2), C + dy * (radius - length // 2)
        x2, y2 = C + dx * (radius + length // 2), C + dy * (radius + length // 2)
        d.line([(x1, y1), (x2, y2)], fill=AMBER, width=width)
    return img


def variant_needle():
    """A：罗盘指针。远端琥珀，近端暗色，菱形收尖。"""
    img = base()
    ring(img)
    ticks(img)
    d = ImageDraw.Draw(img)
    d_ = 0.7071
    far = (C + int(242 * d_), C - int(242 * d_))
    near = (C - int(158 * d_), C + int(158 * d_))
    side1 = (C + int(64 * d_), C + int(64 * d_))
    side2 = (C - int(64 * d_), C - int(64 * d_))
    d.polygon([far, side1, side2], fill=AMBER)          # 指向端
    d.polygon([near, side1, side2], fill=DIM)           # 配重端
    d.ellipse([C - 27, C - 27, C + 27, C + 27], fill=BG_BOT + (255,))
    d.ellipse([C - 13, C - 13, C + 13, C + 13], fill=AMBER)
    return img


def variant_trend():
    """B：上行折线，末端一个琥珀点。"""
    img = base()
    ring(img)
    d = ImageDraw.Draw(img)
    pts = [(374, 646), (458, 552), (549, 596), (664, 410)]
    d.line(pts, fill=WHITE, width=50, joint="curve")
    for p in (pts[0], pts[1], pts[2]):
        d.ellipse([p[0] - 25, p[1] - 25, p[0] + 25, p[1] + 25], fill=WHITE)
    d.ellipse([664 - 40, 410 - 40, 664 + 40, 410 + 40], fill=AMBER)
    return img


def save_all(img, name):
    img.resize((256, 256), Image.LANCZOS).save(os.path.join(OUT, name + ".png"))
    img.resize((512, 512), Image.LANCZOS).save(os.path.join(OUT, name + "_preview.png"))
    for s in (256, 128, 64, 48, 32, 16):
        img.resize((s, s), Image.LANCZOS).save(os.path.join(OUT, "%s_%d.png" % (name, s)))
    img.save(os.path.join(OUT, name + ".ico"), format="ICO",
             sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])


if __name__ == "__main__":
    save_all(variant_needle(), "icon_a")
    save_all(variant_trend(), "icon_b")
    print("已生成到", OUT)
    for f in sorted(os.listdir(OUT)):
        if f.endswith((".ico", "_preview.png")):
            p = os.path.join(OUT, f)
            print(" ", f, round(os.path.getsize(p) / 1024, 1), "KB")
