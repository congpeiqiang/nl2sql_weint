r"""运行时猴子补丁集中入口——替代散落在各模块的 import 副作用。

为什么需要集中：这 4 个补丁原先靠 `main_agent.py` 顶部的 `# noqa: F401` import
触发（"必须在 create_deep_agent 之前导入"）。副作用式 import 有 3 个问题：

  1. **顺序不可见**：读 `main_agent.py` 看不出这几行是必需的、更看不出顺序敏感。
  2. **触发点耦合**：换个入口（`langgraph dev` / 测试 / 脚本）就静默漏掉补丁。
  3. **失败无声**：补丁 import 失败 = 整个 agent 起不来，但错误在 import 链深处。

现在：`start_server.py` 在 `setup_environment()` 之后显式调 `apply_patches()`。
`main_agent.py` 仍保留一次调用作为兜底（走 `langgraph dev` 等非 start_server 入口时）——
`apply_patches()` 是幂等的（见下方 `_applied` 守卫），重复调用无副作用。
"""
import logging

_logger = logging.getLogger(__name__)

_applied = False


def apply_patches() -> bool:
    """应用所有运行时猴子补丁。幂等——重复调用只生效一次。

    返回 True 表示本次调用真正执行了补丁（用于日志/测试断言）。

    顺序敏感：
      ① `check_progress`：增强 check_async_task 返回每步耗时。
         **必须在 create_deep_agent 之前** —— 它 patch 的是 deepagents 的工具构建。
      ② `sync_launcher`：patch start_async_task，子 agent 创建后自动起 todos 同步线程。
         同样依赖 ① 之后、agent 构建之前。
      ③ `deepagents_async_config_patch`：透传父 run 的 configurable（db_name/thread_id）
         到异步子 agent run。patch 的是 runs.create，与 agent 构建顺序无关，但要在
         子 agent 首次启动前。
      ④ `filesystem_backend_patch`：修 Windows/Py3.13 下 `\\?\` 前缀误报越界。
         patch 的是 FilesystemBackend 的路径解析 —— **必须早于任何 backend 实例化**
         （main_agent 模块级就建了 composite_backend，故这条最靠前才安全；
         实际它在 main_agent 里排第 4 位仍成立，因为 backend 实例化在文件更下方）。
    """
    global _applied
    if _applied:
        return False

    # ① check_async_task 增强（含每步耗时）
    import agent.subagents.check_progress  # noqa: F401
    _logger.info("[bootstrap] patch applied: check_progress")

    # ② 子 agent todos 自动同步
    import agent.subagents.sync_launcher  # noqa: F401
    _logger.info("[bootstrap] patch applied: sync_launcher")

    # ③ configurable 透传到异步子 agent
    import agent.middlewares.deepagents_async_config_patch  # noqa: F401
    _logger.info("[bootstrap] patch applied: deepagents_async_config_patch")

    # ④ Windows `\\?\` 前缀修复
    import agent.utils.filesystem_backend_patch  # noqa: F401
    _logger.info("[bootstrap] patch applied: filesystem_backend_patch")

    _applied = True
    _logger.info("[bootstrap] 全部 4 个运行时补丁已应用")
    return True


def patches_applied() -> bool:
    """补丁是否已应用（供测试与预检断言）。"""
    return _applied