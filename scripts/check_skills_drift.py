# -*- coding: utf-8 -*-
"""发版后体检：**外置运行期 skills** 与**镜像内种子**是否漂移（P2-1 同批发版顺手补的洞）。

为什么需要这个检查：`/app/data/shared/skills` 只在**目录缺失时**从镜像内
`src/agent/shared/skills` 播种（见 compose 注释与部署手册 §6），**发版不会刷新**。
而 agent 运行期读的是**外置那份**（`main_agent.py` / `nl2sql_agent.py` 的
`FilesystemBackend(root_dir=_wm.shared_skills_dir)`，`_SHARED_RESOURCES_DIR =$AGENT_DATA_ROOT/shared`）。
于是「改了仓库里的 skill、发版带进了镜像」≠「线上生效」——修复会**静默不生效**，
最典型的是 P1-17：图表归属中间件依赖运行期 `save_chart.py` 打印
`✅ 图表已保存: <路径>`，skill 里的唯一文件名（`_reserve_dest`）也在这份脚本里。

本脚本**只报告、不修改**：覆盖运行期文件属安全策略决定（有人可能就在服务器上调过
prompt/skill），提示里给出 `cp -a` 命令，由发版人决定。

用法（容器内；不需要参数）：
    docker exec nl2sql-app_langgraph-api_1 python /app/scripts/check_skills_drift.py
    python scripts/check_skills_drift.py [--seed DIR] [--runtime DIR] [--list]

退出码：0 = 一致；3 = 有漂移（发布脚本据此告警）；1 = 出错。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import pathlib
import sys


def _norm(p: pathlib.Path) -> str:
    """内容指纹：**忽略行尾差异**。

    外置那份可能是 LF（历史镜像是 Linux 侧构建/`git archive` 物化），而发版 tar 带的是
    Windows 工作树里的 CRLF —— 只差行尾不算漂移，否则每次发版都会报一堆假差异，
    真漂移反而被淹没。
    """
    b = p.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(b).hexdigest()


def _index(root: pathlib.Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not root.is_dir():
        return out
    for p in sorted(root.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts:
            out[p.relative_to(root).as_posix()] = _norm(p)
    return out


def main() -> int:
    data_root = os.environ.get("AGENT_DATA_ROOT", "/app/data")
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", default="/app/src/agent/shared/skills", help="镜像内种子目录")
    ap.add_argument("--runtime", default=os.path.join(data_root, "shared", "skills"), help="外置运行期目录")
    ap.add_argument("--list", action="store_true", help="打印明细文件名（默认只打计数+前几条）")
    args = ap.parse_args()

    seed, runtime = pathlib.Path(args.seed), pathlib.Path(args.runtime)
    print(f"[skills-drift] seed={seed}")
    print(f"[skills-drift] runtime={runtime}")
    if not seed.is_dir():
        print(f"[skills-drift] ❌ 种子目录不存在（镜像内路径变了？）：{seed}")
        return 1
    if not runtime.is_dir():
        print(f"[skills-drift] ⚠️ 外置目录不存在 —— 首启播种还没跑？agent 会读不到 skills")
        return 3

    s, r = _index(seed), _index(runtime)
    only_seed = sorted(set(s) - set(r))
    only_runtime = sorted(set(r) - set(s))
    changed = sorted(k for k in set(s) & set(r) if s[k] != r[k])

    print(f"[skills-drift] 种子 {len(s)} 个文件 / 外置 {len(r)} 个文件")
    if not (only_seed or only_runtime or changed):
        print("[skills-drift] ✅ 一致：外置运行期 skills 与镜像内种子同步")
        return 0

    def _show(title: str, names: list[str]) -> None:
        if not names:
            return
        print(f"  {title}（{len(names)}）")
        for n in (names if args.list else names[:15]):
            print(f"    {n}")
        if not args.list and len(names) > 15:
            print(f"    …还有 {len(names) - 15} 个（--list 看全）")

    print("[skills-drift] ⚠️ 发现漂移：外置那份不会随发版更新，**技能类修复此时不生效**")
    _show("仅在镜像内（新加/重命名，线上读不到）", only_seed)
    _show("仅在外置（仓库已删，线上仍在用）", only_runtime)
    _show("两边都有但内容不同", changed)
    # 路径一律 as_posix()：提示里的命令是给容器内 shell 的，Windows 上跑本脚本做本地核对时
    # 若渲染成反斜杠就成了一条跑不通的命令。
    # ⚠️ 必须是**整目录替换**，不能用 `cp -a` 叠加：技能改名/删除时叠加只加不删，旧技能会继续
    # 被模型读到（`nl2sql-*` → `wren-*` 那次就是这种情况）。
    # 常态不必手跑：发版脚本第 5/7 步已自动整目录替换；这里是兜底（跳过同步 / 首部署 / 手工改过）。
    shared = runtime.parent.as_posix()
    print(
        "[skills-drift] 要同步（**整目录替换**，先备份）：\n"
        f"    # ① 宿主机：把刚解压的源码树里的新种子拷进容器\n"
        f"    cd <AppDir>/backend/src/agent/shared && \\\n"
        f"      docker cp skills nl2sql-app_langgraph-api_1:{shared}/skills.new\n"
        f"    # ② 容器内：新目录就位前不动老的（skills 不用重启；memory 同理但要重启）\n"
        "    docker exec nl2sql-app_langgraph-api_1 bash -c "
        f"'set -e; cd {shared}; tar -czf /app/data/skills.bak-$(date +%Y%m%d-%H%M%S).tgz skills; "
        f"rm -rf skills.old; mv skills skills.old; mv skills.new skills; rm -rf skills.old'\n"
        "    # 别用 `cp -a`：改名/删除场景它只加不删，线上会新旧两套并存"
    )
    return 3


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        print(f"[skills-drift] ❌ 体检失败（{type(e).__name__}: {e}）")
        sys.exit(1)
