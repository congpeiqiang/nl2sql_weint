# -*- coding: utf-8 -*-
# Generate horizontal bar chart SVG for movies over 180 min
data = [
    ("Logistics", 51420),
    ("Ambiance", 43200),
    ("Carnets Filmés (Liste Complète)", 28643),
    ("Modern Times Forever", 14400),
    ("Beijing 2003", 9000),
    ("Soldier", 7200),
    ("Matrjoschka", 5700),
    ("A 2nd generation film", 3077),
    ("World Peace & Prayer Day", 2400),
    ("h36:", 2160),
    ("Five-Year Diary", 2160),
    ("Rock Milestones: Metallica - The Halcyon Years", 2007),
    ("Madonna: Live", 2005),
    ("The Freedom of Uselessness", 2000),
    ("11-22-63: A Novel", 1840),
    ("Hollywood East", 1800),
    ("How Does David Lynch Do It?", 1800),
    ("Azgrab: The Documentary", 1669),
    ("24", 1464),
    ("Grandmother Martha", 1452),
]

# Chart geometry
margin_left = 300
margin_top = 50
margin_bottom = 50
margin_right = 30
plot_w = 670  # 1000 - 300 - 30
plot_h = 600  # 700 - 50 - 50
bar_h = 26.86567164179105
gap = 3.134328358208955
max_val = 51420.0

def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

bars = []
labels = []
n = len(data)
for i, (title, val) in enumerate(data):
    y = margin_top + i * (bar_h + gap)
    w = (val / max_val) * plot_w
    bars.append(f'<rect x="0" y="{y:.2f}" width="{w:.2f}" height="{bar_h:.2f}" fill="#1f77b4"></rect>')
    labels.append(
        f'<g transform="translate(0,{y + bar_h/2:.2f})">'
        f'<line x2="-5" stroke="#ccc" stroke-width="1"></line>'
        f'<text x="-8" text-anchor="end" dominant-baseline="middle" font-size="12" fill="#666" font-family="sans-serif">{esc(title)}</text></g>'
    )

# X axis ticks
ticks = []
for v in [0, 10000, 20000, 30000, 40000, 50000]:
    x = (v / max_val) * plot_w
    ticks.append(
        f'<g transform="translate({x:.2f},600)">'
        f'<line y2="5" stroke="#ccc" stroke-width="1"></line>'
        f'<text y="18" text-anchor="middle" font-size="12" fill="#666" font-family="sans-serif">{v}</text></g>'
    )

svg = f'''<svg xmlns="http://www.w3.org/2000/svg" class="stream-ordinal-frame" width="1000" height="700" role="img" aria-labelledby="semiotic-title" style="font-family:sans-serif">
<title id="semiotic-title">时长大于180分钟的电影（按片长降序）</title>
<g id="data-area" transform="translate({margin_left},{margin_top})">
{''.join(bars)}
<g id="axes" class="ordinal-axes">
<line x1="0" y1="600" x2="670" y2="600" stroke="#ccc" stroke-width="1"></line>
{''.join(ticks)}
<text x="335" y="640" text-anchor="middle" font-size="12" fill="#333" font-family="sans-serif">片长（分钟）</text>
<line x1="0" y1="0" x2="0" y2="600" stroke="#ccc" stroke-width="1"></line>
{''.join(labels)}
<text x="-285" y="300" text-anchor="middle" font-size="12" fill="#333" font-family="sans-serif" transform="rotate(-90, -285, 300)">电影标题</text>
</g>
</g>
<text id="chart-title" x="500" y="22" text-anchor="middle" font-size="16" font-weight="bold" fill="#333" font-family="sans-serif">时长大于180分钟的电影（按片长降序）</text>
</svg>'''

with open("report/Movies_over_180min_chart.svg", "w", encoding="utf-8") as f:
    f.write(svg)
print("SVG saved OK, length:", len(svg))
