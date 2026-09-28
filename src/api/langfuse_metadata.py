# -*- coding: utf-8 -*-
"""Langfuse 请求级元数据注入中间件（M2 监控增强）+ **run 入参授权钳制**（P1-2）。

⚠️ 本中间件承担两件事，别只看名字：
1. （M2）给 run 创建请求注入 Langfuse metadata —— 可被开关关掉。
2. （P1-2，**安全属性，不受开关影响**）run 请求体里的 `config.configurable` 是
   **客户端可控**的，而下游全按它行事：
   - `configurable.user_id` → `thinking_toggle` 用它加载**那个用户**的模型配置
     （含 api_key）；`deepagents_async_config_patch` 把它透传给子 agent。
   - `configurable.db_name` → `tool_filter` 按它裁剪工具（`if not db_name: return
     tools` = **空库名 = 不过滤 = 暴露所有库的 wrenai 工具**，所以不能「清空了之」），
     `record_thread_db` 按它记「这个会话用过哪个库」，后续 trace/报告的库维度
     可见性判定读的就是这条记录。
   外部请求（能读到 `scope["state"]["user"]` = AuthMiddleware 验过 cookie 的身份）
   一律以**登录身份**为准覆盖客户端传的值；`db_name` 不在该用户授权内则 403。
   内部调用（子 agent / sync 循环）不钳制：它们的 configurable 由服务端自己镜像，
   且带着父 run 已校验过的库名。

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

import asyncio
import json
import logging
import os
import re

from agent.auth.ownership import is_real_owner
from agent.workspace_manager import get_workspace_manager
from agent.trace.skill_manifest import get_enriched_skill_manifest
from agent.trace.langfuse_client import langfuse_enabled, prompt_label_info

_logger = logging.getLogger(__name__)

# 匹配 run 创建类端点；捕获 thread_id。
#
# ⚠️ stateless 那几条（/runs、/runs/stream、/runs/wait、/runs/batch）必须一起拦：
# nginx 把 `^/(threads|runs|assistants|store|ok|docs|openapi)` 全量转到后端，外部
# 客户端可以直接 POST /runs/stream 自建临时会话发起 run —— 只拦 /threads/{tid}/runs*
# 的话，伪造 configurable 换个路径就绕过去了。它们没有 thread_id，故只做钳制不做注入。
# 特意不含 /runs/cancel（载荷是 run_ids，没有 config 可钳）与 /runs/crons*（同理）。
_RUN_PATH_RE = re.compile(
    r"^/threads/(?P<tid>[^/]+)/runs($|/stream$|/batch$)"
    r"|^/runs($|/stream$|/wait$|/batch$)"
)

# 逃生开关（仅运维应急）：置 1 时跳过 db_name 授权校验，但仍会钳制 user_id。
# 存在的意义是「授权表配错导致全员问不了数」时有免改代码的回退手段；正常运行
# 不要打开——打开即等于取消 P1-2 的库维度授权。
_SKIP_DB_AUTHZ_ENV = "NL2SQL_SKIP_RUN_DB_AUTHZ"

# 外部请求体里由服务端权威决定的 configurable 键：客户端传了就删（见 _clamp_config）。
_HANDS_OFF_KEYS = ("thread_id", "checkpoint_id", "checkpoint_ns")


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
        if scope.get("type") != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        m = _RUN_PATH_RE.match(scope.get("path", ""))
        if not m:
            await self.app(scope, receive, send)
            return

        tid = m.group("tid") or ""
        user = _scope_user(scope)
        # 外部真实用户 → 钳制 configurable；内部调用（子 agent / sync 循环）不钳制：
        # 它们的 configurable 是服务端自己镜像的，钳制会把 db_name 判成越权。
        enforce = is_real_owner(user.get("user_id"))
        # 注入只在有 thread_id 时做（stateless /runs 没会话，注 session_id 无意义）
        inject = langfuse_enabled() and bool(tid)

        # 内部调用 + 无 trace 需求：连 body 都不读（热路径，省一次序列化往返）
        if not enforce and not inject:
            await self.app(scope, receive, send)
            return

        raw, body = await _read_body(receive)
        if body is None:
            # 非 JSON / 空体：原样回放已消费的 body，不做任何注入与钳制。
            # ⚠️ 必须回放——receive 已经被读空了，直接透传原通道会让下游读到空体。
            await self.app(scope, _buffered_receive(raw, receive), send)
            return

        # ① 授权钳制（安全属性）：出任何错都拒绝，不 fail-open
        if enforce:
            err = ""
            try:
                err = _clamp_body(body, user, tid)
            except Exception as e:  # noqa: BLE001
                _logger.exception("[langfuse_meta] 钳制异常，拒绝执行: %s", e)
                err = "请求参数校验失败"
            if err:
                _logger.warning(
                    "[langfuse_meta] 403 %s path=%s user=%s thread=%s",
                    err, scope.get("path", ""), user.get("user_id"), tid or "-",
                )
                await _send_json(send, 403, {"error": "forbidden", "detail": err})
                return

        # ② Langfuse 注入（监控旁路）：失败不影响主流程
        if inject:
            try:
                body = await self._inject(body, tid, scope)
            except Exception as e:  # noqa: BLE001
                _logger.debug("[langfuse_meta] 注入失败: %s", e)
        elif tid:
            # 无 langfuse 时不注入 metadata，**但会话归属照记**——归属是安全属性
            # （会话可见性），不能挂在监控开关上。此时 db_name 已过 ① 的授权校验。
            try:
                await self._apply_ownership(tid, user.get("user_id", ""), "")
            except Exception as e:  # noqa: BLE001
                _logger.debug("[langfuse_meta] 归属登记失败(无 langfuse): %s", e)

        await self.app(
            scope,
            _buffered_receive(json.dumps(body).encode("utf-8"), receive),
            send,
        )

    # ── 元数据组装 ──────────────────────────────────────────

    @staticmethod
    def _extract_question_summary(body: dict, max_len: int = 40) -> str:
        """从 run 请求体的 input.messages 提取最后一条用户消息作为 trace 名摘要。"""
        inp = body.get("input")
        if not isinstance(inp, dict):
            return ""
        messages = inp.get("messages") or []
        if not isinstance(messages, list):
            return ""
        # 从后往前找最后一条 human 消息
        for msg in reversed(messages):
            text = ""
            if isinstance(msg, dict):
                mtype = msg.get("type", "") or msg.get("role", "")
                if mtype not in ("human", "user"):
                    continue
                content = msg.get("content", "")
                if isinstance(content, str):
                    text = content.strip()
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text", "").strip()
                            if text:
                                break
            elif isinstance(msg, (list, tuple)) and len(msg) >= 2:
                if msg[0] in ("human", "user"):
                    text = str(msg[1]).strip()
            if text and not text.startswith("[系统"):
                return text[:max_len]
        return ""

    async def _apply_ownership(self, tid: str, uid: str, inherited: str) -> None:
        """登记会话归属：grants 表 + 线程 metadata.owner（幂等，可多次调用）。

        归属人优先级：请求体/继承来的真实用户 > 解析出的 uid。
        为什么不能直接用 uid：容器内部调用（子 agent / sync 循环）被标识成 internal，
        但它们是代真实用户干活——记成 internal 会让用户自己都读不到自己的子线程。
        """
        from agent.auth.grants import claim_thread
        from agent.auth.ownership import (
            is_real_owner,
            mark_stamped,
            was_stamped,
        )

        owner = uid if is_real_owner(uid) else ""
        if not owner and is_real_owner(inherited):
            owner = inherited
        if not owner or not tid:
            return

        try:
            # P1-14：SQLite 写 + commit，搬到线程（每个建 run 的请求都会走到）
            await asyncio.to_thread(claim_thread, tid, owner)
        except Exception:  # noqa: BLE001
            _logger.debug("[langfuse_metadata] claim_thread 失败", exc_info=True)

        # 线程 metadata 的 owner：POST /threads 建的主会话由 auth 钩子写过（进程内
        # 集合已标记）；子 agent 线程走 /noauth 创建，钩子不执行，这里补一次 PATCH。
        if was_stamped(tid):
            return
        from api._common import stamp_thread_owner
        if await stamp_thread_owner(tid, owner):
            mark_stamped(tid)

    async def _inject(self, body: dict, tid: str, scope: dict | None = None) -> dict:
        """把 langfuse 元数据写进 body['config']['metadata']，无变化则返回原对象。"""
        if not isinstance(body, dict):
            return body
        config = body.get("config")
        if not isinstance(config, dict):
            config = {}
        configurable = config.get("configurable")
        if not isinstance(configurable, dict):
            configurable = {}
        original_configurable = dict(configurable)  # 快照：检测 configurable 变更

        metadata = config.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}

        merged = dict(metadata)

        # ── Langfuse 保留键（只在缺失时注入，不覆盖客户端显式值）──
        if not merged.get("langfuse_session_id"):
            merged["langfuse_session_id"] = tid
        if not merged.get("langfuse_trace_name"):
            # 低基数稳定名（官方 best-practices：name 不含动态值，用户查询放 metadata/input）
            merged["langfuse_trace_name"] = "chat-turn"
        if "langfuse_tags" not in merged:
            merged["langfuse_tags"] = ["nl2sql"]
        elif isinstance(merged["langfuse_tags"], list) and "nl2sql" not in merged["langfuse_tags"]:
            merged["langfuse_tags"] = [*merged["langfuse_tags"], "nl2sql"]

        # 用户：优先从 AuthMiddleware 注入的 scope state 读取（可靠来源），
        # 回退到 body configurable（客户端不传此键，仅供未来 LangGraph 内部注入兼容）
        uid = _scope_uid(scope)
        uid_source = "scope_state" if uid else "none"
        if not is_real_owner(uid):
            # internal/dev 只说明「调用来自容器内部」，不代表没有用户身份：
            # deepagents 子 agent、sync 循环都在 configurable 里带着父 run 的真实用户
            # (deepagents_async_config_patch 注入)。归属必须记真实用户，否则子线程
            # 归到 internal 名下，用户连自己的子线程都读不到。仅在 uid 非真实用户时启用。
            #
            # 读 `user_id` 而不是 `langgraph_auth_user_id`：子 agent 的 configurable 由
            # `deepagents_async_config_patch._current_configurable()` 透传，其中
            # `langgraph_` 前缀键被 `_is_internal_key` 全部剔除（只剩我们注入的 `user_id`）。
            cand = configurable.get("user_id") or configurable.get("langgraph_auth_user_id")
            if is_real_owner(cand):
                uid, uid_source = cand, "configurable(内部调用带用户)"
        if not uid:
            uid = configurable.get("user_id") or configurable.get("langgraph_auth_user_id")
            if uid:
                uid_source = "configurable"
        _logger.info(
            "[langfuse_meta] uid=%s source=%s path=%s has_cookie=%s",
            uid, uid_source,
            (scope or {}).get("path", ""),
            bool((dict((scope or {}).get("headers", [])).get(b"cookie", b""))),
        )
        if uid and not merged.get("langfuse_user_id"):
            merged["langfuse_user_id"] = str(uid)
        # 注入 user_id 到 configurable，供运行时（create_model 等）读取。
        # source=scope_state ⇒ 外部请求，身份已由 AuthMiddleware 校验，**覆盖式**写入
        # （纵深防御：_clamp_config 已覆盖过一遍，这里再钉一次，防止未来新增
        #  绕过钳制的调用路径时又被客户端值顶掉）。内部调用保持「不覆盖」。
        if uid and uid_source == "scope_state":
            merged["langfuse_user_id"] = str(uid)
            configurable["user_id"] = str(uid)
        elif uid and not configurable.get("user_id"):
            configurable["user_id"] = str(uid)
        # 登记会话归属（grants 表 + metadata.owner；幂等）
        # inherited：子 run 的 metadata 常继承父 run 的 langfuse_user_id（真实用户），
        # 是 uid 被判成 internal 时的兜底归属来源。
        await self._apply_ownership(tid, str(uid or ""), str(merged.get("langfuse_user_id") or ""))

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
        # P2：记录会话用过的库（供 trace/报告库维度判定）。
        # ⚠️ 顺序契约：这里读的 db_name 必须是**钳制之后**的值。record_thread_db 写的
        # 是「本会话用过哪个库」，后续 trace/报告的库维度可见性判定读的就是这条记录——
        # 以前它在没有任何授权校验的情况下无条件写入，等于客户端能自己扩大可见范围。
        # 现在外部请求在 _clamp_body 里已过授权（未授权直接 403，走不到这里）。
        if db_name and tid:
            try:
                from agent.auth.grants import record_thread_db
                # P1-14：同 claim_thread —— 建 run 的请求都会走到这里
                await asyncio.to_thread(record_thread_db, tid, db_name)
            except Exception:  # noqa: BLE001
                _logger.debug("[langfuse_metadata] record_thread_db 失败", exc_info=True)
        # S5 数据漂移可观测化：数据快照（时间戳 + 库名）写入 trace metadata，
        # 供跨 run 比对「是否同一数据窗口」——离线评测按快照分组，指纹/时点不同
        # 不当作模型差异比较（同题多跑归因 S5）。
        if "data_snapshot" not in merged:
            try:
                from datetime import datetime, timezone
                merged["data_snapshot"] = {
                    "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "db_name": db_name or "",
                }
            except Exception:  # noqa: BLE001
                pass
        # 用户查询原文（供 Langfuse UI metadata 过滤 + evaluator 变量引用）
        if "user_question" not in merged:
            q = self._extract_question_summary(body, max_len=200)
            if q:
                merged["user_question"] = q

        # ── M5 灰度：当前进程的 prompt label/版本 + release（A→B 切换可见分组）──
        if "prompt" not in merged:
            try:
                merged["prompt"] = prompt_label_info()
            except Exception:  # noqa: BLE001
                pass
        rel = os.getenv("LANGFUSE_RELEASE", "")
        if rel and "langfuse_release" not in merged:
            merged["langfuse_release"] = rel

        configurable_changed = configurable != original_configurable
        if merged == metadata and not configurable_changed:
            return body

        config = {**config, "metadata": merged}
        if configurable_changed:
            config["configurable"] = configurable
        # 顶层 metadata 一并写入，与 config.metadata 保持一致（server 也读 payload.metadata）
        return {**body, "config": config, "metadata": merged}


# ── ASGI 辅助 ──────────────────────────────────────────────

def _scope_uid(scope: dict | None) -> str:
    """读 AuthMiddleware 注入的登录身份（scope["state"]["user"]["user_id"]）。"""
    if not scope:
        return ""
    state_user = (scope.get("state") or {}).get("user")
    if not isinstance(state_user, dict):
        return ""
    return str(state_user.get("user_id") or "")


def _scope_user(scope: dict | None) -> dict:
    """读 AuthMiddleware 注入的完整用户（含 is_admin）；无则空 dict。"""
    if not scope:
        return {}
    state_user = (scope.get("state") or {}).get("user")
    return state_user if isinstance(state_user, dict) else {}


# ── P1-2：run 入参授权钳制 ──────────────────────────────────

def _clamp_body(body, user: dict, tid: str) -> str:
    """钳制 run 请求体；返回非空串 = 403 原因。

    `/runs/batch` 的载荷是 **list**（RunBatchCreate = list[RunCreateStateless]），
    其余都是单个 dict，故这里统一按「逐个 dict 项」处理。非 dict 项跳过：它承载不了
    config，服务端自己会以 422 拒掉，这里不该替它回 403（会掩盖真实原因）。
    """
    items = body if isinstance(body, list) else [body]
    for item in items:
        if not isinstance(item, dict):
            continue
        err = _clamp_config(item, user, tid)
        if err:
            return err
    return ""


def _clamp_config(body: dict, user: dict, tid: str) -> str:
    """按**登录身份**钳制请求体里的 config.configurable；返回非空串 = 403 原因。

    钳制三件事（都被下游当真）：
    1. `user_id` / `langgraph_auth_user_id` → 强制改写成登录身份。不这么做的后果：
       伪造他人 user_id，`thinking_toggle` 会加载**那个人的**模型配置（含 api_key），
       子 agent 的 trace 也记到他人名下。
    2. `thread_id` / `checkpoint_id` / `checkpoint_ns` → 删除。它们是服务端按 URL
       路径写入的运行状态，客户端传值只可能是伪造（污染会话谱系/父子归属）。
    3. `db_name` → 必须在该用户授权库内，否则拒绝整个 run。**不能只清空**：
       `tool_filter._filter_tools` 里 `if not db_name: return tools`——空库名等于
       不裁剪，会把所有库的 wrenai 工具都暴露给模型，比越权更糟。
    """
    uid = str(user.get("user_id") or "")
    if not uid:
        return ""  # 无身份（理论上到不了：enforce 已判过 is_real_owner）

    config = body.get("config")
    if not isinstance(config, dict):
        config = {}
        body["config"] = config
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        configurable = {}
    config["configurable"] = configurable  # 恒回写：至少要把 user_id 交给下游

    forged = configurable.get("user_id")
    if forged is not None and str(forged) != uid:
        _logger.warning(
            "[langfuse_meta] 覆盖伪造 user_id: %r -> %s (thread=%s)",
            forged, uid, tid or "-",
        )
    configurable["user_id"] = uid
    # langgraph_auth_user_id 是同一身份的另一个别名（sync 循环补写、子 agent 读取），
    # 客户端传了就以登录身份覆盖；langgraph_auth_user（langgraph 内部 auth 对象）
    # 客户端根本无权提供，一律删掉。
    if "langgraph_auth_user_id" in configurable:
        configurable["langgraph_auth_user_id"] = uid
    configurable.pop("langgraph_auth_user", None)

    for k in _HANDS_OFF_KEYS:
        if k in configurable and str(configurable.get(k) or "") != tid:
            _logger.warning(
                "[langfuse_meta] 丢弃伪造 configurable.%s=%r (thread=%s)",
                k, configurable.get(k), tid or "-",
            )
            configurable.pop(k, None)

    db_name = str(configurable.get("db_name") or "").strip()
    if not db_name:
        return ""  # 未选库：前端可空（localStorage 未选过），不是越权
    if (os.getenv(_SKIP_DB_AUTHZ_ENV, "0") or "").strip() in ("1", "true", "yes", "on"):
        _logger.warning("[langfuse_meta] %s=1，跳过库授权校验（db_name=%s）", _SKIP_DB_AUTHZ_ENV, db_name)
        return ""
    from agent.auth.grants import can_access_db

    if not can_access_db(user, db_name):
        return f"无权访问数据库: {db_name}"
    return ""


async def _read_body(receive) -> tuple[bytes, dict | None]:
    """读取请求体；返回 (原始字节, 解析后的 dict)。

    原始字节一并返回，是为了「非 JSON / 读失败」时能把 body 原样回放给下游——
    receive 是一次性的，读过就没了。
    """
    chunks = []
    while True:
        msg = await receive()
        if msg["type"] == "http.disconnect":
            return b"", None
        if msg["type"] != "http.request":
            continue
        chunks.append(msg.get("body", b""))
        if not msg.get("more_body"):
            break
    raw = b"".join(chunks)
    if not raw:
        return raw, {}
    try:
        return raw, json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return raw, None


async def _send_json(send, status: int, payload: dict) -> None:
    """直接回一个 JSON 响应（中间件层拒绝请求用）。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})


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
