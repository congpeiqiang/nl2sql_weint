"""
独立的 checkpointer 模块，供 langgraph_api 通过 LANGGRAPH_CHECKPOINTER 加载。

在 langgraph.json 中配置:
{
  "checkpointer": {
    "backend": "custom",
    "path": "./src/agent/checkpointer_factory.py:checkpointer"
  }
}
"""
import sqlite3
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

base_dir = Path(r"D:\code_work_space\llm\nl2sql\src\agent").resolve()
_CHECKPOINT_DB = str(base_dir / "workspace" / "checkpoints.sqlite")
_conn = sqlite3.connect(_CHECKPOINT_DB, check_same_thread=False)
checkpointer = SqliteSaver(_conn)
