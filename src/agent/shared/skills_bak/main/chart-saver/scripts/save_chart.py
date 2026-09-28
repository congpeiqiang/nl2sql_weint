#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
save_chart.py — 图表保存辅助脚本

将图表工具（generate_echarts）生成的图表内容保存为独立文件到工作区报告目录。

支持三种输入格式：
1. SVG 字符串（ECharts outputType="svg" 返回）：直接提取 <svg>...</svg> 保存
2. base64 HTML iframe：解码 base64 → 提取 <svg> → 保存
3. PNG 文件路径（ECharts outputType="png" 返回）：复制文件到工作区报告目录

用法：
    python save_chart.py --content "<图表内容>" --name "IMDb_Movie_Genres_chart" [--format svg|png] [--dir /workspace/report/]

注意：
- 落盘文件名由**本脚本**决定（调用方只给基名）：`{基名}_{YYYYMMDD_HHMMSS}_{4位随机}.{ext}`。
  report/ 是全站共享目录，"销售趋势"这类同名主题在不同用户/不同会话里天然同名，
  固定基名 = 后写的人静默覆盖前一个人的图，所以必须带唯一后缀。调用方回显的
  `✅ 图表已保存: <路径>` 才是真实文件名，**不要自己拼**。
- 脚本运行在宿主 shell，/workspace/ 虚拟路径会被解析为宿主盘符根目录（错误位置）。
  因此脚本内部使用宿主绝对路径写入，确保虚拟文件系统可见。
- 宿主路径映射（与 CompositeBackend 路由一致）：
  /workspace/ → 当前活跃工作区（不再硬编码默认工作区 src/agent/workspace/——
  非默认工作区下静态图会存错位置）；/shared/memory/ → 共享 memory；
  /shared/skills/ → 共享 skills。
"""

import argparse
import base64
import os
import re
import shutil
import sys
import uuid
from datetime import datetime
from pathlib import Path

# ── 宿主路径映射 ──────────────────────────────────────────────
# 脚本位于 <项目根>/src/agent/shared/skills/main/chart-saver/scripts/save_chart.py
_SCRIPT_DIR = Path(__file__).resolve().parent
# scripts/ -> chart-saver/ -> main/ -> skills/ -> shared/ -> agent/ -> src/
_SRC_DIR = _SCRIPT_DIR.parent.parent.parent.parent.parent.parent
# 默认工作区 report（解析活跃工作区失败时的兜底）：src/agent/workspace/report
_DEFAULT_REPORT_DIR = _SRC_DIR / "agent" / "workspace" / "report"

# 让本脚本可独立导入 WorkspaceManager（读注册表定位活跃工作区）。
# agent.workspace_manager 只依赖标准库，import 无副作用。
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))


def _resolve_report_dir() -> Path:
    """当前活跃工作区的 report 目录（宿主绝对路径）。

    经 WorkspaceManager 解析注册表 workspaces.json 的 active 工作区；
    失败时回退默认工作区（src/agent/workspace/report）。
    """
    try:
        from agent.workspace_manager import get_workspace_manager

        return get_workspace_manager().report_dir
    except Exception as e:  # noqa: BLE001
        print(f"⚠ 解析活跃工作区失败，回退默认工作区 report: {e}", file=sys.stderr)
        return _DEFAULT_REPORT_DIR


def _vfs_to_host(vpath: str) -> str:
    """VFS 虚拟路径 → 宿主绝对路径（与 CompositeBackend 路由一致）。

    /workspace/<rest>      → {活跃工作区}/<rest>
    /shared/memory/<rest>  → {共享 memory}/<rest>
    /shared/skills/<rest>  → {共享 skills}/<rest>
    其它路径按宿主绝对路径解析。
    """
    try:
        from agent.workspace_manager import get_workspace_manager

        wm = get_workspace_manager()
        for prefix, attr in (
            ("/shared/memory/", "shared_memory_dir"),
            ("/shared/skills/", "shared_skills_dir"),
            ("/workspace/", "active_workspace"),
        ):
            if vpath.startswith(prefix):
                root = getattr(wm, attr)
                return str(root / vpath[len(prefix):])
    except Exception as e:  # noqa: BLE001
        print(f"⚠ 解析 VFS 路径失败，按宿主绝对路径处理: {e}", file=sys.stderr)
    return str(Path(vpath).resolve())


def _sanitize_filename(name: str) -> str:
    """清理文件名中的非法字符。"""
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", name.strip())
    return name or "chart"


def _reserve_dest(report_dir: Path, safe_name: str, ext: str) -> Path:
    """在 report 目录里**原子占位**一个唯一文件名并返回它。

    命名 `<基名>_<YYYYMMDD_HHMMSS>_<4位随机><ext>`：
    · 时间戳：让人一眼看出产出时间，也避免跨天同名互相覆盖；
    · 随机后缀：同秒不同用户/会话同名仍会撞，4 位 hex 只是打散，真正的保证是下面
      的 `O_EXCL` 独占创建——检查再写（check-then-act）在并发下必然有窗口。

    占位意味着**先建 0 字节文件再写入**：本函数由调用方在内容解析/校验**之后**调用，
    所以不会留下空文件；写失败时残留的空文件是可接受的（比覆盖别人的图轻得多）。
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    for _ in range(50):
        dest = report_dir / f"{safe_name}_{ts}_{uuid.uuid4().hex[:4]}{ext}"
        try:
            fd = os.open(str(dest), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue
        os.close(fd)
        return dest
    raise RuntimeError(f"无法在 {report_dir} 生成唯一文件名: {safe_name}{ext}")


def _extract_svg(content: str) -> str:
    """从内容中提取 <svg>...</svg> 完整标签。"""
    m = re.search(r"<svg[\s\S]*?</svg>", content)
    if not m:
        raise ValueError("未找到 <svg> 标签")
    return m.group(0)


def _decode_base64_iframe(content: str) -> str:
    """从 base64 编码的 HTML iframe 中解码并提取 SVG。"""
    # 匹配 data:text/html;base64,XXXX
    m = re.search(r"data:text/html;base64,([A-Za-z0-9+/=]+)", content)
    if not m:
        raise ValueError("未找到 base64 编码的 HTML iframe")
    b64 = m.group(1)
    try:
        html = base64.b64decode(b64).decode("utf-8", errors="replace")
    except Exception as e:
        raise ValueError(f"base64 解码失败: {e}")
    return _extract_svg(html)


def _detect_format(content: str) -> str:
    """自动检测内容格式：svg / base64 / png_path / unknown。"""
    content = content.strip()
    if "<svg" in content:
        return "svg"
    if "data:text/html;base64," in content:
        return "base64"
    if content.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
        return "png_path"
    return "unknown"


def save_chart(content: str, name: str, fmt: str = "", out_dir: str = "") -> str:
    """保存图表到工作区报告目录，返回保存的文件路径。"""
    content = content.strip()
    if not content:
        raise ValueError("图表内容为空")

    # 确定输出目录（宿主绝对路径）
    if out_dir:
        # 传入 VFS 虚拟路径（/workspace/report/ 等）→ 映射到当前活跃工作区的宿主路径
        if out_dir.startswith("/"):
            out_dir = _vfs_to_host(out_dir)
        else:
            out_dir = str(Path(out_dir).resolve())
    else:
        out_dir = str(_resolve_report_dir())

    report_dir = Path(out_dir)
    report_dir.mkdir(parents=True, exist_ok=True)

    # 自动检测格式
    detected = _detect_format(content)
    if not fmt:
        fmt = detected

    safe_name = _sanitize_filename(name)

    if fmt == "svg" or detected == "svg":
        svg = _extract_svg(content)
        dest = _reserve_dest(report_dir, safe_name, ".svg")
        dest.write_text(svg, encoding="utf-8")
        return str(dest)

    if fmt == "base64" or detected == "base64":
        svg = _decode_base64_iframe(content)
        dest = _reserve_dest(report_dir, safe_name, ".svg")
        dest.write_text(svg, encoding="utf-8")
        return str(dest)

    if fmt == "png" or detected == "png_path":
        src = Path(content)
        if not src.exists():
            raise ValueError(f"源图片不存在: {content}")
        dest = _reserve_dest(report_dir, safe_name, ".png")
        shutil.copy2(src, dest)
        return str(dest)

    raise ValueError(f"无法识别的图表格式: {fmt}（支持 svg / base64 / png）")


def main():
    parser = argparse.ArgumentParser(description="保存图表到工作区报告目录")
    parser.add_argument("--content", required=True, help="图表内容（SVG 字符串 / base64 iframe / 文件路径）")
    parser.add_argument("--name", required=True, help="目标文件名（不含扩展名）")
    parser.add_argument("--format", default="", help="输出格式：svg / base64 / png（默认自动检测）")
    parser.add_argument("--dir", default="", help="保存目录（默认 /workspace/report/）")
    args = parser.parse_args()

    try:
        dest = save_chart(args.content, args.name, args.format, args.dir)
        print(f"✅ 图表已保存: {dest}")
    except Exception as e:
        print(f"❌ 保存图表失败: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
