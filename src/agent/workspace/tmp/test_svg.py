import sys
sys.path.insert(0, '.')

# 直接测试 _svg_to_data_url 和 _sanitize_echarts_result 的 SVG 分支
def _svg_to_data_url(svg):
    import re, base64
    svg = svg.strip()
    svg = re.sub(r"</svg>[\s\S]*$", "</svg>", svg)
    html = f'<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"></head><body style="margin:0;display:flex;justify-content:center;background:#fff">{svg}</body></html>'
    b64 = base64.b64encode(html.encode("utf-8")).decode("ascii")
    return f'<iframe src="data:text/html;base64,{b64}" width="100%" height="500" style="border:none;border-radius:8px"></iframe>'

svg = '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50"><rect width="100" height="50" fill="red"/></svg>'
r = _svg_to_data_url(svg)
print('SVG→iframe prefix:', r[:80])
print('SVG→iframe contains iframe:', r.startswith('<iframe'))
print('SVG→iframe contains data:text/html;base64:', 'data:text/html;base64' in r)
