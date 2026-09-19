# WIT 语义库「报工口径」知识词条（S1 落地包）

本目录是 S1（权威业务口径）的**待落库内容**，需放进 **WIT 语义库 git 仓库**的
`knowledge/` 目录（本仓库不承载语义库内容，语义库是独立 git repo，经 `semantic_refs`
按 ref 物化）。

## 文件与落点

| 本目录文件 | 落点（WIT 语义库） | 被谁读取 |
|---|---|---|
| `glossary/报工与工时.md` | `knowledge/glossary/报工与工时.md` | `get_all_knowledge()`（metrics/glossary/caveats 全量） |
| `rules/报工口径规则.md` | `knowledge/rules/报工口径规则.md` | `get_instructions()`（rules/*.md 全量） |

参考结构（已存在）：`src/test/wrenai_exec_Chinook/knowledge/{knowledge.yml, rules/, glossary/, metrics/, caveats/, sql/}`。

## 落库步骤

1. 在 WIT 语义库仓库把上述两个文件放入 `knowledge/` 对应子目录，`git commit`。
2. `git tag` 打新版本号（如 `v7`）。
3. 语义库 A/B / 在线服务按 ref 物化（`semantic_refs` / `WREN_SEMANTIC_OVERRIDE` 指向新 tag），
   或直接更新在服务分支后重启后端（见「wrenai 语义库 git 版本化」记忆）。

## 口径依据（已拍板 + trace 实证）

- **应报工人员池 = 在职员工**（`do_department_user_detail.deleted='0'`，约 190 人），
  **不是**月度花名册 `do_table_user`（184 人）。用户 2026-09-04 拍板。
- 剔除规则：`entry_date > 统计周末`（尚未入职）与 `leave_date < 统计周起始`（已离职）。
- **报工判定源 = `do_work_hour_examine`（报工单，本人提交，含 status=1 待审核），
  不是 `do_work_hour`（工时表，含系统补录/预填行）**。用户 2026-09-04 拍板（见 rules R3/R8）。
- 示例（2026-09-04 查「上周谁没报工」）：应报工池 189 − `do_work_hour_examine` 报工 176 =
  **13 人未报工**（trace 实证 + judge 0.9；误用 `do_work_hour` 会得出 17 人、judge 0.0）。
