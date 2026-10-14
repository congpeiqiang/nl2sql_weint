# 守护线程生命周期

> 本文档列出所有 daemon=True 的后台线程及其生命周期。
> 修改守护线程相关代码时**必须同步更新此文档**。

## 线程清单

| # | 文件 | 线程名/变量 | 启动时机 | 停机方式 | 数据丢失风险 |
|---|------|-------------|----------|----------|-------------|
| 1 | `subagents/sync_subagent_todos.py` | `_ACTIVE_LOOPS[t]` | 子任务创建时 | 子任务终态自动退出 | 最后一轮进度丢失（≤500ms） |
| 2 | `utils/eval_queue.py` | `_WORKER` | 首次 eval 入队 | `_stop_event.set()` | 队列中未处理任务丢失（已持久化 SQLite，重启重跑） |
| 3 | `api/message_feedback.py` | `langfuse-feedback` | 反馈提交时 | 无显式停机（daemon） | 未上传反馈丢失（SDK 内部缓冲） |
| 4 | `tools/mcp_tool.py` | MCP reload thread | MCP 配置变更后 | 无显式停机（daemon） | 无 |
| 5 | `subagents/pending_terminal.py` | `pending-terminal-reaper` | `_lifespan` 启动 | `stop_reaper()` | 残留行在 SQLite，下次启动重放 |
| 6 | `utils/retention.py` | `retention-maintenance` | `_lifespan` 启动 | `stop_maintenance()` | 当前轮未完成的清理下次继续 |
| 7 | `retrieval/embedder.py` | `embed-warmer` | `_lifespan` 启动 | `stop_warmer()` | 无落盘状态 |
| 8 | `api/metrics.py` | 指标采样 | `_lifespan` 启动 | `_metrics.stop()` | 最后窗口数据丢失 |

## 停机协议

进程退出时守护线程被强杀。以下数据可能在停机时丢失：

- **sync 线程**最后一轮进度同步（≤500ms 延迟，可接受）
- **eval 队列**中未处理任务（已持久化到 SQLite，重启后重跑）
- **Langfuse 未上传反馈**（SDK 有内部缓冲，可能丢失）
- **嵌入保活**在飞的那一轮（最长 120s，daemon 线程被杀）
- **指标采样**最后窗口（可接受，下次启动补）

## P2-1 排空与守护线程的关系

`custom_app._lifespan` 停机时先调 `drain.begin_drain()` + `wait_for_idle()`，
等待所有在跑的 run 完成。守护线程在此期间仍然活跃（sync 线程继续同步进度）。
排空完成后才依次停止各守护线程（逆序）。
