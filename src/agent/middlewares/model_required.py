"""ModelRequiredMiddleware — 账号未配置大模型时明确报错，**不回落**共享模型。

需求（2026-09-28）：新建账号（**含新建的管理员账号**）不再继承共享 `model_config.json`
—— `model_config_store.get_user_store` 里那段「首次从共享整份拷贝」已删除，新账号
必须自己到「设置 → 模型」新增接入点。

但**只删播种不够**：图节点的默认模型是**模块级** `deepseek_model`
（`llms/model.py` 的 `create_model()` 不带 `user_id` ⇒ 读全局/共享 store），而
`ThinkingToggleMiddleware._maybe_swap` 在 `create_model` 返回 None 时 `return request`
⇒ 保留那个共享模型。于是"没配模型的账号"会**静默地拿别人的 key 打模型**（额度记在
别人名下）—— 这正是本次要根除的继承。前端 composer 虽有门禁
（`ChatInterface.tsx` 的 `modelConfigured`），但子任务完成的自动续跑、离线实验等
入口绕得过它，所以必须在服务端 fail-closed。

拦截方式：**图的最外层**，有登录身份但该账号没有可用模型时直接返回一条友好中文
AIMessage，**连 handler 都不调用**（不构造模型实例、不发任何请求）。范式与
`QuotaErrorMiddleware` 完全一致（不抛异常 ⇒ agent 正常结束、消息进 checkpoint、
前端按普通 AI 回复渲染，界面不卡不空转）。

判据只有一份：`agent.llms.model.has_usable_model(user_id)`（与 `create_model`
同一条解析链），判据函数内部已吞掉一切读取异常（→ 视为「无模型」= fail-closed）。

**无「真实用户」身份时不拦**（维持旧行为）：AUTH_DISABLED 的本地开发（身份是 `dev`
哨兵）、不带真实用户的内部自调用（`internal`）、离线实验（`run_experiment` 的
configurable 不带 user_id，且其端点全是 `require_admin`）、Langfuse 侧 evaluator 等
无身份批处理面，仍读全局 store。判定用 `auth/ownership.is_real_owner`（同一份口径，
**不是**自写的 `if not user_id` —— `dev`/`internal` 是非空串，那样写会误拦）。

⚠️ 本中间件依赖 `configurable.user_id` **就是登录身份**：该键由
`api/langfuse_metadata.LangfuseMetadataMiddleware` 强制改写（P1-2，伪造无效），
且异步子 agent（`deepagents_async_config_patch`）与同步续跑（`sync_subagent_todos`）
都会把它透传下来 ⇒ 子 agent 同样拦得住。
"""
from __future__ import annotations

import logging
from typing import Any, Callable, TypeVar

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage

_logger = logging.getLogger(__name__)

ContextT = TypeVar("ContextT")
ResponseT = TypeVar("ResponseT")

# ── 友好提示（前端直接展示；措辞要能区分"没配"与"配了但没配全"）──────
NO_MODEL_MESSAGE = (
    "当前账号尚未配置可用的大模型，无法执行。模型配置按账号独立（新账号不继承其他账号的配置），"
    "请点击右上角「设置 → 模型」新增一个接入点：填好 base_url、自己的 API Key，"
    "并至少添加一个模型，然后重新发送。"
    "（若已添加过，请检查它还缺 API Key / base_url，或模型列表为空。）"
)


def current_user_id() -> str:
    """读当前 run 的登录身份（`configurable.user_id`）。读不到返回空串。

    ⚠️ 必须走 `langgraph.config.get_config()`：`request.runtime.config` **恒为空**
    （与 `thinking_toggle._resolve_overrides`、`QueryKeywordsMiddleware` 同一个实证结论）。

    `langgraph_auth_user_id` 是同一身份的另一个别名（sync 循环补写、子 agent 读取），
    取不到 `user_id` 时兜底读它。
    """
    try:
        from langgraph.config import get_config

        cfg = get_config()
        configurable = (cfg.get("configurable") or {}) if cfg else {}
    except Exception:  # noqa: BLE001  不在 run 上下文 / 取不到配置 → 视为无身份
        return ""
    uid = configurable.get("user_id") or configurable.get("langgraph_auth_user_id")
    return str(uid) if uid else ""


def is_blocked_for(user_id: str) -> bool:
    """身份已知时判断是否应当拦下（无模型 → True）。判据唯一来源见模块 docstring。

    ⚠️ **只有真实用户**才拦：`internal` / `dev` 是哨兵不是账号 —— `NL2SQL_AUTH_DISABLED=1`
    的本地开发把身份写成 `"dev"`（AuthMiddleware dev 旁路 → langfuse_metadata 覆盖式写入
    `configurable.user_id`），按账号读会**一个模型都没有**、单机开发直接不可用；内部自调用
    （不带真实用户的 sync / 索引）同理。这两类维持旧行为（读全局 store）。
    判定复用 `auth/ownership.is_real_owner`（与归属补打同一份口径，别另写一份黑名单）。
    """
    from agent.auth.ownership import is_real_owner

    if not user_id or not is_real_owner(user_id):
        return False
    try:
        from agent.llms.model import has_usable_model

        return not has_usable_model(user_id)
    except Exception as e:  # noqa: BLE001
        # 读配置炸了：宁可挡住也不能回落别人的模型（隔离优先）。
        # 生产上这条几乎不会走到 —— has_usable_model 内部已把所有读取异常吞成"空配置"。
        _logger.error("[ModelRequired] 判据异常，按「无模型」拦下（不回落共享配置）: %s", e)
        return True


def _friendly_response() -> ModelResponse:
    """构造一条带友好文案的 AIMessage 作为模型响应。

    与 `quota_error._friendly_response` 同款：AIMessage **不带 tool_calls** ⇒
    模型节点后直接走 END，不触发工具节点。返回值即「模型回复」，由 run 正常收尾。
    """
    return ModelResponse(
        result=[AIMessage(content=NO_MODEL_MESSAGE)],
        structured_response=None,
    )


class ModelRequiredMiddleware(AgentMiddleware[ContextT, ResponseT]):
    """账号无可用模型 → 友好提示（不调用 handler，绝不回落共享模型）。"""

    # ── 供验收脚本直接调（不必搭 ModelRequest）────────────────
    def is_blocked(self) -> bool:
        """当前 run 是否应被拦下。"""
        return is_blocked_for(current_user_id())

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        """同步模型调用：账号无模型 → 友好 AIMessage；否则原样交给内层。"""
        if self.is_blocked():
            _logger.warning(
                "[ModelRequired] 账号未配置可用模型，拒绝执行（不回落共享配置）"
            )
            return _friendly_response()
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Any],
    ) -> ModelResponse[ResponseT]:
        """异步模型调用（生产走这条）：账号无模型 → 友好 AIMessage；否则原样交给内层。"""
        if self.is_blocked():
            _logger.warning(
                "[ModelRequired] 账号未配置可用模型，拒绝执行（不回落共享配置）"
            )
            return _friendly_response()
        result = handler(request)
        if hasattr(result, "__await__"):
            result = await result
        return result
