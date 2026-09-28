import asyncio
import concurrent.futures
import json
import logging, base64, os, re, threading, time
from functools import wraps
from pathlib import Path
from typing import Any
from agent.utils.semantic_db import get_detector
from agent.utils.query_tools import is_data_tool

# 模块顶部导入 langgraph.config（而非每次工具调用动态 import）：
# 动态导入会触发整个 langgraph 包加载（实测首次 ~2.4s），拖慢每次工具调用。
try:
    from langgraph.config import get_config as _lg_get_config
except Exception:  # 无 langgraph 环境时容错（本项目必然有，此分支不触发）
    _lg_get_config = None

_log = logging.getLogger(__name__)

# 由 WorkspaceManager 动态解析（支持多工作区切换）
def _get_workspace_dir() -> Path:
    from agent.workspace_manager import get_workspace_manager
    return get_workspace_manager().active_workspace


def _reserve_report_file(report_dir: Path, base_name: str, ext: str) -> tuple[str, Path]:
    """在 report 目录里**原子占位**一个唯一文件名，返回 (文件名, 路径)。

    P1-3：`report/` 是**全站共享目录**，而图表基名来自图表标题（"月度销售趋势"），
    天然在不同用户/会话间重复；原先 `{基名}_{秒级时间戳}` 在同秒必然同名 →
    后落盘的人静默覆盖前一个人的图（报告里引用的还是旧文件名，于是别人的那份被替换）。
    这里加 4 位随机后缀，并用 `O_EXCL` 独占创建消除 check-then-act 竞态。

    先占位再写入：本函数只在内容已解析成功后调用（调用方随后立即 write_text），
    失败时残留 0 字节文件，比覆盖他人文件轻得多。
    """
    import uuid
    from datetime import datetime

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    for _ in range(50):
        fname = f"{base_name}_{ts}_{uuid.uuid4().hex[:4]}{ext}"
        dest = report_dir / fname
        try:
            fd = os.open(str(dest), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue
        os.close(fd)
        return fname, dest
    raise RuntimeError(f"无法在 {report_dir} 生成唯一文件名: {base_name}{ext}")


def _record_chart_owner(fname: str) -> None:
    """把刚落盘的图表文件登记到当前用户（P1-3）。

    身份取自 `configurable`——外部请求的 user_id 已被 LangfuseMetadataMiddleware
    钳制为登录身份（客户端伪造的会被覆盖），所以这里读到的值可信；内部调用/dev
    旁路没有 user_id，直接跳过（文件保持"无记录"→ 走 `can_read_report` 的放行口径）。

    best-effort：失败只记日志，绝不影响图表返回给模型。
    """
    if not fname or _lg_get_config is None:
        return
    try:
        cfg = _lg_get_config().get("configurable", {}) or {}
        uid = str(cfg.get("user_id") or "")
        if not uid:
            return
        from agent.auth.grants import record_report_owner

        record_report_owner(fname, uid, str(cfg.get("thread_id") or ""))
    except Exception:  # noqa: BLE001 账本问题不该影响出图
        _log.debug("[ECHARTS] 图表归属登记失败: %s", fname, exc_info=True)


_CHART_ERROR_MSG = "图表生成失败，请检查数据格式。"

# ECharts 交互式 HTML 模板。
# 图表通过 CDN 加载 echarts.min.js（jsdelivr / unpkg 双源自动 fallback），
# 生成的 HTML 只有几 KB，避免把 1.1MB echarts 内联进工具返回值导致 LLM 上下文爆炸。
# __OPTION_JSON__ 由 _build_echarts_html 注入。
#
# 2026-09-23 修复「聊天里图表全白、单独下载该 HTML 却正常显示」：
#   原模板是 `#chart{width:100vw;height:100vh}` + `<script>` 里**同步** echarts.init。
#   iframe 被内嵌时（聊天正文用 dangerouslySetInnerHTML 注入，且消息根节点带
#   content-visibility:auto，离屏时子树不参与布局），文档解析那一刻 iframe 视口还是
#   0×0 → 容器 0×0 → echarts.init 量不到尺寸，画不出任何东西；而视口之后变成真实
#   尺寸时 iframe 的 window **不会收到 resize 事件**（headless Chrome 实测：parse 时
#   iw/ih/w/h 全 0，t+1000 变 1227×400，resizes=[]），于是空白永久保留 —— 元素在、
#   尺寸在、内容全白。顶层打开（下载 .html、报告预览弹窗）视口从解析起就有效，
#   所以一直正常。修法两条：① 容器改用 100% 撑满（配 html/body height:100%），
#   不再依赖视口单位；② **拿到非零尺寸之前不初始化**，之后再用 resize/ResizeObserver
#   兜住后续尺寸变化。
_ECHARTS_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>html,body{width:100%;height:100%;overflow:hidden;}</style>
<script src="https://cdn.jsdelivr.net/npm/echarts@6.0.0/dist/echarts.min.js"></script>
<script>
// CDN 兜底：主源加载失败时回退到备用源。
// **不能用 document.write**：主源如果是在文档解析完之后才判定失败，document.write
// 会触发隐式 document.open() 把整个文档清空 → iframe 变全白（headless 实测过）。
// 动态 appendChild 是异步的，所以下面的 boot() 必须容忍 echarts 暂时未定义。
if (typeof echarts === 'undefined') {
  var _fb = document.createElement('script');
  _fb.src = 'https://unpkg.com/echarts@6.0.0/dist/echarts.min.js';
  document.head.appendChild(_fb);
}
</script>
</head>
<body style="margin:0;padding:0;background:#fff;font-family:-apple-system,'Segoe UI',Roboto,'Helvetica Neue',Arial,'PingFang SC','Microsoft YaHei',sans-serif;">
<div id="chart" style="width:100%;height:100%;"></div>
<script>
(function() {
  var option = __OPTION_JSON__;
  var el = document.getElementById('chart');
  var timer = null;
  function boot() {
    // 两件事都可能"还没到"，只要没齐就下一拍再试（轮询 150ms，成功即停表）：
    //   ① echarts 尚未加载（CDN 主源慢、正在走兜底源，appendChild 是异步的）；
    //   ② 容器尺寸仍为 0 —— 内嵌时本文档可能先于 iframe 布局完成就被解析
    //      （父级 content-visibility:auto 跳过的子树尤其如此），此刻 init 会
    //      画出永久空白（旧模板正是如此）。
    if (typeof echarts === 'undefined') return;
    if (el.clientWidth <= 0 || el.clientHeight <= 0) return;
    clearInterval(timer);
    var chart = echarts.init(el, null, {renderer: 'canvas'});
    chart.setOption(option);
    // 只用 window resize。**不要加 ResizeObserver**：chart.resize() 会把 canvas 拉到
    // 容器宽，容器随即因为出现滚动条窄了 15px（滚动条宽度），RO 再次触发 → 无限
    // resize 抖动（实测 resizeCalls 在 1212/1227 之间反复）。html/body 已 overflow:hidden
    // 兜住这一层。
    window.addEventListener('resize', function(){ chart.resize(); });
  }
  timer = setInterval(boot, 150);
  boot();
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

    返回保存后的文件名（如 ``chart_20260805_120000_a3f1.html``）；失败返回空字符串。
    文件名 = 基名（优先 option 的 title.text）+ 时间戳 + 随机后缀（见
    `_reserve_report_file`：共享目录下同名必须各存各的，不能互相覆盖）。
    """
    import re as _re
    try:
        report_dir = _get_workspace_dir() / "report"
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

        fname, dest = _reserve_report_file(report_dir, base_name, ".html")
        dest.write_text(html, encoding="utf-8")
        _record_chart_owner(fname)
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

    2026-09-14 修复：**把落盘文件名一并回传**。此前只丢弃返回值（落盘是"隐形"
    副作用），模型手里只有 base64 iframe，于是按它知道的命名规则
    `{图表标题}_{YYYYMMDD_HHMMSS}.html` 自己拼路径 —— 但它只有 build_report 的
    `{ts}`，拼出来的名字差 17 秒（实测 `…_20260914_165606.html` 被写成
    `…_20260914_165623.html`）→ 前端"图表文件"链接全 404（只有报告 md 能打开）。
    走 VFS 路径，VfsPathResolverMiddleware 会改写为真实磁盘路径（与报告一致）。
    2026-09-23（P1-3）：名字里又多了 4 位随机后缀，模型更不可能猜对 —— 所以下面
    那句"照抄此路径"是硬要求，不是建议。
    """
    import base64
    html = _build_echarts_html(option_json)
    if not html:
        return None
    # 自动落盘 .html 到工作区 report 目录（可交互、可分享、可被报告引用）
    saved = _save_echarts_html_to_workspace(html, option_json)
    height = _estimate_echarts_height(option_json)
    b64 = base64.b64encode(html.encode("utf-8")).decode("ascii")
    iframe = (
        f'<iframe src="data:text/html;base64,{b64}" '
        f'width="100%" height="{height}" style="border:none;border-radius:8px;background:#fff"></iframe>'
    )
    if saved:
        iframe += (
            f"\n\n📎 图表 HTML 已保存：`/workspace/report/{saved}`"
            "（可预览/下载；引用图表文件时请**照抄此路径**，不要自行推测文件名）"
        )
    return iframe



def _resolve_args(args, kwargs):
    """Resolve virtual paths to absolute paths in tool arguments."""
    return args, kwargs

def _is_chart_tool(tool: Any) -> bool:
    """Check if a tool is a chart generation tool (ECharts or AntV)."""
    name = getattr(tool, "name", "")
    keywords = ("chart", "render", "suggestchart", "getschema", "diagnose", "repair", "generate-echarts", "echarts")
    return bool(name and any(kw in name.lower() for kw in keywords))
def _sanitize_chart_result(result: Any, is_chart: bool) -> Any:
    """将 ECharts 生成的图表结果转为可渲染内容。"""
    if not is_chart:
        return result

    return _sanitize_echarts_result(result)


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

        report_dir = _get_workspace_dir() / "report"
        report_dir.mkdir(parents=True, exist_ok=True)

        # 保留原文件名（uuid.png），避免重名冲突
        dest = report_dir / src.name
        shutil.copy2(src, dest)
        # P1-3：登记归属（uuid 名不参与覆盖问题，但「谁能读这份图」同样要记账）
        _record_chart_owner(dest.name)
        _log.info(f"[ECHARTS] 图片已复制到工作区: {dest}")
        return str(dest)
    except Exception as e:
        _log.warning(f"[ECHARTS] 复制图片到工作区失败，使用原路径: {e}")
        return src_path


def _svg_to_data_url(svg: str) -> str:
    """将 SVG 字符串转为内嵌 iframe。"""
    import re, base64
    svg = svg.strip()
    svg = re.sub(r"</svg>[\s\S]*$", "</svg>", svg)
    html = f'<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"></head><body style="margin:0;display:flex;justify-content:center;background:#fff">{svg}</body></html>'
    b64 = base64.b64encode(html.encode("utf-8")).decode("ascii")
    return f'<iframe src="data:text/html;base64,{b64}" width="100%" height="500" style="border:none;border-radius:8px"></iframe>'


def _save_svg_to_workspace(svg: str) -> str:
    """将 ECharts 生成的 SVG 字符串自动保存到工作区 report 目录。

    返回保存后的文件名（如 ``echarts_20260803_091223_a3f1.svg``）；失败返回空字符串。
    这样每次生成 SVG 图表都会自动落盘，报告可直接引用，无需手动用 write_file 保存。
    命名规则同 HTML 路径（时间戳 + 随机后缀，共享目录防覆盖）。
    """
    import re as _re
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

        report_dir = _get_workspace_dir() / "report"
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

        fname, dest = _reserve_report_file(report_dir, base_name, ".svg")
        dest.write_text(clean_svg, encoding="utf-8")
        _record_chart_owner(fname)
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
    - SVG 字符串（outputType="svg"）：转为内嵌 iframe，可正常渲染
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


def _inject_db_name(tool_name: str, args: tuple, kwargs: dict) -> tuple[tuple, dict]:
    """将前端选择的 db_name 从 LangGraph configurable 强制注入到 dbmcp 直连工具调用中。

    始终用 configurable.db_name 覆盖 LLM 传入的值——LLM 可能从对话历史中
    取到旧的 db_name（如切库后仍传上一次选的库名），必须以后端权威值为准。
    """
    # 仅 dbmcp 直连工具需要 db_name（按 db_name 路由到对应库的 runner）。
    # wrenai 语义层工具已绑定专属 server（工具名带库名前缀 wrenai_<库名>_），
    # 不注入 db_name——避免多余参数/语义歧义；图表等工具同样跳过。
    if not tool_name.startswith("dbmcp_"):
        return args, kwargs
    # 覆盖 run_sql（执行查询）与 get_db_info（表清单，"有多少表"走它）——
    # 若 LLM 省略 db_name，需从 configurable 注入，否则会落到默认库（imdb）。
    if "run_sql" not in tool_name and "get_db_info" not in tool_name:
        return args, kwargs

    # 从 LangGraph config 中读取 db_name（get_config 已在模块顶部导入）
    # 始终覆盖——LLM 传的 db_name 可能来自对话历史中的旧值，不可信。
    try:
        if _lg_get_config is not None:
            config = _lg_get_config()
            db_name = config.get("configurable", {}).get("db_name", "")
            if db_name:
                # 读取 LLM 原始传入的值用于日志比对
                old_name = ""
                if args and len(args) == 1 and isinstance(args[0], dict):
                    old_name = args[0].get("db_name", "")
                    args = ({**args[0], "db_name": db_name},)
                else:
                    old_name = kwargs.get("db_name", "")
                    kwargs = {**kwargs, "db_name": db_name}
                if old_name and old_name != db_name:
                    _log.warning(
                        "[DB_ROUTE] 强制覆盖 db_name: LLM 传 '%s' → configurable '%s' (tool=%s)",
                        old_name, db_name, tool_name,
                    )
    except Exception:
        pass

    # ── 语义层路由辅助（D2 硬需求）：记录通道选择，供排障 ──
    # 规则：已建模库应走 wrenai_<库名>_run_sql；未建模库应走 dbmcp_run_sql 直连。
    # wrenai 工具因 server 绑定项目不再到达本函数；这里只告警 dbmcp 收到已建模库
    # （LLM 选择直连语义层库，通常应改用 wrenai 工具）。不做强制拦截
    # （LLM 可能有意走直连做 DDL/DML），只打日志暴露路由偏差。
    try:
        if kwargs.get("db_name") and get_detector().is_modeled(kwargs["db_name"]):
            _log.warning(
                "[DB_ROUTE] dbmcp_run_sql 收到已建模库 db_name=%s（语义层库建议 wrenai_<库名>_run_sql）",
                kwargs["db_name"],
            )
    except Exception:  # noqa: BLE001  semantic 检测失败不影响注入
        pass

    return args, kwargs


_CARTESIAN_TYPES = {"bar", "line", "scatter"}


def _auto_fix_cartesian_axes(option: dict) -> dict:
    """P1 修复：mcp-echarts 的 isValidEChartsOption 强制要求 cartesian 系列
    （bar/line/scatter）必须有 xAxis 与 yAxis，否则整表渲染报
    "Invalid ECharts option" 并触发 LLM 重试（每次 ~21s 浪费）。

    DeepSeek 偶尔会省略坐标轴。这里在本地补默认轴，避免图表失败：
    - bar/line：xAxis 缺省 → category 轴（能从 {name,value} 数据里提取类目则填，
      否则留空数组让 ECharts 按索引显示）；yAxis 缺省 → value 轴。
    - scatter：xAxis/yAxis 都缺省 → 都补 value 轴。
    只补缺失侧，已有的轴一律不动（不覆盖 LLM 的显式设计）。
    """
    series = option.get("series")
    if not series:
        return option
    if not isinstance(series, list):
        series = [series]
    # 仅当存在 bar/line/scatter 系列时才需要坐标轴
    cart_types = {
        s.get("type") for s in series
        if isinstance(s, dict) and s.get("type") in _CARTESIAN_TYPES
    }
    if not cart_types:
        return option

    has_x = bool(option.get("xAxis"))
    has_y = bool(option.get("yAxis"))

    # 纯 scatter（无 bar/line）时 X 轴应为数值轴；否则默认分类轴
    pure_scatter = cart_types == {"scatter"}

    if not has_x:
        if pure_scatter:
            option["xAxis"] = {"type": "value"}
            _log.warning("[ECHARTS] P1 自动补齐缺失 xAxis（scatter value 轴）")
        else:
            categories: list[Any] = []
            # 从 {name, value} 形式的数据提取类目（bar/line 常见）
            for s in series:
                if not isinstance(s, dict):
                    continue
                data = s.get("data")
                if isinstance(data, list):
                    for it in data:
                        if isinstance(it, dict) and "name" in it and it["name"] not in categories:
                            categories.append(it["name"])
            if categories:
                option["xAxis"] = {"type": "category", "data": categories}
            else:
                # 无类目数据（如纯数值数组）：仍给 category 空轴，ECharts 按索引展示
                option["xAxis"] = {"type": "category"}
            _log.warning(
                "[ECHARTS] P1 自动补齐缺失 xAxis（%s 类目）",
                len(categories) if categories else "空",
            )
    if not has_y:
        # scatter 的 y 轴是 value；bar/line 也是 value（y 为数值维度）
        option["yAxis"] = {"type": "value"}
        _log.warning("[ECHARTS] P1 自动补齐缺失 yAxis")
    return option


def _pack_chart_props(kwargs: dict) -> dict:
    """ECharts generate_echarts 工具参数处理：
    参数名归一化（echarts → echartsOption）、默认强制 outputType="option"，
    并对缺失坐标轴的 cartesian 系列本地补默认轴（P1，避免 mcp-echarts 校验失败）。
    """
    kwargs = dict(kwargs)
    # 参数名归一化：LLM 可能传 echarts（旧名），echarts-mcp 实际需要 echartsOption
    if "echarts" in kwargs and "echartsOption" not in kwargs:
        kwargs["echartsOption"] = kwargs.pop("echarts")
    # 一律强制 option 输出：返回 ECharts 配置 JSON，系统包装为交互式 HTML 图表。
    # 若尊重 LLM 显式传的 svg/png，可能导致同一图表生成两次（一次交互 HTML、
    # 一次静态 SVG），用户只保留可交互的。因此不区分 outputType，全部走 option。
    kwargs["outputType"] = "option"

    # ── P1：cartesian 系列缺轴 → 本地补默认轴 ──
    # ⚠ mcp-echarts 的 generate_echarts schema 要求 echartsOption 是 JSON **字符串**，
    # 补轴函数返回 dict，若直接回填会在 MCP 侧报 "Expected string, received object
    # at echartsOption" → 补轴后必须 json.dumps 序列化回 str（str 与 dict 入参统一）。
    opt_raw = kwargs.get("echartsOption")
    if isinstance(opt_raw, str):
        try:
            opt = json.loads(opt_raw)
            if isinstance(opt, dict):
                kwargs["echartsOption"] = json.dumps(
                    _auto_fix_cartesian_axes(opt), ensure_ascii=False
                )
        except Exception:  # noqa: BLE001 解析失败不动，交给 mcp-echarts 报错
            pass
    elif isinstance(opt_raw, dict):
        kwargs["echartsOption"] = json.dumps(
            _auto_fix_cartesian_axes(opt_raw), ensure_ascii=False
        )
    return kwargs


# ── 工具调用超时（分级）─────────────────────────────────────────
# 原则：不消灭"长查询"，只消灭"永不返回"。数据库查询再慢也有界，
# 超时的价值是识别 MCP 进程挂死这类异常，而不是打断正常的慢查询。
# 超时返回一条错误消息让 LLM 自主决策（重试/简化/放弃），而非崩溃 run。
#
# 值可调：语义层查询工具（run_sql / query_cube，见 agent.utils.query_tools）
# 容忍真实长查询（300s，统一口径）；文件类工具 1 分钟足够；其余默认 120s。
# None / 0 表示不超时。
_TOOL_TIMEOUTS = {
    # 注：wrenai_run_sql / run_sql 两个精确 key 是单库时期遗留。
    # 多库 server 化后语义层工具名为 wrenai_<库名>_run_sql，由
    # _tool_timeout_for 的 startswith("wrenai_") 前缀判断覆盖（300s）。
    "wrenai_run_sql": 300,
    "run_sql": 300,
    "query_cube": 300,   # Cube 通道（单库时期遗留名，与 run_sql 对称）
    "read_file": 60,
    "write_file": 60,
    "edit_file": 60,
    "grep": 30,
    "ls": 30,
    "glob": 30,
    "execute": 120,
    "default": 120,
}

_TOOL_TIMEOUT_MSG = "工具调用超时（{timeout}s）。可能原因：SQL 复杂度过高 / 数据量过大 / 数据库无响应。请耐心等待"

# 并发槽被占满（而不是自己跑超时）时返回的**另一条**消息：必须与超时区分开 ——
# 超时说明这次调用真跑了，繁忙说明**一次都没跑**，让 LLM 换到"用已有数据继续"的路径上，
# 而不是傻等或重试同一条 SQL。
_TOOL_BUSY_MSG = (
    "⚠️ 工具执行繁忙：并发执行槽已被占满（存在长时间未返回的调用），本次调用**未执行**。"
    "请稍后重试，或先基于已有数据继续分析。"
)


def _tool_timeout_for(name: str) -> int | None:
    """按工具名取超时秒数；None/0 表示不超时。"""
    # 语义层数据表型工具（wrenai_<库名>_run_sql / _query_cube）容忍真实长查询
    # 300s —— 用与落盘闸门同一份清单判据，避免 Cube 通道拿 default 120s
    # 被工具超时打断（2026-09-14 统一；Cube 大结果现在也要落盘，超时即整节没数据）
    if name.startswith("wrenai_") and is_data_tool(name):
        return 300
    t = _TOOL_TIMEOUTS.get(name, _TOOL_TIMEOUTS.get("default"))
    return t if t and t > 0 else None


# ── 同步工具的线程池：满载快速失败 + 卡死回收（P1-10）───────────────
#
# 为什么不是裸 `ThreadPoolExecutor`：它的工作队列**无界**。`max_workers` 个槽位一旦
# 被「永不返回」的 MCP 调用占满，后续 `submit` 只会安静排队，而调用方仍要等到
# 自己的 timeout（语义层数据工具 300s）才拿到超时消息 —— 用户看到的是每个工具都
# 「假超时」（明明一秒都没跑），而那 4 个槽位再也回不来。
#
# 本实现做三件事：
#   1. **自己记在用量**（`_inuse`）并**在取槽阶段**判满 —— 满就快速返回「繁忙」，
#      不排队、不假超时。`_inuse` 只在工作函数真正返回时才减一，所以「超时但还在跑」
#      的任务**仍然占着槽**（这才如实反映"没有可用并发"）。
#   2. 取槽允许等一小会儿（默认 5s）：正常的短工具调用挤在一起时排队几秒是合理的，
#      不该被判繁忙；只有**卡死**的池子才会持续拒绝。
#   3. 满载持续超过回收阈值（默认 300s）就**换一个新池**。Python 杀不掉线程
#      （`future.cancel()` 只对**尚未开始**的任务有效），所以「真取消」在同步路径上
#      做不到；能做的是把被占死的槽位连同旧池一起弃用，让后续调用立刻恢复可用。
#      旧线程随各自那次调用结束自然退出（泄漏上限 = 每次回收 ≤ max_workers 条）。
class _SyncToolPool:
    def __init__(
        self,
        max_workers: int = 4,
        wait_seconds: float = 5.0,
        recycle_after: float = 300.0,
        name: str = "tool-timeout",
    ) -> None:
        self._max = max_workers
        self._wait = wait_seconds
        self._recycle_after = recycle_after
        self._name = name
        self._lock = threading.Lock()
        self._executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._inuse = 0
        self._full_since = 0.0
        self.recycles = 0

    # ── 内部（调用方必须持锁）──

    def _executor_locked(self) -> concurrent.futures.ThreadPoolExecutor:
        if self._executor is None:
            self._executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=self._max, thread_name_prefix=self._name
            )
        return self._executor

    def _take_locked(self) -> bool:
        if self._inuse < self._max:
            self._inuse += 1
            self._full_since = 0.0
            return True
        if not self._full_since:
            self._full_since = time.monotonic()
        return False

    def _recycle_locked(self) -> concurrent.futures.ThreadPoolExecutor | None:
        """满载太久 → 摘掉旧池并清零占用，返回旧池（由调用方在锁外 shutdown）。"""
        if not self._full_since or time.monotonic() - self._full_since < self._recycle_after:
            return None
        old, self._executor = self._executor, None
        self._inuse = 0
        self._full_since = 0.0
        self.recycles += 1
        _log.error(
            "[TOOL POOL] 满载超过 %.0fs，回收换新池（第 %d 次）；旧槽位上的调用仍在后台，"
            "其返回值会被丢弃（同步路径无法取消线程）",
            self._recycle_after, self.recycles,
        )
        return old

    def _release(self) -> None:
        with self._lock:
            if self._inuse > 0:
                self._inuse -= 1

    def _call(self, fn, args, kwargs):
        try:
            return fn(*args, **kwargs)
        finally:
            self._release()  # 无论正常/异常/超时后晚到，槽位都在**真正结束**时归还

    # ── 对外 ──

    def _acquire(self) -> bool:
        """取槽：先自愈（可能换池），再等一小会儿；返回 False = 该报繁忙。"""
        deadline = time.monotonic() + self._wait
        while True:
            with self._lock:
                old = self._recycle_locked()
                if self._take_locked():
                    taken = True
                else:
                    taken = False
            if old is not None:
                try:
                    old.shutdown(wait=False)  # 锁外调用：不等卡住的任务
                except Exception:  # noqa: BLE001
                    pass
            if taken:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)  # 短轮询足够：这里等的是「秒级」级别的槽位释放

    def run(self, fn, args, kwargs, timeout: float) -> tuple[str, Any]:
        """在池里执行 fn。返回 (status, result)：'ok' | 'busy' | 'timeout'。"""
        if not self._acquire():
            return "busy", None
        with self._lock:  # 与回收互斥：不让 submit 落到正在 shutdown 的池上
            executor = self._executor_locked()
            try:
                future = executor.submit(self._call, fn, args, kwargs)
            except RuntimeError:  # 保险：池已停
                self._executor = None
                self._inuse = max(0, self._inuse - 1)
                return "busy", None
        try:
            return "ok", future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            # 槽位**不归还**：线程还在跑，等它自己结束（见 _call 的 finally）
            return "timeout", None

    def stats(self) -> dict:
        with self._lock:
            return {"inuse": self._inuse, "max": self._max, "recycles": self.recycles}


# 进程级单例（测试可整体替换以缩小规模）
_TOOL_POOL: _SyncToolPool | None = None


def _get_tool_pool() -> _SyncToolPool:
    global _TOOL_POOL
    if _TOOL_POOL is None:
        _TOOL_POOL = _SyncToolPool()
    return _TOOL_POOL


def _wren_fast_path(tool: Any, kwargs: dict) -> Any | None:
    """Wren memory 工具快速路径——绕过 MCP 子进程和 MemoryStore。

    当 WREN_MEMORY_BACKEND=grep 时，get_context / recall_queries 在 MCP 子进程中
    会创建 MemoryStore（加载 420MB 嵌入模型），对空知识库或小型 schema 导致 hang 至
    120s 超时。此函数在主进程中直接调用 wren Python API，毫秒级返回。

    前置条件：工具对象上需有 ``_wren_project_path`` 属性（由 mcp_tool.py 注入）。

    Returns:
        快速路径结果（与 MCP 返回格式一致），或 None 表示走原始 MCP 路径。
    """
    project_path = getattr(tool, "_wren_project_path", None)
    if project_path is None:
        _log.debug("[WREN FAST-PATH] %s 跳过：_wren_project_path 未注入", tool.name)
        return None

    backend = os.environ.get("WREN_MEMORY_BACKEND", "").strip().lower()
    if backend != "grep":
        _log.debug("[WREN FAST-PATH] %s 跳过：WREN_MEMORY_BACKEND=%r（非 grep）", tool.name, backend)
        return None

    # ── get_context：小 schema 返回全文，跳过 MemoryStore ──
    if tool.name.endswith("_get_context"):
        try:
            from wren.context import build_json
            from wren.memory.schema_indexer import describe_schema

            manifest = build_json(Path(project_path))
            schema_text = describe_schema(manifest)
            _log.info(
                "[WREN FAST-PATH] %s → full schema (%d chars), 跳过 MemoryStore",
                tool.name, len(schema_text),
            )
            artifact = {
                "strategy": "full",
                "schema": schema_text,
                "note": "WREN_MEMORY_BACKEND=grep: full schema returned.",
            }
            # MCP response_format='content_and_artifact' 要求 (content_str, artifact) 二元组
            return (json.dumps(artifact, ensure_ascii=False), artifact)
        except Exception as e:
            _log.warning("[WREN FAST-PATH] %s 快速路径失败，回退 MCP: %s", tool.name, e)
            return None

    # ── recall_queries：空知识库短路返回 [] ──
    if tool.name.endswith("_recall_queries"):
        try:
            from wren.memory.markdown import load_query_pairs

            pairs = load_query_pairs(Path(project_path))
            if not pairs:
                _log.info(
                    "[WREN FAST-PATH] %s → empty knowledge/sql, 跳过 MemoryStore",
                    tool.name,
                )
                artifact = {"matches": []}
                return (json.dumps(artifact, ensure_ascii=False), artifact)
            # 有数据时走原始 MCP 路径（由 WREN_MEMORY_BACKEND=grep env 控制 GrepIndex）
        except Exception as e:
            _log.warning("[WREN FAST-PATH] %s 快速路径失败，回退 MCP: %s", tool.name, e)
        return None

    # ── list_stored_queries：直接读 markdown，跳过 MemoryStore ──
    if tool.name.endswith("_list_stored_queries"):
        try:
            from wren.memory.markdown import load_query_pairs

            pairs = load_query_pairs(Path(project_path))
            source = kwargs.get("source")
            if source:
                pairs = [p for p in pairs if p.get("source", "user") == source]
            limit = kwargs.get("limit")
            if limit is not None:
                pairs = pairs[:limit]
            queries = [
                {
                    "nl_query": p["nl"],
                    "sql_query": p["sql"],
                    "datasource": p.get("datasource", ""),
                    "tags": p.get("tags", ""),
                    "source": p.get("source", "user"),
                    "path": p.get("path"),
                }
                for p in pairs
            ]
            _log.info(
                "[WREN FAST-PATH] %s → %d pairs from markdown, 跳过 MemoryStore",
                tool.name, len(queries),
            )
            artifact = {"queries": queries}
            return (json.dumps(artifact, ensure_ascii=False), artifact)
        except Exception as e:
            _log.warning("[WREN FAST-PATH] %s 快速路径失败，回退 MCP: %s", tool.name, e)
            return None

    # ── store_query：仅写 markdown，跳过 LanceDB 索引 ──
    if tool.name.endswith("_store_query"):
        try:
            from wren.memory.markdown import write_query_markdown

            tags_str = kwargs.get("tags")
            tag_list = (
                [t.strip() for t in tags_str.split(",") if t.strip()]
                if tags_str else None
            )
            md_path = write_query_markdown(
                Path(project_path),
                kwargs.get("nl_query", ""),
                kwargs.get("sql_query", ""),
                datasource=kwargs.get("datasource"),
                tags=tag_list,
            )
            _log.info(
                "[WREN FAST-PATH] %s → wrote %s, 跳过 LanceDB 索引",
                tool.name, md_path,
            )
            artifact = {"path": str(md_path)}
            return (json.dumps(artifact, ensure_ascii=False), artifact)
        except Exception as e:
            _log.warning("[WREN FAST-PATH] %s 快速路径失败，回退 MCP: %s", tool.name, e)
            return None

    return None


def wrap_tool(tool: Any) -> Any:
    """Wrap a langchain BaseTool to auto-resolve virtual paths in arguments.

    The wrapper intercepts ``_run`` and ``_arun`` (or ``invoke`` / ``ainvoke``)
    calls and converts any virtual paths to real filesystem paths before the
    original tool logic runs.

    For chart tools, ToolException (raised by langchain_mcp_adapters when the
    MCP server returns isError:true) is caught and converted to a friendly
    message so the NL2SQL pipeline does not crash.

    All tool calls also get a per-tool timeout (see ``_TOOL_TIMEOUTS``). On
    timeout the call returns a friendly error message (instead of hanging
    forever) so the LLM can decide to retry / simplify / give up. This keeps
    genuinely long queries alive while making the system immune to a hung MCP
    process.
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
            # 仅图表工具打包 props / 注入 outputType。切勿对 DB 工具（dbmcp_* / wrenai_*）
            # 注入 outputType——会被 args_schema 的 extra=forbid 校验拒绝
            # （"Unexpected keyword argument: outputType='option'"），导致子 agent 所有查询工具失效。
            if is_chart:
                new_kwargs = _pack_chart_props(new_kwargs)
            # ── Wren memory 工具快速路径（绕过 MCP + MemoryStore）──
            fast = _wren_fast_path(tool, new_kwargs)
            if fast is not None:
                return fast
            timeout = _tool_timeout_for(tool.name)
            try:
                if timeout is not None:
                    status, result = _get_tool_pool().run(
                        original_run, new_args, new_kwargs, timeout
                    )
                    if status == "busy":
                        _log.warning(
                            "[TOOL BUSY] %s 未执行：并发槽已满（%s），返回繁忙消息",
                            tool.name, _get_tool_pool().stats(),
                        )
                        return (_TOOL_BUSY_MSG, None)
                    if status == "timeout":
                        _log.warning(
                            "[TOOL TIMEOUT] %s 超过 %ds（同步路径），返回超时消息",
                            tool.name, timeout,
                        )
                        return (_TOOL_TIMEOUT_MSG.format(timeout=timeout), None)
                else:
                    result = original_run(*new_args, **new_kwargs)
                return _sanitize_chart_result(result, is_chart)
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
            # 仅图表工具注入 outputType（与同步路径一致），DB 工具不注入。
            if is_chart:
                new_kwargs = _pack_chart_props(new_kwargs)
            # ── Wren memory 工具快速路径（绕过 MCP + MemoryStore）──
            fast = _wren_fast_path(tool, new_kwargs)
            if fast is not None:
                return fast
            _log.warning(
                f"[_arun] {tool.name} FINAL: component={new_kwargs.get('component')}, "
                f"props_keys={list(new_kwargs.get('props', {}).keys()) if isinstance(new_kwargs.get('props'), dict) else 'N/A'}, "
                f"top_keys={list(new_kwargs.keys())}")
            if is_chart and isinstance(new_kwargs.get("props"), dict):
                _p = new_kwargs["props"]
                _data = _p.get("data", [])
                _first_keys = list(_data[0].keys()) if _data and isinstance(_data[0], dict) else "N/A"
            timeout = _tool_timeout_for(tool.name)
            try:
                if timeout is not None:
                    # 用 asyncio.wait 而非 asyncio.wait_for：超时到点立即返回，
                    # 不等待底层 task 的 cancel 清理完成——即使 MCP 进程不响应
                    # cancel，wrapped_arun 也绝不挂起（对"永不返回"的硬保障）。
                    task = asyncio.create_task(original_arun(*new_args, **new_kwargs))
                    done, _pending = await asyncio.wait({task}, timeout=timeout)
                    if not done:
                        task.cancel()  # 尽力取消（后台继续运行也无妨，结果被丢弃）
                        _log.warning(
                            "[TOOL TIMEOUT] %s 超过 %ss，返回超时消息让 agent 决策",
                            tool.name, timeout,
                        )
                        # 保持 MCP 工具 content_and_artifact 二元组契约
                        return (_TOOL_TIMEOUT_MSG.format(timeout=timeout), None)
                    result = task.result()
                else:
                    result = await original_arun(*new_args, **new_kwargs)
                return _sanitize_chart_result(result, is_chart)
            except asyncio.TimeoutError:
                # 工具自身抛出的超时异常也按超时处理（保留日志，语义一致）
                _log.warning(
                    "[TOOL TIMEOUT] %s 抛超时异常（%ss），返回超时消息让 agent 决策",
                    tool.name, timeout,
                )
                return (_TOOL_TIMEOUT_MSG.format(timeout=timeout), None)
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
