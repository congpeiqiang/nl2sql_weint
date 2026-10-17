# LangChain / LangGraph / DeepAgents 使用教程

> **基于项目实际安装的版本**：langchain 1.3.14 / langchain-core 1.4.9 / langgraph 1.2.9 / deepagents 0.6.12
>
> 全部教程均从 `.venv/lib/site-packages/` 源码逐文件精读编写，包含源码摘录（标注 `文件:行号`）、架构图、完整可运行代码示例及本项目实战分析。

---

## 分册结构

```
docs/langchain使用教程/
├── README.md                     ← 本文件（总索引）
├── langchain使用教程/            ← 分册 1：LangChain（7 篇，专题式）
├── langgrap使用教程/             ← 分册 2：LangGraph（8 篇，源码级）★新增
├── langchain中间件使用教程/      ← 分册 3：中间件专题（1 篇）
└── deepagent使用教程/            ← 分册 4：DeepAgents（1 篇）
```

| 分册 | 篇数 | 行数合计 | 侧重 |
|------|------|---------|------|
| [langchain使用教程/](langchain使用教程/) | 7 | ~10,800 | LangChain 层：流式、结构化输出、HITL、技能、子智能体、中间件 |
| [langgrap使用教程/](langgrap使用教程/) | 8 | ~14,600 | LangGraph 运行时：图构建、通道、Pregel 引擎、持久化、流式、缓存 |
| [langchain中间件使用教程/](langchain中间件使用教程/) | 1 | ~1,940 | 中间件使用视角的专题合辑 |
| [deepagent使用教程/](deepagent使用教程/) | 1 | ~2,040 | DeepAgents 高层封装 API 与内部机制 |

> **分册之间的依赖关系**：LangChain 分册是使用视角，LangGraph 分册是其底层运行时；DeepAgents 分册是最上层封装。建议按 LangGraph → LangChain → DeepAgents 的顺序深入。

---

## 分册 1：LangChain 使用教程（langchain使用教程/）

| # | 文件 | 主题 | 行数 | 大小 | 核心内容 |
|---|------|------|------|------|----------|
| 01 | [流式输出](langchain使用教程/01-流式输出.md) | Streaming | 3,060 | 155KB | StreamMode 7 模式、astream/astream_events、逐 token 流、工具调用流、子图/远程图流、StreamTransformer、4 个完整示例、项目 main_agent 流式架构分析 |
| 02 | [结构化输出](langchain使用教程/02-结构化输出.md) | Structured Output | 1,563 | 74KB | with_structured_output、JSON/Pydantic/OpenAI Tools/XML/List/String 解析器、流式结构化解析、错误处理、4 个完整示例、12 条最佳实践陷阱 |
| 03 | [人机协同](langchain使用教程/03-人机协同.md) | Human-in-the-Loop | 1,055 | 51KB | interrupt 机制、HumanInTheLoopMiddleware、审批/编辑/输入三模式、update_state/resume、多步审批、项目 sql_approval 实战 |
| 04 | [短期技能与长期技能](langchain使用教程/04-短期技能与长期技能.md) | Skills & Memory | 1,259 | 70KB | SkillsMiddleware 生命周期、MemoryMiddleware 读写检索、Summarization 摘要、消息驱逐/溢出裁剪、ContextHub、项目 write_todos/progress_boundary/message_slimmer |
| 05 | [同步子智能体与异步子智能体](langchain使用教程/05-同步子智能体与异步子智能体.md) | Subagents | 1,315 | 79KB | SubAgentMiddleware 同步调度、AsyncSubAgentMiddleware 异步台账、子图机制、SubagentTransformer、工具隔离、项目 sync_launcher/check_progress 实战 |
| 06 | [预构建中间件大全](langchain使用教程/06-预构建中间件大全.md) | Built-in Middlewares | 1,367 | 84KB | LangChain 16 个 + DeepAgents 8 个中间件逐一详解：签名、参数、工作原理、示例、组合使用 |
| 07 | [自定义中间件开发](langchain使用教程/07-自定义中间件开发.md) | Custom Middleware | 1,187 | 65KB | 基类接口、注册顺序、七步开发法、8+4 个项目中间件深度案例分析、测试策略、3 个完整自定义示例、12 条常见陷阱 |

---

## 快速导航

### 按使用场景查找

| 我想实现… | 推荐阅读 |
|-----------|---------|
| 让前端逐 token 显示 AI 回复 | [01-流式输出](langchain使用教程/01-流式输出.md) §5 逐 token 流式输出 |
| 让模型返回结构化 JSON / Pydantic 对象 | [02-结构化输出](langchain使用教程/02-结构化输出.md) §2 with_structured_output |
| 工具调用前需要人工审批 | [03-人机协同](langchain使用教程/03-人机协同.md) §6 审批模式 |
| 给 Agent 加载/卸载技能文件 | [04-短期技能与长期技能](langchain使用教程/04-短期技能与长期技能.md) §2 SkillsMiddleware |
| 让 Agent 有长期记忆（跨会话） | [04-短期技能与长期技能](langchain使用教程/04-短期技能与长期技能.md) §3 MemoryMiddleware |
| 主 Agent 派发子任务并等待结果 | [05-子智能体](langchain使用教程/05-同步子智能体与异步子智能体.md) §2 SubAgentMiddleware |
| 后台异步执行长任务 | [05-子智能体](langchain使用教程/05-同步子智能体与异步子智能体.md) §3 AsyncSubAgentMiddleware |
| 模型超时/失败时自动重试或降级 | [06-预构建中间件](langchain使用教程/06-预构建中间件大全.md) §3 ModelRetry / ModelFallback |
| 限制工具调用次数防止死循环 | [06-预构建中间件](langchain使用教程/06-预构建中间件大全.md) §3 ToolCallLimit |
| 自动检测并脱敏 PII 信息 | [06-预构建中间件](langchain使用教程/06-预构建中间件大全.md) §3 PII |
| 从零开发一个自定义中间件 | [07-自定义中间件](langchain使用教程/07-自定义中间件开发.md) §4 七步开发法 |
| 理解中间件的执行顺序 | [07-自定义中间件](langchain使用教程/07-自定义中间件开发.md) §3 注册与执行顺序 |

### 按源码模块查找

| 源码模块 | 教程覆盖 |
|----------|---------|
| `langgraph/stream/` | [01-流式输出](langchain使用教程/01-流式输出.md) |
| `langchain_core/output_parsers/` | [02-结构化输出](langchain使用教程/02-结构化输出.md) |
| `langgraph/prebuilt/interrupt.py` | [03-人机协同](langchain使用教程/03-人机协同.md) |
| `langchain/agents/middleware/human_in_the_loop.py` | [03-人机协同](langchain使用教程/03-人机协同.md) |
| `deepagents/middleware/skills.py` | [04-技能](langchain使用教程/04-短期技能与长期技能.md) |
| `deepagents/middleware/memory.py` | [04-技能](langchain使用教程/04-短期技能与长期技能.md) |
| `deepagents/middleware/subagents.py` | [05-子智能体](langchain使用教程/05-同步子智能体与异步子智能体.md) |
| `deepagents/middleware/async_subagents.py` | [05-子智能体](langchain使用教程/05-同步子智能体与异步子智能体.md) |
| `langchain/agents/middleware/*` (16个) | [06-预构建中间件](langchain使用教程/06-预构建中间件大全.md) |
| `deepagents/middleware/*` (8个) | [06-预构建中间件](langchain使用教程/06-预构建中间件大全.md) |
| `src/agent/middlewares/*` (28个) | [07-自定义中间件](langchain使用教程/07-自定义中间件开发.md) |

---

## 版本与依赖

本教程基于项目 `.venv` 中实际安装的包版本编写：

```
langchain==1.3.14
langchain-core==1.4.9
langchain-openai==1.3.5
langgraph==1.2.9
deepagents==0.6.12
```

升级依赖后，源码行号可能漂移，但类名/函数名可通过 grep 定位。

---

## 项目实战关联

每篇教程都包含对本项目 (`nl2sql`) 实际实现的分析章节：

| 教程 | 分析的项目文件 |
|------|---------------|
| 01-流式输出 | `main_agent.py`、`langgraph_api` 服务端流式接线 |
| 02-结构化输出 | 项目中的 Pydantic schema 和 JSON 解析用法 |
| 03-人机协同 | `middlewares/sql_approval.py` (v1 HITL → v2 只读硬拦截) |
| 04-技能 | `middlewares/write_todos.py`、`progress_boundary.py`、`message_slimmer.py` |
| 05-子智能体 | `subagents/sync_launcher.py`、`check_progress.py`、`track_progress.py`、`loader.py` |
| 06-预构建中间件 | 项目中间件栈的选型对照 |
| 07-自定义中间件 | 28 个项目中间件中的 12 个深度案例分析 |

---

## 分册 2：LangGraph 源码使用教程（langgrap使用教程/）

> **详细索引见 [langgrap使用教程/README.md](langgrap使用教程/README.md)**。基于 langgraph 1.2.9 源码逐文件精读，共 8 篇。

| # | 文件 | 行数 | 核心内容 |
|---|------|------|----------|
| 01 | [图构建与状态管理](langgrap使用教程/01-图构建与状态管理.md) | 1,344 | StateGraph API、状态定义三方式、节点签名、条件边、compile 流程、子图嵌套 |
| 02 | [通道系统与 Reducer](langgrap使用教程/02-通道系统与Reducer.md) | 1,577 | BaseChannel 四钩子、10 种通道、ManagedValue、reducer 设计模式 |
| 03 | [预构建组件](langgrap使用教程/03-预构建组件.md) | 1,932 | create_react_agent、ToolNode 七步流水线、interrupt 机制 |
| 04 | [Pregel 执行引擎](langgrap使用教程/04-Pregel执行引擎.md) | 3,542 | BSP/superstep、主循环、调度算法、运行器、线程池、节点调用、图验证、重试 |
| 05 | [检查点与 Store 持久化](langgrap使用教程/05-检查点与Store持久化.md) | 1,034 | Memory/Sqlite/Postgres Saver、序列化、StateSnapshot、Store 系统 |
| 06 | [函数式图与装饰器](langgrap使用教程/06-函数式图与装饰器.md) | 1,755 | @entrypoint/@task、Send、Command、RemoteGraph、异常体系 |
| 07 | [流式系统](langgrap使用教程/07-流式系统.md) | 2,101 | 三层架构、StreamMode、StreamMux、逐 token 流、子图穿透、自定义通道 |
| 08 | [缓存配置与内部工具](langgrap使用教程/08-缓存配置与内部工具.md) | 1,294 | Cache 系统、Pregel 缓存接线、15 个内部模块、Config API |

---

## 分册 3 / 4：中间件专题 与 DeepAgents

| 文件 | 核心内容 |
|------|----------|
| [langchain中间件使用教程.md](langchain中间件使用教程/langchain中间件使用教程.md) | 中间件基类与钩子体系、装饰器快捷方式、LangChain 内置中间件（汇总中） |
| [deepagents使用教程.md](deepagent使用教程/deepagents使用教程.md) | create_deep_agent、内置中间件栈、Backend 体系、文件系统与权限、子代理、异步后台任务、记忆与技能、上下文压缩、Rubric、Profile 体系 |

---

## 总体阅读建议

1. **入门（日常使用）**：LangGraph 分册 01→02→03，或 LangChain 分册 [06-预构建中间件大全](langchain使用教程/06-预构建中间件大全.md)
2. **开发（按需查阅）**：各分册的「完整代码示例」章节可直接复用
3. **排障**：LangGraph 分册 [04-Pregel执行引擎](langgrap使用教程/04-Pregel执行引擎.md) §17 排错速查 + LangChain 分册 [01-流式输出](langchain使用教程/01-流式输出.md) 第 11 章
4. **深入源码**：LangGraph 分册 [07-流式系统](langgrap使用教程/07-流式系统.md)、[04-Pregel执行引擎](langgrap使用教程/04-Pregel执行引擎.md)
5. **理解本项目架构**：LangChain 分册 [07-自定义中间件](langchain使用教程/07-自定义中间件开发.md) 第 5 章的 12 个项目案例
