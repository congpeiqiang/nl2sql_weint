"""ProgressTrackerMiddleware — 从 write_todos ToolMessage 提取进度并记录每步耗时。"""
import json, os, tempfile, time, logging
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

_logger = logging.getLogger(__name__)


def _progress_dir():
    d = os.path.join(tempfile.gettempdir(), "nl2sql_progress")
    os.makedirs(d, exist_ok=True)
    return d


def _progress_path(thread_id: str) -> str:
    fname = thread_id.replace("/", "_") if thread_id else "latest"
    return os.path.join(_progress_dir(), f"{fname}.json")


class ProgressTrackerMiddleware(AgentMiddleware):
    def wrap_model_call(self, request, handler):
        return handler(request)

    async def awrap_model_call(self, request, handler):
        response = await handler(request)
        try:
            # 从 messages 历史中找到最后一次 write_todos 的结果
            todos_list = []
            for msg in reversed(request.messages):
                if isinstance(msg, ToolMessage) and msg.name == "write_todos":
                    content = msg.content if isinstance(msg.content, str) else str(msg.content)
                    if "completed" in content:
                        import re
                        items = re.findall(
                            r"\{'content':\s*'([^']*)',\s*'status':\s*'([^']*)'\}",
                            content,
                        )
                        if items:
                            todos_list = [{"content": c, "status": s} for c, s in items]
                            break

            if todos_list:
                thread_id = ""
                try:
                    rt = request.runtime
                    if rt and hasattr(rt, 'thread_id'):
                        thread_id = rt.thread_id
                    elif rt and hasattr(rt, 'execution_info') and rt.execution_info:
                        thread_id = rt.execution_info.thread_id
                except Exception:
                    pass

                now = time.time()
                fpath = _progress_path(thread_id)

                # 读取上一次的进度，用于检测状态变化
                prev_todos = {}
                step_history = []
                started_at = now
                if os.path.exists(fpath):
                    try:
                        with open(fpath, "r", encoding="utf-8") as pf:
                            old = json.load(pf)
                        step_history = old.get("step_history", [])
                        started_at = old.get("started_at", now)
                        for t in old.get("todos", []):
                            prev_todos[t["content"]] = t["status"]
                    except Exception:
                        pass

                # 检测状态变化，记录转换时间
                for t in todos_list:
                    prev_status = prev_todos.get(t["content"])
                    curr_status = t["status"]
                    if prev_status != curr_status:
                        step_history.append({
                            "step": t["content"],
                            "from": prev_status,
                            "to": curr_status,
                            "ts": now,
                        })

                completed = sum(1 for t in todos_list if t["status"] == "completed")
                progress = {
                    "task_id": thread_id,
                    "todos": todos_list,
                    "total": len(todos_list),
                    "completed": completed,
                    "in_progress": sum(1 for t in todos_list if t["status"] == "in_progress"),
                    "started_at": started_at,
                    "last_updated_at": now,
                    "step_history": step_history,
                }

                with open(fpath, 'w', encoding='utf-8') as pf:
                    json.dump(progress, pf, ensure_ascii=False, indent=2)
                _logger.info(f"[Progress] wrote {completed}/{len(todos_list)}")
            else:
                _logger.info("[Progress] no write_todos found in messages")
        except Exception as e:
            _logger.error(f"[Progress] error: {e}", exc_info=True)

        return response


def read_progress(thread_id: str) -> dict | None:
    """读取指定任务的本地进度文件（供 check_progress.py 调用）。"""
    fpath = _progress_path(thread_id)
    if not os.path.exists(fpath):
        return None
    try:
        with open(fpath, "r", encoding="utf-8") as pf:
            return json.load(pf)
    except Exception:
        return None
