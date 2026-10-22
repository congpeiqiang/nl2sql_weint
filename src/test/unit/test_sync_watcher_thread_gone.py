"""主线程被删 ⇒ sync watcher 必须收工（2026-10-22 dev-34 事故）。

背景：用户在定时任务刚 fire 出的会话里点了「删除」。langgraph 的
`DELETE /threads/{id}` 只删注册表行（`langgraph_api/api/threads.py:delete_thread`
不 cancel 任何 run），在飞的主 run 继续跑，还在 03:30:46 派发了一个真实子任务；
它的 sync watcher 从此每 0.5s 的每一轮都撞 `NotFoundError: thread ... not found`
——而 `_async_sync_loop` 的外层 `except Exception` 只打一行 warning 就继续循环，
**没有退出条件**：实测 4 分钟刷了 6682 行日志，直到子 run 结束才可能停。

判据：NotFoundError = 会话（主线程或子线程）真的不在了，404 不是瞬时故障；
给一个短宽限（`NOT_FOUND_GRACE_S`）抵消容器切换/读副本抖动的窗口期，过了就收工。
"""

import asyncio

import pytest

from agent.subagents import sync_subagent_todos as sst


class NotFoundError(Exception):
    """与 langgraph_sdk.errors.NotFoundError **同名**——模块按类名识别。"""


# ── 判据（纯逻辑）────────────────────────────────────────────────


def test_is_thread_gone_by_class_name():
    """只认 NotFoundError：别的异常（API 抖动/解析错）继续重试，不许误杀 watcher。"""
    assert sst._is_thread_gone(NotFoundError("thread x not found"))
    assert not sst._is_thread_gone(RuntimeError("api down"))
    assert not sst._is_thread_gone(asyncio.CancelledError())


def test_guard_exits_immediately_with_zero_grace():
    guard = sst._ThreadGoneGuard(grace_s=0)
    assert guard.on_not_found() is True


def test_guard_waits_for_grace_then_exits():
    guard = sst._ThreadGoneGuard(grace_s=0.05)
    assert guard.on_not_found() is False, "第一次 never 立刻收工（可能是瞬时 404）"
    import time

    time.sleep(0.08)
    assert guard.on_not_found() is True, "宽限期过了就该收工"


def test_guard_alive_resets_clock():
    """见过一次存活信号（成功读到 run 状态）⇒ 计时清零，重新给满宽限。"""
    guard = sst._ThreadGoneGuard(grace_s=0.05)
    assert guard.on_not_found() is False
    guard.alive()
    import time

    time.sleep(0.08)
    assert guard.on_not_found() is False, "alive() 之后必须重新计时"


# ── 接线：循环真的会退出 ──────────────────────────────────────────


def _patch_loop(monkeypatch, status_seq, grace_s):
    """把循环的 IO 全替掉，只留控制流（sleep 走真实小延时，便于计次）。"""
    seen = {"calls": 0}

    async def _get_run_status(client, thread_id, expect_run_id=None):
        return "running"

    async def _read_task_status(client, main_thread_id, sub_thread_id):
        seen["calls"] += 1
        raise NotFoundError(f"thread {main_thread_id} not found")

    real_sleep = asyncio.sleep

    async def _sleep(_seconds):
        await real_sleep(0.01)

    def _no_workspace():
        raise RuntimeError("test: no workspace")

    monkeypatch.setattr(sst, "NOT_FOUND_GRACE_S", grace_s)
    monkeypatch.setattr(sst, "_get_run_status", _get_run_status)
    monkeypatch.setattr(sst, "_read_task_status", _read_task_status)
    monkeypatch.setattr(sst, "_get_workspace_manager", _no_workspace)
    monkeypatch.setattr(asyncio, "sleep", _sleep)
    return seen


def test_loop_exits_when_thread_deleted(monkeypatch):
    seen = _patch_loop(monkeypatch, None, grace_s=0)
    asyncio.run(
        asyncio.wait_for(
            sst._async_sync_loop("main-1", "sub-1", "nl2sql", {"status": "running"}),
            timeout=5,
        )
    )
    # 1 次来自启动自愈（P1-9 boot 探针，异常被它自己的 try 吞掉），1 次来自循环
    # 第一轮 —— 宽限为 0 ⇒ 循环第一次 NotFound 就收工，不再空转。
    assert seen["calls"] == 2, "宽限为 0 ⇒ 第一次 NotFound 就收工，不再空转"


def test_loop_retries_within_grace(monkeypatch):
    """宽限窗口内还要继续重试（可能有容器切换的瞬时 404），但不是无限刷。"""
    seen = _patch_loop(monkeypatch, None, grace_s=0.08)
    asyncio.run(
        asyncio.wait_for(
            sst._async_sync_loop("main-1", "sub-1", "nl2sql", {"status": "running"}),
            timeout=5,
        )
    )
    assert seen["calls"] >= 2, "宽限期内必须重试而不是立刻放弃"
