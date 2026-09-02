"""报告文件访问 API —— build_report 生成的报告预览 / 下载。

GET /api/reports/{filename}?download=1

- 无 download：返回文件内容（text/markdown），前端预览用
- download=1：返回 attachment（Content-Disposition），浏览器原生下载

安全：filename 只允许基本文件名（不含路径分隔符），并做路径穿越防护，
只能读取当前活跃工作区 report/ 目录内的文件——不接受绝对路径、`..` 等。

报告由 build_report 落盘到 `active_workspace/report/`（见 report_builder.py），
返回的 VFS 路径是 `/workspace/report/{filename}`。前端把该路径映射到本 API：
`/workspace/report/xxx.md` → `/api/reports/xxx.md`。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import unquote

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from agent.workspace_manager import get_workspace_manager

_logger = logging.getLogger(__name__)

# 只允许普通文件名：不能包含路径分隔符 / \，防止目录穿越
_SAFE_FNAME = re.compile(r"^[^/\\]+$")


def _resolve_report_file(raw: str) -> Path | None:
    """把 URL 参数安全解析为 report 目录内的文件路径。

    返回 None 表示非法（含路径分隔符、不在 report 目录内、文件不存在）。
    """
    name = unquote(raw or "").strip()
    if not name or not _SAFE_FNAME.match(name):
        return None
    report_dir = get_workspace_manager().report_dir
    candidate = (report_dir / name).resolve()
    # 双保险：解析后必须仍在 report 目录内
    if not str(candidate).startswith(str(report_dir.resolve())):
        return None
    if not candidate.is_file():
        return None
    return candidate


async def get_report_file(request: Request):
    raw = request.path_params.get("filename", "")
    path = _resolve_report_file(raw)
    if path is None:
        return Response("报告不存在或非法文件名", status_code=404, media_type="text/plain")

    download = (request.query_params.get("download") or "").lower() in ("1", "true", "yes")
    try:
        body = path.read_bytes()
    except OSError as e:
        _logger.warning("[report_file] 读取报告失败 %s: %s", path, e)
        return Response("报告读取失败", status_code=500, media_type="text/plain")

    filename = path.name
    headers = {"Content-Length": str(len(body))}
    if download:
        # RFC 5987 编码文件名，避免中文文件名在 Content-Disposition 里乱码
        ascii_name = filename.encode("ascii", "ignore").decode() or "report.md"
        headers["Content-Disposition"] = (
            f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{_quote_filename(filename)}"
        )
        media_type = "application/octet-stream"
    else:
        media_type = "text/markdown; charset=utf-8"

    return Response(body, media_type=media_type, headers=headers)


def _quote_filename(name: str) -> str:
    """RFC 5987 filename* 编码：只允许 %XX 和部分保留字符。"""
    import urllib.parse

    return urllib.parse.quote(name, safe="-_.~")


routes: list[BaseRoute] = [
    Route("/api/reports/{filename}", get_report_file, methods=["GET"]),
]
