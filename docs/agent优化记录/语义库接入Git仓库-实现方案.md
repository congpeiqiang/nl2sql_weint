# 语义库「接入 Git」— 本地新建的库也能拉取已有仓库（已实现）

> 状态：**已实现，未发版**。后端 `src/api/wren_semantic.py`（新原语 `_adopt_git_into`
> + 新端点 `POST /api/wren-projects/{name}/git-adopt` + `from_git` 放宽 409），
> 前端 `src/lib/semanticApi.ts` / `src/app/components/SemanticLibraryPanel.tsx`。
> §7 验证清单可作冒烟回归。

## 1. 背景：三条入库路径都不覆盖这个场景

用户诉求（原话）：「语义库创建后，能不能就支持更新 git 上的仓库，因为有些时候 git 上已经有对应的语义库了」——已确认为**拉取**方向（不是推送）。

**缺口**：平台原本有三条入库路径，没有一条能覆盖「先新建、再发现 git 上已经有这个语义库」：

| 路径 | 原状 | 缺口 |
|---|---|---|
| `create_project` 新建 | 造空骨架 + 关联库，**不建 `.git`** | 之后无法任何方式接上远程仓库 |
| `from_git` Git 导入 | clone → 构建 → 关联 | `dest.exists()` 直接 **409** |
| `git_pull` 卡片「🔄 更新」 | fetch + checkout 到指定 ref | 前端只在 `source==="git"` 时**才渲染按钮**，后端对无 `.git` 的目录直接 400 |

后果：用户只能删掉刚建的库（连带解绑数据库关联）再走 Git 导入。生产上正好撞见这个组合——新建 `aliyun-chinook_semantic`（空库、加载失败）之后无处可去。

**目标**：本地语义库一键接上已有远程仓库（备份本地、干净 clone、构建、工具立刻可用），之后走普通的「更新」；同时放开 Git 导入的同名目录 409。两个入口共用同一套后端原语。

## 2. 为什么是「备份 + 干净 clone + 整目录交换」，不是就地接管

沙箱实测（`d:\tmp`，纯本地 git）：`git init` + `remote add origin` + `fetch --depth 1` + `checkout -f -B <branch> origin/<branch>` 会覆盖同名未跟踪文件（`wren_project.yml`、`knowledge/rules/general.md` 都被换成远程版本），**但远程没有的本地文件会作为未跟踪残留留下来**——本地 introspect/generate 出来的 `models/*.yml` 会一起被 build 烤进 MDL，得到「远程+本地」的混合语义库。这比直接覆盖更难发现（条数看着对、内容是错的）。

故采用：

```
校验 repo_url / target_db
  → _local_content_state(project)（有自建内容且未确认 → 200 + ok:false，不动任何文件）
  → clone_shallow 到 <workspace>/.backups/.adopt-<ts>/project   ← 同一文件系统
  → 校验克隆里有 wren_project.yml
  → os.rename(project, <name>.备份-<ts>)      ← 原子
  → os.rename(clone_dir, project)             ← 失败则把备份 rename 回来
  → 空骨架删掉备份（内容可再生）；有自建内容保留备份（可找回）
  → 关联（路径不变，天然保住）→ 强制真构建 → _invalidate_detector() + _sync_mcp_tools_many()
```

选 rename 而不是「删除后重建目录」的关键理由：**关联天然保住**。`_associated_dbs` 是按 `wren_project` **解析后的路径**匹配的，同名同盘的 rename 路径字符串不变 → 库关联一条都不会掉，无需重挂。`_project_detail` 的 `source` 是现算的 `(p/".git").exists()` → 接管后前端自动变成「Git」。

## 3. 后端

### 3.1 新原语 `_adopt_git_into(project, repo_url, ref, *, discard_local, target_db, build, overwrite_connection)`

顺序刻意排成「先 clone 到保险的位置，再动本地目录」——clone 成功之前不碰本地半分；任何失败都保证本地目录完好（这是「用户点一下就把自己写的知识换掉」的场景，不能有中间态）。第二次 rename 失败会把备份 rename 回来；万一连回滚都失败，异常信息里带上备份路径并说明请手动改回。

### 3.2 新端点 `POST /api/wren-projects/{name}/git-adopt`

body：`{repo_url(必填), ref(可选), discard_local(默认 false), build(默认 true), target_db(可选)}`

守卫：项目不存在 → 404；不在 workspace 内 → 403（与 delete 同法）；**已是 Git 仓库 → 400**（它有「更新」按钮，不让两条路径重叠）。

成功载荷：`{ok, project, backup_dir, build_note, mcp, associated_dbs, warnings, requires_restart: false}`。

### 3.3 `from_git` 放宽 409

`dest.exists()` 时：未传 `replace_existing` → **200 + `{ok:false, code:"exists", adoptable, pristine, local_files, error}`**（前端据 `code` 弹确认）；传 `replace_existing: true` → 走 `_adopt_git_into(discard_local=True)`（前端确认即确认，不再二次问），响应带 `replaced_existing: true`。已有 `.git` 的同名目录仍拒绝（400 + `code:"is_git"`），避免把别人的仓库历史换掉。

> 注意这是**行为变更**：该分支状态码由 409 改为 200。全仓没有别处按这个状态码分支（`MessageFeedbackActions.tsx` 的那处 409 属于反馈模块、另一套 client）。

### 3.4 `git-status` 对非 Git 项目回带本地内容

`!is_git` 时补 `adopt_local_files` / `adopt_pristine` / `adopt_built`，让**确认发生在点按钮之前**（沿用 2026-09-15 的教训：别让用户点了才吃一句拒绝）。

### 3.5 业务可确认态走 2xx body（为什么不回 4xx）

前端 [`handle()`](../../../harness-deep-agents-ui/src/lib/semanticApi.ts) 把**任何**非 2xx 压成一句 `Error(body.error)`，`code` / `local_files` 一并丢掉，对话框就没法据此渲染确认勾选。**业务可确认态走 2xx body** 是本仓既有惯例——`git_pull` 的 `blocked: "dirty"|"unpushed"` 正是 2xx 体。真正不可挽回的失败（地址非法 / 找不到项目 / 已是 Git）仍用真 4xx。

### 3.6 `_local_content_state(project)`：空骨架 vs 有自建内容

| 判据 | 结论 |
|---|---|
| `models/ views/ cubes/` 下有任何文件（`.gitkeep` 除外） | 内容 |
| `knowledge/` 下与 `wren_templates` 逐字不同的文件 | 内容 |
| `target/mdl.json` 存在 | 已构建（也算内容） |
| `config/connection_*.json`（本地凭据，gitignore 常忽略） | 不算内容（构建时按 db_config 重新生成） |
| `wren_project.yml`（新建时生成，会被仓库版本替换） | 不算内容 |

只有「不算内容」的那些 → **空骨架**，可直接接管且事后删掉备份（内容全部可再生）；否则保留备份并要用户确认。判据用现成的 `knowledge_yml()` / `rules_general_md()` 做逐字比较，不新造模板快照。

### 3.7 接管路径**强制真构建**（`force_build=True`）

`_build_with_profile` 原本「有 `target/mdl.json` 就跳过构建」。接管的目标是「拿这个仓库 + **本地**库配置跑起来」：若仓库恰好带了原作者构建的 `target/mdl.json` 而跳过构建，profile 不会注册、connection 不会按本地库重新生成 → 子进程按原作者的 profile 解析连接 → 就是 `aliyun-chinook` 那种「条目在册、0 工具」。所以接管路径强制构建；跳过构建只在「从零导入」（新库本来就没有产物）保留。

### 3.8 工具热加载

接管后调 `_invalidate_detector()`（失效 detector + 触发运行期注册表后台对账）+ `await _sync_mcp_tools_many(dbs)`，**无需重启后端**。

## 4. 前端

| 文件 | 改动 |
|---|---|
| `src/lib/semanticApi.ts` | 新增 `adoptSemanticFromGit(name, {repo_url, ref?, discard_local?, build?, target_db?})` → `Post /api/wren-projects/{name}/git-adopt`；`importSemanticFromGit` payload 增 `replace_existing?`、返回类型增 `code?`/`adoptable?`/`local_files?`/`pristine?`；`GitStatusInfo` 增 `adopt_local_files?`/`adopt_pristine?`/`adopt_built?`。**`handle()` 未动**（它本来就把 2xx body 原样 `res.json()` 出来，附加字段天然透传） |
| `SemanticLibraryPanel.tsx` 卡片 | 「🔄 更新」按钮去掉 `isGit &&` 门控，文案按模式分：`isGit ? "🔄 更新" : "🔗 接入 Git"`，两种模式打开**同一个对话框** |
| `PullGitDialog` | 加「非 Git 模式」：显示必填**仓库地址**输入框 + `ref` 可选输入（不查 git-refs，绑之前取不到），本地内容非空时显示确认勾选（列出文件名 + 说明备份位置），勾了才放行；`busyOp` 按模式分键（`pull-<name>` / `adopt-<name>`） |
| `GitImportDialog` | 收到 `code === "exists"` 不再当错误：列出本地自建文件 + 确认勾选，确认后带 `replace_existing: true` 重发；可「换个名字」取消 |
| 新增 `doGitAdopt` | 与 `doGitPull` 同构：`runOp("adopt-<name>")` + `setNotice`（含 `backup_dir` / `build_note` / `warnings`）+ `refresh()` + 广播 `semantic-projects-changed` |

顺手修掉三处过期文案：`doGitImport` / `doLocalAssociate` / 删除成功的提示原写「重启后端后 Wren 语义工具生效/卸载」，自 2026-09-19 热加载落地后已不成立。

## 5. 明确不做

- 推送方向（用户已排除）；`push_to_git` 未动。
- 远端已有内容时的**合并 / 冲突解决**：接管 = 以远端为准，本地内容只备份不合并。
- 已有 `.git` 的库改换远程地址（那是 `push_to_git` 的 set-url 路径，另一件事）。
- 备份的自动清理 / 回收站 UI（备份目录名即出口，提示里给出路径）。
- 多工作区切换后的陈旧绑定问题（既有问题，不在本次范围）。

## 6. 发版与运维提示

- 改动只在后端 + 前端两个容器，无新增依赖、无 DB 迁移。
- 备份目录落在 `<workspace>/<库名>.备份-<YYYYMMDD-HHMMSS>`；`_scan_wren_projects` 按既有约定跳过含「备份」/`backup` 的一级子目录，所以备份**不会**被当成一个语义库列出来。临时暂存目录是 `<workspace>/.backups/.adopt-<ts>/`（隐藏目录，那一层没有 `wren_project.yml`，同样扫不到）。
- 生产 E2E（发版重启后，用户执行）：
  1. 新建一个库 → 点「🔗 接入 Git」填仓库地址 → 拉取 + 构建 → `GET /api/mcp/status` 看到 `wrenai_<库>` ok / 工具数 → 前端能查该库；
  2. 「Git 导入」一个已有同名本地库的名字 → 弹确认 → 接管成功；
  3. 空骨架那次不应留下备份目录，有自建内容那次备份目录可按提示找回。

## 7. 验证清单

**后端离线**（`d:\tmp\test_git_adopt.py`，仓库根 `uv run python`）：**54/54 通过**。夹具用真实本地 bare 仓库当远程（零网络）。覆盖：① 空骨架接管成功且不留备份；② 非空骨架 → 200 + `code:"local_content"` + `local_files`，且目录逐字未动；③ `discard_local=true` → 备份存在、**真实 `_scan_wren_projects()` 扫不到它**、远程内容就位；④ 仓库不存在 / 缺 `wren_project.yml` → 失败但本地完整无损；⑤ 已是 Git 仓库 → 400；⑥ `from_git` 同名目录两态；⑦ `git-status` 回带三字段；⑧ 关联保留 + `_sync_mcp_tools_many` 收到关联库 + 构建用关联库；⑨ `data_source` 与关联库类型不一致 → `warnings`；⑩ 源码契约（路由注册、`code` 字面量、备份命名）。

**回归**（均全绿）：`test_mcp_hot_reload.py`(72) / `test_dynamic_tool_channel.py`(13) / `test_failed_entry_retry.py`(26) / 既有 `test_semantic_*.py`、`verify_git_*.py`。

**前端**：`npx tsc --noEmit`（改动文件零新增报错）+ `npx next build`（exit 0）+ `d:\tmp\test_git_adopt_ui.js`（**46/46**）：前后端路径/字段名逐字一致、业务可确认态走 2xx、确认发生在点按钮之前、非 Git 卡片也有入口、绑定前不查 refs、确认前不带 `discard_local`、接管强制真构建、备份命名沿用跳过约定。
