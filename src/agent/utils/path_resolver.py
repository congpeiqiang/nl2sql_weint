"""Virtual path → real filesystem path resolver for deepagents backends.

When deepagents operates with CompositeBackend + virtual_mode, all file paths
returned by backend operations are virtual paths starting with ``/``.
External tools (especially MCP tools running in separate processes) cannot
resolve these virtual paths and require real filesystem paths.

This module provides utilities to transparently convert virtual paths to real
paths at the tool-call boundary.
"""

from __future__ import annotations

from functools import wraps
from pathlib import Path
from typing import Any

# Must match agent.py workspace_dir exactly
WORKSPACE_DIR = Path(
    r"D:\学习资料\大模型\huice_008\2026-05-12-nl2sql\nl2sql\src\app"
).resolve()

# Parameter names that commonly carry file-path values.
# Only values associated with these keys (or nested dict keys) are converted.
DEFAULT_PATH_PARAM_NAMES: set[str] = {
    "file_path",
    "path",
    "filename",
    "file",
    "filepath",
    "input_file",
    "output_file",
    "source",
    "destination",
    "src",
    "dst",
    "input",
    "output",
    "dir",
    "directory",
    "root",
    "url",  # some tools accept a local file via url-like param
}

# pylint: disable  MC80OmFIVnBZMlhrdUp2bG43bmx2TG82V0UweFJRPT06MmYwNTJmNTk=

def resolve_virtual_path(vpath: str) -> str:
    """Convert a deepagents virtual path to a real filesystem path.

    Virtual paths start with ``/`` and are relative to ``WORKSPACE_DIR``.
    Windows absolute paths and relative paths without leading ``/`` are
    returned unchanged.

    Examples
    --------
    >>> resolve_virtual_path("/report/test.pdf")
    'C:\\...\\workspace\\report\\test.pdf'

    >>> resolve_virtual_path("C:\\foo\\bar.pdf")
    'C:\\foo\\bar.pdf'

    >>> resolve_virtual_path("report/test.pdf")
    'C:\\...\\workspace\\report\\test.pdf'
    """
    vpath = vpath.strip().replace("\\", "/")

    # Windows absolute path → passthrough
    if len(vpath) >= 2 and vpath[1] == ":":
        return vpath

    # Virtual path (starts with /) → resolve under workspace
    if vpath.startswith("/"):
        return str(WORKSPACE_DIR / vpath.lstrip("/"))

    # Plain relative path → resolve under workspace
    return str((WORKSPACE_DIR / vpath).resolve())


def resolve_value(value: Any, param_name: str = "") -> Any:
    """Recursively resolve virtual paths inside a value.

    Only values whose associated parameter name (or nested dict key) is in
    ``DEFAULT_PATH_PARAM_NAMES`` are converted.  This avoids accidentally
    mutating non-path strings such as API endpoints (``/api/v1/users``).

    A double-leading slash ``//`` can be used as an escape hatch to prevent
    conversion: ``//api/v1`` stays as ``//api/v1``.
    """
    if isinstance(value, str):
        # Escape hatch: // means "do not convert"
        if value.startswith("//"):
            return value

        # Only convert if the parameter name looks like a path param
        if param_name.lower() in DEFAULT_PATH_PARAM_NAMES:
            if value.startswith("/") and not (len(value) >= 2 and value[1] == ":"):
                return resolve_virtual_path(value)
        return value

    if isinstance(value, list):
        return [resolve_value(v, param_name) for v in value]

    if isinstance(value, dict):
        return {
            k: resolve_value(v, param_name=k if k in DEFAULT_PATH_PARAM_NAMES else param_name)
            for k, v in value.items()
        }

    return value

def _resolve_args(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Resolve virtual paths in tool call arguments."""
    # Handle the case where the first positional arg is a dict (common for
    # langchain BaseTool._run(tool_input) style).
    if args and len(args) == 1 and isinstance(args[0], dict):
        resolved_input = {
            k: resolve_value(v, param_name=k)
            for k, v in args[0].items()
        }
        return (resolved_input,), kwargs

    # Resolve kwargs
    if kwargs:
        resolved_kwargs = {
            k: resolve_value(v, param_name=k)
            for k, v in kwargs.items()
        }
        return args, resolved_kwargs

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
    _log.info(f"[CHART] called: is_chart={is_chart}, type={type(result).__name__}")
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
        _log.info(f"[CHART] tuple: content_type={type(cnt).__name__}, has_svg={'<svg' in str(cnt)[:200]}")
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
        _log.warning(f"[CHART] no SVG found in {type(cnt).__name__}: {str(cnt)[:300]}")
        return result

    if isinstance(result, str) and "<svg" in result:
        r = _svg_to_data_url(result)
        _log.info(f"[CHART] str → iframe[:100]={r[:100]}")
        return r

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
            new_args, new_kwargs = _resolve_args(args, kwargs)
            new_args, new_kwargs = _inject_db_name(tool.name, new_args, new_kwargs)
            try:
                return _sanitize_chart_result(original_run(*new_args, **new_kwargs), is_chart)
            except Exception as e:
                if is_chart and ('ToolException' in type(e).__name__ or 'McpError' in type(e).__name__):
                    return (_CHART_ERROR_MSG, None)
                raise

        tool._run = wrapped_run  # type: ignore[method-assign]

    if original_arun is not None:
        @wraps(original_arun)
        async def wrapped_arun(*args: Any, **kwargs: Any) -> Any:
            import logging
            _log = logging.getLogger(__name__)
            _log.info(f"[_arun] {tool.name} called with args={args}")
            new_args, new_kwargs = _resolve_args(args, kwargs)
            new_args, new_kwargs = _inject_db_name(tool.name, new_args, new_kwargs)
            _log.info(f"[_arun] {tool.name} after inject: args={new_args}, kwargs={new_kwargs}")
            try:
                return _sanitize_chart_result(await original_arun(*new_args, **new_kwargs), is_chart)
            except Exception as e:
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
