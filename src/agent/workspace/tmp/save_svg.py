import base64, re

with open('/workspace/tmp/chart_b64.txt', 'r', encoding='utf-8') as f:
    b64 = f.read().strip()

html = base64.b64decode(b64).decode('utf-8')
match = re.search(r'<svg[^>]*>.*?</svg>', html, re.DOTALL)
if match:
    svg = match.group()
    with open('/workspace/report/Movies_over_180min_chart.svg', 'w', encoding='utf-8') as f:
        f.write(svg)
    print('SVG saved successfully, length:', len(svg))
else:
    print('SVG not found')
