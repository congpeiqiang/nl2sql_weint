# 全局状态审计

> Phase 2-4 交付物。本文件是**服务里模块级可变状态的唯一清单**——新增/删除模块级状态时同步改本表。
>
> 目的不是「消灭全局状态」，而是把**真正会出错的那几处**从几百个良性的里面挑出来。

## 0. 方法与口径

**扫描方式**：对 `src/**/*.py`（排除 `src/test/`）做 AST 模块级赋值扫描，收集三类：

1. `threading.{Lock,RLock,Event,Semaphore,Condition,Barrier}` 赋值 → 本模块的锁
2. `{}` / `[]` / `set()` / `dict()` / `OrderedDict()` / `defaultdict()` 字面量与构造
3. `= None` 占位、以及注解为 `dict[...]` / `list[...]` / `set[...]` 的赋值

**「有锁/无锁」的判据是本模块内是否存在模块级锁对象**（AST 可见），**不是**逐条追踪 `with` 块。
因此本表中的「无锁」= 「本模块没有模块级锁」，若某处的保护来自**别的模块**的锁（或事件循环单线程假设），
会在该行的「保护」列写明。这条口径是刻意的：它可复现，也不会因为某处恰好用了别人的锁就漏报。

**复现**：见 §10。

**关键前提**：ASGI 服务的请求处理跑在**同一个事件循环**里（多 worker 部署时是多个进程，
每进程一个循环）。所以 `dict` 的**单次** `get`/`set`/`pop` 在 GIL 下是原子的，
「无锁」不等于「会读到半写坏的对象」；真正的风险来自 **(a) 多进程不一致**、**(b) 跨线程复合操作**、
**(c) 无界增长**。下表按这三条打风险。

---

## 1. 结论速览

| 类别 | 数量级 | 需要动手 |
|------|--------|---------|
| A 只读常量表（查表用的 dict/set/list） | ~55 | 0 |
| B 构建期写入、运行期只读（路由表、中间件链） | ~25 | 0 |
| C 缓存（有锁 / 无锁 / LRU / 无界） | ~30 | **2** |
| D 懒初始化单例（`= None` + 首次赋值） | ~25 | 1 |
| E 连接（sqlite / psycopg） | 4 | 0（已有既定约束） |
| F 可变状态（注册表、计数器、会话表） | ~20 | **2** |

**需要动手的只有 4 处**（§8）：`auth/users.py` 跨进程陈旧、`langfuse_client` 会话表无界、
`path_resolver._TOOL_POOL` 竞态建池、`api/experiment.py` 重启僵尸（已在别处记录、这里只挂链）。

---

## 2. A 只读常量表 — 无需处理

import 时构造、之后**只读**的查表结构。**只要没有写点，就不是全局状态问题**，不逐条列出，
只给判据与代表例：

| 代表 | 文件 | 形态 |
|------|------|------|
| `TYPE_MAP` | `agent/utils/db_introspect.py`、`db_mcp_server` | 类型映射 |
| `KNOWN_MODEL_CONTEXT_WINDOWS` / `_FUZZY_WINDOW_PATTERNS` / `KNOWN_MODEL_MAX_TOKENS` / `_FUZZY_MAX_TOKENS_PATTERNS` | `agent/llms/model.py` | 模型能力表 |
| `SCOPE_MAP` | `agent/config.py` | 作用域映射 |
| `_RETRIEVAL_MODES` / `_RETRIEVAL_BACKENDS` / `_TRUTHY` / `_FALSY` | `agent/settings/feature_flags.py` | 开关取值域 |
| `FILE_PERMISSIONS` / `NL2SQL_FILE_PERMISSIONS` | `agent/settings/file_permissions.py` | 权限表 |
| `_WRITE_LEADING` / `_READ_LEADING` | `agent/middlewares/sql_approval.py` | SQL 首词表 |
| `_TOOL_FALLBACKS` | `agent/middlewares/dynamic_mcp_tools.py` | 兜底表 |
| `_FILTER_OPS` / `_GRANULARITY_CN` / `_SQL_ORIGIN_TEXT` / `_VERDICT_LABEL` | `agent/tools/report_builder.py` | 报告文案表 |
| `_SHAPE_ERROR_CODE` / `_SHAPE_LABEL` | `agent/tools/attribution_analyze.py` | 归因文案表 |
| `HEURISTIC_TO_SKILL` | `agent/utils/process_audit.py` | 启发式→技能 |
| `_ITEM_TYPE_TO_KIND` / `_KNOWLEDGE_KIND` | `agent/retrieval/{indexer,schema}.py` | 类型归类 |
| `CALIBER_SPECS` | `agent/settings/caliber_spec.py` | 口径规格 |
| `TOOL_SKILL_MAP` / `_TOOL_OWNER_SKILLS` | `agent/middlewares/langfuse_span.py` | 工具→技能 |
| `_VALID_DECISION_TYPES` / `_TERMINAL_STATUSES` / `_ACTIVE_STATUSES` / `_FAILED_STATUSES` | `api/{sql_approval,task_cancel,thread_run_status}.py` | 状态枚举 |
| `_CARTESIAN_TYPES` / `_TOOL_TIMEOUTS` | `agent/utils/path_resolver.py` | 超时表 |
| `_FAILED_RUN_STATUS` | `agent/utils/failure_signal.py` | 失败状态 |
| `_CAPACITY_FIELD_GROUPS` | `api/model_config.py` | 字段分组 |
| `_KNOWLEDGE_CATEGORIES` | `api/wren_semantic.py` | 知识分类 |
| `BAD_TYPES` / `BAD_TYPE_LABELS` / `_DEFAULTS` | `agent/eval/{bad_types,eval_flags}.py` | 评估判据 |

**判据（改代码时自查）**：这个名字是否只出现在 `Read`/下标取值/迭代位置？若出现过 `[k] = v` /
`.append` / `.update` / `.clear` / `del`，它就不属于 A，要挪到 C 或 F。

> ⚠️ 唯一的例外陷阱：**常量表与外层容器共用名字**（如 `_SQL_ORIGIN_TEXT` 是 dict 但内层值可变）。
> 本审计按「顶层名字是否被重新赋值/改内容」判定，不递归内层。

---

## 3. B 构建期写入、运行期只读

这类**是 `list`/`dict` 且真的会被写**，但写点全在 **import / app 构建期**（装饰器注册、装配），
请求期只读。**无需加锁**。

| 名字 | 出现处 | 写点 |
|------|--------|------|
| `routes` | 19 个 `api/*.py` | FastAPI/LangGraph 路由装饰器，import 期追加 |
| `ROUTES` | `api/custom_app.py` | 组合根汇总 |
| `MAIN_AGENT_MIDDLEWARE` / `NL2SQL_MIDDLEWARE` / `_middleware` / `resolved_tools` | `agent/main_agent.py`、`agent/graphs/nl2sql_agent.py` | agent 装配 |
| `__all__` | 多处 | 导出声明，非状态 |

**风险**：只有「运行期往 `routes` 里加路由」这一种误用会出问题（并发 append + 迭代）。
**当前无此用法**——新 API 一律在模块顶部 `@router` 注册。

---

## 4. C 缓存

### 4.1 有锁且设计正确（不动）

| 名字 | 文件 | 形态 | 保护 |
|------|------|------|------|
| `_ENGINE_CACHE` | `agent/utils/wren_plan.py` | `OrderedDict`，**真 LRU**：命中 `move_to_end`、溢出 `popitem(last=False)`；上限 `DEFAULT_CACHE_MAX=16`，`WREN_PLAN_CACHE_MAX` 可覆盖 | `_CACHE_LOCK` |
| `_CACHE` | `agent/retrieval/store.py` | dict | `_LOCK` |
| `_GRAM_CACHE` / `_VEC_CACHE` | `agent/retrieval/backends/jsonl.py` | dict ×2 | `_LOCK` |
| `_TABLE_CACHE` / `_META_CACHE` | `agent/retrieval/backends/lance.py` | dict ×2 | `_LOCK` |
| `_CORPUS_CACHE` | `agent/utils/caliber_evidence.py` | dict | `_CORPUS_LOCK` |
| `_TS_CACHE` | `agent/utils/filesystem_backend_patch.py` | dict | `_TS_LOCK` |
| `_INDEX_CACHE` / `_CUBE_GATE_REMINDED` | `agent/middlewares/cube_route_gate.py` | dict / set | `_INDEX_LOCK` / `_REMINDED_LOCK` |
| `_BUCKETS` | `agent/middlewares/progress_boundary.py` | dict | `_BUCKETS_LOCK` |
| `_REMINDED` | `agent/middlewares/query_gate.py` | set | `_REMINDED_LOCK` |
| `_stats_cache` | `api/feedback_annotation.py` | dict | `_stats_lock` |
| `_cache` / `_path_cache` | `agent/eval/eval_flags_store.py` | dict ×2 | `_lock` |
| `_skills_cache` / `_key_locks` / `_remote_cache` | `agent/utils/skills_versioning.py` | dict ×3 | `_skills_lock` / `_key_locks_guard` / `_remote_lock` |
| `_prompt_cache` / `_key_locks` | `agent/utils/prompt_versioning.py` | dict ×2 | `_prompt_lock` / `_key_locks_guard` |
| `_semantic_override_cache` / `_materialize_locks` / `_db_name_norm_cache` | `agent/utils/semantic_db.py` | dict ×3 | `_semantic_override_lock` / `_materialize_locks_guard` / `_db_name_norm_lock` |
| `_STORES` | `agent/trace/event_store.py` | dict | `_STORES_LOCK` |
| `_inflight_user` | `agent/utils/llm_gate.py` | dict | `_gate_lock` |
| `_seen_memo` | `agent/auth/grants.py` | dict/set | `_seen_lock` |
| `_LAST_SIZES` / `_LAST_RUN` | `agent/utils/retention.py` | dict ×2 | `_THREAD_LOCK` |
| `_ALERT_STATE` / `_LAST_TICK` | `api/metrics.py` | dict ×2 | `_STATE_LOCK` |
| `_counter_meta` / `_state` | `api/drain.py` | dict ×2 | `_lock` |
| `_sub_snapshot` / `_sub_lookup` / `_sub_entries` / `_mcp_server_results` / `_mcp_server_tool_counts` / `_skipped_servers` | `agent/tools/mcp_tool.py` | 热更新快照组 | `_sub_entries_lock` |

### 4.2 无锁但**良性**（保留，不必改）

| 名字 | 文件 | 为什么良性 |
|------|------|-----------|
| `_manifest_cache` / `_enriched_cache` | `agent/trace/skill_manifest.py` | **本仓库的范本**：`built` 标志位懒建 ×1，`return list(...)` **返回副本** ⇒ 外部拿不到内部对象、并发建两次只是白干一次 |
| `_fresh_cache` | `agent/retrieval/adapters.py` | 按 `index_dir` 分桶（项目数极少），写路径有显式 `pop` 失效；桶内是 `(时间戳, bool)`，覆盖无害 |
| `_EXPERIMENT_DATASET_CACHE` | `agent/eval/run_experiment.py` | `dataset_name → ds_id`，**天然只增不改**，同名必然同值 |
| `_wrenai_display_cache` | `agent/trace/langfuse_client.py` | `tuple|None`，整体重建/置空，无部分写 |
| `_title_models` | `api/auto_title.py` | 按模型键缓存实例；`clear()` + 重填，竞争下最坏是多建一个实例 |
| `_API_OK` | `agent/utils/wren_plan.py` | **三态** `Optional[bool] = None`（未探测 / 通 / 不通），探测幂等 |
| `_STAMPED` | `agent/auth/ownership.py` | 上限 `_STAMPED_CAP=20000`，超限 `clear()`——注释已写明「清空只是多打几次标，无害」 |

> 「无锁但良性」的共同点：**写的是整对象不是部分**，且**并发重复计算的结果等价**。
> 新增缓存要进这一栏，必须先满足这两条。

### 4.3 无锁且**有实际问题**（§8 动手项）

| 名字 | 文件 | 问题 |
|------|------|------|
| `_users_cache` | `agent/auth/users.py` | **跨进程不失效**，见 §8-1 |
| `_THREAD_TRACE_MAP` | `agent/trace/langfuse_client.py` | **无界增长**，见 §8-2 |

---

## 5. D 懒初始化单例（`= None` + 首次赋值）

模式统一：模块级 `_X: T | None = None` + `def _get_x()` 里首次赋值。
**判定标准是「首次赋值的双检是否有锁」**（无锁时两个线程都建一次，丢掉一个）。

| 名字 | 文件 | 双检保护 | 丢弃代价 |
|------|------|---------|---------|
| `_STORE` / `_REAPER` | `agent/subagents/pending_terminal.py` | `_STORE_LOCK` ✅ | — |
| `_STORE` / `_RUNNER` / `_WORKER` | `agent/eval/eval_queue.py` | `_STORE_LOCK` / `_WORKER_LOCK` ✅ | — |
| `_default_store` / `_user_stores` | `agent/settings/model_config_store.py`、`db_mcp_server/.../db_config_store.py` | `_LOCK` ✅ | — |
| `_PG_SAVER_CLS` | `agent/checkpoint/checkpointer_factory.py` | `_heal_lock` ✅ | — |
| `_THREAD` | `agent/utils/retention.py` | `_THREAD_LOCK` ✅ | — |
| `_client` / `_handler` | `agent/trace/langfuse_client.py` | 无锁（惰性单例，重复建 = 重复注册 handler） | 低 |
| `_meta_metrics` / `_meta_error` / `_task` | `api/metrics.py` | lifespan 单点启动 ✅ | — |
| `_STORE` | `agent/trace/trace_bind_store.py` | 无锁，构造即打开 sqlite | 低（句柄泄漏） |
| `_store` / `_store_path` | `agent/feedback/store.py` | 无锁 | 低 |
| `_default_manager` | `agent/workspace_manager/manager.py` | 无锁 | 低 |
| `_flags` | `agent/settings/feature_flags.py` | 无锁，读 env 的快照 | 低 |
| `_detector` / `_TOOL_POOL` / `_conn` | 见下 | **见 §8-3** | — |
| `_main_tools` / `_sub_tools` / `_all_tools` / `_sub_tool_filter` / `_sub_reload_thread` | `agent/tools/mcp_tool.py` | `_sub_entries_lock` + `_reload_thread_lock` ✅ | — |
| `_lance_ok` | `agent/retrieval/backends/__init__.py` | 探测结果三态，幂等 | — |
| `_db_conn` | `agent/auth/grants.py` | `_init_lock` ✅ | — |

---

## 6. E 连接型

| 名字 | 文件 | 类型 | 约束 |
|------|------|------|------|
| `_conn` | `api/thread_search.py` | sqlite3 单连接 | `_refresh_lock`；**读也要进锁**（同连接并发读会互踩）——见 memory `sqlite-shared-conn-lock-all-access` |
| `_db_conn` | `agent/auth/grants.py` | sqlite3 | `_init_lock`；grants 账本在 `agent_data` |
| `_default_store` / `_user_stores` | `model_config_store.py` / `db_config_store.py` | 文件存储单例 | `_LOCK` |
| `_TABLE_CACHE` 相关 LanceDB 连接 | `agent/retrieval/backends/lance.py` | 表句柄 | `_LOCK` |

**`checkpointer.conn` 不在这里**——它是 `_SelfHealingPostgresSaver` 的**实例**属性
（`self.conn = await AsyncConnection.connect(...)`，由 `_ensure_conn` 自愈），
**不是模块级状态**，把它列进「全局状态」是分类错误。

---

## 7. F 可变状态（注册表 / 会话表 / 计数器）

### 7.1 有锁（不动）

| 名字 | 文件 | 锁 |
|------|------|-----|
| `_ACTIVE_LOOPS` | `agent/subagents/sync_subagent_todos.py` | `_ACTIVE_LOOPS_LOCK`（判重：调用方必须先判 `is_sync_alive`，`launch_sync` 本身不幂等） |
| `_BUCKETS` | `agent/middlewares/progress_boundary.py` | `_BUCKETS_LOCK` |
| `_sub_*` 热更新组 | `agent/tools/mcp_tool.py` | `_sub_entries_lock` |
| `_counter_meta` / `_state` | `api/drain.py` | `_lock` |
| `_ALERT_STATE` / `_LAST_TICK` | `api/metrics.py` | `_STATE_LOCK` |
| `_STORES` | `agent/trace/event_store.py` | `_STORES_LOCK` |
| `_LAST_*` | `agent/utils/retention.py` | `_THREAD_LOCK` |

### 7.2 无锁但**依赖单事件循环**（当前正确，改动时要注意）

| 名字 | 文件 | 说明 |
|------|------|------|
| `_RUN_TASKS` | `api/experiment.py` | `dict[str, asyncio.Task]`；写点全在 async handler ⇒ 单循环内安全。**真正的坑是重启留僵尸 `running`**，见 §8-4 |
| `_PROJECT_LOCKS` | `api/wren_semantic.py` | `dict[str, asyncio.Lock]`；`asyncio.Lock` **本来就只能在单循环内用**，所以不需要额外锁保护这个 dict |

> ⚠️ 这一栏的隐含前提是「**只有一个事件循环**」。若将来引入多 worker 或
> `run_in_executor` 里碰这些名字，前提立刻失效。

### 7.3 `_SYNC_WRITE_LOCK` 的分类更正

手册模板把 `_SYNC_WRITE_LOCK` 列进「可变状态 → 锁 = 自身 → 风险高（全局串行化）」。
**这是分类错误**：它**是锁**，不是状态；「全局串行化」是它的**设计意图**（
`sync_subagent_todos.py` 里多个同步循环要串行写 state），不是风险。
真正的风险描述应该是「谁在持锁时做了慢 I/O」——这是另一个问题，不属于本审计。

---

## 8. 需要动手的（4 处）

### 8-1 `_users_cache` 跨进程永不失效 ⚠️ 唯一一条**会真的用错数据**的

`agent/auth/users.py:46` `_users_cache: list[dict] | None = None`，`_users_lock: RLock`。

事实：
- **没有 TTL**。失效只在 `reload_users()`（`:162-165` 置 `None`）。
- `reload_users()` 在 `src/agent/auth/users.py` **之外没有任何调用者**（grep 无命中）。
- 缓存**按引用返回**（`:175` / `:179` `return _users_cache`），调用方可变。
- 写路径（`add_user`）走「load → append → save」**原地改**，所以**本进程内**看不到陈旧。

⇒ 风险：**多进程 / 蓝绿双色**下，A 色改了用户，B 色的 `_users_cache` 永远不知道，
直到进程重启。当前 dev/prod 单容器单进程时不可见，**发版切流那一刻**会显现。

**建议**（不在本 Task 实施）：要么给 TTL，要么在写路径显式调 `reload_users()`，
要么 `return [dict(u) for u in _users_cache]` 切断引用。

### 8-2 `_THREAD_TRACE_MAP` 无界、无锁

`agent/trace/langfuse_client.py:46` `_THREAD_TRACE_MAP: dict[str, tuple[str, str]] = {}`。

- 写点：`:509`（`_finalize` 路径）、`:918`（子 run root chain 启动）。
- **读点**：`:500`。
- **没有逐出**（对比 `_TASK_TRACE_MAP` 在 `:545` 有 `del ...[_oldest]`）。
- 同模块另有 `_TASK_TRACE_MAP` / `_ROOT_OBS_MAP` / `_PROMPT_VERSIONS` / `_PROMPT_SOURCE` 四个 map，**同样无锁**。

⇒ 风险：长跑进程里**每多一个会话线程多一条**，只增不减；且这些 map 会被 §1 的三个
`langfuse-feedback*` **daemon 线程**写（见 `docs/daemon-threads.md`）。
单次 `[k]=v` 在 GIL 下安全，但**复合操作**（读-然后-写）跨线程无保证。

**建议**：给 `_THREAD_TRACE_MAP` 加与 `_TASK_TRACE_MAP` 同款上限 + 逐出；加锁与否取决于
是否真有复合操作（需先确认）。

### 8-3 `_TOOL_POOL` 懒初始化无锁

`agent/utils/path_resolver.py:882-889`：

```python
_TOOL_POOL: _SyncToolPool | None = None
def _get_tool_pool() -> _SyncToolPool:
    global _TOOL_POOL
    if _TOOL_POOL is None:
        _TOOL_POOL = _SyncToolPool()
    return _TOOL_POOL
```

**无双检、无锁**。与 §4.2 那些良性缓存的**关键差别**：丢掉的池**持有线程**，
不是「白干一次」而是**线程泄漏**（且是非 daemon 池，见 `daemon-threads.md` §2）。

⇒ 修法：模块级 `threading.Lock` + 双检（照 `eval_queue._STORE_LOCK` 的写法）。

### 8-4 `api/experiment.py` 的 `_RUN_TASKS` 重启僵尸

已在 `docs/daemon-threads.md` §5.4 记录成因（纯内存注册表 + 状态文件 `running`）。
本审计**不重复**，仅在此挂链，因为它是「F 类可变状态」里唯一有持久化后果的。

---

## 9. 与手册模板的差异（模板 4 处错误）

手册 `## Task 2-4` Step 2 给的模板有 4 处与代码不符，本审计已按实际代码更正：

| 模板写的 | 实际 |
|---------|------|
| `_users_cache` … 「60s TTL」 | **没有 TTL**（见 §8-1） |
| `_secret_cache` (auth/token.py) … `bytes` | 实际是 `str \| None`；无锁；只在 `reload_secret()`（`:91`）重置 |
| `_CACHE` (engine) (wren_plan.py) | 名字是 **`_ENGINE_CACHE`**；是真 LRU（`DEFAULT_CACHE_MAX=16`，`WREN_PLAN_CACHE_MAX` 覆盖） |
| `_API_OK` … `bool` | 是 `Optional[bool] = None`，**三态** |
| `_db_conn` (eval_queue.py) … sqlite3 单连接 | **此名字不存在**；eval_queue 持有的是 `_STORE`（`_STORE_LOCK`）。真实的 `_db_conn` 在 `auth/grants.py` |
| `checkpointer.conn` … 自愈连接 | 是 `_SelfHealingPostgresSaver` 的**实例属性** `self.conn`，**不是模块级状态** |
| `_SYNC_WRITE_LOCK` … 「可变状态 / 风险高」 | 它**是锁不是状态**（见 §7.3） |

> Step 1 的 grep（`^_[A-Z_]*\s*=`）**抓不到**带类型注解的赋值与缩进在 `try` 里的名字，
> 所以模板的清单天然不全——这正是本审计改用 AST 的原因。§10 的命令可一键复现。

---

## 10. 复现命令

```bash
.venv/Scripts/python.exe - <<'PY'
import ast, os
LOCKF = {"Lock","RLock","Event","Semaphore","Condition","Barrier"}
CONTF = {"dict","list","set","deque","OrderedDict","defaultdict"}
def fn(f): return f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
for dp, dn, fs in os.walk("src"):
    dn[:] = [d for d in dn if d not in ("test","__pycache__")]
    for f in fs:
        if not f.endswith(".py"): continue
        p = os.path.join(dp, f).replace("\\","/")
        t = ast.parse(open(p, encoding="utf-8").read())
        locks, mut = [], []
        for n in t.body:
            if isinstance(n, ast.Assign):
                tg = [x.id for x in n.targets if isinstance(x, ast.Name)]; v = n.value
            elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
                tg = [n.target.id]; v = n.value
            else: continue
            c = fn(v.func) if isinstance(v, ast.Call) else None
            for nm in tg:
                if c in LOCKF: locks.append(nm)
                elif isinstance(v, (ast.Dict, ast.List, ast.Set)) or c in CONTF: mut.append(nm)
                elif isinstance(v, ast.Constant) and v.value is None: mut.append(nm)
        if mut: print(p, "| locks:", ",".join(locks) or "-", "| mut:", ",".join(mut))
PY
```

---

## 11. 修改规则

- 新增模块级**可变**对象前，先回答三问：**多进程一致吗？跨线程有复合操作吗？有上限吗？**
  三个都安全才可以不加锁。
- 新增缓存照 §4.2 的范本（`skill_manifest.py`）：**built 标志位 + `return list(...)` 副本**。
- 新增懒初始化单例照 §5 的范本（`eval_queue`）：**模块级锁 + 双检**。
- 有跨进程语义的状态（用户、配置、开关）**不许缓存或必须带 TTL**——见 §8-1 的教训。
- 改了本文件里的任何一行 ⇒ 同步改本表。
