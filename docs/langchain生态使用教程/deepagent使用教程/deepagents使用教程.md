# DeepAgents 使用教程

> 基于 deepagents 0.6.12 源码通读编写，覆盖全部公开 API 与关键内部机制。

---

## 目录

- [一、架构总览](#一架构总览)
- [二、核心入口：create_deep_agent](#二核心入口create_deep_agent)
- [三、内置中间件栈](#三内置中间件栈)
- [四、后端体系（Backend）](#四后端体系backend)
- [五、文件系统与权限](#五文件系统与权限)
- [六、子代理（SubAgents）](#六子代理subagents)
- [七、异步后台任务（AsyncSubAgent）](#七异步后台任务asyncsubagent)
- [八、记忆与技能（Memory & Skills）](#八记忆与技能memory--skills)
- [九、上下文压缩（Summarization）](#九上下文压缩summarization)
- [十、自我评估迭代（Rubric）](#十自我评估迭代rubric)
- [十一、Profile 体系](#十一profile-体系)
- [十二、State 管理](#十二state-管理)
- [十三、自定义中间件编写](#十三自定义中间件编写)
- [十四、实战：nl2sql 项目架构](#十四实战nl2sql-项目架构)
- [十五、常见陷阱与最佳实践](#十五常见陷阱与最佳实践)
- [十六、结构化输出（Structured Output）](#十六结构化输出structured-output)
- [十七、废弃时间线](#十七废弃时间线)

---

## 一、架构总览

### 1.1 DeepAgents 是什么

DeepAgents 是构建在 LangGraph + LangChain 之上的**自治 Agent 框架**。它在 `langchain.agents.create_agent` 的基础上封装了一套完整的生产级 Agent 运行时，包括：

- **文件系统工具集**（ls/read/write/edit/glob/grep/execute）
- **上下文自动压缩**（摘要 + 历史落盘 + 工具参数截断）
- **子代理调度**（同步 + 异步，隔离执行）
- **记忆与技能注入**（AGENTS.md / SKILL.md 规范）
- **模型 Profile 系统**（按模型自动调优提示词、工具、中间件）
- **权限控制**（文件读写 + 命令执行的 allow/deny/interrupt）

### 1.2 分层架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                       用户代码层                                      │
│  create_deep_agent(model, tools, subagents, middleware, ...)        │
└─────────────────────────────────┬───────────────────────────────────┘
                                  │
┌─────────────────────────────────▼───────────────────────────────────┐
│                     中间件栈（Middleware Stack）                       │
│  ┌─ 基础栈 ──────────────────────────────────────────────────────┐  │
│  │ TodoList → Skills → Filesystem → SubAgent → Summarization   │  │
│  │ → PatchToolCalls → AsyncSubAgent                             │  │
│  └───────────────────────────────────────────────────────────────┘  │
│  ┌─ 用户自定义中间件 ─────────────────────────────────────────────┐  │
│  │ 你的中间件按传入顺序插入                                        │  │
│  └───────────────────────────────────────────────────────────────┘  │
│  ┌─ 尾部栈 ──────────────────────────────────────────────────────┐  │
│  │ Profile Extra → ToolExclusion → PromptCaching               │  │
│  │ → Memory → HumanInTheLoop                                    │  │
│  └───────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────┬───────────────────────────────────┘
                                  │
┌─────────────────────────────────▼───────────────────────────────────┐
│                     后端体系（Backend）                                │
│  CompositeBackend                                                    │
│  ├── /shared/memory/  → FilesystemBackend (read-only)               │
│  ├── /shared/skills/  → FilesystemBackend (read-only)               │
│  ├── /workspace/      → FilesystemBackend (read-write)              │
│  ├── /memories/       → StoreBackend (persistent)                   │
│  └── default          → LocalShellBackend / StateBackend            │
└─────────────────────────────────┬───────────────────────────────────┘
                                  │
┌─────────────────────────────────▼───────────────────────────────────┐
│                     Profile 体系                                      │
│  HarnessProfile（提示词/工具/中间件/子代理调优）                        │
│  ProviderProfile（模型构造参数/版本检查）                              │
└─────────────────────────────────┬───────────────────────────────────┘
                                  │
┌─────────────────────────────────▼───────────────────────────────────┐
│              LangGraph CompiledStateGraph                            │
│  recursion_limit=9999 · DeltaChannel(snapshot=50)                   │
│  LangSmith tracing · Checkpointer · Store                           │
└─────────────────────────────────────────────────────────────────────┘
```

### 1.3 与 langchain.agents.create_agent 的关系

```
langchain.agents.create_agent          ← 底层：模型 + 工具 + 中间件
    ↑
deepagents.create_deep_agent           ← 上层：自动组装中间件栈 + 后端 + Profile
```

`create_deep_agent` 最终调用 `langchain.agents.create_agent`，但在那之前它做了大量的自动化组装工作。

---

## 二、核心入口：create_deep_agent

### 2.1 函数签名

```python
from deepagents import create_deep_agent

agent = create_deep_agent(
    model,                          # str | BaseChatModel | None
    tools,                          # Sequence[BaseTool | Callable | dict] | None
    *,                              # ← 以下全部 keyword-only
    system_prompt=None,             # str | SystemMessage | None
    middleware=(),                  # Sequence[AgentMiddleware]
    subagents=None,                 # Sequence[SubAgent | CompiledSubAgent | AsyncSubAgent]
    skills=None,                    # list[str] — POSIX 路径
    memory=None,                    # list[str] — AGENTS.md 路径
    permissions=None,               # list[FilesystemPermission]
    backend=None,                   # BackendProtocol | BackendFactory | None → StateBackend()
    interrupt_on=None,              # dict[str, bool | InterruptOnConfig]
    response_format=None,           # ResponseFormat | type | dict
    state_schema=None,              # type[DeepAgentState] | None
    context_schema=None,            # type[ContextT] | None
    checkpointer=None,              # Checkpointer | None
    store=None,                     # BaseStore | None
    debug=False,                    # bool
    name=None,                      # str | None
    cache=None,                     # BaseCache | None
)
```

### 2.2 参数详解

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `model` | `str \| BaseChatModel \| None` | `None` → `claude-sonnet-4-6`（已废弃，应显式指定） | 模型标识，格式 `provider:model`；或预初始化的 `BaseChatModel` 实例 |
| `tools` | `Sequence[BaseTool \| Callable \| dict] \| None` | `None` | 自定义工具（**累加**到内置工具上，不替换） |
| `system_prompt` | `str \| SystemMessage \| None` | `None` | 用户系统提示（放在最前面，优先级最高）。传 `SystemMessage` 保留 `cache_control` 标记 |
| `middleware` | `Sequence[AgentMiddleware]` | `()` | 自定义中间件（插入基础栈和尾部栈之间） |
| `subagents` | `Sequence[SubAgent \| CompiledSubAgent \| AsyncSubAgent] \| None` | `None` | 子代理定义（同步/编译/异步三种形式） |
| `skills` | `list[str]` | `None` | SKILL.md 目录路径（相对于 backend 根目录） |
| `memory` | `list[str]` | `None` | AGENTS.md 文件路径 |
| `permissions` | `list[FilesystemPermission]` | `None` | 文件权限规则（first-match-wins） |
| `backend` | `BackendProtocol \| BackendFactory` | `None` → `StateBackend()` | 文件存储后端（支持工厂函数） |
| `interrupt_on` | `dict[str, bool \| InterruptOnConfig]` | `None` | 工具审批配置（tool_name → 是否暂停等人工审批） |
| `response_format` | `ResponseFormat \| type \| dict` | `None` | 结构化输出格式（详见§十六）。传 Pydantic/dataclass/TypedDict/JSON Schema，或 `ToolStrategy`/`ProviderStrategy`/`AutoStrategy` 实例 |
| `state_schema` | `type[DeepAgentState]` | `DeepAgentState` | 自定义状态 schema（必须继承 `DeepAgentState`，保留 DeltaChannel reducer） |
| `context_schema` | `type[ContextT] \| None` | `None` | 运行期不可变上下文 schema，透传给 `create_agent` |
| `checkpointer` | `Checkpointer \| None` | `None` | 状态持久化（同 thread 跨 run 可见），透传给 `create_agent` |
| `store` | `BaseStore \| None` | `None` | 持久存储（`StoreBackend` 需要此参数），透传给 `create_agent` |
| `debug` | `bool` | `False` | 调试模式，透传给 `create_agent` |
| `name` | `str \| None` | `None` | Agent 名称（写入 LangSmith metadata） |
| `cache` | `BaseCache \| None` | `None` | LLM 调用缓存，透传给 `create_agent` |

### 2.3 内部执行流程

```
create_deep_agent() 内部 10 步：

1. 模型解析
   └─ str → resolve_model(model) → BaseChatModel 实例

2. Harness Profile 查找
   └─ _harness_profile_for_model(model) → 模型专属配置

3. 验证 excluded_middleware
   └─ 不允许排除 FilesystemMiddleware / SubAgentMiddleware

4. 工具描述覆盖
   └─ _apply_tool_description_overrides(tools, profile.overrides)

5. 后端默认
   └─ backend=None → StateBackend()

6. 子代理处理
   ├─ 分类：AsyncSubAgent / CompiledSubAgent / SubAgent
   ├─ 对每个 SubAgent：
   │   ├─ 解析模型 + 子代理 profile
   │   ├─ 解析权限（子代理覆盖父代理）
   │   ├─ 构建子代理中间件栈
   │   ├─ 应用 excluded_middleware
   │   └─ 继承/覆盖工具
   └─ 自动添加 general-purpose 子代理（除非禁用）

7. 组装主中间件栈（最多 13 层，严格顺序）

8. 系统提示组装
   └─ USER → (BASE 或 CUSTOM) → SUFFIX

9. 调用 langchain.agents.create_agent(...)

10. 应用 .with_config(recursion_limit=9999, metadata={...})
```

### 2.4 模型解析机制（resolve_model）

`model` 参数接受字符串或预初始化实例，内部通过 `resolve_model()` 统一转换：

```
输入                        处理路径                           输出
─────────────────────────────────────────────────────────────────────
BaseChatModel 实例    →    直接返回（不做任何转换）          → BaseChatModel
"openai:gpt-4o"       →    apply_provider_profile("openai")  → init_chat_model(
                               → {"use_responses_api": True}      "openai:gpt-4o",
                           init_chat_model(...)                     use_responses_api=True)
"openrouter:..."      →    check_openrouter_version()        → init_chat_model(
                           + 注入 attribution headers              ..., app_title=...)
                           + Azure 后端过滤
"anthropic:..."       →    无额外 profile kwargs             → init_chat_model(...)
```

**Provider 归一化规则：**
- 小写化 + 连字符转下划线
- 别名映射：`azure_openai` → `azure`、`mistralai` → `mistral`
- Bedrock 别名：`amazon_bedrock` / `anthropic_bedrock` / `aws` / `bedrock` / `bedrock_converse` 统一识别

**Bedrock Nova 模型检测：**
- 通过地域前缀识别：`apac.` / `amer.` / `au.` / `eu.` / `global.` / `jp.` / `sa.` / `us.` / `us-gov.` + `amazon.nova-`

**辅助函数：**

| 函数 | 作用 |
|---|---|
| `get_model_identifier(model)` | 从 `model_name` 或 `model` 属性提取模型名 |
| `get_model_provider(model)` | 通过 `_get_ls_params()` 获取 `ls_provider` |
| `model_matches_spec(model, spec)` | 判断模型实例是否匹配 `provider:model` 规格 |
| `is_bedrock_model(model)` | 通过 provider 或类名检测是否为 Bedrock 模型 |

### 2.5 最小使用示例

```python
from deepagents import create_deep_agent

# 最简 Agent（使用 StateBackend，所有文件存在 state 中）
agent = create_deep_agent(
    model="openai:gpt-4o",
    system_prompt="你是一个有帮助的助手。",
)

# 调用
result = agent.invoke(
    {"messages": [{"role": "user", "content": "帮我写个 Python 脚本"}]},
    config={"configurable": {"thread_id": "thread-1"}},
)
```

### 2.6 完整配置示例

```python
from deepagents import (
    create_deep_agent, SubAgent, AsyncSubAgent,
    FilesystemMiddleware, FilesystemPermission,
)
from deepagents.backends import CompositeBackend, FilesystemBackend, LocalShellBackend
from langchain.agents.middleware import ModelRetryMiddleware

# 1. 构建后端
shell_backend = LocalShellBackend(
    root_dir="/home/user/project",
    inherit_env=True,
)
backend = CompositeBackend(
    default=shell_backend,
    routes={
        "/memories/": store_backend,     # 持久记忆
        "/shared/": shared_backend,      # 共享资源
    },
)

# 2. 定义子代理
researcher = SubAgent(
    name="researcher",
    description="专门搜索和整理资料",
    system_prompt="你是研究员，负责搜索信息并整理为结构化报告。",
    tools=[search_tool, web_tool],
)

# 3. 权限规则
permissions = [
    FilesystemPermission(paths="*.env", mode="deny"),
    FilesystemPermission(paths="/etc/**", mode="deny"),
    FilesystemPermission(paths="**/*", mode="allow"),
]

# 4. 创建 Agent
agent = create_deep_agent(
    model="anthropic:claude-sonnet-4-6",
    tools=[custom_tool_1, custom_tool_2],
    system_prompt="你是一个高级 AI 助手，擅长数据分析和代码编写。",
    subagents=[researcher],
    skills=["/shared/skills/"],
    memory=["/shared/AGENTS.md"],
    permissions=permissions,
    backend=backend,
    middleware=[ModelRetryMiddleware(max_retries=2)],
    name="data-analyst",
    checkpointer=checkpointer,
)

# 5. 调用
async for event in agent.astream(
    {"messages": [{"role": "user", "content": "分析上月销售数据"}]},
    config={"configurable": {"thread_id": "analysis-001"}},
    stream_mode=["updates", "messages"],
):
    print(event)
```

---

## 三、内置中间件栈

`create_deep_agent` 自动组装最多 13 层框架中间件（含条件加载项），用户自定义中间件插入第 7 层与第 8 层之间：

```
位置  中间件                         条件                    说明
─────────────────────────────────────────────────────────────────────────
 1   TodoListMiddleware             始终                    任务规划
 2   SkillsMiddleware               skills 不为 None        技能注入
 3   FilesystemMiddleware           始终（受保护）           文件工具集
 4   SubAgentMiddleware             有同步子代理时（受保护）  子代理调度
 5   SummarizationMiddleware        始终                    上下文压缩
 6   PatchToolCallsMiddleware       始终                    悬挂调用修补
 7   AsyncSubAgentMiddleware        有异步子代理时          后台任务
 ─── 用户自定义 middleware 插入此处 ───
 8   Profile extra_middleware       Profile 定义时          Profile 扩展
 9   _ToolExclusionMiddleware       Profile 排除工具时      工具过滤
10   AnthropicPromptCaching         始终                    Anthropic 缓存
11   BedrockPromptCaching           langchain-aws 已装时    Bedrock 缓存
12   MemoryMiddleware               memory 不为 None        记忆注入
13   HumanInTheLoopMiddleware       interrupt_on 不为 None  人机协同
```

### 排序设计原则

| 原则 | 说明 |
|---|---|
| Profile extra 在 Memory 之前 | Memory 变化不破坏 Anthropic 缓存前缀 |
| PromptCaching 靠近尾部 | 包裹最终系统提示内容 |
| HITL 最内层 | 拦截所有工具调用，在所有其他中间件之后 |
| Filesystem/SubAgent 受保护 | Profile 不能排除这两个基础设施中间件 |

### 内置工具清单

由中间件自动注入的工具：

| 工具 | 来源 | 说明 |
|---|---|---|
| `write_todos` | TodoListMiddleware | 任务规划 |
| `ls` | FilesystemMiddleware | 目录列表 |
| `read_file` | FilesystemMiddleware | 文件读取（支持分页、多模态） |
| `write_file` | FilesystemMiddleware | 文件创建 |
| `edit_file` | FilesystemMiddleware | 字符串替换编辑 |
| `glob` | FilesystemMiddleware | 文件模式匹配 |
| `grep` | FilesystemMiddleware | 内容搜索 |
| `execute` | FilesystemMiddleware | Shell 命令（需 SandboxBackend） |
| `task` | SubAgentMiddleware | 启动子代理 |
| `compact_conversation` | SummarizationToolMiddleware | 手动压缩 |
| `start_async_task` | AsyncSubAgentMiddleware | 启动后台任务 |
| `check_async_task` | AsyncSubAgentMiddleware | 检查任务状态 |
| `update_async_task` | AsyncSubAgentMiddleware | 更新任务 |
| `cancel_async_task` | AsyncSubAgentMiddleware | 取消任务 |
| `list_async_tasks` | AsyncSubAgentMiddleware | 列出任务 |

### 内置文件工具参数详解

#### ls — 目录列表

```python
ls(path: str)  # 绝对路径，返回目录条目列表
```

#### read_file — 文件读取

```python
read_file(
    file_path: str,        # 绝对路径
    offset: int = 0,       # 起始行号（0-indexed）
    limit: int = 100,      # 最多读取行数
)
```

**特殊行为：**
- 超过 5000 字符的行自动拆分，带续行标记（5.1, 5.2, ...）
- 图片/音频/视频/PDF 自动转为多模态内容块（base64 + mime_type）

#### write_file — 文件创建

```python
write_file(
    file_path: str,        # 绝对路径（新文件）
    content: str,          # 文本内容
)
```

#### edit_file — 字符串替换编辑

```python
edit_file(
    file_path: str,        # 绝对路径
    old_string: str,       # 要替换的文本（必须唯一匹配，除非 replace_all=True）
    new_string: str,       # 替换为
    replace_all: bool = False,
)
```

#### glob — 文件模式匹配

```python
glob(
    pattern: str,          # glob 模式（如 **/*.py）
    path: str | None = None,  # 搜索基目录（默认 backend 根目录）
)
# 超时：20 秒；使用 wcmatch（BRACE | GLOBSTAR）
```

#### grep — 内容搜索

```python
grep(
    pattern: str,          # 搜索文本（字面量，非正则）
    path: str | None = None,    # 搜索目录
    glob: str | None = None,    # 文件过滤（如 *.py）
    output_mode: Literal[
        "files_with_matches",  # 默认：只返回文件名
        "content",            # 返回匹配行内容
        "count",              # 返回匹配计数
    ] = "files_with_matches",
)
```

#### execute — Shell 命令

```python
execute(
    command: str,             # Shell 命令
    timeout: int | None = None,  # 超时（秒）
)
# 约束：
# - timeout 必须非负
# - 不得超过 max_execute_timeout（默认 3600s / 1 小时）
# - timeout=0 表示无超时（仅限支持的后端）
# - 仅当 backend 实现 SandboxBackendProtocol 时可用
```

### 大结果驱逐机制（Result Eviction）

`FilesystemMiddleware` 内置两层自动驱逐，防止长输出撑爆上下文：

**第一层：工具结果驱逐（tool_token_limit_before_evict）**

| 参数 | 默认值 | 说明 |
|---|---|---|
| `tool_token_limit_before_evict` | `20000`（token） | 工具输出超过此阈值时驱逐 |
| 触发条件 | `len(content) > 4 × limit`（字符数） | 按 4 字符 ≈ 1 token 估算 |

```
触发时：
1. 工具结果写入 backend 文件（{large_tool_results_prefix}/{tool_call_id}）
2. 对话中替换为截断预览 + 文件路径引用
3. Agent 可通过 read_file 按需读取完整内容

豁免工具（不驱逐）：
- ls / glob / grep — 内置截断
- read_file — 已有分页机制
- write_file / edit_file — 输出极小
```

**第二层：HumanMessage 驱逐（human_message_token_limit_before_evict）**

| 参数 | 默认值 | 说明 |
|---|---|---|
| `human_message_token_limit_before_evict` | `50000`（token） | 用户消息超过此阈值时驱逐 |

```
触发时：
1. 仅检查最近一条 HumanMessage（且未被标记过）
2. 内容写入 {conversation_history_prefix}/{uuid}.md
3. 消息标记 additional_kwargs["lc_evicted_to"] = file_path
4. 后续模型调用时：替换为头尾截断预览，非文本块（图片）保留
5. 完整内容仍在 state 中，仅模型视角被截断
```

---

## 四、后端体系（Backend）

### 4.1 后端继承关系

```
BackendProtocol (protocol.py)          ← 所有后端的抽象基类
├── StateBackend (state.py)            ← 临时，存在 LangGraph state 中
├── FilesystemBackend (filesystem.py)  ← 直接读写宿主机文件系统
│   └── LocalShellBackend (local_shell.py) ← + Shell 命令执行
├── StoreBackend (store.py)            ← 持久，跨线程，LangGraph BaseStore
├── ContextHubBackend (context_hub.py)  ← 持久，远程，LangSmith Hub
│
SandboxBackendProtocol (protocol.py)   ← BackendProtocol + execute()
└── BaseSandbox (sandbox.py)           ← 远程沙箱的模板方法基类
    └── LangSmithSandbox (langsmith.py) ← LangSmith SDK 沙箱

CompositeBackend (composite.py)        ← 路径路由，聚合上述任意后端
```

### 4.2 BackendProtocol — 协议接口

所有后端实现的标准接口：

```python
class BackendProtocol(ABC):
    async def als(self, path: str) -> LsResult: ...
    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult: ...
    async def awrite(self, file_path: str, content: str) -> WriteResult: ...
    async def aedit(self, file_path: str, old_string: str, new_string: str,
                    replace_all: bool = False) -> EditResult: ...
    async def agrep(self, pattern: str, path: str | None = None,
                    glob: str | None = None) -> GrepResult: ...
    async def aglob(self, pattern: str, path: str | None = None) -> GlobResult: ...
    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]: ...
    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]: ...
```

### 4.3 StateBackend — 临时存储

```python
from deepagents.backends import StateBackend

# 文件存在 LangGraph state 中，随 checkpointer 持久化
# 适合：临时文件、Agent 工作产物
backend = StateBackend(file_format="v2")

agent = create_deep_agent(model="openai:gpt-4o", backend=backend)
# Agent 写入的文件在 thread 内可见，跨 thread 不可见
```

**特点：**
- 文件存储在 LangGraph 的 `files` channel 中
- 随 checkpointer 自动持久化（同一 thread 跨 run 可见）
- 不跨 thread

### 4.4 FilesystemBackend — 宿主机文件系统

```python
from deepagents.backends import FilesystemBackend

# 直接读写宿主机文件
backend = FilesystemBackend(
    root_dir="/home/user/project",
    virtual_mode=True,     # 路径隔离：所有路径相对于 root_dir
    max_file_size_mb=10,   # grep 搜索的文件大小上限
)

agent = create_deep_agent(model="openai:gpt-4o", backend=backend)
# Agent 可以直接读写 /home/user/project 下的文件
```

**virtual_mode 安全机制：**
- 所有路径视为相对于 `root_dir` 的虚拟路径
- `..` 和 `~` 遍历被阻止
- 解析后的绝对路径验证不超出 root

### 4.5 LocalShellBackend — 本地 Shell

```python
from deepagents.backends import LocalShellBackend

# 文件系统 + Shell 命令执行
backend = LocalShellBackend(
    root_dir="/home/user/project",
    virtual_mode=True,
    timeout=120,              # 命令超时（秒）
    max_output_bytes=100_000, # 输出截断上限
    inherit_env=True,         # 继承父进程环境变量
    env={"DEBUG": "1"},       # 额外环境变量
)

agent = create_deep_agent(model="openai:gpt-4o", backend=backend)
# Agent 现在多了 execute 工具：
# execute("python script.py", timeout=60)
```

**注意：** `virtual_mode` 只限制文件操作路径，**不限制 Shell 命令**。Shell 命令在宿主机上直接执行。

### 4.6 StoreBackend — 持久跨线程存储

```python
from deepagents.backends import StoreBackend

# 文件存在 LangGraph BaseStore 中，跨 thread 持久化
store_backend = StoreBackend(
    store=my_store,  # BaseStore 实例（或 None 从 context 获取）
    namespace=lambda runtime: ("user", runtime.context["user_id"]),
    file_format="v2",
)

# 常用作 CompositeBackend 的路由目标
backend = CompositeBackend(
    default=StateBackend(),
    routes={"/memories/": store_backend},
)
# Agent 写 /memories/notes.txt → 持久存储，跨 thread 可见
```

### 4.7 CompositeBackend — 路径路由

```python
from deepagents.backends import CompositeBackend, FilesystemBackend, LocalShellBackend, StoreBackend

# 多路由架构：不同路径映射到不同后端
backend = CompositeBackend(
    default=LocalShellBackend(root_dir="/workspace"),   # 默认：可执行 Shell
    artifacts_root="/workspace/",                        # 产物目录
    routes={
        "/shared/memory/": FilesystemBackend(           # 共享记忆（只读）
            root_dir="/data/shared/memory", virtual_mode=True),
        "/shared/skills/": FilesystemBackend(            # 共享技能（只读）
            root_dir="/data/shared/skills", virtual_mode=True),
        "/memories/": StoreBackend(),                    # 持久记忆
        "/workspace/": FilesystemBackend(                # 工作区（读写）
            root_dir="/data/workspace", virtual_mode=True),
    },
)

agent = create_deep_agent(model="openai:gpt-4o", backend=backend)
# Agent 看到统一的虚拟文件系统：
# ls("/") → [shared/, memories/, workspace/]
# read_file("/shared/skills/data-analysis/SKILL.md") → 从 skills 后端读取
# write_file("/workspace/report.md", "...") → 写入 workspace 后端
# execute("python analyze.py") → 在 default 后端执行
```

**路由规则：**
- 最长前缀匹配（`/shared/memory/` 优先于 `/shared/`）
- 未匹配路径 → `default` 后端
- `execute` / `aexecute` → **始终**走 `default` 后端（不可路由）
- `grep`/`glob` 搜索所有后端，结果路径加上路由前缀

### 4.8 BaseSandbox — 远程沙箱

```python
from deepagents.backends.sandbox import BaseSandbox
from deepagents.backends.protocol import ExecuteResponse

class MyDockerSandbox(BaseSandbox):
    """自定义 Docker 沙箱"""

    def __init__(self, container_id: str):
        self._container_id = container_id

    @property
    def id(self) -> str:
        return self._container_id

    def execute(self, command: str, timeout: int = 120) -> ExecuteResponse:
        import subprocess
        result = subprocess.run(
            ["docker", "exec", self._container_id, "bash", "-c", command],
            capture_output=True, timeout=timeout, text=True,
        )
        return ExecuteResponse(
            output=result.stdout + result.stderr,
            exit_code=result.returncode,
            truncated=False,
        )

    def upload_files(self, files):
        # 实现文件上传到容器
        ...

    def download_files(self, paths):
        # 实现从容器下载文件
        ...

# 使用
sandbox = MyDockerSandbox("my-container-123")
agent = create_deep_agent(model="openai:gpt-4o", backend=sandbox)
# ls/read/write/edit/grep/glob 全部通过 execute() 间接实现
# execute 命令在容器内运行
```

**BaseSandbox 的模板方法模式：**
- 子类只需实现 `execute()` / `upload_files()` / `download_files()` / `id`
- `ls`/`read`/`write`/`edit`/`grep`/`glob` 全部通过组合 `execute()` 自动实现
- `edit` 有两种策略：inline（小文件，单轮）和 via-upload（大文件，三轮）

---

## 五、文件系统与权限

### 5.1 FilesystemPermission

```python
from deepagents import FilesystemPermission

# 权限规则列表（first-match-wins，不匹配 = 默认允许）
permissions = [
    # 拒绝访问敏感文件
    FilesystemPermission(paths="*.env", mode="deny"),
    FilesystemPermission(paths="*.pem", mode="deny"),
    FilesystemPermission(paths="/etc/**", mode="deny"),

    # 需要人工审批的操作（operations 仅支持 "read" / "write"）
    FilesystemPermission(
        paths="/workspace/production/**",
        operations=["write"],   # "read" | "write"，edit_file 归入 write
        mode="interrupt",       # 暂停等待人工审批
    ),

    # 允许其余所有操作
    FilesystemPermission(paths="**/*", mode="allow"),
]

agent = create_deep_agent(
    model="openai:gpt-4o",
    backend=backend,
    permissions=permissions,
)
```

### 5.2 权限模式

| 模式 | 行为 |
|---|---|
| `"allow"` | 直接执行 |
| `"deny"` | 返回权限错误，不执行 |
| `"interrupt"` | 暂停 Agent，弹出 HITL 审批 |

### 5.3 interrupt 模式与 HITL 联动

```python
from deepagents import FilesystemPermission
from langchain.agents.middleware import InterruptOnConfig

# 文件权限的 interrupt 模式自动生成 HITL 配置
permissions = [
    FilesystemPermission(
        paths="/workspace/deploy/**",
        operations=["write"],
        mode="interrupt",
    ),
]

# 等价于自动添加 interrupt_on：
# interrupt_on={
#     "write_file": InterruptOnConfig(
#         when=lambda req: path_matches("/workspace/deploy/**", req.args["file_path"]),
#         ...
#     ),
# }

agent = create_deep_agent(
    model="openai:gpt-4o",
    backend=backend,
    permissions=permissions,
    # HumanInTheLoopMiddleware 自动添加到尾部栈
)
```

---

## 六、子代理（SubAgents）

### 6.1 三种子代理形式

| 形式 | 类 | 执行方式 | 返回值 |
|---|---|---|---|
| 声明式同步 | `SubAgent` | 在当前进程内运行到完成 | 结果文本 |
| 编译式同步 | `CompiledSubAgent` | 预编译的 Runnable | 结果文本 |
| 异步远程 | `AsyncSubAgent` | 远程 LangGraph 服务器 | 立即返回 task_id |

### 6.2 SubAgent — 声明式同步子代理

```python
from deepagents import SubAgent

researcher = SubAgent(
    name="researcher",
    description="搜索和整理资料，输出结构化报告",
    system_prompt="你是研究员。搜索信息，整理为要点列表，标注来源。",
    tools=[search_tool, web_tool],          # 可选：不指定则继承父 Agent 的工具
    middleware=[custom_middleware],          # 可选：额外中间件
    permissions=[...],                     # 可选：覆盖父 Agent 权限
    skills=["/shared/skills/research/"],   # 可选：独立技能集
)

# 注册到主 Agent
agent = create_deep_agent(
    model="openai:gpt-4o",
    subagents=[researcher],
)

# 主 Agent 通过 task 工具调用子代理：
# task(agent_type="researcher", description="搜索 2024 年 AI 论文趋势")
```

**子代理构建过程：**
1. 解析模型 + 子代理 HarnessProfile
2. 构建子代理独立中间件栈（TodoList → Filesystem → Summarization → PatchToolCalls → Skills → 用户 → Profile → 缓存）
3. 合并权限（子代理覆盖父代理）
4. 继承工具（除非子代理显式声明自己的）
5. 通过 `langchain.agents.create_agent` 编译

### 6.3 CompiledSubAgent — 预编译子代理

```python
from deepagents import CompiledSubAgent

# 预编译一个独立 Agent
compiled = CompiledSubAgent(
    name="code-reviewer",
    description="代码审查专家",
    runnable=pre_built_agent,   # 已编译的 CompiledStateGraph
)

agent = create_deep_agent(
    model="openai:gpt-4o",
    subagents=[compiled],
)
```

### 6.4 General-Purpose 子代理（自动添加）

`create_deep_agent` 默认自动添加一个 `general-purpose` 子代理：

```python
# 自动添加条件：
# 1. Profile 没有 gp_subagent.enabled = False
# 2. 没有用户提供的名为 "general-purpose" 的子代理

# 它继承父 Agent 的：模型、工具、技能、权限
# 排在 subagent 列表的第 0 位

# 可通过 Profile 禁用：
# profile.general_purpose_subagent = GeneralPurposeSubagentProfile(enabled=False)
```

### 6.5 子代理隔离

子代理的 state 与主 Agent 隔离：

| 共享 | 不共享 |
|---|---|
| 文件（通过 Backend） | `messages`（对话历史） |
| 工具 | `todos`（任务列表） |
| 技能 | `structured_response` |
| | Private state 字段 |

---

## 七、异步后台任务（AsyncSubAgent）

### 7.1 概念

AsyncSubAgent 在**远程 LangGraph Platform** 上运行，主 Agent 通过 API 管理任务生命周期：

```
主 Agent                    远程 LangGraph Platform
  │                              │
  ├── start_async_task ────────► │ 创建 thread + 启动 run
  │   ← task_id                  │
  │                              │ (异步执行中)
  ├── check_async_task ────────► │ 查询状态
  │   ← status + result          │
  │                              │
  ├── update_async_task ───────► │ 中断当前 run + 新输入重启
  │                              │
  └── cancel_async_task ───────► │ 取消 run
```

### 7.2 声明与使用

```python
from deepagents import AsyncSubAgent

nl2sql_agent = AsyncSubAgent(
    name="nl2sql",
    description="NL2SQL 查询专家，将自然语言转换为 SQL 并执行",
    graph_id="nl2sql_agent",   # 远程部署的 graph 标识
)

agent = create_deep_agent(
    model="openai:gpt-4o",
    subagents=[nl2sql_agent],
)

# 主 Agent 使用 5 个工具管理异步任务：
# start_async_task(name="nl2sql", description="查询上月销售额")
# → 返回 task_id
#
# check_async_task(task_id="xxx")
# → 返回 status + 结果摘要
#
# update_async_task(task_id="xxx", message="追加条件：只看线上渠道")
# → 中断当前运行，用新输入重启
#
# cancel_async_task(task_id="xxx")
# → 取消任务
#
# list_async_tasks()
# → 所有任务及状态
```

### 7.3 任务状态

| 状态 | 含义 |
|---|---|
| `pending` | 已创建，等待执行 |
| `running` | 正在执行 |
| `success` | 成功完成 |
| `error` | 执行出错 |
| `timeout` | 超时 |
| `cancelled` | 已取消 |
| `interrupted` | 被 update 中断 |

---

## 八、记忆与技能（Memory & Skills）

### 8.1 MemoryMiddleware — AGENTS.md

```python
# 项目中的 AGENTS.md 文件
# /project/AGENTS.md:
# # 项目规范
# - 所有 SQL 使用参数化查询
# - 图表优先使用 ECharts
# - 报告格式遵循公司模板

agent = create_deep_agent(
    model="openai:gpt-4o",
    memory=["/project/AGENTS.md", "/project/.claude/AGENTS.md"],
    backend=backend,
)
# AGENTS.md 内容注入到系统提示中（每次都注入，不做渐进式披露）
# HTML 注释被自动剥离
```

**系统提示占位符：** Memory 内容替换系统提示中的 `{agent_memory}` 标记。

### 8.2 SkillsMiddleware — SKILL.md

```python
# 技能目录结构
# /shared/skills/
# ├── data-analysis/
# │   └── SKILL.md          ← YAML frontmatter + 技能说明
# ├── chart-generation/
# │   └── SKILL.md
# └── report-writing/
#     └── SKILL.md

# SKILL.md 格式：
# ---
# name: data-analysis
# description: 数据分析技能，支持 SQL 查询和统计分析
# compatibility: Python 3.10+, pandas
# ---
# # 数据分析
# ## 使用步骤
# 1. 理解用户需求
# 2. 编写 SQL 查询
# ...

agent = create_deep_agent(
    model="openai:gpt-4o",
    skills=["/shared/skills/"],
    backend=backend,
)

# 渐进式披露：
# 1. 系统提示中注入技能名 + 描述 + 路径
# 2. Agent 需要时通过 read_file("/shared/skills/data-analysis/SKILL.md") 读取全文
```

**技能名验证规则：**
- 1-64 字符
- 只允许小写字母、数字、连字符
- 必须匹配目录名

---

## 九、上下文压缩（Summarization）

### 9.1 deepagents 的增强压缩 vs LangChain 原版

| 特性 | LangChain 版 | deepagents 版 |
|---|---|---|
| state 修改 | 破坏性 `REMOVE_ALL_MESSAGES` | **非破坏性**（`_summarization_event`） |
| 历史去向 | 永久丢失 | **落盘**到 `/conversation_history/{thread_id}.md` |
| SystemMessage | state 中的被删 | ✅ 安全 |
| 工具参数截断 | 无 | 预压缩阶段截断 |
| ContextOverflowError | 不处理 | 自动降级 |
| 手动压缩 | 无 | `compact_conversation` 工具 |

### 9.2 使用示例

```python
from deepagents.middleware.summarization import (
    create_summarization_middleware,
    create_summarization_tool_middleware,
)

# 工厂函数（自动计算默认值）
summ = create_summarization_middleware(
    model="openai:gpt-4o-mini",
    backend=backend,
)
# 有 model profile: trigger=("fraction", 0.85), keep=("fraction", 0.10)
# 无 profile:       trigger=("tokens", 170000), keep=("messages", 6)

# 手动配置
summ = SummarizationMiddleware(
    model="openai:gpt-4o-mini",
    backend=backend,
    trigger=("fraction", 0.8),    # 达到 80% 上下文时触发
    keep=("fraction", 0.15),      # 保留 15%
)

# 附带手动压缩工具
summ_tool = SummarizationToolMiddleware(summ)
# Agent 获得 compact_conversation 工具

# create_deep_agent 自动配置压缩（无需手动添加）
# 压缩中间件在内置栈的第 5 位自动注入
```

### 9.3 压缩处理流程

```
wrap_model_call 钩子内：

1. 重建有效消息列表
   └─ 从 _summarization_event 恢复之前的压缩事件

2. 预压缩：工具参数截断
   ├─ 扫描旧消息中 write_file/edit_file 的大参数
   ├─ 截断到 max_length（默认 2000 chars）
   └─ 重新计算 token → 若已降到阈值以下则跳过完整压缩

3. 检查触发条件
   └─ 未触发 → 正常调用模型

4. 提取内联媒体
   └─ base64/SVG → 上传到 backend → 替换为路径引用

5. 历史落盘
   ├─ 过滤之前的摘要（避免重复存储）
   ├─ 写入 /conversation_history/{thread_id}.md
   └─ 失败非致命（警告，继续压缩）

6. 生成摘要
   └─ DEEPAGENTS_DEFAULT_SUMMARY_PROMPT（含媒体引用附录）

7. ContextOverflowError 降级
   └─ 模型调用抛出 → 执行压缩 → 重试
```

---

## 十、自我评估迭代（Rubric）

### 10.1 概念

RubricMiddleware 让 Agent 在完成后进行**自我评估**。如果评分不达标，注入反馈继续迭代。

```
Agent 完成 → 评分 Agent 评估 → needs_revision?
    │                              │
    │                              ├── 是 → 注入反馈 → 继续迭代
    │                              │
    └── satisfied / failed / max_iterations → 结束
```

### 10.2 使用示例

```python
from deepagents import RubricMiddleware

rubric = RubricMiddleware(
    model="openai:gpt-4o",       # 评分模型
    max_iterations=3,            # 最多迭代 3 次
    on_evaluation=lambda eval: print(f"评分: {eval.verdict}"),
)

agent = create_deep_agent(
    model="openai:gpt-4o",
    middleware=[rubric],
)

# 在 state 中传入评分标准
result = agent.invoke({
    "messages": [{"role": "user", "content": "分析销售数据并生成报告"}],
    "rubric": [
        {"criterion": "数据准确性", "description": "所有数据指标计算正确"},
        {"criterion": "完整性", "description": "覆盖所有必需的分析维度"},
        {"criterion": "可读性", "description": "报告结构清晰，图表标注完整"},
    ],
}, config={"configurable": {"thread_id": "report-1"}})
```

### 10.3 评分流程

```
after_agent 钩子：

1. 检查 state 中是否有 rubric → 无则跳过

2. 构建评分输入
   ├─ 对话记录截取最近 30 条（每条最多 4000 chars）
   └─ nonce 标记防止提示注入

3. 调用评分模型（response_format=GraderResponse）
   └─ 结构化输出：每个 criterion 的 pass/fail + 总体 verdict

4. 根据 verdict 决策
   ├─ "satisfied" → 结束
   ├─ "needs_revision" → 注入反馈 + jump_to="model"
   ├─ "failed" → 结束
   └─ max_iterations → 结束
```

---

## 十一、Profile 体系

### 11.1 两层 Profile

| 层 | 类型 | 控制什么 | 示例 |
|---|---|---|---|
| **HarnessProfile** | 如何运行 | 提示词后缀、工具覆盖、中间件排除、子代理配置 | Sonnet/Opus/Codex 特定调优 |
| **ProviderProfile** | 如何构造模型 | `init_chat_model` 参数、版本检查 | OpenAI 启用 Responses API |

### 11.2 HarnessProfile — 运行时调优

```python
from deepagents.profiles import (
    HarnessProfile, HarnessProfileConfig,
    register_harness_profile, GeneralPurposeSubagentProfile,
)

# 注册自定义 Harness Profile
register_harness_profile(
    key="my-provider:my-model",
    profile=HarnessProfile(
        # 替换默认系统提示（完全替换 BASE_AGENT_PROMPT）
        base_system_prompt="你是一个专门处理客服问题的 Agent。",

        # 追加到系统提示末尾（模型注意力最高的位置）
        system_prompt_suffix="""
        <behavior>
        - 始终以客户满意度为优先
        - 不确定时转接人工
        </behavior>
        """,

        # 工具描述覆盖
        tool_description_overrides={
            "execute": "在沙箱中执行 Python 脚本",
        },

        # 排除特定工具
        excluded_tools=frozenset({"glob"}),

        # 排除特定中间件（不能排除 Filesystem/SubAgent）
        excluded_middleware=frozenset({"TodoListMiddleware"}),

        # 额外中间件
        extra_middleware=[my_custom_middleware],

        # General-purpose 子代理配置
        general_purpose_subagent=GeneralPurposeSubagentProfile(
            enabled=True,
            description="通用助手",
            system_prompt="你是一个通用助手。",
        ),
    ),
)
```

**内置 HarnessProfile：**

| 模型 | system_prompt_suffix 内容 |
|---|---|
| `anthropic:claude-sonnet-4-6` | `<use_parallel_tool_calls>` 最大化并行工具调用（如同时读 3 个文件）；`<investigate_before_answering>` 禁止猜测，必须先读文件再回答；`<tool_result_reflection>` 反思工具结果质量再决定下一步 |
| `anthropic:claude-opus-4-7` | Sonnet 全部内容 + `<tool_usage>` 主动调查（不靠猜，用工具验证）+ `<subagent_usage>` 并行扇出子代理处理独立子任务 |
| `anthropic:claude-haiku-4-5` | `<use_parallel_tool_calls>` 强制并行 + `<tool_result_reflection>` 结果反思（小模型更需要显式引导） |
| `openai:gpt-5.{1,2,3}-codex` | **自主高级工程师模式**：bias to action、不做前置计划、端到端坚持完成；**并行工具**：批量读取/搜索，仅真正依赖时才串行；**TODO 对账**：完成前必须 reconcile 所有 TODO（标记 done/blocked/cancelled） |

### 11.3 ProviderProfile — 模型构造参数

```python
from deepagents.profiles import ProviderProfile, register_provider_profile

# 注册自定义 Provider Profile
register_provider_profile(
    key="my-provider",
    profile=ProviderProfile(
        # 传给 init_chat_model 的静态参数
        init_kwargs={"temperature": 0.7, "max_tokens": 4096},

        # 初始化前的副作用（如版本检查）
        pre_init=lambda: check_version("my-sdk", ">=1.0.0"),

        # 运行时动态参数（如从环境变量读取）
        init_kwargs_factory=lambda: {"api_key": os.getenv("MY_API_KEY")},
    ),
)
```

**内置 ProviderProfile：**

| Provider | 配置 | 详情 |
|---|---|---|
| `openai` | `init_kwargs={"use_responses_api": True}` | 所有 `openai:*` 模型默认启用 Responses API（而非 Chat Completions）。如需关闭，传预初始化实例：`init_chat_model("openai:gpt-4o", use_responses_api=False)` |
| `openrouter` | `pre_init` + `init_kwargs_factory` | ① `pre_init`：强制 `langchain-openrouter >= 0.2.0`；② 注入 attribution（`app_title="Deep Agents"`, `app_url=github 地址`，可用 `OPENROUTER_APP_URL` / `OPENROUTER_APP_TITLE` 环境变量覆盖）；③ 默认过滤 Azure 后端（`openrouter_provider={"ignore": ["azure"]}`），因 OpenRouter `/responses` beta 无状态无法查找 `rs_*` 推理项，可用 `DEEPAGENTS_OPENROUTER_ALLOW_AZURE=1` 解除 |

### 11.4 第三方插件

通过 Python entry points 注册自定义 Profile：

```toml
# pyproject.toml
[project.entry-points."deepagents.provider_profiles"]
my_provider = "my_package.profiles:register_my_profiles"

[project.entry-points."deepagents.harness_profiles"]
my_model = "my_package.profiles:register_my_harness"
```

---

## 十二、State 管理

### 12.1 DeepAgentState

```python
from deepagents import DeepAgentState

class DeepAgentState(AgentState):
    """扩展 AgentState，添加 DeltaChannel 减少 checkpoint 增长"""
    messages: Required[
        Annotated[
            list[AnyMessage],
            DeltaChannel(_messages_delta_reducer, snapshot_frequency=50)
        ]
    ]
```

**DeltaChannel 优化：**
- 常规 channel：每次 checkpoint 写入全部消息 → O(N²) 增长
- DeltaChannel：只写入增量，每 50 条消息做一次全量快照 → O(N) 增长

### 12.2 自定义 State Schema

```python
from deepagents import DeepAgentState
from typing import Annotated, NotRequired
from typing_extensions import TypedDict

def merge_dict_by_key(old: dict, new: dict) -> dict:
    """按 key 合并（不覆盖已有 key）"""
    return {**old, **new}

class MyAgentState(DeepAgentState):
    # 自定义字段
    user_context: NotRequired[dict]
    # 并发安全的字段（自定义 reducer）
    task_results: Annotated[NotRequired[dict[str, dict]], merge_dict_by_key]
    # 临时字段（不进入 checkpoint）
    temp_data: NotRequired[Annotated[dict, UntrackedValue]]

agent = create_deep_agent(
    model="openai:gpt-4o",
    state_schema=MyAgentState,
)
```

### 12.3 Private State

中间件可以通过 `PrivateStateAttr` 声明私有 state 字段：

```python
from langchain.agents.middleware.types import OmitFromSchema

class MyMiddlewareState(TypedDict):
    # 不进入输入/输出 schema，不传播给子代理
    _internal_counter: NotRequired[
        Annotated[int, OmitFromSchema(input=True, output=True)]
    ]
```

`create_deep_agent` 自动收集所有中间件的 private state 字段，传递给 `SubAgentMiddleware`，确保子代理不能访问父代理的私有数据。

---

## 十三、自定义中间件编写

### 13.1 wrap_model_call — 修改模型请求

```python
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse

class DatabaseContextMiddleware(AgentMiddleware):
    """在每次模型调用前注入当前数据库上下文"""

    def __init__(self, get_db_name):
        self.get_db_name = get_db_name

    def wrap_model_call(self, request: ModelRequest, handler) -> ModelResponse:
        db_name = self.get_db_name()
        if db_name:
            from langchain_core.messages import SystemMessage
            extra = f"\n\n当前数据库: {db_name}"
            new_sys = SystemMessage(
                content=(request.system_message.content if request.system_message else "") + extra
            )
            request = request.override(system_message=new_sys)
        return handler(request)
```

### 13.2 wrap_tool_call — 拦截工具结果

```python
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
import hashlib

class ResultDeduplicationMiddleware(AgentMiddleware):
    """去重相同的工具结果，减少 token 消耗"""

    def __init__(self):
        self._cache = {}

    def wrap_tool_call(self, request: ToolCallRequest, handler):
        result = handler(request)
        if isinstance(result, ToolMessage):
            content_hash = hashlib.md5(str(result.content).encode()).hexdigest()
            if content_hash in self._cache:
                return ToolMessage(
                    content=f"[与之前结果相同，引用: {content_hash[:8]}]",
                    tool_call_id=request.tool_call["id"],
                )
            self._cache[content_hash] = True
        return result
```

### 13.3 before_model — 状态注入

```python
from langchain.agents.middleware import AgentMiddleware, hook_config

class TokenBudgetMiddleware(AgentMiddleware):
    """当消息过多时提前退出"""

    def __init__(self, max_messages=100):
        self.max_messages = max_messages

    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime):
        if len(state["messages"]) > self.max_messages:
            from langchain_core.messages import AIMessage
            return {
                "jump_to": "end",
                "messages": [AIMessage(content="对话过长，请开始新会话。")],
            }
        return None
```

### 13.4 dynamic_prompt — 动态系统提示

```python
from langchain.agents.middleware import dynamic_prompt

@dynamic_prompt
def context_aware_prompt(request):
    """根据运行时上下文动态调整系统提示"""
    config = request.runtime.config or {}
    configurable = config.get("configurable", {})

    db_name = configurable.get("db_name", "unknown")
    user_role = request.runtime.context.get("role", "user") if request.runtime.context else "user"

    prompt_parts = [
        f"当前数据库: {db_name}",
        f"用户角色: {user_role}",
    ]
    if db_name == "analytics":
        prompt_parts.append("优先使用聚合查询，避免返回原始数据。")

    return "\n".join(prompt_parts)
```

### 13.5 中间件排序纪律

```python
# 推荐的自定义中间件排序
custom_middleware = [
    # 外层：守护层（错误处理、限流）
    ModelRequiredMiddleware(),     # 检查模型配置
    QuotaErrorMiddleware(),        # 配额错误友好提示
    ModelTimeoutMiddleware(),      # 超时友好提示

    # 中间：业务逻辑层
    DatabaseContextMiddleware(),   # 注入数据库上下文
    ToolFilterMiddleware(),        # 工具过滤
    ResultDeduplicationMiddleware(), # 结果去重

    # 内层：最接近模型（后处理）
    VfsPathResolverMiddleware(),   # 路径解析
]

agent = create_deep_agent(
    model="openai:gpt-4o",
    middleware=custom_middleware,  # 插入在基础栈和尾部栈之间
)
```

---

## 十四、实战：nl2sql 项目架构

### 14.1 整体架构

nl2sql 项目深度使用 deepagents，展示了生产级 Agent 的完整架构：

```
main_agent (create_deep_agent)
├── 模型：anthropic:claude-sonnet-4-6
├── 子代理：nl2sql (AsyncSubAgent)
│   └── 独立 graph (create_deep_agent)
│       ├── 19 层自定义中间件
│       ├── 独立权限集（比主 Agent 更严格）
│       ├── YAML 配置驱动
│       └── 动态提示注入（数据库、Cube 通道、口径约束）
├── 后端：CompositeBackend（5 路由）
│   ├── /shared/memory/  → 共享记忆（只读）
│   ├── /shared/skills/  → 共享技能（只读）
│   ├── /workspace/      → 工作区（读写，DynamicFilesystemBackend）
│   └── default          → DynamicLocalShellBackend
├── 15+ 自定义中间件
│   ├── 守护层：ModelRequired / QuotaError / ModelTimeout
│   ├── 业务层：MessageSlimmer / KnowledgeTrim / DynamicMCPTools
│   └── 追踪层：LangfuseSpan / VfsPathResolver
└── 扩展机制
    ├── YAML 子代理加载器
    ├── Monkey-patch 协调（sync_launcher / check_progress）
    └── 自定义 Backend（DynamicFilesystem / OpenSandbox）
```

### 14.2 自定义 Backend 扩展

```python
# DynamicFilesystemBackend — 延迟解析工作目录
class DynamicFilesystemBackend(FilesystemBackend):
    """支持运行时切换工作区"""

    def __init__(self, get_root_dir: Callable[[], str], **kwargs):
        self._get_root_dir = get_root_dir
        super().__init__(**kwargs)

    @property
    def cwd(self) -> str:
        return self._get_root_dir()  # 每次访问重新解析

    def _resolve_path(self, key: str) -> Path:
        self.root_dir = Path(self.cwd)  # 动态更新
        return super()._resolve_path(key)

# 使用
workspace_backend = DynamicFilesystemBackend(
    get_root_dir=lambda: workspace_manager.active_workspace,
    virtual_mode=True,
)
```

### 14.3 Monkey-Patch 协调模式

项目通过 monkey-patch 扩展 AsyncSubAgent 的行为：

```python
# sync_launcher.py — 启动任务时自动开启 todo 同步
_PATCHED = False

def apply_patch():
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    from deepagents.middleware import async_subagents
    _original = async_subagents._build_start_tool

    def _patched_build_start_tool(...):
        tool = _original(...)
        # 拦截返回的 Command，提取 async_tasks
        # 自动启动 todo sync 线程
        return wrapped_tool

    async_subagents._build_start_tool = _patched_build_start_tool

apply_patch()  # 在 create_deep_agent 之前调用
```

### 14.4 自定义 State Schema

```python
class MainAgentState(DeepAgentState):
    """主 Agent 状态，支持并发多查询协调"""
    todos: NotRequired[list]
    query_headers: Annotated[NotRequired[dict[str, dict]], _merge_dict_by_key]
    subagent_steps_map: Annotated[NotRequired[dict[str, list]], _merge_dict_by_key]
    active_queries: Annotated[NotRequired[dict[str, bool]], _merge_dict_by_key]
    token_stats: Annotated[NotRequired[dict], _accumulate_token_stats]
    answer_fingerprints: Annotated[NotRequired[dict[str, dict]], _merge_dict_by_key]
```

所有并发字段使用 `_merge_dict_by_key` reducer，确保多个异步子代理同时写入时不互相覆盖。

---

## 十五、常见陷阱与最佳实践

### 15.1 中间件排序陷阱

| 陷阱 | 解决方案 |
|---|---|
| 错误处理中间件放在内层 → 捕获不到外层异常 | 错误/限流/配额中间件放在**最外层**（列表前面） |
| PromptCaching 在 Memory 之前 → 缓存命中率低 | 确保 Profile extra 在 Memory 之前（框架已保证） |
| 工具过滤中间件太早 → 看不到后面注入的工具 | `_ToolExclusionMiddleware` 放在尾部栈 |

### 15.2 Backend 选择指南

| 场景 | 推荐 Backend |
|---|---|
| 快速原型 / 测试 | `StateBackend` |
| 本地开发 | `LocalShellBackend` |
| 生产环境（文件操作） | `FilesystemBackend(virtual_mode=True)` |
| 持久记忆 | `StoreBackend` |
| 多路由混合 | `CompositeBackend` |
| 远程沙箱 | `BaseSandbox` 子类 |

### 15.3 子代理 vs 异步任务

| 维度 | SubAgent（同步） | AsyncSubAgent（异步） |
|---|---|---|
| 执行位置 | 当前进程 | 远程服务器 |
| 返回方式 | 阻塞到完成 | 立即返回 task_id |
| 适用场景 | 简单子任务 | 长时间运行的复杂任务 |
| 工具 | `task()` | `start/check/update/cancel/list_async_task()` |

### 15.4 系统提示组装顺序

```
最终系统提示 = 用户 system_prompt + "\n\n" + BASE_AGENT_PROMPT + "\n\n" + Profile suffix
                 ↑                              ↑                        ↑
            用户指令（最前面）            内置行为指引              模型特定调优（最后面，注意力最高）
```

**关键：** 用户 `system_prompt` 始终在最前面，优先级最高。Profile suffix 在最后面（模型注意力最高的位置）。

### 15.5 工具描述覆盖

```python
# 通过 HarnessProfile 覆盖工具描述（不改变工具本身）
profile = HarnessProfile(
    tool_description_overrides={
        "execute": "在安全沙箱中执行 Python 代码",
        "task": "启动专门的子代理处理复杂子任务",
    },
)
register_harness_profile("my:model", profile)
```

### 15.6 递归限制

`create_deep_agent` 默认设置 `recursion_limit=9999`，支持长时间自治执行。如需更严格的控制，使用 `ModelCallLimitMiddleware`：

```python
from langchain.agents.middleware import ModelCallLimitMiddleware

agent = create_deep_agent(
    model="openai:gpt-4o",
    middleware=[ModelCallLimitMiddleware(run_limit=30)],  # 单次 run 最多 30 次模型调用
)
```

### 15.7 Checkpoint 效率

`DeepAgentState` 的 `DeltaChannel` 每 50 条消息做一次全量快照。对于超长对话（>100 条消息），这比默认的 `add_messages` reducer 节省 ~95% 的 checkpoint 存储空间。

### 15.8 权限安全

```python
# ✅ 正确：先 deny 再 allow（first-match-wins）
permissions = [
    FilesystemPermission(paths="*.env", mode="deny"),
    FilesystemPermission(paths="*.key", mode="deny"),
    FilesystemPermission(paths="**/*", mode="allow"),
]

# ❌ 错误：allow 在前面，deny 永远不生效
permissions = [
    FilesystemPermission(paths="**/*", mode="allow"),  # 匹配所有，后面的规则被跳过
    FilesystemPermission(paths="*.env", mode="deny"),  # 永远不会执行
]
```

---

## 十六、结构化输出（Structured Output）

### 16.1 概述

`create_deep_agent` 通过 `response_format` 参数支持结构化输出，让 Agent 的最终回复按照指定的 schema 返回结构化数据，而不仅仅是自由文本。

```python
agent = create_deep_agent(
    model="openai:gpt-4o",
    response_format=MySchema,  # ← 这里控制输出格式
)
result = agent.invoke(...)
structured = result["structured_response"]  # ← 从 state 中获取结构化结果
```

### 16.2 三种策略

| 策略 | 类 | 原理 | 适用场景 |
|---|---|---|---|
| **ToolStrategy** | `ToolStrategy` | 注入人造工具，模型通过 tool_call 返回结构化数据 | 所有模型通用 |
| **ProviderStrategy** | `ProviderStrategy` | 使用 provider 原生结构化输出 API（如 OpenAI `response_format`） | 支持原生 API 的模型 |
| **AutoStrategy** | `AutoStrategy`（默认） | 自动选择最佳策略 | 不确定时用这个 |

### 16.3 支持的 Schema 类型

| 类型 | 示例 |
|---|---|
| **Pydantic Model** | `class Report(BaseModel): ...` |
| **dataclass** | `@dataclass class Report: ...` |
| **TypedDict** | `class Report(TypedDict): ...` |
| **JSON Schema dict** | `{"type": "object", "properties": {...}}` |

### 16.4 基础用法

#### 方式一：直接传 schema 类型（AutoStrategy，推荐）

```python
from pydantic import BaseModel, Field
from deepagents import create_deep_agent

class AnalysisReport(BaseModel):
    """数据分析报告"""
    summary: str = Field(description="分析摘要")
    key_findings: list[str] = Field(description="关键发现列表")
    recommendation: str = Field(description="建议措施")
    confidence: float = Field(description="置信度 0-1", ge=0, le=1)

agent = create_deep_agent(
    model="openai:gpt-4o",
    response_format=AnalysisReport,  # 直接传类型 → AutoStrategy
)

result = agent.invoke(
    {"messages": [{"role": "user", "content": "分析上月销售数据趋势"}]},
    config={"configurable": {"thread_id": "analysis-1"}},
)

# 获取结构化输出
report: AnalysisReport = result["structured_response"]
print(report.summary)          # "上月销售额环比增长 12%..."
print(report.key_findings)     # ["线上渠道增长 20%", ...]
print(report.confidence)       # 0.85
```

#### 方式二：ToolStrategy（工具调用策略）

```python
from langchain.agents.structured_output import ToolStrategy

agent = create_deep_agent(
    model="openai:gpt-4o",
    response_format=ToolStrategy(
        schema=AnalysisReport,
        tool_message_content="已生成分析报告",  # 工具返回的消息内容
        handle_errors=True,  # 解析失败时自动重试
    ),
)
```

**ToolStrategy 工作原理：**
```
1. 将 schema 转换为一个人造工具（StructuredTool）
2. 模型通过 tool_call 调用该工具，参数即为结构化数据
3. 框架解析 tool_call 参数 → 验证 schema → 写入 state["structured_response"]
4. 同时注入一条 ToolMessage 到对话中

若解析失败（handle_errors=True）：
→ 注入错误消息，让模型重新生成
```

**handle_errors 选项：**

| 值 | 行为 |
|---|---|
| `True` | 捕获所有错误，注入默认错误消息让模型重试 |
| `"自定义消息"` | 捕获所有错误，使用自定义消息 |
| `ValueError` | 只捕获指定异常类型 |
| `(ValueError, TypeError)` | 捕获多种异常类型 |
| `lambda e: f"错误: {e}"` | 自定义错误消息生成函数 |
| `False` | 不捕获，异常直接传播 |

#### 方式三：ProviderStrategy（原生 API 策略）

```python
from langchain.agents.structured_output import ProviderStrategy

agent = create_deep_agent(
    model="openai:gpt-4o",
    response_format=ProviderStrategy(
        schema=AnalysisReport,
        strict=True,  # 启用 OpenAI strict mode
    ),
)
```

**ProviderStrategy 工作原理：**
```
1. 将 schema 转为 JSON Schema
2. 通过 provider 原生 API 传递（如 OpenAI 的 response_format 参数）
3. Provider 保证输出符合 schema
4. 框架从 AIMessage.content 中解析 JSON → 验证 → 写入 structured_response
```

### 16.5 Union 类型（多选一输出）

```python
from typing import Union
from pydantic import BaseModel

class SuccessResult(BaseModel):
    """查询成功"""
    data: list[dict]
    row_count: int

class ErrorResult(BaseModel):
    """查询失败"""
    error: str
    suggestion: str

# Union 类型：模型选择其中一种返回
agent = create_deep_agent(
    model="openai:gpt-4o",
    response_format=ToolStrategy(
        schema=Union[SuccessResult, ErrorResult],
        handle_errors=True,
    ),
)

result = agent.invoke(
    {"messages": [{"role": "user", "content": "查询不存在的表"}]},
    config={"configurable": {"thread_id": "query-1"}},
)

response = result["structured_response"]
if isinstance(response, SuccessResult):
    print(f"返回 {response.row_count} 行数据")
elif isinstance(response, ErrorResult):
    print(f"查询失败: {response.error}，建议: {response.suggestion}")
```

**注意：** Union 类型只支持 `ToolStrategy`（每个变体生成一个独立工具），不支持 `ProviderStrategy`。

### 16.6 TypedDict Schema

```python
from typing_extensions import TypedDict, NotRequired

class QueryResult(TypedDict):
    sql: str                    # 生成的 SQL
    result_summary: str         # 结果摘要
    row_count: int              # 行数
    warnings: NotRequired[list[str]]  # 可选字段

agent = create_deep_agent(
    model="anthropic:claude-sonnet-4-6",
    response_format=QueryResult,
)

result = agent.invoke(
    {"messages": [{"role": "user", "content": "统计各部门人数"}]},
    config={"configurable": {"thread_id": "q-1"}},
)

qr: QueryResult = result["structured_response"]
print(qr["sql"])           # "SELECT dept, COUNT(*) ..."
print(qr["row_count"])     # 12
print(qr.get("warnings"))  # None 或 ["部门名含特殊字符"]
```

### 16.7 dataclass Schema

```python
from dataclasses import dataclass, field

@dataclass
class CodeReview:
    """代码审查结果"""
    score: int                       # 1-10 评分
    issues: list[str]                # 发现的问题
    suggestions: list[str] = field(default_factory=list)  # 改进建议
    approved: bool = False           # 是否通过

agent = create_deep_agent(
    model="openai:gpt-4o",
    response_format=CodeReview,
)

result = agent.invoke(
    {"messages": [{"role": "user", "content": "审查这段代码: def add(a,b): return a+b"}]},
    config={"configurable": {"thread_id": "review-1"}},
)

review: CodeReview = result["structured_response"]
print(f"评分: {review.score}, 通过: {review.approved}")
```

### 16.8 JSON Schema Dict

```python
# 直接使用 JSON Schema dict（无需定义 Python 类）
json_schema = {
    "type": "object",
    "title": "SentimentAnalysis",
    "description": "情感分析结果",
    "properties": {
        "sentiment": {
            "type": "string",
            "enum": ["positive", "negative", "neutral"],
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "explanation": {"type": "string"},
    },
    "required": ["sentiment", "confidence", "explanation"],
}

agent = create_deep_agent(
    model="openai:gpt-4o",
    response_format=json_schema,  # 直接传 dict
)

result = agent.invoke(
    {"messages": [{"role": "user", "content": "分析评论情感：'这个产品太棒了！'"}]},
    config={"configurable": {"thread_id": "sa-1"}},
)

# JSON Schema dict 返回原始 dict（不做 Python 类型转换）
analysis: dict = result["structured_response"]
print(analysis["sentiment"])   # "positive"
print(analysis["confidence"])  # 0.95
```

### 16.9 策略选择指南

| 场景 | 推荐策略 | 原因 |
|---|---|---|
| 不确定 | `AutoStrategy`（默认，直接传 schema） | 自动选择最佳 |
| Union 类型 | `ToolStrategy` | 每个变体一个工具 |
| OpenAI + 简单 schema | `ProviderStrategy(strict=True)` | provider 强制保证格式 |
| 需要错误重试 | `ToolStrategy(handle_errors=True)` | 解析失败自动重试 |
| Anthropic 模型 | `ToolStrategy` | Anthropic 不支持原生 `response_format` |
| 最大化可靠性 | `ProviderStrategy` | provider 端强制，不会格式出错 |

### 16.10 访问结构化输出

```python
# invoke 同步调用
result = agent.invoke(input, config)
structured = result.get("structured_response")  # None = 模型未返回结构化数据

# ainvoke 异步调用
result = await agent.ainvoke(input, config)
structured = result.get("structured_response")

# stream 流式调用 — structured_response 在最终 state 中
final_state = None
async for event in agent.astream(input, config, stream_mode="updates"):
    if "structured_response" in event.get("__end__", {}):
        structured = event["__end__"]["structured_response"]

# 检查是否成功返回
if structured is None:
    print("Agent 未返回结构化输出（可能因工具循环或提前退出）")
else:
    print(f"结构化输出: {structured}")
```

### 16.11 内部处理流程

```
create_agent 内部处理 response_format：

1. 解析 response_format 类型
   ├─ None → 不启用结构化输出
   ├─ ToolStrategy / ProviderStrategy / AutoStrategy → 直接使用
   └─ type / dict → 包装为 AutoStrategy(schema=...)

2. AutoStrategy 自动选择
   ├─ 尝试 ProviderStrategy（检查模型是否支持原生 API）
   └─ 回退到 ToolStrategy

3. ToolStrategy 路径：
   ├─ 将 schema 转为 StructuredTool（人造工具）
   ├─ Union 类型 → 每个变体一个工具
   ├─ 工具注入到模型的工具列表中
   └─ 模型调用该工具 → 解析参数 → 验证 → 写入 structured_response

4. ProviderStrategy 路径：
   ├─ 将 schema 转为 JSON Schema
   ├─ 通过 model_kwargs 传给模型（如 response_format=json_schema）
   ├─ 模型保证输出格式
   └─ 从 AIMessage.content 解析 JSON → 验证 → 写入 structured_response

5. 错误处理（ToolStrategy only）：
   ├─ MultipleStructuredOutputsError → 模型返回了多个结构化调用
   ├─ StructuredOutputValidationError → 参数不符合 schema
   └─ handle_errors=True → 注入错误消息让模型重试
```

### 16.12 与子代理结合

```python
from deepagents import SubAgent

# 子代理也可以使用结构化输出
analyzer = SubAgent(
    name="data-analyzer",
    description="数据分析子代理",
    system_prompt="分析数据并返回结构化结果。",
    response_format=AnalysisReport,  # 子代理独立的 response_format
)

agent = create_deep_agent(
    model="openai:gpt-4o",
    subagents=[analyzer],
    # 主 Agent 可以不设 response_format（自由文本）
)

# 主 Agent 通过 task 工具调用子代理
# 子代理返回的文本中包含结构化数据的摘要
# 主 Agent 的 structured_response 由主 Agent 自己的 response_format 控制
```

### 16.13 异常处理

```python
from langchain.agents.structured_output import (
    MultipleStructuredOutputsError,
    StructuredOutputValidationError,
)

try:
    result = agent.invoke(input, config)
    structured = result.get("structured_response")
except MultipleStructuredOutputsError as e:
    print(f"模型返回了多个结构化输出: {e.tool_names}")
    print(f"原始 AIMessage: {e.ai_message}")
except StructuredOutputValidationError as e:
    print(f"结构化输出验证失败: {e.tool_name}")
    print(f"原始异常: {e.source}")
    print(f"原始 AIMessage: {e.ai_message}")
```

### 16.14 完整实战示例

```python
from pydantic import BaseModel, Field
from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, LocalShellBackend, StoreBackend
from langchain.agents.structured_output import ToolStrategy

# 定义输出 schema
class DataInsight(BaseModel):
    """数据洞察报告"""
    question: str = Field(description="用户原始问题")
    sql: str = Field(description="执行的 SQL")
    result_summary: str = Field(description="结果摘要（2-3 句话）")
    key_metrics: dict[str, float] = Field(description="关键指标及其值")
    anomalies: list[str] = Field(default_factory=list, description="异常发现")
    confidence: float = Field(description="结果置信度 0-1", ge=0, le=1)

# 创建 Agent
agent = create_deep_agent(
    model="openai:gpt-4o",
    system_prompt="你是一个数据分析专家。分析用户的问题，执行 SQL 查询，返回结构化洞察。",
    response_format=ToolStrategy(
        schema=DataInsight,
        handle_errors=True,       # 格式错误自动重试
        tool_message_content="洞察报告已生成",
    ),
    backend=CompositeBackend(
        default=LocalShellBackend(root_dir="/data", inherit_env=True),
        routes={"/memories/": StoreBackend()},
    ),
    name="data-analyst",
)

# 调用
result = agent.invoke(
    {"messages": [{"role": "user", "content": "分析上月各渠道销售趋势，找出异常点"}]},
    config={"configurable": {"thread_id": "insight-001"}},
)

# 使用结构化输出
insight: DataInsight = result["structured_response"]
print(f"问题: {insight.question}")
print(f"SQL: {insight.sql}")
print(f"摘要: {insight.result_summary}")
for metric, value in insight.key_metrics.items():
    print(f"  {metric}: {value}")
if insight.anomalies:
    print(f"异常: {', '.join(insight.anomalies)}")
print(f"置信度: {insight.confidence}")
```

---

## 十七、废弃时间线

| 废弃于 | 移除于 | 内容 | 替代方案 |
|---|---|---|---|
| 0.5.0 | 0.6.0 | `LocalShellBackend` / `FilesystemBackend` 的 `virtual_mode=None` 默认值 | 显式传 `virtual_mode=True` 或 `False` |
| 0.5.0 | 0.7.0 | `BackendContext` 类 | 使用 `Runtime` 替代 |
| 0.5.0 | 0.7.0 | `StateBackend(runtime=...)` 参数 | 删除该参数 |
| 0.5.0 | 0.7.0 | 可调用 backend 工厂（传 callable 作为 `backend`） | 直接传 `BackendProtocol` 实例 |
| 0.5.0 | 0.7.0 | `SummarizationMiddleware(history_path_prefix=...)` 参数 | 使用 `backend` 路径控制 |
| 0.5.0 | 0.7.0 | `backend.read()` 返回纯 `str` | 返回 `ReadResult` 对象 |
| 0.5.3 | 1.0.0 | `model=None`（依赖默认 `claude-sonnet-4-6`） | 显式指定模型（如 `ChatAnthropic(model_name=...)`） |

---

> **源码版本：** deepagents 0.6.12
> **编写日期：** 2026-10-14
> **数据来源：** 通读 `.venv/Lib/site-packages/deepagents/` 全部源码 + nl2sql 项目实际使用分析
