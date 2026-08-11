import sys
sys.path.insert(0, '.')

# 模拟 _svg_to_data_url
def _svg_to_data_url(svg):
    import re, base64
    svg = svg.strip()
    svg = re.sub(r"</svg>[\s\S]*$", "</svg>", svg)
    html = f'<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"></head><body style="margin:0;display:flex;justify-content:center;background:#fff">{svg}</body></html>'
    b64 = base64.b64encode(html.encode("utf-8")).decode("ascii")
    return f'<iframe src="data:text/html;base64,{b64}" width="100%" height="500" style="border:none;border-radius:8px"></iframe>'

# 模拟 _sanitize_echarts_result 的 dict 处理逻辑
def sanitize(result):
    if isinstance(result, dict) and isinstance(result.get("content"), list):
        for item in result["content"]:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and "<svg" in str(item.get("text", "")):
                return _svg_to_data_url(item["text"])
            if item.get("type") == "image":
                data = item.get("data", "")
                mime = item.get("mimeType", "image/png")
                if data:
                    return f'<img src="data:{mime};base64,{data}" style="max-width:100%;border-radius:8px"/>'
        texts = [str(i.get("text", "")) for i in result["content"] if isinstance(i, dict) and i.get("type") == "text"]
        joined = "".join(texts)
        if "<svg" in joined:
            return _svg_to_data_url(joined)
    return result

# 测试 MCP 标准响应格式（SVG）
r1 = sanitize({'content': [{'type': 'text', 'text': '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50"><rect width="100" height="50" fill="red"/></svg>'}]})
print('TEST1 (MCP text SVG):', r1[:60])

# 测试 MCP 标准响应格式（image）
r2 = sanitize({'content': [{'type': 'image', 'data': 'iVBORw0KGgoAAAANSUhEUg==', 'mimeType': 'image/png'}]})
print('TEST2 (MCP image):', r2[:60])

# 测试纯字符串 SVG
r3 = sanitize('<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50"><rect width="100" height="50" fill="blue"/></svg>')
print('TEST3 (str SVG):', r3[:60])
