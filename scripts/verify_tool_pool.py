# -*- coding: utf-8 -*-
"""P1-10 同步工具池：满载快速失败 + 卡死回收（离线，无需后端/数据库/网络）。

**被测的坑**：同步工具路径原先是裸 `ThreadPoolExecutor(max_workers=4)`，而它的工作
队列**无界**。4 个「永不返回」的 MCP 调用占满槽位后，后续 `submit` 只会安静排队 ——
调用方要等到**自己的** timeout（语义层数据工具 300s）才拿到超时消息。而超时的任务
**不取消**（旧注释自陈「后台继续跑」），槽位永远回不来。于是：

  · 用户看到的是每个工具都「假超时」（明明一秒都没跑）；
  · 变成**全局性**故障：不是某个用户慢，是所有用户的同步工具调用一起排队。

本脚本验的口径：
  ① 满载 → 第 N+1 个调用**快速**返回「繁忙」（而不是等满自己的 timeout）；
     超时但仍在跑的调用**继续占槽**（如实反映"没有可用并发"，不是假装空闲）。
  ①b 负对照：用裸 ThreadPoolExecutor 复刻旧写法，**必须**等满自己的 timeout 才返回
     —— 证明 ① 的「快速失败」不是恒真断言，而是这版新加的。
  ② 卡死的调用真的返回后，槽位归还，后续调用正常执行。
  ③ 满载**持续**超过回收阈值 → 换新池，后续调用立刻恢复可用；且旧线程**没被杀**
     （Python 杀不掉线程，所以是「弃用」不是「取消」，如实标注）。
  ④ wrap_tool 接线：池满时 `tool._run(...)` 返回的确实是那条繁忙消息。
  ⑤ 破坏式负对照：把取槽上限拆掉（复刻旧写法）→ 同一局面变回「假超时」，
     证明 ①/④ 的断言不是恒真。

未做的部分（如实标注）：同步路径**无法真正取消**已在跑的线程（Python 无 kill），
所以本项给的是「满载快速失败 + 阈值后换池」，而不是「超时即杀线程」。

运行：
    PYTHONIOENCODING=utf-8 uv run --no-project python scripts/verify_tool_pool.py

退出码 0 = 全部通过。
"""
from __future__ import annotations

import concurrent.futures
import os
import pathlib
import sys
import tempfile
import threading
import time

os.environ.setdefault("AGENT_DATA_ROOT", tempfile.mkdtemp(prefix="nl2sql-verify-pool-"))

_HERE = pathlib.Path(__file__).resolve()
for cand in (_HERE.parents[1] / "src", pathlib.Path("/app/src")):
    if cand.is_dir() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))

OK = "\033[32mPASS\033[0m"
NG = "\033[31mFAIL\033[0m"
results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{OK if cond else NG}] {label}" + (f"  {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def wait_until(pred, timeout: float = 3.0, step: float = 0.02) -> bool:
    """等待条件成立（池的状态更新发生在别的线程里，只能轮询）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return False


def make_hang(state: dict):
    """永不返回的工具：一直等到 state['ev'] 被 set。"""
    ev = state["ev"]
    state.setdefault("started", 0)

    def hang(*_a, **_k):
        state["started"] += 1
        ev.wait()
        state["finished"] = state.get("finished", 0) + 1
        return "hung-done"

    return hang


def run_in_thread(fn, *a, **k) -> tuple[threading.Thread, dict]:
    box: dict = {}
    t = threading.Thread(target=lambda: box.update(r=fn(*a, **k)), daemon=True)
    t.start()
    return t, box


# ── ① 满载快速失败 ────────────────────────────────────────────────────

def t1_saturation() -> None:
    section("① 池满 → 第 N+1 个调用快速返回「繁忙」，且超时任务仍占槽")
    from agent.utils.path_resolver import _SyncToolPool

    state: dict = {"ev": threading.Event(), "finished": 0}
    hang = make_hang(state)
    pool = _SyncToolPool(max_workers=2, wait_seconds=0.3, recycle_after=999.0)

    # 两个调用：timeout 0.5s —— 调用方 0.5s 后拿到超时消息，但工作线程还卡着
    t1, _ = run_in_thread(pool.run, hang, (), {}, 0.5)
    t2, _ = run_in_thread(pool.run, hang, (), {}, 0.5)

    # 等待两个槽位被占（工作线程已进入 hang）
    check(wait_until(lambda: pool.stats()["inuse"] == 2), "两个槽位已被占（inuse=2）",
          str(pool.stats()))
    t1.join(3)
    t2.join(3)
    check(t1.is_alive() is False and t2.is_alive() is False, "调用方各自在超时后返回")
    inuse_after = pool.stats()["inuse"]
    check(inuse_after == 2,
          "超时**不归还**槽位：调用方已返回，工作线程还在跑（inuse 仍为 2）",
          f"inuse={inuse_after}")

    # 第 3 个调用：必须快速繁忙，且**一次都没执行**
    calls_before = state["started"]
    t0 = time.monotonic()
    status, value = pool.run(lambda: "never-run", (), {}, 30.0)
    elapsed = time.monotonic() - t0
    check(status == "busy", "第 3 个调用 → busy（旧写法这里是排队等满 30s 的假超时）",
          f"status={status}")
    check(elapsed < 2.0, "繁忙是**快速**返回，不等自己的 timeout", f"{elapsed:.2f}s « 30s")
    check(value is None and state["started"] == calls_before,
          "繁忙时那个函数**没有被执行**（不是执行后被丢结果）")
    state["ev"].set()
    wait_until(lambda: pool.stats()["inuse"] == 0)


def t1b_negative_control() -> None:
    section("①b 负对照：裸 ThreadPoolExecutor（旧写法）确实会「假超时」")
    state: dict = {"ev": threading.Event(), "finished": 0}
    hang = make_hang(state)
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=2)

    f1 = ex.submit(hang)
    f2 = ex.submit(hang)
    for f in (f1, f2):
        try:
            f.result(timeout=0.3)
        except concurrent.futures.TimeoutError:
            pass
    check(f1.done() is False and f2.done() is False, "两个任务仍卡着（槽位不会回收）")

    # 旧写法的第 3 个调用：submit 进去排队，然后只能等自己的 timeout
    third_ran = {"v": False}

    def third():
        third_ran["v"] = True
        return "ran"

    t0 = time.monotonic()
    f3 = ex.submit(third)
    try:
        f3.result(timeout=0.6)
        status_old = "ok"
    except concurrent.futures.TimeoutError:
        status_old = "timeout"
    elapsed = time.monotonic() - t0
    check(status_old == "timeout" and elapsed >= 0.55,
          "旧写法第 3 个调用**只能等满自己的 timeout**（这就是「假超时」）",
          f"{elapsed:.2f}s")
    check(third_ran["v"] is False,
          "而那个函数**根本没开始跑** → 慢是排队，不是真的在算（正是本项要修的东西）")

    # 镜像断言：同样的局面，新实现给的是 busy 而不是 timeout
    from agent.utils.path_resolver import _SyncToolPool
    pool = _SyncToolPool(max_workers=2, wait_seconds=0.2, recycle_after=999.0)
    run_in_thread(pool.run, hang, (), {}, 0.2)
    run_in_thread(pool.run, hang, (), {}, 0.2)
    wait_until(lambda: pool.stats()["inuse"] == 2)
    new_status, _ = pool.run(third, (), {}, 0.6)
    check(new_status == "busy", "同一局面新实现给 busy（可区分「排队」与「超时」）",
          f"status={new_status}")
    state["ev"].set()
    wait_until(lambda: pool.stats()["inuse"] == 0)
    ex.shutdown(wait=False)


# ── ② 卡死调用返回后槽位归还 ──────────────────────────────────────────

def t2_release_on_return() -> None:
    section("② 卡死的调用真的返回 → 槽位归还，后续调用正常")
    from agent.utils.path_resolver import _SyncToolPool

    state: dict = {"ev": threading.Event(), "finished": 0}
    hang = make_hang(state)
    pool = _SyncToolPool(max_workers=1, wait_seconds=0.2, recycle_after=999.0)

    status, _ = pool.run(hang, (), {}, 0.2)
    check(status == "timeout" and pool.stats()["inuse"] == 1,
          "先占满唯一槽位（超时返回，槽位仍被占）", str(pool.stats()))
    check(pool.run(lambda: "x", (), {}, 1.0)[0] == "busy", "此时第 2 个调用 busy")

    state["ev"].set()  # 卡死的那次调用终于结束
    check(wait_until(lambda: pool.stats()["inuse"] == 0),
          "调用结束后槽位自动归还（inuse 回 0）", str(pool.stats()))
    status, value = pool.run(lambda: "ok-value", (), {}, 2.0)
    check(status == "ok" and value == "ok-value", "随后调用恢复正常执行", f"{status}/{value}")

    # 工具抛异常也要归还槽位（否则一次异常 = 永久少一个槽）
    def boom():
        raise ValueError("tool failed")

    try:
        pool.run(boom, (), {}, 2.0)
    except ValueError:
        pass
    check(pool.stats()["inuse"] == 0, "工具抛异常同样归还槽位（finally 里减）",
          str(pool.stats()))


# ── ③ 满载持续 → 回收换新池 ───────────────────────────────────────────

def t3_recycle() -> None:
    section("③ 满载超阈值 → 换新池，恢复可用（且旧线程没被杀）")
    from agent.utils.path_resolver import _SyncToolPool

    state: dict = {"ev": threading.Event(), "finished": 0}
    hang = make_hang(state)
    # 阈值从「首次观测到满」起算，且**只在再次尝试取槽时**判定 —— 所以取
    # wait_seconds=0.1 / recycle_after=0.5：一次等待（0.1s）跨不过 0.5s 的阈值，
    # 不会像上一版（wait 0.2 / 阈值 0.3）那样在同一个等待循环里就把自己回收掉。
    pool = _SyncToolPool(max_workers=1, wait_seconds=0.1, recycle_after=0.5)

    check(pool.run(hang, (), {}, 0.15)[0] == "timeout", "唯一槽位被卡死调用占住")
    check(pool.run(lambda: "x", (), {}, 0.6)[0] == "busy",
          "第 1 次再尝试 → busy（同时开始计满负荷时长）")
    check(pool.run(lambda: "x", (), {}, 0.6)[0] == "busy", "满载时长未到阈值 → 仍 busy")
    check(pool.recycles == 0, "尚未回收（阈值没到就不换池）", f"recycles={pool.recycles}")

    time.sleep(0.55)  # 让满载时长越过阈值
    status, value = pool.run(lambda: "fresh", (), {}, 3.0)
    check(status == "ok" and value == "fresh",
          "越过阈值后 → 换新池，调用立刻成功（不再无限繁忙）", f"{status}/{value}")
    check(pool.recycles == 1, "回收计数 +1（有日志可观测）", f"recycles={pool.recycles}")
    check(state.get("finished", 0) == 0,
          "旧线程**没有被杀**：Python 杀不掉线程，所以是「弃用」而非「取消」（如实标注）")

    state["ev"].set()
    check(wait_until(lambda: state.get("finished") == 1),
          "旧调用自己结束时正常退出（线程最终收敛，不是永久泄漏）")


# ── ④ wrap_tool 接线 ──────────────────────────────────────────────────

def t4_wrap_tool() -> None:
    section("④ wrap_tool 接线：池满时 tool._run 返回繁忙消息")
    from agent.utils import path_resolver as pr

    class _Tool:
        name = "grep"          # _TOOL_TIMEOUTS["grep"] = 30
        description = "fake"

        def _run(self, *a, **k):
            return "grep-result"

    state: dict = {"ev": threading.Event(), "finished": 0}
    hang = make_hang(state)

    # 用一个小池顶替进程级单例，便于造出「满载」局面
    small = pr._SyncToolPool(max_workers=1, wait_seconds=0.2, recycle_after=999.0)
    old_pool, pr._TOOL_POOL = pr._TOOL_POOL, small
    try:
        tool = _Tool()
        pr.wrap_tool(tool)
        check(tool._run("x") == "grep-result", "池空闲时 _run 正常（接线没改坏原行为）")

        small.run(hang, (), {}, 0.2)          # 占满唯一槽位
        check(small.stats()["inuse"] == 1, "槽位已满", str(small.stats()))
        t0 = time.monotonic()
        out = tool._run("x")
        elapsed = time.monotonic() - t0
        # 契约与超时分支一致：MCP 工具的返回是 (content, artifact) 二元组
        check(isinstance(out, tuple) and len(out) == 2 and out[0] == pr._TOOL_BUSY_MSG,
              "池满 → _run 返回繁忙消息（而不是 30s 假超时）", repr(out[0] if isinstance(out, tuple) else out)[:40])
        check(elapsed < 2.0, "返回很快", f"{elapsed:.2f}s")
        check(out[0] != pr._TOOL_TIMEOUT_MSG.format(timeout=30),
              "繁忙消息与超时消息**不是同一条**（LLM 才能分辨「没跑」和「跑超时了」")
    finally:
        state["ev"].set()
        pr._TOOL_POOL = old_pool


# ── ⑤ 破坏式负对照：把快速失败拆掉 ────────────────────────────────────

def t5_destructive_control() -> None:
    section("⑤ 负对照（破坏式）：拆掉取槽上限 → 「繁忙」断言必须失败")
    from agent.utils import path_resolver as pr

    state: dict = {"ev": threading.Event(), "finished": 0}
    hang = make_hang(state)
    pool = pr._SyncToolPool(max_workers=1, wait_seconds=0.3, recycle_after=999.0)
    pool.run(hang, (), {}, 0.2)          # 占满唯一槽位
    check(pool.run(lambda: "x", (), {}, 0.5)[0] == "busy", "未破坏时 → busy（基线）")

    original = pr._SyncToolPool._acquire
    pr._SyncToolPool._acquire = lambda self: True   # 复刻旧写法：来者不拒，直接排队
    try:
        t0 = time.monotonic()
        status, _ = pool.run(lambda: "x", (), {}, 0.5)
        elapsed = time.monotonic() - t0
        check(status == "timeout" and elapsed >= 0.45,
              "拆掉上限后同一局面变成「假超时」（0.5s 才返回）→ ⑤ 的断言不是恒真",
              f"status={status} {elapsed:.2f}s")
        check(status != "busy", "「繁忙」这条路径确实是本次新加的守卫，不是原有行为")
    finally:
        pr._SyncToolPool._acquire = original
        state["ev"].set()
        wait_until(lambda: pool.stats()["inuse"] == 0)


def main() -> int:
    print(f"临时 AGENT_DATA_ROOT = {os.environ['AGENT_DATA_ROOT']}")
    t1_saturation()
    t1b_negative_control()
    t2_release_on_return()
    t3_recycle()
    t4_wrap_tool()
    t5_destructive_control()

    failed = [label for ok, label in results if not ok]
    print(f"\n{'=' * 60}\n{len(results) - len(failed)}/{len(results)} 通过")
    for label in failed:
        print(f"  {NG} {label}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
