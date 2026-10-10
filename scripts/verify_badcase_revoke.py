# -*- coding: utf-8 -*-
"""「移出 BadCase」端点验证（离线：不连 Langfuse、不连目标库、只写临时 AGENT_DATA_ROOT）。

被测：`POST /api/feedback/annotations/revoke-badcase`（`api/feedback_annotation.py`
的 `remove_badcase_annotation`）+ `store.reopen_annotation(from_status=…)` 的新口子。

为什么值得单开一支：这条路上有三个**容易写反**的不变量，都不是「跑一下看看」能发现的，
必须显式钉住：

  1. `badcase_status.json` 是**按 trace 键**的，且 `run_experiment` 对「查不到的 trace」
     是 **fail-open（默认收录）**。所以状态**只能置 invalid、绝不能删条目**，且只有该
     trace 已无 BadCase 条目时才关——这正是本脚本里唯一的负对照（③）。
  2. 定位键必须是 dataset **item_id**（自动采集条没有 message_id），本地标注行是
     「若有则回退」——没有本地行**不是错误**（②）。
  3. 新口子不能让旧护栏漏：`reopen_annotation` 的默认参数对 badcase 行仍须返回 None
     （⑪），普通 DELETE 对 badcase 行仍须 409、`ANNOTATION_DELETABLE` 一个字不变（⑩）。

运行：
    uv run --no-sync python scripts/verify_badcase_revoke.py

退出码 0 = 全部通过。
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import tempfile

# ⚠️ 必须在 import token/grants/store 之前设置：auth_secret 与 message_feedback.db
# 都会落在 <AGENT_DATA_ROOT> 下。不设就写进 CWD（=仓库根），污染仓库、还会被打进发版 tar。
os.environ.setdefault("AGENT_DATA_ROOT", tempfile.mkdtemp(prefix="nl2sql-verify-badrevoke-"))
os.environ.setdefault("NL2SQL_AUTH_DISABLED", "0")

_HERE = pathlib.Path(__file__).resolve()
for cand in (_HERE.parents[1] / "src", pathlib.Path("/app/src")):
    if cand.is_dir() and str(cand) not in sys.path:
        sys.path.insert(0, str(cand))

import httpx  # noqa: E402
from starlette.applications import Starlette  # noqa: E402

OK = "\033[32mPASS\033[0m"
NG = "\033[31mFAIL\033[0m"

NGINX_IP = "172.18.0.9"  # 容器网段（真实浏览器经 nginx 后端的来源）
PATH = "/api/feedback/annotations/revoke-badcase"

ADMIN = "Z9001"
PLAIN = "Z0051"

# trace id 一律用「谁都不是谁的前缀」的形状：`badcase_status.mark` 支持前缀匹配，
# 若 a1111111 是某 id 的前缀，断言会因为解析到别处而诡异地飘。
T1 = "a1111111"    # ① 人工确认条：有本地行
T2 = "b2222222"    # ② 自动采集条：无本地行（负对照 ③ 也用它）
T3 = "c3333333"    # ④ 同一 trace 两条
T5 = "e5555555"    # ⑤ 条目早已不在数据集
T6 = "f6666666"    # ⑥ 本地行不是 badcase 态
T7 = "d7777777"    # ⑦ Langfuse 已禁用
T11 = "0abcdef0"   # ⑪ reopen_annotation 默认参数仍拒 badcase

results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{OK if cond else NG}] {label}" + (f"  {detail}" if detail else ""))


# ── Langfuse 桩 ────────────────────────────────────────────────

class _Item:
    """`_list_dataset_items` 只取 `.id`（`getattr`），给这一个字段就够了。"""

    def __init__(self, item_id: str):
        self.id = item_id


class _ListResp:
    def __init__(self, data):
        self.data = data


class _FakeDatasetItems:
    def __init__(self, owner: "FakeClient"):
        self._o = owner

    def list(self, dataset_name=None, source_trace_id=None, limit=100):
        return _ListResp(list(self._o.data.get((dataset_name, source_trace_id), [])))

    def delete(self, item_id):
        # **必须真的从数据集里摘掉**：`_remove_badcase_item` 的「剩余条数」是
        # `len(list) - deleted` 算的，桩若不摘，第二次 list 仍返回两条 ⇒ 「删完最后
        # 一条才关状态」这条路根本没被跑到（会让本脚本对真 bug 视而不见）。
        self._o.deleted.append(item_id)
        for key, items in self._o.data.items():
            self._o.data[key] = [it for it in items if it.id != item_id]


class FakeClient:
    def __init__(self):
        self.data: dict[tuple[str, str], list] = {}
        self.deleted: list[str] = []
        self.api = type("Api", (), {"dataset_items": _FakeDatasetItems(self)})()


# ── app / 请求 ─────────────────────────────────────────────────

def mint(uid: str, is_admin: bool = False) -> str:
    from agent.auth import grants
    grants.register_user = lambda *a, **k: None  # 中间件放行后会调它，打桩掉磁盘写
    from _auth_test_support import mint_for
    return mint_for(uid, is_admin)


def build_app() -> Starlette:
    from api.auth_middleware import AuthMiddleware
    from api import feedback_annotation

    # 挂整份 routes：本脚本只打一个端点，但路由表要真（否则字面量段/参数段的匹配关系
    # 就没被覆盖——`/annotations/revoke-badcase` 与 `/annotations/{tid}/{mid}` 段数不同）。
    return AuthMiddleware(Starlette(routes=list(feedback_annotation.routes)))


async def call(app, body: dict, *, uid: str | None = ADMIN, method: str = "POST",
               path: str = PATH):
    headers = {"X-Forwarded-For": "1.2.3.4"}
    if uid:
        headers["Cookie"] = f"nl2sql_token={mint(uid, uid == ADMIN)}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(NGINX_IP, 12345)),
        base_url="http://testserver",
    ) as c:
        r = await c.request(method, path, headers=headers, json=body)
    try:
        return r.status_code, r.json()
    except Exception:  # noqa: BLE001
        return r.status_code, None


# ── 主流程 ─────────────────────────────────────────────────────

def seed_row(tid: str, mid: str, status: str) -> None:
    from agent.feedback.store import get_store

    st = get_store()
    st.enqueue_annotation(tid, mid, rating="negative", note="seed", question="种子问题")
    if status != "queued":
        assert st.update_annotation(tid, mid, status=status) is not None


async def main() -> int:
    from agent.feedback.store import get_store
    from agent.eval import badcase_status as bs
    import agent.trace.langfuse_client as lc

    store = get_store()
    fake = FakeClient()
    fake.data = {
        ("badcase", T1): [_Item("it1")],
        ("badcase", T2): [_Item("it2")],
        ("badcase", T3): [_Item("itA"), _Item("itB")],
        ("badcase", T5): [],                        # 条目早已被手工删掉
        ("badcase", T6): [_Item("it6")],
        ("badcase", T7): [_Item("it7")],
        ("badcase", T11): [_Item("it11")],
    }
    lf_on = {"v": True}
    lc.langfuse_enabled = lambda: lf_on["v"]
    lc.get_client = lambda: fake

    seed_row(T1, "m1", "badcase")      # 人工确认条
    seed_row(T6, "m6", "queued")       # 非 badcase 的本地行
    seed_row(T11, "m11", "badcase")    # 留给 ⑪，不能被动到

    app = build_app()

    # ── ① 人工确认条：删条目 + 关状态 + 本地回 queued ──────────────
    st, js = await call(app, {"item_id": "it1", "trace_id": T1,
                              "thread_id": T1, "message_id": "m1"})
    check(st == 200, "①人工条 → 200", f"HTTP {st} {js}")
    check(fake.deleted == ["it1"], "①真删了数据集条目 it1", str(fake.deleted))
    check((js or {}).get("deleted_items") == 1 and (js or {}).get("remaining_items") == 0,
          "①删除 1 条、剩余 0 条", str(js))
    check((js or {}).get("local_reopened") is True, "①本地标注行回退到「待判断」", str(js))
    check((js or {}).get("status_closed") is True, "①关闭了状态标记", str(js))
    row1 = store.get_annotation(T1, "m1")
    check(row1 is not None and row1.status == "queued",
          "①本地行确实是 queued（不是只回了 ok）", row1.status if row1 else "None")
    check(row1 is not None and row1.is_valid is None and row1.gold_sql == "",
          "①入集决定字段被清空（is_valid/gold_sql）", "")
    check(bs.load_status().get(T1, {}).get("status") == "invalid",
          "①" + T1 + " 状态置 invalid", str(bs.load_status().get(T1)))

    # ── ② 自动采集条：没有本地行**不是错误** ──────────────────────
    st, js = await call(app, {"item_id": "it2", "trace_id": T2,
                              "thread_id": T2, "message_id": ""})
    check(st == 200, "②自动采集条（无本地行）→ 200", f"HTTP {st} {js}")
    check((js or {}).get("error") is None, "②不是错误（无 error 字段）", str(js))
    check((js or {}).get("local_reopened") is False, "②local_reopened=False", str(js))
    check((js or {}).get("status_closed") is True, "②状态仍被关", str(js))
    check(bs.load_status().get(T2, {}).get("status") == "invalid",
          "②" + T2 + " 状态置 invalid", str(bs.load_status().get(T2)))

    # ── ③ 负对照：证明「删状态条目」是错的（捷径会重新收录）────────
    check(T2 in bs.load_status(),
          "③负对照：" + T2 + " 的状态条目**存在**（没被删）", str(list(bs.load_status())))
    check(bs.is_open(T2) is False, "③负对照：is_open(" + T2 + ")=False（不在回归集）")
    check(bs.is_open("从未注册过的trace") is True,
          "③负对照：未注册的 trace is_open=True ⇒ 删条目会 fail-open 重新收录")

    # ── ④ 同一 trace 两条：先删一条不关状态，删完最后一条才关 ──────
    st, js = await call(app, {"item_id": "itA", "trace_id": T3,
                              "thread_id": T3, "message_id": ""})
    check(st == 200 and (js or {}).get("remaining_items") == 1,
          "④同 trace 两条：删掉 itA 后剩 1 条", str(js))
    check((js or {}).get("status_closed") is False, "④剩 1 条时**不**关状态", str(js))
    check("还有 1 条" in ((js or {}).get("warning") or ""),
          "④响应里点明「还有 N 条」", str((js or {}).get("warning")))
    check(T3 not in bs.load_status(),
          "④" + T3 + " 状态未被写入（兄弟条目仍在回归集）", str(bs.load_status().get(T3)))
    st, js = await call(app, {"item_id": "itB", "trace_id": T3,
                              "thread_id": T3, "message_id": ""})
    check(st == 200 and (js or {}).get("status_closed") is True,
          "④删完最后一条才关状态", str(js))
    check(bs.load_status().get(T3, {}).get("status") == "invalid",
          "④" + T3 + " 这时才 invalid", str(bs.load_status().get(T3)))

    # ── ⑤ 条目早已不在数据集：不算错，状态照样关 ──────────────────
    st, js = await call(app, {"item_id": "早就没了", "trace_id": T5,
                              "thread_id": T5, "message_id": ""})
    check(st == 200 and (js or {}).get("deleted_items") == 0,
          "⑤条目不在 → 200 且 deleted_items=0", str(js))
    check("没找到对应条目" in ((js or {}).get("warning") or ""),
          "⑤给出「没找到」的 warning", str((js or {}).get("warning")))
    check(bs.load_status().get(T5, {}).get("status") == "invalid",
          "⑤状态仍被关（条目不在 ≠ 不该关）", str(bs.load_status().get(T5)))

    # ── ⑥ 本地行不是 badcase 态：不回退、不报错 ───────────────────
    st, js = await call(app, {"item_id": "it6", "trace_id": T6,
                              "thread_id": T6, "message_id": "m6"})
    check(st == 200, "⑥本地行非 badcase → 200", f"HTTP {st} {js}")
    check((js or {}).get("local_reopened") is False, "⑥不回退本地行", str(js))
    row6 = store.get_annotation(T6, "m6")
    check(row6 is not None and row6.status == "queued",
          "⑥本地行原样未动", row6.status if row6 else "None")
    check("本地状态为 queued" in ((js or {}).get("warning") or ""),
          "⑥warning 里说明了为什么没回退", str((js or {}).get("warning")))

    # ── ⑦ Langfuse 关闭：只给 warning，状态仍关 ───────────────────
    lf_on["v"] = False
    st, js = await call(app, {"item_id": "it7", "trace_id": T7,
                              "thread_id": T7, "message_id": ""})
    check(st == 200 and (js or {}).get("deleted_items") == 0,
          "⑦Langfuse 关 → 200 且未删条目", str(js))
    check("Langfuse 已禁用" in ((js or {}).get("warning") or ""),
          "⑦warning 点明 Langfuse 已禁用", str((js or {}).get("warning")))
    check(bs.load_status().get(T7, {}).get("status") == "invalid",
          "⑦状态仍置 invalid（本地/状态不受 Langfuse 开关影响）",
          str(bs.load_status().get(T7)))
    check("it7" not in fake.deleted, "⑦确实没去删数据集条目", str(fake.deleted))
    lf_on["v"] = True

    # ── ⑧ 缺 trace_id / item_id → 400 ─────────────────────────────
    st, js = await call(app, {"item_id": "x"})
    check(st == 400, "⑧缺 trace_id → 400", f"HTTP {st} {js}")
    st, js = await call(app, {"trace_id": T2})
    check(st == 400, "⑧缺 item_id → 400", f"HTTP {st} {js}")

    # ── ⑨ 非管理员 → 403（守卫在最前，不摸 body / Langfuse）────────
    before = list(fake.deleted)
    st, js = await call(app, {"item_id": "it2", "trace_id": T2,
                              "thread_id": T2, "message_id": ""}, uid=PLAIN)
    check(st == 403, "⑨非管理员 → 403", f"HTTP {st} {js}")
    check(fake.deleted == before, "⑨非管理员请求没有触到 Langfuse", str(fake.deleted))

    # ── ⑩ 负对照：普通 DELETE 仍删不掉 badcase ────────────────────
    from agent.feedback.store import ANNOTATION_DELETABLE

    check(tuple(ANNOTATION_DELETABLE) == ("queued", "annotating", "validated", "rejected"),
          "⑩负对照：ANNOTATION_DELETABLE 一个字没变", str(ANNOTATION_DELETABLE))
    st, js = await call(
        app, {"expected_status": "badcase"}, method="DELETE",
        path=f"/api/feedback/annotations/{T11}/m11",
    )
    check(st == 409, "⑩负对照：普通 DELETE 对 badcase 行仍 409", f"HTTP {st} {js}")
    row11 = store.get_annotation(T11, "m11")
    check(row11 is not None and row11.status == "badcase",
          "⑩负对照：badcase 行还在（没被普通 DELETE 删掉）",
          row11.status if row11 else "None")

    # ── ⑪ 新口子是 opt-in：默认参数仍拒 badcase ───────────────────
    check(store.reopen_annotation(T11, "m11") is None,
          "⑪reopen_annotation(默认) 对 badcase 行仍返回 None")
    moved = store.reopen_annotation(T11, "m11", from_status="badcase")
    check(moved is not None and moved.status == "queued",
          "⑪显式 from_status='badcase' 才放行",
          moved.status if moved else "None")
    check(store.reopen_annotation(T11, "m11", from_status="good") is None,
          "⑪负对照：队列行不因 from_status='good' 被放行")

    total = len(results)
    passed = sum(1 for ok, _ in results if ok)
    print(f"\n=== 结果：{passed}/{total} 通过 ===")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
