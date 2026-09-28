#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 Chrome headless 把自包含 SVG 渲染成 PNG，做像素级自检，再经 chart-saver 保存。

自检思路：同一份 SVG 渲染两次 —— 注入样式版 vs 剥掉 <style> 版。
若两版差异显著（配色像素占比变化大），说明语义样式确实生效，而不是渲染成黑白线框。
"""
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
OUT = Path(r"D:\code_work_space\llm\nl2sql\docs\archify-output")
SVG = OUT / "_standalone.svg"
SKILL_SCRIPTS = Path(r"/agent/shared/skills_bak\main\chart-saver\scripts")
sys.path.insert(0, str(SKILL_SCRIPTS))

svg_text = SVG.read_text(encoding="utf-8")
plain = re.sub(r"<style>[\s\S]*?</style>", "", svg_text, count=1)
plain_path = OUT / "_plain.svg"
plain_path.write_text(plain, encoding="utf-8")

size = re.search(r'width="(\d+)" height="(\d+)"', svg_text)
w, h = int(size.group(1)), int(size.group(2))
print(f"viewBox size: {w}x{h}")


def shoot(svg_path: Path, png_path: Path) -> None:
    cmd = [
        CHROME,
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--force-device-scale-factor=2",
        f"--window-size={w},{h}",
        f"--screenshot={png_path}",
        svg_path.as_uri(),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    print(f"chrome exit={r.returncode} -> {png_path.name}")


styled_png = OUT / "_standalone_x2.png"
plain_png = OUT / "_plain_x2.png"
shoot(SVG, styled_png)
shoot(plain_path, plain_png)

from PIL import Image  # noqa: E402


def stats(png: Path):
    im = Image.open(png).convert("RGB")
    px = list(im.getdata())
    c = Counter(px)
    total = len(px)
    top = c.most_common(1)[0]
    # 近似“纯灰阶”像素占比（R≈G≈B）：未着色线框会很高
    grayish = sum(n for (r, g, b), n in c.items() if abs(r - g) <= 6 and abs(g - b) <= 6)
    return {
        "size": im.size,
        "unique": len(c),
        "top": top[0],
        "top_share": round(top[1] / total, 3),
        "grayish_share": round(grayish / total, 3),
    }


s_styled = stats(styled_png)
s_plain = stats(plain_png)
print("styled:", s_styled)
print("plain :", s_plain)

# 采样若干语义节点填充色，确认调色板真的画出来了
im = Image.open(styled_png).convert("RGB")
sw, sh = im.size
samples = {
    "frontend 节点": (130, 378),
    "host 节点": (855, 378),
    "store 节点": (810, 862),
    "ext 节点": (1080, 880),
    "画布背景": (int(sw * 0.5), int(sh * 0.985)),
}
for name, (x, y) in samples.items():
    sx, sy = int(x / w * sw), int(y / h * sh)
    print(f"  {name} @({x},{y}) -> {im.getpixel((min(sx, sw - 1), min(sy, sh - 1)))}")

from save_chart import save_chart  # noqa: E402

dest = save_chart(str(styled_png), "nl2sql后端整体架构", fmt="png")
print(f"chart-saver png -> {dest}")
