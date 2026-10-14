# Langfuse 使用教程

> 基于 [Langfuse 官方文档](https://langfuse.com/docs) + NL2SQL Agent 项目实践  
> Langfuse 版本：**v4.16.0**（自托管） / Cloud  
> SDK 版本：**langfuse v4.14.4**  
> 教程更新时间：2026-10-14

---

## 📖 教程目录

| 序号 | 文件 | 内容 |
|------|------|------|
| 01 | [快速入门](01-快速入门.md) | 安装、配置、5 分钟上报第一条 trace |
| 02 | [界面导航](02-界面导航.md) | Traces / Scores / Datasets / Prompts / Evaluations 各页面说明 |
| 03 | [Trace 追踪详解](03-Trace追踪详解.md) | Trace 结构、嵌套、Session、Metadata、Tags |
| 04 | [评估与打分](04-评估与打分.md) | 在线打分、LLM-as-Judge、代码评估、用户反馈 |
| 05 | [Prompt 管理](05-Prompt管理.md) | 版本管理、标签发布、A/B 灰度 |
| 06 | [数据集与实验](06-数据集与实验.md) | Dataset 创建、离线实验、回归门禁 |
| 07 | [反馈闭环](07-反馈闭环.md) | 用户 👍/👎 → BadCase 采集 → 迭代修复 → 发版 |
| 08 | [NL2SQL 项目实践](08-NL2SQL项目实践.md) | 本项目的 Langfuse 集成架构与开发者指南 |
| 09 | [常见问题与排查](09-常见问题与排查.md) | FAQ + 故障排查表 |

---

## 🎯 Langfuse 是什么

[Langfuse](https://langfuse.com) 是一个**开源的 LLM 工程平台**，用于协作式地调试、分析和迭代 AI Agent 应用。它将以下能力集成在一个闭环中：

- **Tracing（追踪）**：记录 LLM 调用、工具调用、Agent 链路的完整时间线
- **Evaluation（评估）**：在线打分 + LLM-as-Judge + 代码评估 + 用户反馈
- **Prompt Management（提示词管理）**：版本管理、标签发布、灰度部署
- **Datasets & Experiments（数据集与实验）**：沉淀测试集、批量回归、A/B 对比
- **Analytics（分析）**：成本、延迟、使用量看板

```
┌─────────────────────────────────────────────────┐
│                 Langfuse 平台                     │
│                                                   │
│  ┌─────────┐ ┌─────────┐ ┌──────────┐           │
│  │ Traces  │ │ Scores  │ │ Prompts  │           │
│  │ 追踪链路 │ │ 评估打分 │ │ 版本管理  │           │
│  └─────────┘ └─────────┘ └──────────┘           │
│  ┌─────────┐ ┌─────────┐ ┌──────────┐           │
│  │Datasets │ │Experiments│ │Analytics│          │
│  │ 数据集   │ │ 实验对比  │ │ 分析看板 │           │
│  └─────────┘ └─────────┘ └──────────┘           │
└─────────────────────────────────────────────────┘
```

## 🚀 快速开始

→ 阅读 [01-快速入门](01-快速入门.md)，5 分钟内完成第一条 trace 上报。

## 📚 参考资源

- [Langfuse 官方文档](https://langfuse.com/docs)
- [Langfuse Python SDK (PyPI)](https://pypi.org/project/langfuse/)
- [Langfuse GitHub](https://github.com/langfuse/langfuse)
- [Python Decorator 指南](https://langfuse.com/blog/2024-04-python-decorator)
- [LangChain 集成](https://langfuse.com/integrations/frameworks/langchain)
- [LLM-as-a-Judge](https://langfuse.com/docs/evaluation/evaluation-methods/llm-as-a-judge)
- [评估概览](https://langfuse.com/docs/evaluation/overview)
