#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 archify 交付的 HTML 里的内联 SVG 抽成自包含（带样式）的 .svg，再交给 chart-saver 保存。"""
import re
import sys
from pathlib import Path

HTML = Path(r"D:\code_work_space\llm\nl2sql\docs\archify-output\nl2sql后端整体架构.html")
SKILL_SCRIPTS = Path(r"D:\code_work_space\llm\nl2sql\src\agent\shared\skills\main\chart-saver\scripts")
sys.path.insert(0, str(SKILL_SCRIPTS))

html = HTML.read_text(encoding="utf-8")
svg = re.search(r"<svg[\s\S]*?</svg>", html).group(0)
print(f"svg bytes = {len(svg.encode('utf-8'))}")

# 1) 收集 SVG 里用到的 class
used = set()
for m in re.finditer(r'class="([^"]+)"', svg):
    used.update(m.group(1).split())
print(f"svg classes ({len(used)}): {', '.join(sorted(used))}")

# 2) 收集页面 CSS 中与这些 class 有关的规则 + 变量定义块
css = "\n".join(m.group(1) for m in re.finditer(r"<style[^>]*>([\s\S]*?)</style>", html))
print(f"page css bytes = {len(css)}")

rules = re.findall(r"[^{}]+\{[^{}]*\}", css)
keep = []
for rule in rules:
    selector = rule.split("{")[0].strip()
    body = rule.split("{", 1)[1]
    if selector.startswith(":root") or "[data-theme" in selector or selector.startswith("@media"):
        keep.append(rule)
        continue
    if any(re.search(r"\." + re.escape(c) + r"(?![A-Za-z0-9_-])", selector) for c in used):
        keep.append(rule)
    elif "var(--" in body and selector in ("*", "svg"):
        keep.append(rule)
print(f"kept rules = {len(keep)} / {len(rules)}")

style_block = "<style>\n" + "\n".join(keep) + "\n</style>\n"

# 3) 组装自包含 SVG：补 xmlns、尺寸，并注入样式
svg_open = re.match(r"<svg[^>]*>", svg).group(0)
attrs = svg_open
if "xmlns" not in attrs:
    attrs = attrs[:-1] + ' xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
vb = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg_open)
if vb:
    w, h = vb.group(1), vb.group(2)
    attrs = attrs[:-1] + f' width="{w}" height="{h}">'
standalone = svg.replace(svg_open, attrs + "\n" + style_block, 1)
# 4) 主题变量：默认用浅色主题
standalone = standalone.replace("<svg ", '<svg data-theme="light" ', 1)

tmp = Path(r"D:\code_work_space\llm\nl2sql\docs\archify-output\_standalone.svg")
tmp.write_text(standalone, encoding="utf-8")
print(f"standalone svg -> {tmp} ({tmp.stat().st_size} bytes)")

from save_chart import save_chart  # noqa: E402

dest = save_chart(standalone, "nl2sql后端整体架构", fmt="svg")
print(f"chart-saver svg -> {dest}")
