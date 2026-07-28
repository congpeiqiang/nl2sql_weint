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



def _resolve_args(args, kwargs):
    """Resolve virtual paths to absolute paths in tool arguments."""
    return args, kwargs

def _is_chart_tool(tool: Any) -> bool:
    """Check if a tool is a chart generation tool (Semiotic or AntV)."""
    name = getattr(tool, "name", "")
    keywords = ("chart", "render", "suggestchart", "getschema", "diagnose", "repair")
    return bool(name and any(kw in name.lower() for kw in keywords))
def _sanitize_chart_result(result: Any, is_chart: bool) -> Any:
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
