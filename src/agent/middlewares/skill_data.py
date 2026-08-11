"""
@File    :  skill_data.py
@Author  :  CongPeiQiang
@Time    :  2026/7/29 14:53
@Desc    :  
"""
# agent/middleware/skill_data.py
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional
from datetime import datetime

from langchain.agents.middleware import AgentMiddleware


# try:
#     from langchain.agents.middleware import wrap_tool_call
#     HAS_WRAP_TOOL_CALL = True
# except ImportError:
#     HAS_WRAP_TOOL_CALL = False
#     # ✅ 修复 2: 如果导入失败，定义占位符装饰器
#     # 这样 wrap_tool_call 始终存在，不会报错
#     def wrap_tool_call(func):
#         return func

class SkillDataMiddleware(AgentMiddleware):
    """
    Skill 数据传输中间件 - 按 thread_id 隔离
    存储路径: /workspace/nl2sql_process_data/{thread_id}/knowledge.json
    """

    def __init__(self, backend=None, data_dir: str = "/workspace/nl2sql_process_data"):
        self.backend = backend
        self.data_dir = data_dir
        self._current_thread_id: Optional[str] = None

        # ============================================
        # 🔧 新增：LangGraph 中间件接口
        # ============================================

        def wrap_tool_call(self, request, call_next):
            """
            LangGraph 中间件标准接口
            在工具调用前后注入 Skill 数据
            """
            # 1. 预处理：在调用工具前注入 session 数据
            if hasattr(request, 'tool_call'):
                tool_name = request.tool_call.name
                print(f"🔧 [SkillDataMiddleware] 执行工具: {tool_name}")

                # 从请求中提取 thread_id
                if hasattr(request, 'state') and request.state:
                    thread_id = request.state.get('thread_id')
                    if thread_id:
                        self.set_thread_id(thread_id)
                        print(f"📌 [SkillDataMiddleware] 会话 ID: {thread_id}")

            # 2. 调用下一个中间件或执行工具
            try:
                response = call_next(request)
                print(f"✅ [SkillDataMiddleware] 工具执行成功")
                return response
            except Exception as e:
                print(f"❌ [SkillDataMiddleware] 工具执行失败: {e}")
                raise

        def __call__(self, request, call_next):
            """
            兼容旧版本的调用方式
            """
            return self.wrap_tool_call(request, call_next)

    def set_thread_id(self, thread_id: str):
        """设置当前会话 ID"""
        self._current_thread_id = thread_id

        # 确保会话目录存在
        if self.backend and thread_id:
            session_dir = f"{self.data_dir}/{thread_id}"
            self.backend.create_dir(session_dir)

    def _get_session_dir(self) -> str:
        """获取当前会话的目录路径"""
        if not self._current_thread_id:
            raise RuntimeError("❌ thread_id 未设置，请先调用 set_thread_id()")
        return f"{self.data_dir}/{self._current_thread_id}"

    def _get_file_path(self, filename: str) -> str:
        """获取当前会话的文件路径"""
        return f"{self._get_session_dir()}/{filename}"

    def save_output(self, skill_name: str, data: Dict[str, Any], filename: str) -> None:
        """
        保存 Skill 输出到当前会话

        Args:
            skill_name: Skill 名称
            data: 要保存的数据
            filename: 文件名（默认 knowledge.json）
        """
        if not self.backend:
            return

        # 在会话目录下按 Skill 分目录存储
        skill_dir = f"{self._get_session_dir()}/{skill_name}"
        self.backend.create_dir(skill_dir)

        file_path = f"{skill_dir}/{filename}"

        # 读取现有数据
        existing_data = {}
        try:
            content = self.backend.read(file_path)
            if content:
                existing_data = json.loads(content)
        except:
            pass

        # 合并数据（保留历史记录）
        if "history" not in existing_data:
            existing_data["history"] = []

        # 添加新记录
        existing_data["history"].append({
            "timestamp": datetime.now().isoformat(),
            "data": data
        })

        # 保存最新数据到根
        existing_data["current"] = data
        existing_data["last_updated"] = datetime.now().isoformat()

        # 写入文件
        self.backend.write(file_path, json.dumps(existing_data, indent=2, ensure_ascii=False))

    def get_output(self, skill_name: str, key: Optional[str] = None, filename: str = "knowledge.json") -> Any:
        """
        获取指定 Skill 的输出数据

        Args:
            skill_name: Skill 名称
            key: 可选，指定获取的键（如 "current", "history"）
            filename: 文件名
        """
        if not self.backend or not self._current_thread_id:
            return None

        file_path = f"{self._get_session_dir()}/{skill_name}/{filename}"

        try:
            content = self.backend.read(file_path)
            if not content:
                return None

            data = json.loads(content)

            if key:
                return data.get(key)
            return data.get("current")  # 默认返回最新数据
        except:
            return None

    def get_all_outputs(self) -> Dict[str, Any]:
        """获取当前会话所有 Skill 的输出"""
        if not self.backend or not self._current_thread_id:
            return {}

        result = {}
        session_dir = self._get_session_dir()

        try:
            # 列出所有 Skill 目录
            items = self.backend.list_dir(session_dir)

            for item in items:
                # 检查是否是 Skill 目录
                skill_path = f"{session_dir}/{item}"
                if self.backend.is_dir(skill_path):
                    # 读取 knowledge.json
                    data = self.get_output(item)
                    if data:
                        result[item] = data
        except:
            pass

        return result

    def get_session_data(self, thread_id: str, skill_name: Optional[str] = None) -> Any:
        """
        获取指定会话的数据（用于调试）

        Args:
            thread_id: 会话 ID
            skill_name: 可选，指定 Skill 名称
        """
        if not self.backend:
            return None

        session_dir = f"{self.data_dir}/{thread_id}"

        if skill_name:
            file_path = f"{session_dir}/{skill_name}/knowledge.json"
            try:
                content = self.backend.read(file_path)
                return json.loads(content) if content else None
            except:
                return None
        else:
            # 返回整个会话目录的内容
            result = {}
            try:
                items = self.backend.list_dir(session_dir)
                for item in items:
                    skill_path = f"{session_dir}/{item}"
                    if self.backend.is_dir(skill_path):
                        file_path = f"{skill_path}/knowledge.json"
                        content = self.backend.read(file_path)
                        if content:
                            result[item] = json.loads(content)
            except:
                pass
            return result

    def clear_session(self, thread_id: Optional[str] = None) -> None:
        """清除指定会话的数据"""
        if not self.backend:
            return

        target_id = thread_id or self._current_thread_id
        if not target_id:
            return

        session_dir = f"{self.data_dir}/{target_id}"
        try:
            self.backend.delete_dir(session_dir)
        except:
            pass

    def list_sessions(self) -> list:
        """列出所有有数据的会话"""
        if not self.backend:
            return []

        result = []
        try:
            items = self.backend.list_dir(self.data_dir)
            for item in items:
                if self.backend.is_dir(f"{self.data_dir}/{item}"):
                    # 检查是否有 Skill 数据
                    skill_dirs = self.backend.list_dir(f"{self.data_dir}/{item}")
                    if skill_dirs:
                        result.append(item)
        except:
            pass

        return result