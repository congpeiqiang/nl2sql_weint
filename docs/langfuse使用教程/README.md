# Langfuse 使用教程

> 基于 [Langfuse 官方文档](https://langfuse.com/docs) + NL2SQL Agent 项目深度实践  
> Langfuse 平台版本：**v4.16.0**（自托管 Docker Compose） / Langfuse Cloud  
> Python SDK 版本：**langfuse v4.14.4**  
> 教程编写时间：2026-10-14

---

## 📖 教程目录

### 基础篇

| 序号 | 文件 | 内容 |
|------|------|------|
| 01 | [快速入门](01-快速入门.md) | 安装、配置、5 分钟上报第一条 trace；多种接入方式（SDK/OTel/LangChain） |
| 02 | [界面导航](02-界面导航.md) | Traces / Scores / Datasets / Prompts / Evaluations / Analytics 全页面详解 |
| 03 | [Trace 追踪详解](03-Trace追踪详解.md) | 四层数据模型、嵌套规则、OTel context、Session/Metadata/Tags、命名规范 |

### 进阶篇

| 序号 | 文件 | 内容 |
|------|------|------|
| 04 | [评估与打分](04-评估与打分.md) | 五维评估体系、LLM-as-Judge、评估单元、rubric 工程化、校准回路 |
| 05 | [Prompt 管理](05-Prompt管理.md) | 版本管理、标签发布、A/B 灰度、canary 部署、本地兜底、CI/CD 集成 |
| 06 | [数据集与实验](06-数据集与实验.md) | 四维 A/B 实验、多臂对比、统计门禁、Expected Output 金标、双落点 |

### 实战篇

| 序号 | 文件 | 内容 |
|------|------|------|
| 07 | [反馈闭环](07-反馈闭环.md) | 反馈类型判别、BadCase 自动采集、标注队列、状态机、三道闸门发版 |
| 08 | [NL2SQL 项目实践](08-NL2SQL项目实践.md) | 中间件架构、trace 嵌套方案、五维评分、环境变量、开发者操作手册 |
| 09 | [LangChain/LangGraph 集成](09-LangChain集成.md) | CallbackHandler、Agent Graph、子 Agent 归并、DeepAgents 集成 |
| 10 | [自托管部署与运维](10-自托管部署与运维.md) | Docker Compose 架构、组件拓扑、内存调优、运维命令、升级指南 |
| 11 | [数据分析与看板](11-数据分析与看板.md) | Dashboard 定制、成本分析、延迟分布、评分趋势、告警配置 |
| 12 | [常见问题与排查](12-常见问题与排查.md) | 完整 FAQ、错误码表、调试脚本、ClickHouse 查询、性能调优 |

---

## 🎯 Langfuse 是什么

[Langfuse](https://langfuse.com) 是一个**开源的 AI 工程平台**，专为 LLM 应用的全生命周期设计。它将以下能力统一集成在一个闭环中：

### 核心能力矩阵

| 能力域 | 功能 | 解决的痛点 |
|--------|------|-----------|
| **Tracing** | 全链路追踪、嵌套 Observation、Agent Graph 可视化 | 不知道 LLM 应用内部发生了什么 |
| **Evaluation** | 在线打分、LLM-as-Judge、代码评估、用户反馈 | 无法量化 LLM 应用质量 |
| **Prompt Mgmt** | 版本管理、标签发布、A/B 灰度、回滚 | 改 prompt 要发版、无法回滚 |
| **Datasets** | 测试集沉淀、BadCase 采集、金标管理 | 没有回归测试基础 |
| **Experiments** | 多臂对比、统计门禁、Expected Output | 不知道改动是进步还是退化 |
| **Analytics** | 成本/延迟/用量看板、异常检测 | 无法监控生产健康状况 |

### 架构定位

```
┌─────────────────────────────────────────────────────────────────┐
│                     Langfuse 平台全景                             │
│                                                                   │
│  ┌──────────────────────── 观测层 ─────────────────────────┐    │
│  │ Traces │ Generations │ Sessions │ Agent Graph │ Scores │    │
│  └──────────────────────────────────────────────────────────┘    │
│  ┌──────────────────────── 评估层 ─────────────────────────┐    │
│  │ Code Eval │ LLM-as-Judge │ User Feedback │ Experiments │    │
│  └──────────────────────────────────────────────────────────┘    │
│  ┌──────────────────────── 管理层 ─────────────────────────┐    │
│  │ Prompts │ Datasets │ Models │ Analytics │ Settings      │    │
│  └──────────────────────────────────────────────────────────┘    │
│  ┌──────────────────────── 基础设施 ──────────────────────┐    │
│  │ ClickHouse(事件) │ PostgreSQL(元数据) │ Redis(队列) │ MinIO │    │
│  └──────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
```

### v4 架构变化

v4 是 Langfuse 的一次重大架构升级：

- **数据核心迁移到 ClickHouse**：trace/observation 数据存储在 ClickHouse 事件表（`events_core`/`events_full`），查询性能大幅提升
- **OTel 原生支持**：SDK 底层基于 OpenTelemetry，支持跨语言、跨框架
- **新 API 体系**：`v2/observations`、`v3/scores` 等 v4 原生接口替代旧版 REST API
- **Agent Graph**：自动将 trace 聚合成 DAG 视图
- **Experiment 页面**：独立的实验对比 UI

> ⚠️ 旧版 `langfuse.trace()` 等 API 在 v4 已移除，请使用 `start_as_current_observation()` 上下文管理器。

## 🚀 学习路径建议

### 快速上手（1-2 小时）

```
01-快速入门 → 02-界面导航 → 发一条真实查询看 trace
```

### 深入理解（1 天）

```
03-Trace追踪详解 → 04-评估与打分 → 05-Prompt管理
```

### 项目实战（2-3 天）

```
08-NL2SQL项目实践 → 09-LangChain集成 → 07-反馈闭环
```

### 运维部署（半天）

```
10-自托管部署与运维 → 11-数据分析与看板 → 12-常见问题与排查
```

## 📚 参考资源

### 官方文档

| 资源 | 链接 |
|------|------|
| [Langfuse 文档首页](https://langfuse.com/docs) | 全部文档入口 |
| [LLM Tracing 入门](https://langfuse.com/docs/observability/get-started) | 追踪快速上手 |
| [Python SDK (PyPI)](https://pypi.org/project/langfuse/) | 安装与版本 |
| [Python Decorator](https://langfuse.com/blog/2024-04-python-decorator) | `@observe()` 装饰器 |
| [LangChain 集成](https://langfuse.com/integrations/frameworks/langchain) | LangChain tracing |
| [LangGraph 集成](https://langfuse.com/integrations/frameworks/langgraph) | LangGraph tracing |
| [LLM-as-a-Judge](https://langfuse.com/docs/evaluation/evaluation-methods/llm-as-a-judge) | 自动评估 |
| [评估核心概念](https://langfuse.com/docs/evaluation/core-concepts) | Scores/Datasets/Experiments |
| [Prompt Management](https://langfuse.com/docs/prompts) | 提示词管理 |
| [Best Practices](https://langfuse.com/docs/observability/best-practices) | Trace 命名与组织 |
| [GitHub 仓库](https://github.com/langfuse/langfuse) | 开源代码 |

### 项目内文档

| 文档 | 路径 | 内容 |
|------|------|------|
| 接入实现方案 | `docs/langfuse平台/Langfuse接入实现方案.md` | 完整架构设计 |
| 评估精准化方案 | `docs/langfuse平台/NL2SQL-评估精准化设计方案.md` | 五维评估 + 校准回路 |
| 反馈闭环方案 | `docs/langfuse平台/NL2SQL反馈闭环优化设计方案.md` | 反馈类型 + 标注队列 |
| 全流程实现手册 | `docs/langfuse平台/NL2SQL-Langfuse全流程实现手册.md` | 操作手册 |
| Trace 层级方案 | `docs/langfuse平台/Langfuse标准Trace层级方案.md` | 嵌套规范 |
| 实验离线方案 | `docs/langfuse平台/LangfuseExperiment离线实施方案.md` | 四维 A/B |
| 连接说明 | `docs/langfuse平台/Langfuse连接说明.md` | 部署架构 |
| Dataset 字段契约 | `docs/langfuse平台/Dataset字段契约.md` | Item 字段约定 |
| 脚本命令手册 | `docs/langfuse平台/项目脚本命令手册.md` | 所有命令参考 |
