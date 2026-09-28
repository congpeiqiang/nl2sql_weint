from datetime import timedelta
from pathlib import Path

import httpx
from langchain.chat_models import init_chat_model
from langchain_openai import ChatOpenAI
from opensandbox.config import ConnectionConfigSync

from agent.env_utils import (
    DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL,
    ZHIPU_API_KEY, ZHIPU_BASE_URL,
)

# ---------- 模型配置 ----------
# 主 Agent 模型
MAIN_MODEL = ChatOpenAI(
    model="deepseek-v4-pro",
    temperature=1.1,
    openai_api_key=DEEPSEEK_API_KEY,
    openai_api_base=DEEPSEEK_BASE_URL,
    max_tokens=2560000,
    model_kwargs={
        "extra_body": {
            "thinking": {"type": "disabled"}
        }
    }
)
# 摘要专用模型（摘要需要稳定输出，temperature 设为较低值）
SUMMARY_MODEL = ChatOpenAI(
    model="deepseek-v4-flash",
    temperature=0.3,
    openai_api_key=DEEPSEEK_API_KEY,
    openai_api_base=DEEPSEEK_BASE_URL,
    max_tokens=2560000,
    model_kwargs={
        "extra_body": {
            "thinking": {"type": "disabled"}
        }
    }
)
# 备用模型（当主模型故障时使用）
# 注意: GLM 是智谱的模型，使用智谱的 base_url
FALLBACK_MODEL = init_chat_model(
    "glm-5.1",
    model_provider="openai",
    temperature=1.0,
    base_url=ZHIPU_BASE_URL,
    api_key=ZHIPU_API_KEY,
    profile={
        "max_input_tokens": 128000,
        "max_output_tokens": 8192,
        "tool_calling": True,
        "structured_output": True,
    }
)

# ---------- 沙箱配置 ----------
# OpenSandbox 沙箱配置连接
SANDBOX_CONFIG = ConnectionConfigSync(
    domain="http://39.100.100.28:8080",
    use_server_proxy=True,
    request_timeout=timedelta(seconds=60),
    transport=httpx.HTTPTransport(limits=httpx.Limits(max_connections=20)),
)

# ---------- 路径常量 ----------
EXAMPLE_DIR = Path(__file__).parent.parent
print(f'当前代码执行的工作目录为：{EXAMPLE_DIR}')
# 沙箱内技能根路径
SANDBOX_SKILLS_ROOT = "/skills"
# 沙箱内记忆根路径（用户私有记忆存放处）
SANDBOX_MEMORIES_ROOT = "/memories"
# 沙箱内分析中间文件存放目录
SANDBOX_ANALYSIS_ROOT = "/analysis"
# 沙箱内数据文件存放目录
SANDBOX_DATA_ROOT = "/data"
# 本地技能资源目录（项目内的路径，相对于项目根）
LOCAL_SKILLS_DIR = EXAMPLE_DIR / "skills"
# 本地下载目录（从沙箱下载文件的目标路径）
DOWNLOAD_DIR = EXAMPLE_DIR / "download"
# 本地子 Agent 配置目录
LOCAL_SUBAGENT_CONFIG_DIR = EXAMPLE_DIR / "agent/subagents"
# 本地的Agent记忆文件
LOCAL_AGENTS_MD = EXAMPLE_DIR / "agent/memory/AGENTS.md"

# ---------- 文件名常量 ----------
# 主 Agent 只读指引文件（上传到沙箱 /AGENTS.md）
AGENTS_MD_FILENAME = "/AGENTS.md"
# 用户偏好文件名（在 /memories/{user_id}/ 下）
USER_PREFERENCES_FILENAME = "preferences.md"

# ---------- 用户技能持久化 ----------
# 技能持久化 StoreBackend 路由路径
PERSISTED_SKILLS_ROOT = "/persisted-skills"
# 技能 StoreBackend 命名空间（按 Agent scope 组织，无用户隔离）
SKILLS_STORE_NAMESPACE = ("skills",)
# 子 Agent 名称 → 技能 scope 目录映射
SCOPE_MAP = {
    "main": "main",
    "procurement-analyst": "procurement",
    "procurement-order": "order",
}

# ---------- 中间件参数 ----------

# ---------- 已删除：MongoDB 配置 / MongoDBSaver / InMemoryStore（2026-09-24，P2-10 收尾） ----------
# 原样（`MONGODB_URI` / `STORE` / `CHECKPOINTER`）在本仓**零引用**，且副作用是**导入即执行**：
#   ① `MongoClient(MONGODB_URI)` + `MongoDBSaver(...)` 在模块导入时就把一个**硬编码凭据**
#      （`mongodb://root:123456@39.100.100.28/...`）指向外网主机 —— 而 src/ 是整包发版内容，
#      等于把凭据随发行包发出去；
#   ② `pymongo` / `langgraph.checkpoint.mongodb` **不在 `pyproject.toml`** ⇒ 这两个 import
#      在任何环境都必然 ModuleNotFoundError ⇒ **`import agent.config` 目前是坏的**
#      （唯一消费者是 `agent/backends/sandbox_setup.py`，而它按本仓设计文档是 E3 未接线死代码，
#      故一直没有暴露出来）。
# checkpointer 的真实入口是 `agent/checkpoint/checkpointer_factory.py`（经
# `LANGGRAPH_CHECKPOINTER` 加载，见 `main_agent.py:225`）——**别再往本文件加回来**。
#
# 附带说明（本文件整体已死，不必去修）：`from agent.env_utils import (...)` 指向的
# `src/agent/env_utils.py` **不存在**（只有这一处引用）⇒ `import agent.config` 在删除上面
# 那两条 Mongo import 之前就已经是 ModuleNotFoundError，与本次改动无关；
# 本文件也**没有任何真实消费者**（唯一 importer 是 E3 死代码 sandbox_setup.py）。
# 若要整文件删除，需先确认没有按字符串路径加载它（如 langgraph 的
# `LANGGRAPH_CHECKPOINTER`/自定义 app 钩子那类字符串入口）—— 见清单 P2-10 收尾项。


