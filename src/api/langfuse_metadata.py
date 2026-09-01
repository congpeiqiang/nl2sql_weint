# -*- coding: utf-8 -*-
"""Langfuse 请求级元数据注入中间件（M2 监控增强）。

背景：
- Langfuse CallbackHandler 在 root chain start 读取 chain 的 `config.metadata`，
  从中解析 `langfuse_session_id` / `langfuse_trace_name` / `langfuse_tags` /
  `langfuse_user_id`，并把其余键原样透传到 trace metadata（已 spike 验证，
  d:/tmp/langfuse_m2_metadata_probe.py：trace 拿到 session_id/trace_name/tags，
  且 test_marker 等额外键透传）。
- 主 run 的发起方（前端 useChat / SDK runs.create）不传 metadata，故需服务端注入。
- 注入点选在 run 创建端点（POST /threads/{tid}/runs[/stream] / /runs），
  把 metadata 写进请求体 `config.metadata`——数据随请求透传到 worker 线程，
  天然避开「propagate_attributes 的 OTel contextvar 跨线程失效」问题（方案原文
  写 propagate_attributes，实测不可行，改走 metadata 注入，验收口径一致）。

实现约束：
- 纯 ASGI 中间件，只拦截 POST 的 run 端点，只读请求体；响应事件原样转发、
  不缓冲——避免破坏 /runs/stream 的 SSE 流（禁全局 JSON 中间件的教训）。
- 挂到 custom_app 的 `user_middleware`，langgraph server 会提取并全局应用
  （langgraph_api/server.py: custom_middleware + global_middleware）。
"""
from __future__ import annotations

import json
import logging
import os
import re

from agent.workspace_manager import get_workspace_manager
from agent.trace.skill_manifest import get_enriched_skill_manifest
from agent.trace.langfuse_client import langfuse_enabled, prompt_label_info

_logger = logging.getLogger(__name__)

# 匹配 run 创建类端点；捕获 thread_id（stateless /runs 无 thread_id，跳过注入）
_RUN_PATH_RE = re.compile(
    r"^/threads/(?P<tid>[^/]+)/runs($|/stream$|/batch$)|^/runs$"
)


class LangfuseMetadataMiddleware:
    """为 run 创建请求注入 Langfuse 元数据（config.metadata）。"""

    def __init__(self, app):
        self.app = app
        self._skills: list[dict] = []
        try:
            # M6：Langfuse 解析版本 + source 标记（未同步/失败回退本地 source=local）
            self._skills = get_enriched_skill_manifest()
        except Exception as e:  # noqa: BLE001
            _logger.warning("[langfuse_meta] skill manifest 加载失败: %s", e)

    async def __call__(self, scope, receive, send):
        # 总开关 LANGFUSE_ENABLE=false：不注入 metadata（无 trace 消费它），原样透传
        if not langfuse_enabled():
            await self.app(scope, receive, send)
            return

        if scope.get("type") != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        m = _RUN_PATH_RE.match(scope.get("path", ""))
        if not m:
            await self.app(scope, receive, send)
            return

        tid = m.group("tid")
        if not tid:
            await self.app(scope, receive, send)
            return

        try:
            body = await _read_body(receive)
            if body is None:
                await self.app(scope, receive, send)
                return
            new_body = self._inject(body, tid)
            if new_body is body:
                # 未改动：原样透传（保留原始 receive 通道）
                await self.app(scope, receive, send)
                return
            payload = json.dumps(new_body).encode("utf-8")
            wrapped_receive = _buffered_receive(payload, receive)
            await self.app(scope, wrapped_receive, send)
        except Exception as e:  # noqa: BLE001
            # 注入失败不影响主流程（监控旁路）
            _logger.debug("[langfuse_meta] 注入失败: %s", e)
            await self.app(scope, receive, send)

    # ── 元数据组装 ──────────────────────────────────────────

    def _inject(self, body: dict, tid: str) -> dict:
        """把 langfuse 元数据写进 body['config']['metadata']，无变化则返回原对象。"""
        if not isinstance(body, dict):
            return body
        config = body.get("config")
        if not isinstance(config, dict):
            config = {}
        configurable = config.get("configurable")
        if not isinstance(configurable, dict):
            configurable = {}

        metadata = config.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}

        merged = dict(metadata)

        # ── Langfuse 保留键（只在缺失时注入，不覆盖客户端显式值）──
        if not merged.get("langfuse_session_id"):
            merged["langfuse_session_id"] = tid
        if not merged.get("langfuse_trace_name"):
            merged["langfuse_trace_name"] = f"query:{tid}"
        if "langfuse_tags" not in merged:
            merged["langfuse_tags"] = ["nl2sql"]
        elif isinstance(merged["langfuse_tags"], list) and "nl2sql" not in merged["langfuse_tags"]:
            merged["langfuse_tags"] = [*merged["langfuse_tags"], "nl2sql"]

        # 用户：本地无登录体系，auth 时 configurable 会带 langgraph_auth_user_id
        uid = configurable.get("langgraph_auth_user_id")
        if uid and not merged.get("langfuse_user_id"):
            merged["langfuse_user_id"] = str(uid)

        # ── 业务元数据（透传到 trace metadata）──
        if "workspace" not in merged:
            try:
                wm = get_workspace_manager()
                merged["workspace"] = {
                    "name": wm.active_name,
                    "path": str(wm.active_workspace),
                }
            except Exception:  # noqa: BLE001
                pass
        if "skills" not in merged:
            merged["skills"] = self._skills
        db_name = configurable.get("db_name", "")
        if db_name and "db_name" not in merged:
            merged["db_name"] = db_name

        # ── M5 灰度：当前进程的 prompt label/版本 + release（A→B 切换可见分组）──
        if "prompt" not in merged:
            try:
                merged["prompt"] = prompt_label_info()
            except Exception:  # noqa: BLE001
                pass
        rel = os.getenv("LANGFUSE_RELEASE", "")
        if rel and "langfuse_release" not in merged:
            merged["langfuse_release"] = rel

        if merged == metadata:
            return body

        config = {**config, "metadata": merged}
        # 顶层 metadata 一并写入，与 config.metadata 保持一致（server 也读 payload.metadata）
        return {**body, "config": config, "metadata": merged}


# ── ASGI 辅助 ──────────────────────────────────────────────

async def _read_body(receive) -> dict | None:
    """读取请求体并解析 JSON；非 JSON 返回 None。"""
    chunks = []
    while True:
        msg = await receive()
        if msg["type"] == "http.disconnect":
            return None
        if msg["type"] != "http.request":
            continue
        chunks.append(msg.get("body", b""))
        if not msg.get("more_body"):
            break
    raw = b"".join(chunks)
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _buffered_receive(body: bytes, original_receive):
    """构造新的 receive：先吐注入后的 body，再转发原始通道。"""
    sent = False

    async def _wrapped_receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await original_receive()

    return _wrapped_receive
