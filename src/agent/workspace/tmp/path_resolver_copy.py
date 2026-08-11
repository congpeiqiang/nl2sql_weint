import json
import logging, base64, os, re
from functools import wraps
from pathlib import Path
from typing import Any
from agent.settings.setting import settings

_log = logging.getLogger(__name__)

# Must match agent.py workspace_dir exactly
WORKSPACE_DIR = Path(
    Path(__file__).parent.parent / "workspace"
).resolve()

_CHART_ERROR_MSG = "图表生成失败，请检查数据格式。"

# ECharts 交互式 HTML 模板。
# 图表通过 CDN 加载 echarts.min.js（jsdelivr / unpkg 双源自动 fallback），
# 生成的 HTML 只有几 KB，避免把 1.1MB echarts 内联进工具返回值导致 LLM 上下文爆炸。
# __ECHARTS_SRC__（CDN script 标签）与 __OPTION_JSON__ 由 _build_echarts_html 注入。
_ECHARTS_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<script src="https://cdn.jsdelivr.net/npm/echarts@6.0.0/dist/echarts.min.js"></script>
<script>
// CDN 兜底：主源加载失败时回退到备用源
if (typeof echarts === 'undefined') {
  document.write('<script src="https://unpkg.com/echarts@6.0.0/dist/echarts.min.js"><\\/script>');
}
</script>
</head>
<body style="margin:0;padding:0;background:#fff;font-family:-apple-system,'Segoe UI',Roboto,'Helvetica Neue',Arial,'PingFang SC','Microsoft YaHei',sans-serif;">
<div id="chart" style="width:100vw;height:100vh;"></div>
<script>
(function() {
  var option = __OPTION_JSON__;
  var chart = echarts.init(document.getElementById('chart'), null, {renderer: 'canvas'});
  chart.setOption(option);
  window.addEventListener('resize', function(){ chart.resize(); });
})();
</script>
</body>
</html>"""


def _build_echarts_html(option_json: str) -> str | None:
    """将 ECharts option JSON 包装为交互式 HTML（CDN 加载 echarts，轻量）。"""
    try:
        json.loads(option_json)  # 验证 JSON 合法性
    except Exception as e:
        _log.warning(f"[ECHARTS] option JSON 非法: {e}")
        return None
    return _ECHARTS_HTML_TEMPLATE.replace("__OPTION_JSON__", option_json)


def _is_echarts_option(text: str) -> bool:
    """判断文本是否为 ECharts option JSON（outputType='option' 返回）。"""
    text = (text or "").strip()
    if not text or not (text.startswith("{") or text.startswith("[")):
        return False
    try:
        obj = json.loads(text)
        return isinstance(obj, dict)
    except Exception:
        return False


def _parse_echarts_option(result: Any) -> str | None:
    """从 generate_echarts 返回值中提取 ECharts option JSON 字符串。

    outputType='option' 时 mcp-echarts 返回统一 MCP 响应：
        {content: [{type:"text", text:"{...JSON...}"}]}
    也兼容纯字符串 / list 直接返回的形态。
    """
    def _extract_text(node: Any) -> str | None:
        if isinstance(node, str):
            return node
        if isinstance(node, dict):
            # MCP content 块：{type:"text", text:"..."}
            if node.get("type") == "text" and isinstance(node.get("text"), str):
                return node["text"]
            for v in node.values():
                r = _extract_text(v)
                if r:
                    return r
        if isinstance(node, list):
            for item in node:
                r = _extract_text(item)
                if r:
                    return r
        return None

    if isinstance(result, tuple) and len(result) == 2:
        result = result[0]

    text = _extract_text(result)
    if text and _is_echarts_option(text):
        return text
    return None


def _save_echarts_html_to_workspace(html: str, option_json: str) -> str:
    """将交互式 HTML 图表保存到工作区 report 目录。

    返回保存后的文件名（如 ``chart_20260805_120000.html``）；失败返回空字符串。
    文件名优先从 option 的 title.text 提取，否则用时间戳。
    """
    import re as _re
    from datetime import datetime
    try:
        report_dir = WORKSPACE_DIR / "report"
        report_dir.mkdir(parents=True, exist_ok=True)

        # 尝试从 option 标题提取图表名，否则用时间戳
        base_name = "chart"
        try:
            obj = json.loads(option_json)
            title = obj.get("title", {})
            if isinstance(title, dict):
                t = str(title.get("text", "")).strip()
            elif isinstance(title, str):
                t = title.strip()
            else:
                t = ""
            if t:
                t = _re.sub(r'[\\/:*?"<>|]', "_", t)
                if t:
                    base_name = t
        except Exception:
            pass

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = f"{base_name}_{ts}.html"
        dest = report_dir / fname
        dest.write_text(html, encoding="utf-8")
        _log.info(f"[ECHARTS] 交互式 HTML 图表已保存到工作区: {dest}")
        return fname
    except Exception as e:
        _log.warning(f"[ECHARTS] 自动保存 HTML 图表到工作区失败: {e}")
        return ""


def _estimate_echarts_height(option_json: str) -> int:
    """根据 ECharts option 内容估算合理的图表高度（像素），实现高度自适应。

    - 饼图/环形图/漏斗/仪表盘/雷达/桑基等单容器图 → 固定高度（约 420）
    - 水平柱状图（长标签，yAxis 为 category）→ 按分类数量每项约 34px
    - 垂直柱状图/折线图 → 分类多时适当加高
    - 解析失败 → 回退 520
    """
    try:
        obj = json.loads(option_json)
    except Exception:
        return 520
    if not isinstance(obj, dict):
        return 520

    series = obj.get("series")
    if not isinstance(series, list) or not series:
        return 520
    first = series[0] if isinstance(series[0], dict) else {}
    stype = str(first.get("type", "")).lower()

    # 单容器/固定尺寸图表类型
    if stype in ("pie", "donut", "funnel", "gauge", "sunburst", "treemap",
                 "radar", "graph", "sankey", "heatmap", "wordcloud", "candlestick"):
        return 420

    # 水平柱状图：yAxis 为 category（长标签）
    yaxis = obj.get("yAxis")
    is_horizontal = False
    ycats = []
    if isinstance(yaxis, dict) and yaxis.get("type") == "category":
        is_horizontal = True
        ycats = yaxis.get("data") or []
    elif isinstance(yaxis, list):
        for a in yaxis:
            if isinstance(a, dict) and a.get("type") == "category":
                is_horizontal = True
                ycats = a.get("data") or []
                break

    if is_horizontal:
        n = len(ycats) if isinstance(ycats, list) else 0
        # 每个分类约 34px，加标题/图例留白，封顶 900
        return max(360, min(900, 120 + n * 34))

    # 垂直柱状/折线：统计分类数量
    xaxis = obj.get("xAxis")
    xcats = []
    if isinstance(xaxis, dict):
        xcats = xaxis.get("data") or []
    elif isinstance(xaxis, list):
        xcats = xaxis[0].get("data") or [] if xaxis and isinstance(xaxis[0], dict) else []
    n = len(xcats) if isinstance(xcats, list) else 0
    if n >= 30:
        return 560
    if n >= 12:
        return 500
    return 440


def _echarts_option_to_data_url(option_json: str) -> str | None:
    """将 ECharts option JSON 包装为内嵌 iframe（data:text/html;base64）。

    返回 iframe HTML 字符串；包装失败返回 None（调用方回退到原 result）。
    """
    import base64
    html = _build_echarts_html(option_json)
    if not html:
        return None
    # 自动落盘 .html 到工作区 report 目录（可交互、可分享、可被报告引用）
    _save_echarts_html_to_workspace(html, option_json)
    height = _estimate_echarts_height(option_json)
    b64 = base64.b64encode(html.encode("utf-8")).decode("ascii")
    return (
        f'<iframe src="data:text/html;base64,{b64}" '
        f'width="100%" height="{height}" style="border:none;border-radius:8px;background:#fff"></iframe>'
    )



def _resolve_args(args, kwargs):
    """Resolve virtual paths to absolute paths in tool arguments."""
    return args, kwargs

def _is_chart_tool(tool: Any) -> bool:
    """Check if a tool is a chart generation tool (Semiotic, ECharts or AntV)."""
    name = getattr(tool, "name", "")
    keywords = ("chart", "render", "suggestchart", "getschema", "diagnose", "repair", "generate-echarts", "echarts")
    return bool(name and any(kw in name.lower() for kw in keywords))
def _sanitize_chart_result(result: Any, is_chart: bool) -> Any:
    """根据 CHART_ENGINE 选择对应的图表结果处理逻辑。

    - Semiotic: 将 SVG 转为内嵌 iframe
    - ECharts: 将 SVG 字符串转为内嵌 iframe，或将 PNG 文件路径转为 <img> 标签
    """
    if not is_chart:
        return result

    engine = settings.CHART_ENGINE.lower()
    if engine == "echarts":
        return _sanitize_echarts_result(result)
    return _sanitize_semiotic_result(result, is_chart)


def _move_echarts_image_to_workspace(src_path: str) -> str:
    """将 echarts-mcp 生成的图片从默认目录（Downloads）复制到工作区 report 目录。

    返回工作区中的新路径；若复制失败则返回原路径。
    这样 echarts-mcp 源码无需修改（跨环境安全），图片统一收拢到工作区。
    """
    import shutil
    try:
        src = Path(src_path)
        if not src.exists():
            _log.warning(f"[ECHARTS] 源图片不存在，跳过移动: {src_path}")
            return src_path

        report_dir = WORKSPACE_DIR / "report"
        report_dir.mkdir(parents=True, exist_ok=True)

        # 保留原文件名（uuid.png），避免重名冲突
        dest = report_dir / src.name
        shutil.copy2(src, dest)
        _log.info(f"[ECHARTS] 图片已复制到工作区: {dest}")
        return str(dest)
    except Exception as e:
        _log.warning(f"[ECHARTS] 复制图片到工作区失败，使用原路径: {e}")
        return src_path


def _svg_to_data_url(svg: str) -> str:
    """将 SVG 字符串转为内嵌 iframe（与 Semiotic 引擎一致）。"""
    import re, base64
    svg = svg.strip()
    svg = re.sub(r"</svg>[\s\S]*$", "</svg>", svg)
    html = f'<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"></head><body style="margin:0;display:flex;justify-content:center;background:#fff">{svg}</body></html>'
    b64 = base64.b64encode(html.encode("utf-8")).decode("ascii")
    return f'<iframe src="data:text/html;base64,{b64}" width="100%" height="500" style="border:none;border-radius:8px"></iframe>'


def _save_svg_to_workspace(svg: str) -> str:
    """将 ECharts 生成的 SVG 字符串自动保存到工作区 report 目录。

    返回保存后的文件名（如 ``echarts_20260803_091223.svg``）；若保存失败返回空字符串。
    这样每次生成 SVG 图表都会自动落盘，报告可直接引用，无需手动用 write_file 保存。
    """
    import re as _re
    from datetime import datetime
    try:
        svg = svg.strip()
        if not svg:
            return ""
        # 提取 <svg>...</svg> 完整内容
        m = _re.search(r"<svg[\s\S]*?</svg>", svg)
        if not m:
            _log.warning("[ECHARTS] 未找到 <svg> 标签，跳过自动保存")
            return ""
        clean_svg = m.group(0)

        report_dir = WORKSPACE_DIR / "report"
        report_dir.mkdir(parents=True, exist_ok=True)

        # 尝试从 SVG 标题提取图表名，否则用时间戳
        title_m = _re.search(r"<text[^>]*>([^<]{1,40})</text>", clean_svg)
        base_name = "echarts"
        if title_m:
            # 清理标题中的非法文件名字符
            t = title_m.group(1).strip()
            t = _re.sub(r'[\\/:*?"<>|]', "_", t)
            if t:
                base_name = t

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = f"{base_name}_{ts}.svg"
        dest = report_dir / fname
        dest.write_text(clean_svg, encoding="utf-8")
        _log.info(f"[ECHARTS] SVG 已自动保存到工作区: {dest}")
        return fname
    except Exception as e:
        _log.warning(f"[ECHARTS] 自动保存 SVG 到工作区失败: {e}")
        return ""


def _sanitize_echarts_result(result: Any) -> Any:
    """将 ECharts 生成的图表结果转为可渲染内容。

    支持三种输出：
    - ECharts option JSON（outputType="option"）：包装为自包含交互式 HTML（内联 echarts.min.js，
      支持 tooltip / 缩放 / 图例切换），以 base64 iframe 渲染在会话，并自动落盘 .html 到工作区
    - SVG 字符串（outputType="svg"）：转为内嵌 iframe，与 Semiotic 引擎一致，可正常渲染
    - PNG 文件路径（outputType="png"）：转为 <img> 标签，并自动复制到工作区 report 目录
    """
    import logging
    _log = logging.getLogger(__name__)
    _log.warning(f"[ECHARTS] ENTER: type={type(result).__name__}, preview={str(result)[:200]}")

    # ECharts option JSON（outputType="option"）：优先处理，生成交互式 HTML。
    # 注意：mcp-echarts 工具声明了 response_format="content_and_artifact"，
    # _run 原生返回 (content, artifact) 二元组，此处必须保持二元组契约，
    # 否则 LangChain 校验 "two-tuple of message content and raw tool output" 会抛错。
    try:
        option_json = _parse_echarts_option(result)
        if option_json:
            r = _echarts_option_to_data_url(option_json)
            if r:
                _log.info(f"[ECHARTS] option JSON → 交互式 HTML iframe[:100]={r[:100]}")
                if isinstance(result, tuple) and len(result) == 2:
                    return (r, result[1])
                return r
            _log.warning("[ECHARTS] option 包装失败，回退到默认处理")
    except Exception as e:
        _log.warning(f"[ECHARTS] option 解析异常: {e}")

    # 处理 MCP 标准响应格式：{content: [{type:"text", text:"<svg>..."}]} 或 {content:[{type:"image",...}]}
    if isinstance(result, dict) and isinstance(result.get("content"), list):
        for item in result["content"]:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and "<svg" in str(item.get("text", "")):
                _save_svg_to_workspace(item["text"])
                r = _svg_to_data_url(item["text"])
                _log.info(f"[ECHARTS] MCP content text SVG → iframe[:100]={r[:100]}")
                return r
            if item.get("type") == "image":
                data = item.get("data", "")
                mime = item.get("mimeType", "image/png")
                if data:
                    return f'<img src="data:{mime};base64,{data}" style="max-width:100%;border-radius:8px"/>'
        # 若 content 中无 SVG/图片，尝试拼接所有 text 内容
        texts = [str(i.get("text", "")) for i in result["content"] if isinstance(i, dict) and i.get("type") == "text"]
        joined = "".join(texts)
        if "<svg" in joined:
            _save_svg_to_workspace(joined)
            return _svg_to_data_url(joined)

    # 处理 echarts-mcp 直接返回的 list 格式：[{type:"text", text:"<svg>..."}] 或 [{type:"image", data:...}]
    if isinstance(result, list):
        for item in result:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and "<svg" in str(item.get("text", "")):
                _save_svg_to_workspace(item["text"])
                r = _svg_to_data_url(item["text"])
                _log.info(f"[ECHARTS] MCP list text SVG → iframe[:100]={r[:100]}")
                return r
            if item.get("type") == "image":
                data = item.get("data", "")
                mime = item.get("mimeType", "image/png")
                if data:
                    return f'<img src="data:{mime};base64,{data}" style="max-width:100%;border-radius:8px"/>'
        # 若 list 中无 SVG/图片，尝试拼接所有 text 内容
        texts = [str(i.get("text", "")) for i in result if isinstance(i, dict) and i.get("type") == "text"]
        joined = "".join(texts)
        if "<svg" in joined:
            _save_svg_to_workspace(joined)
            return _svg_to_data_url(joined)

    # ECharts MCP 返回的是文件路径字符串（如 C:\\Users\\xxx\\Downloads\\uuid.png）
    if isinstance(result, str):
        _log.info(f"[ECHARTS] str result: {result[:200]}")
        # 检查是否为 SVG 字符串（outputType="svg" 时返回）
        if "<svg" in result:
            _save_svg_to_workspace(result)
            r = _svg_to_data_url(result)
            _log.info(f"[ECHARTS] str SVG → iframe[:100]={r[:100]}")
            return r
        # 检查是否为文件路径（包含 .png 等图片扩展名）
        if result.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp')):
            # 自动复制到工作区 report 目录
            moved = _move_echarts_image_to_workspace(result)
            return f'<img src="file:///{moved.replace(chr(92), "/")}" style="max-width:100%;border-radius:8px"/>'
        # 检查是否为 data URL 或 http URL
        if result.startswith('data:image'):
            return f'<img src="{result}" style="max-width:100%;border-radius:8px"/>'
        if result.startswith('http://') or result.startswith('https://'):
            return f'<img src="{result}" style="max-width:100%;border-radius:8px"/>'
        _log.warning(f"[ECHARTS] str result not SVG/image path/URL: {result[:200]}")

    # tuple 形式 (content, artifact)
    if isinstance(result, tuple) and len(result) == 2:
        cnt, artifact = result
        # cnt 可能是 str（SVG/路径/URL）或 list（MCP content 列表）
        if isinstance(cnt, str):
            if "<svg" in cnt:
                _save_svg_to_workspace(cnt)
                r = _svg_to_data_url(cnt)
                _log.info(f"[ECHARTS] tuple SVG → iframe[:100]={r[:100]}")
                return (r, artifact)
            if cnt.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp')):
                moved = _move_echarts_image_to_workspace(cnt)
                return (f'<img src="file:///{moved.replace(chr(92), "/")}" style="max-width:100%;border-radius:8px"/>', artifact)
            if cnt.startswith('data:image'):
                return (f'<img src="{cnt}" style="max-width:100%;border-radius:8px"/>', artifact)
            if cnt.startswith('http://') or cnt.startswith('https://'):
                return (f'<img src="{cnt}" style="max-width:100%;border-radius:8px"/>', artifact)
        # cnt 是 list（MCP content 列表）：递归处理
        if isinstance(cnt, list):
            _sanitized = _sanitize_echarts_result(cnt)
            if _sanitized is not cnt:
                return (_sanitized, artifact)

    return result


def _sanitize_semiotic_result(result: Any, is_chart: bool) -> Any:
    """将图表 SVG 转为内嵌 iframe。"""
    import logging
    _log = logging.getLogger(__name__)
    _log.warning(f"[CHART] ENTER: is_chart={is_chart}, type={type(result).__name__}, preview={str(result)[:200]}")
    if isinstance(result, str):
        _log.info(f"[CHART] str result[:100]={result[:100]}")
    if not is_chart:
        return result

    import re, time
    from pathlib import Path

    def _svg_to_data_url(svg: str) -> str:
        import base64
        svg = svg.strip()
        svg = re.sub(r"</svg>[\s\S]*$", "</svg>", svg)
        html = f'<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"></head><body style="margin:0;display:flex;justify-content:center;background:#fff">{svg}</body></html>'
        b64 = base64.b64encode(html.encode("utf-8")).decode("ascii")
        return f'<iframe src="data:text/html;base64,{b64}" width="100%" height="500" style="border:none;border-radius:8px"></iframe>'

    if isinstance(result, tuple) and len(result) == 2:
        cnt, artifact = result
        _log.info(f"[CHART] tuple: content_type={type(cnt).__name__}, len={len(str(cnt))}, preview={str(cnt)[:200]}")
        if isinstance(cnt, list) and len(cnt) > 0:
            _log.info(f"[CHART] list[0] type={type(cnt[0]).__name__}, keys={list(cnt[0].keys()) if isinstance(cnt[0], dict) else 'N/A'}")
        if isinstance(cnt, str) and "<svg" in cnt:
            r = _svg_to_data_url(cnt)
            _log.info(f"[CHART] transformed to iframe[:100]={r[:100]}")
            return (r, artifact)
        # Try extracting SVG from list/dict content
        import re as _re
        def _extract_svg(data):
            if isinstance(data, str) and '<svg' in data:
                m = _re.search(r'<svg[\s\S]*?</svg>', data)
                return m.group(0) if m else None
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        for v in item.values():
                            r = _extract_svg(v)
                            if r:
                                return r
            if isinstance(data, dict):
                for v in data.values():
                    r = _extract_svg(v)
                    if r:
                        return r
            return None
        svg_str = _extract_svg(cnt)
        if svg_str:
            r = _svg_to_data_url(svg_str)
            _log.info(f"[CHART] extracted SVG from nested {type(cnt).__name__} → iframe[:100]={r[:100]}")
            return (r, artifact)
        # Fallback: try wrapping as image
        _log.warning(f"[CHART] no SVG found in {type(cnt).__name__}: {str(cnt)[:300]}")
        # Check if it's a base64 image or URL
        if isinstance(cnt, str):
            if cnt.startswith('data:image'):
                return (f'<img src="{cnt}" style="max-width:100%;border-radius:8px"/>', artifact)
            if cnt.startswith('http://') or cnt.startswith('https://'):
                return (f'<img src="{cnt}" style="max-width:100%;border-radius:8px"/>', artifact)
        return result

    if isinstance(result, str):
        _log.info(f"[CHART] str result: len={len(result)}, preview={result[:200]}")
        if "<svg" in result:
            r = _svg_to_data_url(result)
            _log.info(f"[CHART] str SVG → iframe[:100]={r[:100]}")
            return r
        if result.startswith('data:image'):
            return f'<img src="{result}" style="max-width:100%;border-radius:8px"/>'
        if result.startswith('http'):
            return f'<img src="{result}" style="max-width:100%;border-radius:8px"/>'
        _log.warning(f"[CHART] str result not SVG/image: {result[:200]}")

    return result


def _inject_db_name(tool_name: str, args: tuple, kwargs: dict) -> tuple[tuple, dict]:
    """将前端选择的 db_name 从 LangGraph configurable 注入到 run_sql 工具调用中。"""
    if tool_name != "run_sql":
        return args, kwargs

    # 检查参数中是否已有 db_name
    if args and len(args) == 1 and isinstance(args[0], dict):
        if args[0].get("db_name"):
            return args, kwargs
    if kwargs.get("db_name"):
        return args, kwargs

    # 从 LangGraph config 中读取 db_name
    try:
        from langgraph.config import get_config
        config = get_config()
        db_name = config.get("configurable", {}).get("db_name", "")
        if db_name:
            if args and len(args) == 1 and isinstance(args[0], dict):
                args = ({**args[0], "db_name": db_name},)
            else:
                kwargs = {**kwargs, "db_name": db_name}
    except Exception:
        pass

    return args, kwargs


def _pack_chart_props(kwargs: dict) -> dict:
    """根据 CHART_ENGINE 选择对应的参数处理逻辑。

    - Semiotic: 将 chart 属性（data, categoryAccessor, valueAccessor …）打包进 ``props`` 字段
    - ECharts: generate_echarts 工具参数（width/height/echartsOption/outputType）直接透传，
      但需做参数名归一化（echarts → echartsOption）并默认强制 outputType="option"
    """
    engine = settings.CHART_ENGINE.lower()
    if engine == "echarts":
        kwargs = dict(kwargs)
        # 参数名归一化：LLM 可能传 echarts（旧名），echarts-mcp 实际需要 echartsOption
        if "echarts" in kwargs and "echartsOption" not in kwargs:
            kwargs["echartsOption"] = kwargs.pop("echarts")
        # 一律强制 option 输出：返回 ECharts 配置 JSON，系统包装为交互式 HTML 图表。
        # 若尊重 LLM 显式传的 svg/png，可能导致同一图表生成两次（一次交互 HTML、
        # 一次静态 SVG），用户只保留可交互的。因此不区分 outputType，全部走 option。
        kwargs["outputType"] = "option"
        return kwargs

    return _pack_semiotic_props(kwargs)


def _pack_semiotic_props(kwargs: dict) -> dict:
    """semiotic-mcp 的 renderChart / diagnoseConfig 等工具要求 chart 属性
    （data, categoryAccessor, valueAccessor …）全部嵌套在 ``props`` 字段内：

        { component: "BarChart", props: { data: [...], ... }, format: "svg" }

    但 LLM 经常把所有字段平铺到顶层。本函数把非元数据字段打包进 ``props``。
    同时自动修正 xy / ordinal 图表的 accessor 命名差异：
        xy 图表（LineChart 等）使用 xAccessor / yAccessor
        ordinal 图表（BarChart 等）使用 categoryAccessor / valueAccessor
    """
    # semiotic-mcp 元数据字段 + LangChain 框架内部字段，均不应被打包进 props
    TOP_LEVEL_KEYS = {
        "component", "props", "theme", "format", "usageMode", "viewportWidth",
        # LangChain BaseTool / StructuredTool 框架参数
        "config", "callbacks", "tags", "metadata", "run_name",
    }
    # xy 类图表使用 xAccessor/yAccessor；ordinal 类使用 categoryAccessor/valueAccessor
    _XY_COMPONENTS = {
        "LineChart", "AreaChart", "StackedAreaChart", "Scatterplot", "BubbleChart",
        "Heatmap", "ConnectedScatterplot", "QuadrantChart", "MultiAxisLineChart",
        "CandlestickChart", "DifferenceChart", "BumpChart",
    }
    _ORDINAL_COMPONENTS = {
        "BarChart", "StackedBarChart", "GroupedBarChart", "SwarmPlot", "BoxPlot",
        "DotPlot", "Histogram", "ViolinPlot", "RidgelinePlot", "PieChart",
        "DonutChart", "FunnelChart", "LikertChart", "SwimlaneChart", "Treemap",
        "ParallelCoordinatesChart", "SummaryChart",
    }
    if "component" not in kwargs:
        return kwargs
    if "props" in kwargs and isinstance(kwargs["props"], dict) and not any(
        k not in TOP_LEVEL_KEYS for k in kwargs
    ):
        # props 已存在且无多余顶层字段，仍需检查 accessor 命名
        pass
    else:
        props = kwargs.get("props") if isinstance(kwargs.get("props"), dict) else {}
        extra = {k: v for k, v in kwargs.items() if k not in TOP_LEVEL_KEYS}
        if not extra:
            return kwargs
        new_props = {**extra, **props}  # props 内已有字段优先（防止被覆盖）
        kwargs = {k: v for k, v in kwargs.items() if k in TOP_LEVEL_KEYS} | {"props": new_props}

    # 修正 accessor 命名
    component = kwargs.get("component", "")
    props = kwargs.get("props")
    if isinstance(props, dict):
        if component in _XY_COMPONENTS:
            if "categoryAccessor" in props and "xAccessor" not in props:
                props["xAccessor"] = props.pop("categoryAccessor")
            if "valueAccessor" in props and "yAccessor" not in props:
                props["yAccessor"] = props.pop("valueAccessor")
            # LineChart 的 xScaleType 只接受 linear/log/time，不支持 ordinal。
            # 当 x 值是字符串时：把分类值映射为数值索引（0,1,2…），保留原始标签
            # 存到 _x_labels 供坐标轴显示。
            _x_acc = props.get("xAccessor")
            _data = props.get("data")
            if (
                component == "LineChart"
                and _x_acc
                and isinstance(_data, list)
                and _data
                and isinstance(_data[0], dict)
                and isinstance(_data[0].get(_x_acc), str)
            ):
                # 按出现顺序建立 分类值 → 索引 映射
                _seen = {}
                _labels = []
                for row in _data:
                    v = row.get(_x_acc)
                    if v not in _seen:
                        _seen[v] = len(_labels)
                        _labels.append(v)
                # 把原始标签存到 _label 字段，供 tooltip 显示
                for row in _data:
                    row["_label"] = row[_x_acc]
                    row[_x_acc] = _seen[row[_x_acc]]
                # 确保数据点可见，方便 hover 显示 tooltip
                props.setdefault("showPoints", True)
                _log.warning(
                    f"[_pack_chart_props] LineChart categorical x mapped to indices: "
                    f"labels={_labels}")
        elif component in _ORDINAL_COMPONENTS:
            if "xAccessor" in props and "categoryAccessor" not in props:
                props["categoryAccessor"] = props.pop("xAccessor")
            if "yAccessor" in props and "valueAccessor" not in props:
                props["valueAccessor"] = props.pop("yAccessor")
    return kwargs


def wrap_tool(tool: Any) -> Any:
    """Wrap a langchain BaseTool to auto-resolve virtual paths in arguments.

    The wrapper intercepts ``_run`` and ``_arun`` (or ``invoke`` / ``ainvoke``)
    calls and converts any virtual paths to real filesystem paths before the
    original tool logic runs.

    For chart tools, ToolException (raised by langchain_mcp_adapters when the
    MCP server returns isError:true) is caught and converted to a friendly
    message so the NL2SQL pipeline does not crash.
    """
    is_chart = _is_chart_tool(tool)

    # Try to wrap _run / _arun first (works for most BaseTool subclasses)
    original_run = getattr(tool, "_run", None)
    original_arun = getattr(tool, "_arun", None)

    if original_run is not None:
        @wraps(original_run)
        def wrapped_run(*args: Any, **kwargs: Any) -> Any:
            kwargs.pop("runtime", None)
            kwargs.pop("run_manager", None)
            new_args, new_kwargs = _resolve_args(args, kwargs)
            new_args, new_kwargs = _inject_db_name(tool.name, new_args, new_kwargs)
            new_kwargs = _pack_chart_props(new_kwargs)
            try:
                return _sanitize_chart_result(original_run(*new_args, **new_kwargs), is_chart)
            except Exception as e:
                _log.warning(f"[CHART] TOOL ERROR: {type(e).__name__}: {e}", exc_info=True)
                if is_chart and ('ToolException' in type(e).__name__ or 'McpError' in type(e).__name__):
                    return (_CHART_ERROR_MSG, None)
                raise

        tool._run = wrapped_run  # type: ignore[method-assign]



    if original_arun is not None:
        @wraps(original_arun)
        async def wrapped_arun(*args: Any, **kwargs: Any) -> Any:
            import logging
            _log = logging.getLogger(__name__)
            kwargs.pop("runtime", None)
            kwargs.pop("run_manager", None)
            _log.info(f"[_arun] {tool.name} called with args={args}")
            new_args, new_kwargs = _resolve_args(args, kwargs)
            new_args, new_kwargs = _inject_db_name(tool.name, new_args, new_kwargs)
            new_kwargs = _pack_chart_props(new_kwargs)
            _log.warning(
                f"[_arun] {tool.name} FINAL: component={new_kwargs.get('component')}, "
                f"props_keys={list(new_kwargs.get('props', {}).keys()) if isinstance(new_kwargs.get('props'), dict) else 'N/A'}, "
                f"top_keys={list(new_kwargs.keys())}")
            if is_chart and isinstance(new_kwargs.get("props"), dict):
                _p = new_kwargs["props"]
                _data = _p.get("data", [])
                _first_keys = list(_data[0].keys()) if _data and isinstance(_data[0], dict) else "N/A"
            try:
                return _sanitize_chart_result(await original_arun(*new_args, **new_kwargs), is_chart)
            except Exception as e:
                _log.warning(f"[CHART] TOOL ERROR: {type(e).__name__}: {e}", exc_info=True)
                if is_chart and ('ToolException' in type(e).__name__ or 'McpError' in type(e).__name__):
                    return (_CHART_ERROR_MSG, None)
                raise

        tool._arun = wrapped_arun  # type: ignore[method-assign]

    # Fallback: also wrap invoke / ainvoke at the BaseTool level
    original_invoke = getattr(tool, "invoke", None)
    if original_invoke is not None and original_invoke is not tool.invoke:
        # Already wrapped above via _run, skip double wrapping
        pass

    return tool
