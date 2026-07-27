"""
独立的 checkpointer 模块，供 langgraph_api 通过 LANGGRAPH_CHECKPOINTER 加载。

在 graph.json / langgraph.json 中配置:
{
  "checkpointer": {
    "backend": "custom",
    "path": "./src/agent/checkpointer_factory.py:checkpointer"
  }
}

使用 AsyncSqliteSaver 以兼容 langgraph_api 的异步运行时。
通过 from_conn_string() 异步上下文管理器创建，确保事件循环可用。
"""
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

base_dir = Path(r"D:\code_work_space\llm\nl2sql\src\agent").resolve()
_CHECKPOINT_DB = str(base_dir / "workspace" / "checkpoints.sqlite")

# 导出异步上下文管理器，langgraph_api 的 _yield_checkpointer() 会自动处理
checkpointer = AsyncSqliteSaver.from_conn_string(_CHECKPOINT_DB)
