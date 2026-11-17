#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
行程助手（CalendarAssistant）应用图标生成 — macOS 大苏尔风格 squircle
两个方向：
  A 日历＋定位针   —— 日历页 + 右下角蓝色定位针（强调"行程/出发提醒"）
  B 日历＋行程条   —— 日历页 + 下方一蓝一灰两条行程块（强调"日程列表"）
配色 4 色：蓝 #2563EB（与 App 界面主按钮同源）/ 墨 #1F2937 / 浅灰 #D3D7DE / 白
2× 超采样抗锯齿；输出 1024 母版 + 512/64 预览。
"""
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path

OUT = Path(__file__).resolve().parent
FINAL = 1024
SS = 2                      # 超采样倍数，抗锯齿
S = FINAL * SS              # 2048 实绘
R = int(0.225 * S)          # squircle 圆角半径 ≈ 22.5%

BLUE, INK, GRAY, WHITE = "#2563EB", "#1F2937", "#D3D7DE", "#FFFFFF"
CN_FONT = "/System/Library/Fonts/Hiragino Sans GB.ttc"
NUM_FONT = "/System/Library/Fonts/Helvetica.ttc"

def font(path, size, index=0):
    return ImageFont.truetype(path, size, index=index)

def base_card():
    """白底 squircle 母版 + 同形遮罩"""
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, S, S], radius=R, fill=WHITE)
    return img

def paste_band(img):
    """顶部蓝色表头带（用同一 squircle 遮罩裁出圆角）"""
    band_h = int(0.27 * S)
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rounded_rectangle([0, 0, S, S], radius=R, fill=BLUE)          # 全幅蓝
    d.rectangle([0, band_h, S, S], fill=(0, 0, 0, 0))               # 挖掉表头以下
    d.rectangle([0, R, S, band_h], fill=BLUE)                       # 补直边段
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S, S], radius=R, fill=255)
    img.alpha_composite(Image.composite(layer, Image.new("RGBA", (S, S), (0,0,0,0)), layer.split()[3]))
    return img, band_h

def draw_text_center(img, xy, text, f, fill):
    d = ImageDraw.Draw(img)
    l, t, rr, b = d.textbbox((0, 0), text, font=f)
    w, h = rr - l, b - t
    d.text((xy[0] * SS - w / 2 - l, xy[1] * SS - h / 2 - t), text, font=f, fill=fill)

def make_a():
    """方向 A：日历 + 右下角定位针（针作角标，不压数字）"""
    img, band_h = paste_band(base_card())
    draw_text_center(img, (512, 130), "三月", font(CN_FONT, 118), WHITE)
    draw_text_center(img, (460, 470), "22", font(NUM_FONT, 620, index=1), INK)
    # 定位针：圆头（带白孔）+ 下探针尾，右下角出血（越出卡面裁掉一部分，读作角标）
    d = ImageDraw.Draw(img)
    cx, cy, r = int(0.80 * S), int(0.78 * S), int(0.115 * S)
    tail = [(cx - r * 0.92, cy + r * 0.45), (cx + r * 0.92, cy + r * 0.45), (cx, cy + r * 2.0)]
    d.polygon(tail, fill=BLUE)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=BLUE)
    hole = int(r * 0.42)
    d.ellipse([cx - hole, cy - hole, cx + hole, cy + hole], fill=WHITE)
    return _finish(img)

def make_b():
    """方向 B：日历 + 两条行程块（一蓝一灰）"""
    img, band_h = paste_band(base_card())
    draw_text_center(img, (512, 130), "三月", font(CN_FONT, 118), WHITE)
    draw_text_center(img, (512, 460), "22", font(NUM_FONT, 560, index=1), INK)
    d = ImageDraw.Draw(img)
    m, bw, bh, br = int(0.12 * S), int(0.56 * S), int(0.062 * S), int(0.031 * S)
    y1, y2 = int(0.685 * S), int(0.80 * S)
    d.rounded_rectangle([m, y1, m + bw, y1 + bh], radius=br, fill=BLUE)
    d.rounded_rectangle([m, y2, m + bw * 0.72, y2 + bh], radius=br, fill=GRAY)
    return _finish(img)

def _finish(img):
    """遮罩成 squircle + 缩到 1024"""
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S, S], radius=R, fill=255)
    img.putalpha(mask)
    return img.resize((FINAL, FINAL), Image.LANCZOS)

if __name__ == "__main__":
    for tag, fn in (("A-日历+定位针", make_a), ("B-日历+行程条", make_b)):
        p = OUT / f"图标方向{tag}.png"
        fn().save(p)
        print(p, p.stat().st_size, "bytes")
    # 预览拼版：A/B 各 512 + 64，白底并排
    sheet = Image.new("RGBA", (2 * 512 + 3 * 48, 512 + 64 + 96), (255, 255, 255, 255))
    for i, tag in enumerate(("A-日历+定位针", "B-日历+行程条")):
        im = Image.open(OUT / f"图标方向{tag}.png")
        s512 = im.resize((512, 512), Image.LANCZOS)
        s64 = im.resize((64, 64), Image.LANCZOS)
        x = 48 + i * (512 + 48)
        sheet.alpha_composite(s512, (x, 24))
        sheet.alpha_composite(s64, (x + 224, 512 + 48))
    sheet = sheet.crop((0, 0, 2 * 512 + 3 * 48, 512 + 64 + 72))
    sheet.convert("RGB").save(OUT / "图标预览拼版.png")
    print(OUT / "图标预览拼版.png", (OUT / "图标预览拼版.png").stat().st_size, "bytes")
