"""`apply_patch()` 的结构契约（Phase 2-2a）。

Phase 2-2a 把 `apply_patch` 函数体内 13 个内嵌函数**提升为模块级**——纯代码搬移，
零行为变更。搬移的动机是 `apply_patch` 长到 537 行，但其中 597 行都在内嵌 def 里，
真正的「装配逻辑」只有几十行——读的人得先翻过 500 行才看得到装配骨架。

本文件钉住两件事，防止将来有人把它们再塞回函数里、或挪到别的模块：

1. **被提升的名字必须是模块级属性。** `scripts/verify_*.py` 里 8 个脚本按模块路径
   读这些私有名，挪走一个就静默失效——`from x import *` 传不了下划线开头的名字。
2. **`_reapply_task_descriptions` 的合并语义。** 它是搬移清单里唯一纯函数，
   可以直测；其余 6 个要么持有 run/client 状态、要么是工厂。
"""

import pytest

# 期望出现在模块级的名字（Phase 2-2a 提升清单中的 5 个直提 + 2 个工厂产物不在此列）
_HOISTED_NAMES = [
    "_enhanced_build_check_result",
    "_enhanced_build_check_command",
    "_reapply_task_descriptions",
    "_relaunch_sync_after_redispatch",
    "_make_patched_build_check_tool",
    "_make_patched_build_update_tool",
    "_make_patched_build_list_tasks_tool",
]


class TestHoistedNamesAreModuleLevel:
    """提升后的名字必须在模块命名空间里——这是 8 个 verify 脚本的依赖面。"""

    @pytest.mark.parametrize("name", _HOISTED_NAMES)
    def test_name_exists_at_module_level(self, name: str):
        import agent.subagents.check_progress as cp

        assert hasattr(cp, name), (
            f"{name} 应已提升为模块级（Phase 2-2a）；"
            f"若它又回到 apply_patch 内部，scripts/verify_*.py 的私有名导入会静默失效"
        )

    @pytest.mark.parametrize("name", _HOISTED_NAMES)
    def test_name_is_callable(self, name: str):
        import agent.subagents.check_progress as cp

        assert callable(getattr(cp, name))


class TestPatchContract:
    """打补丁后的可观测契约——这些断言在搬移前后都必须成立。"""

    def test_module_import_applies_patch(self):
        """模块导入即打补丁（源码末尾 `apply_patch()`），幂等标志应已置位。"""
        import agent.subagents.check_progress as cp

        assert cp._PATCHED is True

    def test_apply_patch_is_idempotent(self):
        """重复调用不重复装配、不抛异常。"""
        import agent.subagents.check_progress as cp

        cp.apply_patch()
        cp.apply_patch()
        assert cp._PATCHED is True

    def test_result_and_command_are_replaced_by_identity(self):
        """这两个是**直接赋值**，因此可用 `is` 断言（工厂产物不行，见下）。"""
        import agent.subagents.check_progress as cp
        from deepagents.middleware import async_subagents as mod

        assert mod._build_check_result is cp._enhanced_build_check_result
        assert mod._build_check_command is cp._enhanced_build_check_command

    @pytest.mark.parametrize(
        "attr,expected_name",
        [
            ("_build_check_tool", "_patched_build_check_tool"),
            ("_build_update_tool", "_patched_build_update_tool"),
            ("_build_list_tasks_tool", "_patched_build_list_tasks_tool"),
        ],
    )
    def test_builders_are_wrapped_by_name(self, attr: str, expected_name: str):
        """三个 builder 是**工厂产物**（闭包），只能按名字断言，不能用 `is`。

        用 `__name__` 而非 `__qualname__`：搬移前限定名是
        `apply_patch.<locals>._patched_build_check_tool`，搬移后是
        `_make_patched_build_check_tool.<locals>._patched_build_check_tool`——
        限定名本来就会变，`__name__` 才是稳定契约。
        """
        from deepagents.middleware import async_subagents as mod

        fn = getattr(mod, attr, None)
        assert fn is not None, f"{attr} 应已被替换"
        assert fn.__name__ == expected_name


class TestReapplyTaskDescriptions:
    """`_reapply_task_descriptions(out, state)` 的合并语义（纯函数）。"""

    @staticmethod
    def _reapply(out, state):
        from agent.subagents.check_progress import _reapply_task_descriptions

        return _reapply_task_descriptions(out, state)

    @staticmethod
    def _command(update: dict):
        from langgraph.types import Command

        return Command(update=update)

    def test_non_command_passthrough(self):
        """非 Command 原样返回（update_async_task 也可能返回别的类型）。"""
        sentinel = object()
        assert self._reapply(sentinel, {}) is sentinel

    def test_no_async_tasks_passthrough(self):
        cmd = self._command({"other": 1})
        assert self._reapply(cmd, {}) is cmd
        assert cmd.update == {"other": 1}

    def test_empty_async_tasks_passthrough(self):
        cmd = self._command({"async_tasks": {}})
        assert self._reapply(cmd, {}) is cmd

    def test_restores_missing_description_from_state(self):
        """核心语义：新条目丢 description 时，从旧 state 补回。"""
        cmd = self._command({"async_tasks": {"t1": {"status": "running"}}})
        state = {"async_tasks": {"t1": {"description": "查上月工时", "status": "running"}}}

        out = self._reapply(cmd, state)

        assert out is cmd
        assert cmd.update["async_tasks"]["t1"]["description"] == "查上月工时"

    def test_keeps_existing_description(self):
        """新条目自带 description 时不覆盖。"""
        cmd = self._command({"async_tasks": {"t1": {"description": "新的"}}})
        state = {"async_tasks": {"t1": {"description": "旧的"}}}

        self._reapply(cmd, state)

        assert cmd.update["async_tasks"]["t1"]["description"] == "新的"

    def test_no_old_description_leaves_entry_untouched(self):
        """旧 state 也没有 description —— 不该凭空造一个。"""
        cmd = self._command({"async_tasks": {"t1": {"status": "running"}}})
        out = self._reapply(cmd, {"async_tasks": {"t1": {"status": "running"}}})

        assert out is cmd
        assert "description" not in cmd.update["async_tasks"]["t1"]

    def test_state_none_is_safe(self):
        cmd = self._command({"async_tasks": {"t1": {"status": "running"}}})
        out = self._reapply(cmd, None)

        assert out is cmd
        assert "description" not in cmd.update["async_tasks"]["t1"]

    def test_mixed_entries_only_fills_the_missing_one(self):
        cmd = self._command(
            {
                "async_tasks": {
                    "has": {"description": "自带"},
                    "missing": {"status": "running"},
                }
            }
        )
        state = {
            "async_tasks": {
                "has": {"description": "旧的"},
                "missing": {"description": "补回"},
            }
        }

        self._reapply(cmd, state)

        tasks = cmd.update["async_tasks"]
        assert tasks["has"]["description"] == "自带"
        assert tasks["missing"]["description"] == "补回"

    def test_other_entries_in_new_tasks_are_preserved(self):
        """合并是按整张表重建的，不能把没提 description 的条目弄丢。"""
        cmd = self._command({"async_tasks": {"t1": {"status": "running"}}})
        self._reapply(cmd, {"async_tasks": {"t1": {"description": "d"}}})

        assert set(cmd.update["async_tasks"]) == {"t1"}
