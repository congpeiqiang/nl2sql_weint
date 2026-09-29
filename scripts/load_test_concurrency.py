# -*- coding: utf-8 -*-
"""P2-7 真实并发压测（**在 nl2sql 后端容器内跑**，真实 HTTP + 真实模型调用）。

回答清单 P2-7 的验收：N 个并发问数 → 队列深度 / P95 延迟 / 内存 / MCP 子进程数 的曲线，
并**校验评估报告 §1 的推导「一次问数 ≈ 3 个 run ⇒ 并发用户约 3~4 人」**。

测量原理（为什么不自己数 run、只看指标）：
  * `/metrics` 的 `nl2sql_run_queue_running`（= langgraph `Runs.stats().n_running`）是**全局**
    在跑 run 数，含主 run、子 agent 的独立后台 run、以及子任务成功后的自动续跑 run
    —— 这正是「一次问数吃几个槽」的直接观测值：`峰值 n_running ÷ 并发问数`。
  * 另采：pending（排队）、worker 槽位余量、事件循环延迟、RSS、子进程数、MCP 注册表、
    LLM 三态计数与耗时（取**差值**）、SQLite 锁竞争。
  * 每个并发单元的"完成"不是看主 run 成功，而是看 `/api/threads/{tid}/run-status` 落到
    「无活跃 run + 最后一条是终稿」（= 端点自己的 `turn_incomplete=False`，见
    `turn_settled`）——它把**自动续跑那一轮**也算进延迟里，否则会系统性低估真实用户的
    等待时间。**不再要求 `next` 为空**：那会把「答复已终稿但图头挂着一个没人推进的幽灵
    节点」误报成卡死（P2-12）。
  * ⚠️ **光有 `turn_settled` 还不够**（2026-09-28 实测纠正）：主 run 派完子任务后会先说一句
    「查询已提交（任务ID: …），请稍候。」。这句是普通 AI 消息、子 agent 又跑在**自己的
    thread** 里（主线程 `has_active_run=False`）⇒ 端点**完全合法地**在 ack 处报"这一轮结束"。
    实测那次 ack 在 ~5s 出现、真答案在 **+151s** 的自动续跑轮（runs 1→2）里到 —— 只看端点
    会把 ack 延迟当成 `latency_turn`，**系统性低估 2 个数量级**。因此判据加一条：末条 AI
    消息命中 `ACK_MARK` 就不算结束，继续等续跑；等不到则记为 `ack_only_no_continuation`
    （**独立结局**，绝不冒充成功，也不与"超时"混为一谈）。`latency_ack` 单独记
    （"提交后多久给回执"本身是个有用的数）。
  * ack 窗口的时长 = 子 agent 流水线时长（~150s），所以**光靠时间分不出**「子会话还在跑」
    与「子会话完了但续跑没来」—— 后者才是真问题。ack 里那句「任务ID: …」就是子会话的
    thread_id，于是额外探一次它的 `run-status`：`working` → 长尾，继续等（`--ack-grace`
    默认 300s 兜底）；`done` 却仍无续跑 → **自终稿起**再等 60s 才判断链；读不到 → `unknown`，
    退回纯时间判据（宁可等，也不把读不到的会话误报成断链）。
    计时口径必须钉在"自 done 起"（不是"自 ack 起"）：子会话自己就能跑 4 分钟（口径核验
    打回重跑），按 ack 起算会在它刚完成的那一刻立刻误判成断链 —— 2026-09-28 实测踩过一次。

跑法（本机 Git Bash；脚本不进仓库镜像、用 stdin 灌进容器）：
    ssh -o BatchMode=yes weint@192.168.25.64 \
      "docker exec -i <容器> /app/.venv/bin/python - --confirm --levels 1,2,4,6 --rounds 3" \
      < scripts/load_test_concurrency.py

安全栏（这是压生产，默认值都按"保护真实用户"取）：
  * `--confirm` 必给（没有它只打印计划，不提交任何 run）；
  * `--lag-abort`（默认 5.0s）事件循环延迟超阈值 → **立刻停止后续档位**（已提交的等它跑完）；
  * `--max-seconds`（默认 2400s）全局墙钟上限；`--budget-queries`（默认 60）总问数上限；
  * 每档结束等队列清零（`--quiet-timeout`，默认 300s）再进下一档，避免档位互相污染；
  * 检测到 `awaiting_interrupt` 立刻取消那个 run（审批态会占槽最长 2 小时，
    绝不能让压测把槽位挂住）；
  * 收尾：取消所有没正常结束的 run，并按 `--keep-threads` 决定是否删掉本次造的会话。

⚠️ 「账号数 ≠ 并发数」的说明：**并发轴是"并发问数"**（每个并发单元 = 一个独立会话 + 一次
真实问数），账号只是发问的马甲。run 槽位是**全局**池、与会话/账号无关，因此这对容量曲线
没有影响；受影响的是"多账号各自的授权/归属路径"那部分。默认会用生产里所有账号（含真人
admin / Z0051）⇒ **多用户测试必须显式 `--accounts load01,…,load06`**，把并发轴钉在一次性
测试账号上：既保住「多用户」这个口径（不同账号 = 不同身份、各自 provider 与 grants），
也不去消耗真人账号的配额（它们配的是另一个 provider，会把模型链也搅进来）。
"""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any

import httpx

sys.path.insert(0, "/app/src")
from agent.auth.token import sign_token  # noqa: E402

BASE = "http://localhost:2026"
GRAPH = "chat_agent"
TERMINAL_RUN = {"success", "error", "timeout", "interrupted", "cancelled"}

# 固定问题集：**真实问数**（会走 主 run → nl2sql 子 agent 独立 run → 自动续跑），
# 且都是"小结果集"的计数/取前 N 类问题 —— 压的是并发与编排开销，不是目标库的扫描能力。
# 第一个在口径治理里已拍板（应报工池在职 = 190），顺带能看并发下会不会答错。
DEFAULT_QUESTIONS = [
    "应报工池在职人数是多少？",
    "有多少个部门？",
    "有多少张表？",
    "列出人数最多的前 10 个部门",
]

# 主 run 派完子任务后的那句 ack（`start_async_task` 之后必然出现）。
# ⚠️ 它不是"这一轮的结束" —— 真答案在**自动续跑那一轮**，见 one_query 的结算注释。
ACK_MARK = "查询已提交"


def _last_ai_text(client: httpx.Client, tid: str) -> str:
    """末条 AI 消息的文本（只用来识别 ack）。读不到 → 空串 = 当作"不是 ack"，
    退回端点的结论字段（宁可少管，也不要因为多打一次 /state 失败就把成功算成失败）。"""
    try:
        st = client.get(f"/threads/{tid}/state")
        if st.status_code != 200:
            return ""
        msgs = ((st.json() or {}).get("values") or {}).get("messages") or []
        for m in reversed(msgs):
            if isinstance(m, dict) and m.get("type") == "ai":
                c = m.get("content")
                if isinstance(c, list):
                    c = "".join(p.get("text", "") for p in c if isinstance(p, dict))
                return str(c or "")
    except Exception:  # noqa: BLE001
        return ""
    return ""


# ack 里那句「任务ID: xxx」就是**子 agent 会话自己的 thread_id**（start_async_task 建的新 thread）。
ACK_TASK_RE = re.compile(r"任务\s*ID\s*[:：]\s*([0-9a-fA-F][0-9a-fA-F-]{7,})")


def _subagent_state(client: httpx.Client, ack_text: str) -> tuple[str, str]:
    """子会话三态：`("working"|"done"|"unknown", sub_tid)`。

    为什么值得在 ack 窗口里多打这一次请求：ack 窗口的时长**就是子 agent 流水线的时长**
    （实测 ~151s，报告类问数还要走口径打回更久）。只看时间没法把两件性质完全不同的事分开：
      · 子会话还在跑    → 只是长尾，等下去就有答案（**正常**）；
      · 子会话已终稿却等不来续跑 → 通知/续跑断链（**真问题**，P2-7 要的结论）。
    读不到（id 提不出 / 401 / 网络）一律 "unknown" ⇒ 退回纯时间判据，绝不当成断链。
    """
    m = ACK_TASK_RE.search(ack_text or "")
    if not m:
        return "unknown", ""
    sub = m.group(1)
    try:
        g = client.get(f"/api/threads/{sub}/run-status")
        if g.status_code != 200:
            return "unknown", sub
        j = g.json() or {}
        if j.get("has_active_run") or j.get("turn_incomplete"):
            return "working", sub
        return "done", sub
    except Exception:  # noqa: BLE001
        return "unknown", sub


# ── 指标解析 ────────────────────────────────────────────────────────


def _labels_of(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in raw.strip("{}").split(","):
        if "=" in part:
            k, _, v = part.partition("=")
            out[k.strip()] = v.strip().strip('"')
    return out


def parse_prom(text: str) -> dict[str, list[tuple[dict[str, str], float]]]:
    """把 prometheus 文本解析成 {指标名: [(labels, 值)]}（同名不同 label 的多行都留着）。"""
    res: dict[str, list[tuple[dict[str, str], float]]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        head, _, rest = line.partition(" ")
        labels: dict[str, str] = {}
        if "{" in head:
            name, _, lab = head.partition("{")
            labels = _labels_of(lab.rstrip("}"))
        else:
            name = head
        try:
            val = float(rest.strip().split()[0])
        except (ValueError, IndexError):
            continue
        res.setdefault(name, []).append((labels, val))
    return res


def prom_value(prom: dict, name: str, **want: str) -> float | None:
    """取某个带标签序列的值；找不到 → None（**不是 0**：没采到和真的是 0 要分开）。"""
    for labels, val in prom.get(name, []):
        if all(labels.get(k) == v for k, v in want.items()):
            return val
    return None


def prom_sum(prom: dict, name: str, **want: str) -> float | None:
    """某个指标所有（匹配标签的）序列求和；一条都没有 → None。"""
    tot, seen = 0.0, False
    for labels, val in prom.get(name, []):
        if all(labels.get(k) == v for k, v in want.items()):
            tot += val
            seen = True
    return tot if seen else None


class Sampler(threading.Thread):
    """后台采样：每 interval 秒读一次 `/metrics`（prometheus 文本 + `?format=json` 队列快照）。

    用**已鉴权**的客户端（`/metrics` 可能落在鉴权中间件后面；带 cookie 最坏也无副作用）。
    采样失败只记 `error`，绝不让压测本身挂掉。
    """

    def __init__(self, client: httpx.Client, interval: float = 2.0) -> None:
        super().__init__(daemon=True, name="load-sampler")
        self.interval = interval
        self.samples: list[dict[str, Any]] = []
        self._client = client
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.samples.append(self._sample())
            except Exception as e:  # noqa: BLE001
                self.samples.append({"ts": time.time(), "error": f"{type(e).__name__}: {e}"[:200]})
            self._stop.wait(self.interval)

    def _sample(self) -> dict[str, Any]:
        prom = parse_prom(self._client.get("/metrics").text)
        snap = json.loads(self._client.get("/metrics", params={"format": "json"}).text)
        q = snap.get("queue") or {}
        w = snap.get("workers") or {}
        return {
            "ts": time.time(),
            "running": prom_value(prom, "nl2sql_run_queue_running"),
            "pending": prom_value(prom, "nl2sql_run_queue_pending"),
            "available": prom_value(prom, "nl2sql_worker_slots", state="available"),
            "lag": prom_value(prom, "nl2sql_event_loop_lag_seconds"),
            "lag_max": prom_value(prom, "nl2sql_event_loop_lag_max_seconds"),
            "rss": prom_value(prom, "nl2sql_process_rss_bytes"),
            "children": prom_value(prom, "nl2sql_process_children"),
            "mcp_ok": prom_sum(prom, "nl2sql_mcp_servers", status="ok"),
            "mcp_failed": prom_sum(prom, "nl2sql_mcp_servers", status="failed"),
            "llm_ok": prom_sum(prom, "nl2sql_llm_calls_total", outcome="ok"),
            "llm_timeout": prom_sum(prom, "nl2sql_llm_calls_total", outcome="timeout"),
            "llm_error": prom_sum(prom, "nl2sql_llm_calls_total", outcome="error"),
            "llm_lat_count": prom_sum(prom, "nl2sql_llm_latency_seconds_count"),
            "llm_lat_sum": prom_sum(prom, "nl2sql_llm_latency_seconds_sum"),
            "lock_contention": prom_sum(prom, "nl2sql_sqlite_lock_contention_total"),
            "q_running": q.get("n_running"),
            "q_pending": q.get("n_pending"),
            "q_wait_max": q.get("pending_runs_wait_time_max_secs"),
            "w_available": w.get("available"),
        }

    def stop(self) -> None:
        self._stop.set()
        self.join(timeout=5)


# ── 单个并发单元：建会话 → 提 run → 等这一轮真正结束 ────────────────


def client_of(uid: str, is_admin: bool) -> httpx.Client:
    """手签 token 冒充某用户（容器内跑，`/app/src` 就是权威代码）。

    ⚠️ 必须带**当前** `token_version`：P1-12 起 token 里的 `pv` 会与账号当前版本比对，
    改过密码的账号（pv≥1）手签不带版本 → 401，看上去像"隔离逻辑坏了"。
    """
    version = 0
    try:
        from agent.auth.users import find_user, token_version_of

        rec = find_user(uid)
        if rec is None:
            print(f"  ⚠️ 账号 {uid} 不在 auth_users.json 里，手签 token 会被判 401")
        else:
            version = token_version_of(rec)
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠️ 读账号记录失败（按 token_version=0 签）: {e}")
    token = sign_token(uid, uid, is_admin, token_version=version)
    return httpx.Client(
        base_url=BASE,
        headers={"Cookie": f"nl2sql_token={token}"},
        timeout=60.0,
        limits=httpx.Limits(max_connections=8, max_keepalive_connections=8),
    )


def turn_settled(settle: dict) -> bool:
    """「这一轮真的结束」的判据 —— 直接采用生产端点 run-status 的结论字段。

    与端点对齐的四条：无活跃 run、无审批等待、不是终态失败、不是 turn_incomplete。
    `turn_incomplete` 由端点按「next 非空 ∧ 无活跃 run ∧ 无审批 ∧ 最后一条不是终稿」
    算出，所以本判据等价于「无活跃 run + 最后一条是终稿」。

    ⚠️ **刻意不再要求 `next` 为空**（2026-09-24 修正，P2-12）：`state.next` 非空但最后
    一条已是终稿，是端点判据 4 明确认定的形态（生产 trace 01a09ed5：run=success +
    next 非空 + 2723 字答复）。它的典型来源是 sync 的
    `update_state(as_node="__start__")` 落在主 run 终态之后，把 `next` 写成图入口节点
    —— 一个**没有人推进的幽灵节点，但不是丢步**（机制回归见
    `scripts/verify_phantom_next.py`）。旧判据要求 `not next` ⇒ 这种会话会空等满
    `--settle-timeout`，被报成「卡死 248s」且**不记录答案**（P2-7 报告 §四 第五条即此
    误判，`ans_len=0`）。反过来，**真的**半轮被打断（next 非空且最后一条是 tool_calls）
    仍然 `turn_incomplete=True`，本判据照旧不收敛。
    """
    return bool(
        not settle.get("has_active_run")
        and not settle.get("awaiting_interrupt")
        and not settle.get("turn_failed")
        and not settle.get("turn_incomplete")
        and settle.get("last_message_is_final")
    )


def one_query(
    client: httpx.Client,
    db_name: str,
    question: str,
    *,
    run_timeout: float,
    settle_timeout: float,
    poll: float,
    ack_grace: float = 90.0,
) -> dict[str, Any]:
    """一个并发单元：新建会话 → 提问 → 等到"这一轮真的结束"。返回可入表的原始记录。"""
    out: dict[str, Any] = {"question": question, "t_submit": time.time()}
    tid = ""
    try:
        r = client.post(
            "/threads",
            json={"metadata": {"graph_id": GRAPH, "title": f"[压测] {question[:20]}"}},
        )
        if r.status_code >= 300:
            out.update(outcome="thread_failed", error=f"HTTP {r.status_code} {r.text[:120]}")
            return out
        tid = (r.json() or {}).get("thread_id", "")
        out["thread_id"] = tid

        r = client.post(
            f"/threads/{tid}/runs",
            json={
                "assistant_id": GRAPH,
                "input": {"messages": [{"role": "user", "content": question}]},
                # 与前端同形：db_name 走 configurable（后端会按登录身份再做一次授权钳制）
                "config": {"configurable": {"db_name": db_name}, "recursion_limit": 500},
            },
        )
        if r.status_code >= 300:
            out.update(outcome="run_submit_failed", error=f"HTTP {r.status_code} {r.text[:160]}")
            return out
        rid = (r.json() or {}).get("run_id", "")
        out["run_id"] = rid

        # ① 等主 run 落到终态（只看 run 对象，语义无歧义）
        t0 = time.time()
        status = ""
        while time.time() - t0 < run_timeout:
            g = client.get(f"/threads/{tid}/runs/{rid}")
            if g.status_code == 200:
                status = (g.json() or {}).get("status", "") or ""
                if status in TERMINAL_RUN:
                    break
            time.sleep(poll)
        out["main_run_status"] = status
        out["latency_main_run"] = round(time.time() - out["t_submit"], 2)
        if status not in TERMINAL_RUN:
            out.update(outcome="main_run_timeout")
            return out

        # ② 等**整轮**结束（自动续跑也跑完）。判据=生产端点 run-status 的结论字段
        #    （见 turn_settled）**外加一条「末条不是 ack」**；审批态单独识别（要立刻取消）。
        #
        # ⚠️ 为什么必须加那条（2026-09-28 实测，P2-7 的延迟表受同一问题影响）：
        #    主 run 派完子任务会立刻以一句 ack 收尾（「查询已提交（任务ID…），请稍候。」），
        #    而子 agent 跑在**它自己的会话**上 ⇒ 本会话 `has_active_run=False`；ack 又是
        #    无 tool_calls 的纯 AI 消息 ⇒ `last_message_is_final=True` ⇒ 端点按自己的定义
        #    （正确地）判「这一轮结束」。但真答案在**自动续跑那一轮**才出现 —— 实测同一问数
        #    `runs 1→2`、真答案比 ack 晚 **151s**（报告类问数要走完流水线 + 口径打回）。
        #    ⇒ 在 ack 处 break 会记下 ack 延迟（4~8s）并把答案记成那句 ack：测的不是用户等待。
        t0 = time.time()
        settle: dict = {}
        converged = False
        rs_http = 0
        rs_fail_run = 0
        ack_ts: float | None = None
        # 子会话状态：未探到之前一律按 "unknown"（= 只按时间兜底），绝不当成"断链"
        sub_state = "unknown"
        sub_probe_ts = 0.0
        # 第一次采到子会话 `done` 的时刻。**必须按"自 done 起"计时**，不能用 ack 以来的时长：
        # 子会话自己可能跑 4 分钟（口径核验打回重跑），那样一探到 done 就会立刻判死 ——
        # 2026-09-28 实测正是如此（子会话 255s 完成，脚本在 253s 就报了 ack_only，误判）。
        sub_done_ts: float | None = None
        while time.time() - t0 < settle_timeout:
            g = client.get(f"/api/threads/{tid}/run-status")
            rs_http = g.status_code
            if g.status_code == 200:
                rs_fail_run = 0
                settle = g.json() or {}
                if settle.get("awaiting_interrupt"):
                    client.post(f"/threads/{tid}/runs/{rid}/cancel")
                    out.update(outcome="awaiting_approval_cancelled", settle=settle)
                    return out
                if settle.get("turn_failed"):
                    out.update(outcome="turn_failed", last_error=settle.get("last_error", ""))
                    return out
                if turn_settled(settle):
                    last_ai = _last_ai_text(client, tid)
                    if ACK_MARK not in last_ai:
                        converged = True
                        out["answer"] = last_ai
                        break
                    # 还是 ack ⇒ 续跑还没来（或不会来）。ack 延迟单独记一笔，它本身是个有用
                    # 的数（"提交后多久给出回执"），但**不能**冒充 latency_turn。
                    if ack_ts is None:
                        ack_ts = time.time()
                        out["latency_ack"] = round(ack_ts - out["t_submit"], 1)
                    ack_wait = time.time() - ack_ts
                    # 子会话三态决定这是"长尾"还是"断链"（见 _subagent_state）。节流：每
                    # 8s 才探一次，别让判据自己变成压测负载。
                    if time.time() - sub_probe_ts > 8.0:
                        sub_probe_ts = time.time()
                        sub_state, sub_tid = _subagent_state(client, last_ai)
                        # 名字带 `_at_ack`：这是**ack 窗口内**最后一次观测，收敛后不再刷新，
                        # 免得报告把它读成"这一轮结束时子会话是什么状态"（那是另一件事）。
                        out["subagent_at_ack"] = sub_state
                        if sub_tid:
                            out["subagent_thread"] = sub_tid
                        if sub_state == "done" and sub_done_ts is None:
                            sub_done_ts = time.time()
                    # 两条独立的退出线（任一命中即判"续跑没来"）：
                    #   · 子会话已终稿 **且自终稿起又等了 60s** → 续跑本该几秒内到，这是断链；
                    #   · 子会话一直在跑/读不到 → 按 ack_grace 兜底（不能无限等，会有僵住的任务）。
                    done_wait = None if sub_done_ts is None else time.time() - sub_done_ts
                    if (done_wait is not None and done_wait > 60.0) or ack_wait > ack_grace:
                        out.update(
                            outcome="ack_only_no_continuation",
                            answer=last_ai,
                            ack_wait=round(ack_wait, 1),
                            sub_done_wait=None if done_wait is None else round(done_wait, 1),
                        )
                        return out
            else:
                # run-status 打不开（例如它依赖的 /state 挂了）→ 连着几次就别空等满 settle_timeout，
                # 否则会把「诊断链路坏了」伪装成「这一轮跑了 4 分钟」。
                rs_fail_run += 1
                if rs_fail_run >= 5:
                    out.update(
                        outcome="run_status_unavailable",
                        run_status_http=rs_http,
                        run_status_body=g.text[:200],
                    )
                    return out
            time.sleep(poll)
        out["settle"] = {
            k: settle.get(k)
            for k in ("has_active_run", "next", "last_message_is_final", "turn_incomplete", "turn_failed")
        }
        # 幽灵 next：收敛时 next 仍非空 = 答复已终稿但图头挂着一个没人推进的节点
        # （P2-12 / verify_phantom_next.py）。记下来是为了下次能直接数它的发生率，
        # 而不是靠人读 settle 里的原始值。
        out["phantom_next"] = bool(converged and settle.get("next"))
        out["latency_turn"] = round(time.time() - out["t_submit"], 2)
        if not converged:
            # ⚠️ 不收敛就**不能算 ok**：否则「等满 240s 超时」会被报成一条成功记录（第一版就犯过）。
            out.update(outcome="settle_timeout", run_status_http=rs_http)
            return out

        # 会话里一共落了几次主 run（≥2 说明发生过自动续跑 → 这条数确实吃了多个槽）
        g = client.get(f"/threads/{tid}/runs")
        if g.status_code == 200:
            body = g.json()
            if isinstance(body, list):
                out["main_thread_runs"] = len(body)
            elif isinstance(body, dict):
                runs = body.get("runs") or body.get("items") or []
                out["main_thread_runs"] = len(runs) if isinstance(runs, list) else None
        # 答案摘录：并发下答错/答不全靠它看出来（第一个问题是已拍板口径）
        st = client.get(f"/threads/{tid}/state")
        if st.status_code == 200:
            msgs = ((st.json() or {}).get("values") or {}).get("messages") or []
            texts = [m.get("content") for m in msgs if isinstance(m, dict) and m.get("type") == "ai"]
            if texts:
                c = texts[-1]
                if isinstance(c, list):
                    c = "".join(p.get("text", "") for p in c if isinstance(p, dict))
                out["answer"] = str(c)[:200]
        out["outcome"] = "ok"
        return out
    except Exception as e:  # noqa: BLE001
        out.update(outcome="exception", error=f"{type(e).__name__}: {e}"[:200])
        return out
    finally:
        out["t_end"] = time.time()


# ── 档位与调度 ─────────────────────────────────────────────────────


def wait_quiet(client: httpx.Client, timeout: float) -> bool:
    """等队列清零（进入下一档前必须做：否则上一档的 run 会污染下一档的峰值）。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            snap = json.loads(client.get("/metrics", params={"format": "json"}).text)
            q = snap.get("queue") or {}
            if not q.get("n_running") and not q.get("n_pending"):
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(3)
    return False


def slice_samples(samples: list[dict], t0: float, t1: float) -> list[dict]:
    return [s for s in samples if t0 <= s.get("ts", 0) <= t1 and "error" not in s]


def _max_of(samples: list[dict], key: str) -> float | None:
    vals = [s[key] for s in samples if s.get(key) is not None]
    return max(vals) if vals else None


def _min_of(samples: list[dict], key: str) -> float | None:
    vals = [s[key] for s in samples if s.get(key) is not None]
    return min(vals) if vals else None


def _first_last(samples: list[dict], key: str) -> tuple[float | None, float | None]:
    """计数器取首末值算差值；一次都没采到 → (None, None)（不是 0，0 会被误读成"没发生过"）。"""
    vals = [s[key] for s in samples if s.get(key) is not None]
    return (vals[0], vals[-1]) if vals else (None, None)


def _pct(xs: list[float], p: float) -> float | None:
    """最近秩百分位（样本少时 p95 就等于最大值，这是对的，别插值假装平滑）。"""
    if not xs:
        return None
    s = sorted(xs)
    k = max(0, min(len(s) - 1, int(math.ceil(p / 100.0 * len(s))) - 1))
    return round(s[k], 1)


def run_level(
    level: int,
    rounds: int,
    clients: list[tuple[str, httpx.Client]],
    args: argparse.Namespace,
    sampler: Sampler,
    created_threads: list[str],
) -> dict[str, Any]:
    """一个档位：rounds 轮 × level 个并发单元。"""
    print(f"\n=== 档位 {level} 并发问数 × {rounds} 轮 ===", flush=True)
    t_level0 = time.time()
    per_query: list[dict] = []
    qidx = 0
    for rd in range(rounds):
        t0 = time.time()
        results: list[dict | None] = [None] * level
        threads: list[threading.Thread] = []

        def _work(slot: int) -> None:
            uid, cli = clients[slot % len(clients)]
            q = args.questions[qidx % len(args.questions)]
            r = one_query(
                cli,
                args.db,
                q,
                run_timeout=args.run_timeout,
                settle_timeout=args.settle_timeout,
                poll=args.poll,
                ack_grace=args.ack_grace,
            )
            r["account"] = uid
            r["slot"] = slot
            results[slot] = r

        for i in range(level):
            qidx += 1
            t = threading.Thread(target=_work, args=(i,), name=f"q{i}", daemon=True)
            threads.append(t)
            t.start()
            time.sleep(args.stagger)  # 错峰几百毫秒：模拟"人不是同一毫秒点的发送"
        for t in threads:
            t.join()
        done = [r for r in results if r]
        for r in done:
            if r.get("thread_id"):
                created_threads.append(r["thread_id"])
        lat = [r["latency_turn"] for r in done if r.get("latency_turn")]
        if lat:
            print(
                f"  第 {rd + 1}/{rounds} 轮：{len(done)} 个完成，耗时 {time.time() - t0:.0f}s，"
                f"turn 延迟 p50={statistics.median(lat):.0f}s max={max(lat):.0f}s",
                flush=True,
            )
        else:
            print(f"  第 {rd + 1}/{rounds} 轮：{len(done)} 个，无有效延迟数据", flush=True)
        per_query.extend(done)

    t_level1 = time.time()
    s = slice_samples(sampler.samples, t_level0, t_level1)
    lat = [r["latency_turn"] for r in per_query if r.get("latency_turn")]

    llm = _first_last(s, "llm_ok")
    lto = _first_last(s, "llm_timeout")
    lerr = _first_last(s, "llm_error")
    lcnt = _first_last(s, "llm_lat_count")
    lsum = _first_last(s, "llm_lat_sum")
    lock = _first_last(s, "lock_contention")
    peak_running = _max_of(s, "running")

    def _delta(pair: tuple[float | None, float | None]) -> float | None:
        return None if pair[0] is None else pair[1] - pair[0]

    mean_llm_lat = None
    if lcnt[0] is not None and (lcnt[1] - lcnt[0]) > 0:
        mean_llm_lat = round((lsum[1] - lsum[0]) / (lcnt[1] - lcnt[0]), 2)

    rss_peak = _max_of(s, "rss")
    return {
        "level": level,
        "rounds": rounds,
        "queries": len(per_query),
        "ok": sum(1 for r in per_query if r.get("outcome") == "ok"),
        # 答复已终稿但图头挂着幽灵 `next` 的问数（P2-12）。它是**正常**形态，不是失败；
        # 单独计数只为了盯住发生率（旧判据会把这些全报成 settle_timeout）。
        "phantom_next": sum(1 for r in per_query if r.get("phantom_next")),
        # ack 到手后**续跑没来**（通知丢 / 子任务卡死）：独立结局，既不算成功也不算超时
        "ack_only": sum(1 for r in per_query if r.get("outcome") == "ack_only_no_continuation"),
        "outcomes": {
            o: sum(1 for r in per_query if r.get("outcome") == o)
            for o in {r.get("outcome") for r in per_query}
        },
        "turn_latency": {
            "p50": round(statistics.median(lat), 1) if lat else None,
            "p95": _pct(lat, 95),
            "max": round(max(lat), 1) if lat else None,
        },
        "main_run_latency_max": (
            round(max((r.get("latency_main_run") or 0) for r in per_query), 1) if per_query else None
        ),
        "peak_running": peak_running,
        "peak_pending": _max_of(s, "pending"),
        "min_available": _min_of(s, "available"),
        "runs_per_query_peak": round(peak_running / level, 2) if peak_running is not None else None,
        "peak_lag": _max_of(s, "lag"),
        "lag_max": _max_of(s, "lag_max"),
        "peak_rss_mb": round(rss_peak / 1048576, 1) if rss_peak else None,
        "peak_children": _max_of(s, "children"),
        "mcp_failed": _max_of(s, "mcp_failed"),
        "llm_calls_delta": {"ok": _delta(llm), "timeout": _delta(lto), "error": _delta(lerr)},
        "llm_mean_latency": mean_llm_lat,
        "lock_contention_delta": _delta(lock),
        "samples": len(s),
        "wall_secs": round(t_level1 - t_level0, 1),
        "per_query": per_query,
    }


# ── 主流程 ─────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description="P2-7 并发压测（生产容器内运行）")
    ap.add_argument("--confirm", action="store_true", help="确认对生产发起真实压测（不给则只打印计划）")
    ap.add_argument("--levels", default="1,2,4,6", help="并发问数档位（逗号分隔）")
    ap.add_argument("--rounds", type=int, default=3, help="每档轮数")
    ap.add_argument("--db", default="witops", help="db_name（会走授权钳制）")
    ap.add_argument("--questions", nargs="*", default=None, help="覆盖固定问题集")
    ap.add_argument("--stagger", type=float, default=0.4, help="并发单元之间的错峰秒数")
    ap.add_argument("--poll", type=float, default=2.0, help="轮询间隔秒")
    ap.add_argument("--run-timeout", type=float, default=600.0, help="等主 run 终态的上限秒")
    ap.add_argument("--settle-timeout", type=float, default=240.0, help="等整轮结束（含续跑）的上限秒")
    ap.add_argument("--ack-grace", type=float, default=300.0,
                    help="ack 后等续跑的上限秒（子会话已终稿时改用 min(本值,60)）")
    ap.add_argument("--quiet-timeout", type=float, default=300.0, help="档位之间等队列清零的上限秒")
    ap.add_argument("--max-seconds", type=float, default=2400.0, help="全局墙钟上限")
    ap.add_argument("--budget-queries", type=int, default=60, help="总问数上限")
    ap.add_argument("--lag-abort", type=float, default=5.0, help="事件循环延迟超此值即停止后续档位")
    ap.add_argument("--accounts", default="", help="只用这些账号（逗号分隔；默认用全部）")
    ap.add_argument("--keep-threads", action="store_true", help="保留本次造的会话（默认删掉）")
    ap.add_argument("--json-out", default="", help="把完整结果写到该路径（默认只打到 stdout）")
    args = ap.parse_args()
    args.questions = args.questions or DEFAULT_QUESTIONS

    levels = [int(x) for x in str(args.levels).replace(" ", "").split(",") if x]
    planned = sum(levels) * args.rounds
    print("=== P2-7 并发压测计划 ===")
    print(f"  目标 {BASE} / 库 {args.db} / 档位 {levels} / 每档 {args.rounds} 轮 / 计划问数 {planned}")
    print(f"  问题集（{len(args.questions)} 个）：{args.questions}")
    print(f"  安全栏：lag-abort={args.lag_abort}s max-seconds={args.max_seconds}s budget={args.budget_queries}")
    if not args.confirm:
        print("\n（未给 --confirm：只打印计划，不提交任何 run）")
        return 0
    if planned > args.budget_queries:
        print(f"\n✗ 计划问数 {planned} 超过 --budget-queries {args.budget_queries}，先调参再来")
        return 2

    from agent.auth.users import load_users

    accounts = [(u.get("user_id") or "", bool(u.get("is_admin"))) for u in load_users()]
    accounts = [(uid, adm) for uid, adm in accounts if uid]
    # --accounts：把并发轴钉在指定账号上。不限定的话会把**真人账号**（admin/Z0051，
    # 配的是另一个 provider）也拉进来 —— 既污染「多用户」口径，也白白消耗真人的配额。
    if args.accounts:
        want = [x.strip() for x in args.accounts.replace(" ", "").split(",") if x.strip()]
        by_id = dict(accounts)
        missing = [u for u in want if u not in by_id]
        if missing:
            print(f"✗ --accounts 里有不存在的账号：{missing}")
            return 2
        accounts = [(u, by_id[u]) for u in want]
    if not accounts:
        print("✗ 读不到账号，退出")
        return 2
    clients = [(uid, client_of(uid, adm)) for uid, adm in accounts]
    print(f"  账号 {[u for u, _ in clients]}（生产仅 {len(clients)} 个 → 并发轴 = 并发问数，账号轮转使用）")

    probe = clients[0][1]
    before = json.loads(probe.get("/metrics", params={"format": "json"}).text)
    print(f"  压测前队列：{before.get('queue')}")

    sampler = Sampler(probe, interval=2.0)
    sampler.start()
    created: list[str] = []
    level_reports: list[dict] = []
    t_start = time.time()
    aborted = ""
    try:
        for lv in levels:
            if time.time() - t_start > args.max_seconds:
                aborted = f"墙钟超 {args.max_seconds}s"
                break
            rep = run_level(lv, args.rounds, clients, args, sampler, created)
            level_reports.append(rep)
            print(
                f"  → 成功 {rep['ok']}/{rep['queries']}（ack-only {rep['ack_only']}）"
                f" / 峰值在跑 {rep['peak_running']} / 排队 {rep['peak_pending']} / "
                f"余槽 {rep['min_available']} / 每问数吃槽 {rep['runs_per_query_peak']} / "
                f"lag峰值 {rep['peak_lag']} / RSS峰值 {rep['peak_rss_mb']}MB / "
                f"子进程峰值 {rep['peak_children']}",
                flush=True,
            )
            if rep["peak_lag"] is not None and rep["peak_lag"] > args.lag_abort:
                aborted = (
                    f"事件循环延迟 {rep['peak_lag']}s 超 --lag-abort {args.lag_abort}"
                    "（保护真实用户，停后续档位）"
                )
                print(f"  ⚠️ {aborted}", flush=True)
                break
            if not wait_quiet(probe, args.quiet_timeout):
                aborted = "档位之间队列未清零（超 quiet-timeout）"
                print(f"  ⚠️ {aborted}", flush=True)
                break
    finally:
        sampler.stop()

    # 收尾：取消没正常结束的 run；会话留下还是删掉看 --keep-threads
    cancel_attempts = 0
    for r in [q for rep in level_reports for q in rep["per_query"]]:
        tid, rid = r.get("thread_id"), r.get("run_id")
        if tid and rid and r.get("outcome") != "ok":
            try:
                probe.post(f"/threads/{tid}/runs/{rid}/cancel")
                cancel_attempts += 1
            except Exception:  # noqa: BLE001
                pass
    if not args.keep_threads:
        for tid in created:
            try:
                probe.delete(f"/threads/{tid}")
            except Exception:  # noqa: BLE001
                pass

    after = json.loads(probe.get("/metrics", params={"format": "json"}).text)
    result = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "db": args.db,
        "accounts": [u for u, _ in clients],
        "levels": levels,
        "rounds": args.rounds,
        "aborted": aborted,
        "before_queue": before.get("queue"),
        "after_queue": after.get("queue"),
        "cancel_attempts": cancel_attempts,
        "threads_deleted": 0 if args.keep_threads else len(created),
        "level_reports": level_reports,
    }

    # 汇总表（报告直接抄这一段）
    print("\n=== 汇总 ===")
    print(
        f"{'档位':>4} {'问数':>4} {'成功':>4} {'p50':>6} {'p95':>7} {'max':>7} "
        f"{'峰值在跑':>8} {'峰值排队':>8} {'余槽':>5} {'吃槽/问':>7} {'lag':>6} "
        f"{'RSS(MB)':>8} {'子进程':>6} {'LLM超时':>7}"
    )
    for rep in level_reports:
        d = rep["llm_calls_delta"]
        print(
            f"{rep['level']:>4} {rep['queries']:>4} {rep['ok']:>4} "
            f"{rep['turn_latency']['p50']!s:>6} {rep['turn_latency']['p95']!s:>7} "
            f"{rep['turn_latency']['max']!s:>7} {rep['peak_running']!s:>8} "
            f"{rep['peak_pending']!s:>8} {rep['min_available']!s:>5} "
            f"{rep['runs_per_query_peak']!s:>7} {rep['peak_lag']!s:>6} "
            f"{rep['peak_rss_mb']!s:>8} {rep['peak_children']!s:>6} {d.get('timeout')!s:>7}"
        )
    if aborted:
        print(f"\n⚠️ 提前结束：{aborted}")
    print(f"\n压测后队列：{after.get('queue')}")
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"完整结果已写 {args.json_out}")
    print("\n=== JSON（汇总，不含逐条明细）===")
    print(json.dumps({k: v for k, v in result.items() if k != "level_reports"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
