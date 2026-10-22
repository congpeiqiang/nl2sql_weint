"""聊天工具 manage_scheduled_task 单测（阶段 3，方案 §7）。

覆盖面：
- 注入铁律：模块**没有** `from __future__ import annotations`（AST 检查）、
  `"runtime" in tool._injected_args_keys`（verify_attribution §⑰ 同款断言——
  2026-09-30 生产事故：字符串化注解让 ToolRuntime 注入静默失效）
- manage() 纯分发层：create/list/pause/resume/remove/run_now + 未知 action +
  缺身份/缺 task_id 拒绝；ScheduleError → {"ok":false,"error"} 机器可读文案
- 单一实现纪律：全部动作落到 agent.scheduler.service（NO second job engine）
- origin_thread_id 盖章（回溯创建现场）
"""
import ast
import json
from pathlib import Path

import pytest

from agent.scheduler import service
from agent.tools.manage_scheduled_task import TOOL_NAME, manage

TZ = "Asia/Shanghai"


@pytest.fixture
def store(tmp_path):
    from agent.scheduler.store import ScheduledTaskStore

    return ScheduledTaskStore(tmp_path / "scheduled_tasks.sqlite")


@pytest.fixture
def user(monkeypatch):
    monkeypatch.setattr(service, "can_access_db", lambda u, db: db == "db1")
    return {"user_id": "u1", "is_admin": False}


def _payload(**kw):
    base = dict(
        name="每日工时报告",
        prompt="统计上月各部门工时并生成报告",
        db_name="db1",
        sched_kind="cron",
        sched_expr="0 9 * * *",
    )
    base.update(kw)
    return base


class TestInjectionIronLaw:
    def test_module_has_no_future_annotations_import(self):
        import agent.tools.manage_scheduled_task as mod

        src = Path(mod.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                names = [a.name for a in node.names]
                assert "annotations" not in names, (
                    "工具模块禁止 from __future__ import annotations —— "
                    "ToolRuntime 注入会静默失效（_injected_args_keys 判据）"
                )

    def test_runtime_is_injected(self):
        from agent.tools.manage_scheduled_task import manage_scheduled_task_tool

        assert "runtime" in manage_scheduled_task_tool._injected_args_keys
        assert manage_scheduled_task_tool.name == TOOL_NAME


class TestManageDispatch:
    def test_create_ok(self, store, user):
        out = json.loads(manage("create", user, _payload(), store=store))
        assert out["ok"] is True
        task = out["task"]
        assert task["name"] == "每日工时报告"
        assert task["sched_display"] == "每天 09:00"
        assert task["next_run_at"].endswith("+00:00")
        # 落库验证（走的是 service 单一实现）
        assert store.get_task(task["id"])["owner"] == "u1"

    def test_create_stamps_origin_thread(self, store, user):
        out = json.loads(manage("create", user, _payload(), thread_id="th-9", store=store))
        assert store.get_task(out["task"]["id"])["origin_thread_id"] == "th-9"

    def test_create_validation_error_machine_readable(self, store, user):
        out = json.loads(manage("create", user, _payload(sched_kind="interval", sched_expr="1"), store=store))
        assert out["ok"] is False
        assert "分钟" in out["error"]

    def test_list_compact(self, store, user):
        manage("create", user, _payload(), store=store)
        manage("create", user, _payload(name="第二个"), store=store)
        out = json.loads(manage("list", user, {}, store=store))
        assert out["ok"] is True
        assert len(out["tasks"]) == 2
        row = out["tasks"][0]
        # 紧凑字段集（防 MessageSlimmer 8000 截断）：不带 prompt 全文
        assert "prompt" not in row
        assert {"id", "name", "sched_display", "state", "next_run_at", "last_status"} <= set(row)

    def test_pause_resume(self, store, user):
        tid = json.loads(manage("create", user, _payload(), store=store))["task"]["id"]
        out = json.loads(manage("pause", user, {"task_id": tid}, store=store))
        assert out["ok"] is True and out["task"]["state"] == "paused"
        out = json.loads(manage("resume", user, {"task_id": tid}, store=store))
        assert out["ok"] is True and out["task"]["state"] == "scheduled"

    def test_run_now(self, store, user, monkeypatch):
        fired = []
        monkeypatch.setattr(service, "submit_fire", fired.append)
        tid = json.loads(manage("create", user, _payload(), store=store))["task"]["id"]
        out = json.loads(manage("run_now", user, {"task_id": tid}, store=store))
        assert out["ok"] is True
        assert out["task"]["running"] is True  # 紧凑视图：running 布尔（不裸露 thread id）
        assert fired == [tid]

    def test_remove_then_again_404(self, store, user):
        tid = json.loads(manage("create", user, _payload(), store=store))["task"]["id"]
        assert json.loads(manage("remove", user, {"task_id": tid}, store=store))["ok"] is True
        out = json.loads(manage("remove", user, {"task_id": tid}, store=store))
        assert out["ok"] is False
        assert "不存在" in out["error"]

    def test_runs_history(self, store, user):
        tid = json.loads(manage("create", user, _payload(), store=store))["task"]["id"]
        out = json.loads(manage("runs", user, {"task_id": tid}, store=store))
        assert out["ok"] is True and out["runs"] == []

    def test_unknown_action(self, store, user):
        out = json.loads(manage("launch", user, {}, store=store))
        assert out["ok"] is False
        assert "create" in out["error"] and "run_now" in out["error"]

    def test_missing_identity_refused(self, store):
        out = json.loads(manage("list", None, {}, store=store))
        assert out["ok"] is False
        assert "身份" in out["error"]

    def test_task_action_without_task_id(self, store, user):
        out = json.loads(manage("pause", user, {}, store=store))
        assert out["ok"] is False
        assert "task_id" in out["error"]

    def test_other_user_task_invisible(self, store, user):
        tid = json.loads(manage("create", user, _payload(), store=store))["task"]["id"]
        intruder = {"user_id": "u2", "is_admin": False}
        out = json.loads(manage("pause", intruder, {"task_id": tid}, store=store))
        assert out["ok"] is False
        assert "不存在" in out["error"]
