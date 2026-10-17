# LangGraph 源码使用教程

> **基于项目实际安装的版本**：langgraph 1.2.9 / langgraph-checkpoint 4.1.1 / langgraph-checkpoint-sqlite 3.1.1 / langgraph-checkpoint-postgres 3.1.2 / langchain-core 1.4.9
>
> 全部 8 篇教程均从 `.venv/lib/site-packages/langgraph/` 源码逐文件精读编写，共约 90 个 Python 源文件。含源码摘录（标注 `文件:行号`）、架构图、完整可运行代码示例及本项目实战分析。
>
> ⚠️ 注意：本机 `.venv` 的 langgraph 存在**本地补丁**（如 `DEFAULT_RECURSION_LIMIT` 被改为 10007，上游为 25）。判线上行为以容器内实际文件为准。

---

## 教程目录

| # | 文件 | 行数 | 源码模块 | 核心内容 |
|---|------|------|----------|----------|
| 01 | [图构建与状态管理](01-图构建与状态管理.md) | 1,344 | `graph/` | StateGraph 核心 API、状态定义三方式（TypedDict+Pydantic+MessagesState）、MessageGraph、节点签名九种协议、条件边与 path_map、compile 九步流程、UI 消息、子图嵌套（进程内 vs 部署级）、5 个完整示例、建图期+运行期错误清单 |
| 02 | [通道系统与 Reducer](02-通道系统与Reducer.md) | 1,577 | `channels/` `managed/` | Pregel 通道模型、BaseChannel 四钩子、LastValue、BinaryOperatorAggregate(reducer)、`Annotated[T, reducer]` 映射、Topic/AnyValue/EphemeralValue/DeltaChannel/NamedBarrierValue/UntrackedValue、ManagedValue 体系、`_fields.py`、reducer 设计模式、通道序列化、4 个完整示例、18 类陷阱 |
| 03 | [预构建组件](03-预构建组件.md) | 1,932 | `prebuilt/` | **create_react_agent** 全参数（16 参数、model/tools/prompt 各四形态、v1/v2 差异）、**ToolNode** 七步执行流水线（注入/校验/并行/错误策略）、**interrupt 机制**（HumanInterrupt 协议、GraphInterrupt 异常链）、ValidationNode、工具调用流式、4 个完整示例 |
| 04 | [Pregel 执行引擎](04-Pregel执行引擎.md) | 3,542 | `pregel/` | BSP 模型与 superstep、Pregel 主类、invoke/ainvoke/stream 四种调用、`_loop.py` 主循环、`_algo.py` 调度算法、`_runner.py` 运行器、`_executor.py` 线程池、`_call.py` 节点调用、`_io/_read/_write` 通道读写、`_validate.py` 图验证、`_retry.py` 重试、`_checkpoint.py`、recursion_limit 与 GraphRecursionError、20 文件职责速查、3 个完整示例、13 行排错速查 |
| 05 | [检查点与 Store 持久化](05-检查点与Store持久化.md) | 1,034 | `checkpoint/` `store/` | BaseCheckpointSaver、MemorySaver、Sqlite/AsyncSqliteSaver、PostgresSaver（10 条 MIGRATIONS）、序列化系统（JsonPlus/Encrypted/msgpack）、Checkpoint 数据结构与 uuid6、StateSnapshot 与时间旅行、BaseStore/InMemory/Sqlite/Postgres Store、Store vs Checkpoint 选型、项目 checkpointer_factory 双事故复盘、5 个示例 |
| 06 | [函数式图与装饰器](06-函数式图与装饰器.md) | 1,755 | `func/` `types.py` | **@entrypoint** / **@task** 装饰器、Send 动态扇出、**Command**（goto/update/resume/parent/jump_to）、interrupt() 逐行解析、StateSnapshot 时间旅行、异常体系全继承树、warnings 弃用体系、callbacks、**RemoteGraph** 远程执行（SSE/REST）、重试与超时、配置工具、4 个完整示例 |
| 07 | [流式系统](07-流式系统.md) | 2,101 | `stream/` | 三层架构（Pregel 泵→StreamChannel→StreamTransformer）、StreamMode 全模式、stream/astream、stream_events v3 运行流对象、StreamChannel 游标/tee、StreamMux 多路复用、`_convert.py` v2→v3 协议桥、逐 token 流式（content-block 协议）、子图流式穿透（ns/lc_agent_name）、自定义流式通道（get_stream_writer）、3 个完整示例、10 个内置 Transformer 速查 |
| 08 | [缓存配置与内部工具](08-缓存配置与内部工具.md) | 1,294 | `cache/` `_internal/` | Cache 系统（缓存 node writes 而非 LLM 响应）、BaseCache、InMemory/Redis/SqliteCache、Pregel 缓存接线（CachePolicy/default_cache_key）、15 个 `_internal` 模块（配置合并/字段裁决/重试退避/超时/草稿板/重放/队列）、Config API（RunnableConfig/get_config）、项目 config.py 对照、3 个完整示例 |

**合计：8 篇 / 14,579 行**

---

## 快速导航

### 按使用场景查找

| 我想实现… | 推荐阅读 |
|-----------|---------|
| 搭建一个带条件分支的图 | [01-图构建与状态管理](01-图构建与状态管理.md) §2 §6 |
| 让状态字段累积（消息、列表） | [02-通道系统与Reducer](02-通道系统与Reducer.md) §4 §5 |
| 用现成组件快速搭一个 Agent | [03-预构建组件](03-预构建组件.md) §1 create_react_agent |
| 工具调用前需要中断审批 | [03-预构建组件](03-预构建组件.md) §3 interrupt 机制 |
| 理解图是怎么一步步执行的 | [04-Pregel执行引擎](04-Pregel执行引擎.md) §1 §4 |
| 让会话可恢复、支持时间旅行 | [05-检查点与Store持久化](05-检查点与Store持久化.md) §8 §9 |
| 跨会话保存用户画像/长期记忆 | [05-检查点与Store持久化](05-检查点与Store持久化.md) §9 Store 系统 |
| 用函数式 API 写图（@entrypoint/@task） | [06-函数式图与装饰器](06-函数式图与装饰器.md) §2 §3 |
| 动态扇出/并行 map-reduce | [06-函数式图与装饰器](06-函数式图与装饰器.md) §4 Send |
| 用 Command 控制路由/更新状态 | [06-函数式图与装饰器](06-函数式图与装饰器.md) §5 Command |
| 逐 token 流式输出到前端 | [07-流式系统](07-流式系统.md) §8 逐 token 流式 |
| 让子图的事件也流出来 | [07-流式系统](07-流式系统.md) §9 子图流式穿透 |
| 从节点内推自定义进度事件 | [07-流式系统](07-流式系统.md) §10 get_stream_writer |
| 缓存节点结果加速重复执行 | [08-缓存配置与内部工具](08-缓存配置与内部工具.md) §1 Cache 系统 |
| 给节点配重试/超时策略 | [08-缓存配置与内部工具](08-缓存配置与内部工具.md) §10 §11 |
| 排查图执行报错 | [04-Pregel执行引擎](04-Pregel执行引擎.md) §17 排错速查 |

### 按源码模块查找

| 源码模块 | 教程覆盖 |
|----------|---------|
| `graph/state.py` `graph/message.py` `graph/_branch.py` `graph/_node.py` | [01-图构建与状态管理](01-图构建与状态管理.md) |
| `channels/*.py`（10 个通道）| [02-通道系统与Reducer](02-通道系统与Reducer.md) |
| `managed/*.py` | [02-通道系统与Reducer](02-通道系统与Reducer.md) §12 |
| `prebuilt/chat_agent_executor.py` `tool_node.py` `interrupt.py` | [03-预构建组件](03-预构建组件.md) |
| `pregel/main.py` `_loop.py` `_algo.py` 等 20 个文件 | [04-Pregel执行引擎](04-Pregel执行引擎.md) |
| `checkpoint/base|memory|sqlite|postgres` | [05-检查点与Store持久化](05-检查点与Store持久化.md) |
| `store/base|memory|sqlite|postgres` | [05-检查点与Store持久化](05-检查点与Store持久化.md) §9 |
| `func/__init__.py`（@entrypoint/@task）| [06-函数式图与装饰器](06-函数式图与装饰器.md) §2 §3 |
| `types.py`（Send/Command/interrupt）| [06-函数式图与装饰器](06-函数式图与装饰器.md) §4 §5 §6 |
| `pregel/remote.py`（RemoteGraph）| [06-函数式图与装饰器](06-函数式图与装饰器.md) §11 |
| `stream/*.py`（7 个文件）| [07-流式系统](07-流式系统.md) |
| `cache/*.py` | [08-缓存配置与内部工具](08-缓存配置与内部工具.md) §1-6 |
| `_internal/*.py`（15 个文件）| [08-缓存配置与内部工具](08-缓存配置与内部工具.md) §7-17 |

---

## 学习路径建议

1. **入门三篇**：01（怎么建图）→ 02（状态怎么存）→ 03（现成组件）
   —— 这三篇覆盖了 90% 的日常使用。
2. **原理两篇**：04（执行引擎）→ 07（流式系统）
   —— 想搞懂「图为什么这样跑」「前端为什么收不到 token」必读。
3. **进阶三篇**：05（持久化）→ 06（函数式/Command/Send）→ 08（缓存与内部机制）
   —— 生产级应用需要的能力。

---

## 与其它分册的关系

| 分册 | 关系 |
|------|------|
| [../langchain使用教程/](../langchain使用教程/) | LangChain 层封装（agent/中间件/输出解析）；本分册是其底层运行时 |
| [../langchain中间件使用教程/](../langchain中间件使用教程/) | 中间件的使用视角；钩子体系见本分册 04/07 |
| [../deepagent使用教程/](../deepagent使用教程/) | DeepAgents 高层封装；其子图/异步子代理依赖本分册 04/06 |

---

## 版本与依赖

```
langgraph==1.2.9
langgraph-checkpoint==4.1.1
langgraph-checkpoint-sqlite==3.1.1
langgraph-checkpoint-postgres==3.1.2
langchain-core==1.4.9
```

升级依赖后源码行号可能漂移，但类名/函数名可通过 grep 定位。