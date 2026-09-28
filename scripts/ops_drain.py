# -*- coding: utf-8 -*-
"""P2-1 发版用：**在容器内**主动排空（协作式），让在跑的 run 跑完再停。

为什么是"容器内脚本"：`/api/admin/drain` 要管理员身份，而发版脚本手里没有会话，
也不该把管理员口令写进脚本。容器里有 `auth_secret` 与 `auth.sqlite`，可以直接
**按现有管理员账号的当前 `token_version` 签一枚 token**（与 `scripts/e2e_thread_isolation.py`
同一套路）—— 等价于"管理员本人点了这个按钮"，不需要任何口令输入。

用法（在宿主机上，经 ssh 到服务器执行；容器名见 docker-compose）：
    docker exec nl2sql-app_langgraph-api_1 python /app/scripts/ops_drain.py            # 排空并等到清零/超时
    docker exec nl2sql-app_langgraph-api_1 python /app/scripts/ops_drain.py --budget 60
    docker exec nl2sql-app_langgraph-api_1 python /app/scripts/ops_drain.py --undrain  # 撤销（改主意/回退）

`--budget` 是**本方愿意等多久**（≠ 服务端预算 `NL2SQL_DRAIN_SECS`）：到点仍没清零就
打印剩余数并退出 2，把"等不等"的决定留给发布脚本/人（那时停容器也仍是安全的：服务端会
把同一份预算接着用完，不会二次长等）。

退出码：0 = 已清零（可安全停容器）；2 = 预算用尽仍有任务在跑；1 = 出错（端点不可达等）。
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
import urllib.request

# 容器里 `docker exec python /app/scripts/ops_drain.py` 不会自动带上 src（那是
# start_server.py 插的），这里自己兜住；本地离线跑同一份也成立。
for _cand in (pathlib.Path(__file__).resolve().parents[1] / "src", pathlib.Path("/app/src")):
    if _cand.is_dir() and str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))

BASE = os.environ.get("NL2SQL_OPS_URL", "http://127.0.0.1:2026")
TIMEOUT_PER_CALL = 30.0


def _admin_token() -> tuple[str, str]:
    """给一个现有管理员账号签一枚当前版本的 token（容器内可直接读 auth 存储）。

    优先挑**不需要改密**的管理员：开了 `NL2SQL_FORCE_PASSWORD_CHANGE` 时，中间件会把
    未改初始密码的账号挡在 403（`_send_403_must_change`），拿它的 token 驱动不了端点。
    """
    from agent.auth.token import sign_token
    from agent.auth.users import load_users, must_change_password_of, token_version_of

    admins = [u for u in (load_users() or []) if u.get("is_admin")]
    if not admins:
        raise SystemExit("❌ auth 存储里没有管理员账号，无法驱动排空端点")
    record = next((u for u in admins if not must_change_password_of(u)), admins[0])
    if must_change_password_of(record):
        print("⚠️ 所有管理员都还没改初始密码；若开了 NL2SQL_FORCE_PASSWORD_CHANGE，端点会 403", file=sys.stderr)
    uid = record.get("user_id") or record.get("username")
    return uid, sign_token(uid, record.get("display_name") or uid, True, token_version=token_version_of(record))


def _request(method: str, path: str, token: str) -> dict:
    req = urllib.request.Request(f"{BASE}{path}", method=method)
    req.add_header("Cookie", f"nl2sql_token={token}")
    with urllib.request.urlopen(req, timeout=TIMEOUT_PER_CALL) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def _fmt(state: dict) -> str:
    return (
        f"在跑 {state.get('remaining')} 个后台任务"
        f"（draining={state.get('draining')}, drained={state.get('drained')}, "
        f"原因={state.get('reason') or '-'}, 服务端预算={state.get('budget_secs')}s）"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=int, default=180, help="本方最多等多少秒（默认 180）")
    ap.add_argument("--poll", type=float, default=5.0, help="轮询间隔秒（默认 5）")
    ap.add_argument("--undrain", action="store_true", help="撤销排空、恢复接单")
    args = ap.parse_args()

    uid, token = _admin_token()

    if args.undrain:
        state = _request("DELETE", "/api/admin/drain", token)
        print(f"✅ 已撤销排空（操作身份={uid}）：{_fmt(state)}")
        return 0

    # `?wait=0`：只置位、由本方轮询 —— 这样能看见"还剩几个"的过程，
    # 而不是盯着一个可能卡 180s 的请求 black box。
    state = _request("POST", "/api/admin/drain?wait=0", token)
    print(f"🚧 已进入排空态（操作身份={uid}）：新提交的查询会收到 503 + 明确提示；{_fmt(state)}")

    deadline = time.monotonic() + max(0, args.budget)
    while True:
        if state.get("remaining") == 0:
            print(f"✅ 排空完成：在跑任务已清零（等了 {args.budget - max(0, deadline - time.monotonic()):.0f}s），可以停容器")
            return 0
        if time.monotonic() >= deadline:
            print(
                f"⏱️ 预算 {args.budget}s 用尽，仍有 {state.get('remaining')} 个后台任务在跑。\n"
                f"   继续发版 = 这些 run 会被截断（这就是那批'半轮对话'的来源）。\n"
                f"   想再等：重跑本脚本；想放弃：`--undrain` 恢复接单，或直接停容器（服务端会自己再等一轮）。"
            )
            return 2
        time.sleep(args.poll)
        state = _request("GET", "/api/admin/drain", token)
        print(f"   …{_fmt(state)}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        print(f"❌ 排空驱动失败（{type(e).__name__}: {e}）—— 不要因此卡住发版："
              f"直接 `docker-compose stop -t 240` 仍会触发服务端的信号式排空。")
        sys.exit(1)
