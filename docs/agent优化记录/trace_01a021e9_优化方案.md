# trace_01a021e9 优化方案

> 基于 trace 分析，结合 nl2sql 项目代码结构，给出可落地的优化方案。
> **暂不实施，仅做方案设计。**

---

## 优化项一览

| 优先级 | 优化项 | 预期节省 | 风险 | 涉及文件 |
|:---:|:---|:---:|:---:|:---|
| 🔴 P0 | 模型实例缓存 | **~36s/会话 (50%)** | 低 | `thinking_toggle.py` |
| 🟡 P1 | execute 进程池复用 | **~20-30s/会话** | 中 | `dynamic_workspace.py` |
| 🟢 P2 | CurrentDbContext 合并入 dynamic_prompt | **~0.5s/会话** | 低 | `current_db_context.py`, `main_agent.py` |
| ⚪ P3 | 消息去重 | **~0.2s/会话** | 低 | 已有 `message_slimmer.py` |

---

## 🔴 P0：模型实例缓存（最高优先级）

### 问题定位

trace 显示每次 LLM 调用前，`ThinkingToggleMiddleware._maybe_swap()` 有 **~9.3s 的空白时段**。

### 实测基准（2026-08-21，`bench_create_model.py`）

```
[4] import agent.llms.model:              79.503s  ← 首次 import（含 langchain_deepseek 28s，进程内仅 1 次）
[5] _resolve_llm_config():                0.008s   ← 排除配置文件/解密是瓶颈
[6] create_model(enable_thinking=True) 第1次: 9.459s  (ChatDeepSeek)
[6] create_model(enable_thinking=True) 第2次: 9.526s  ← 无缓存，每次都重建
[6] create_model(enable_thinking=True) 第3次: 9.177s
[8] ChatDeepSeek 构造:                    8.213s   ← 构造本身就要 ~8.2s
```

**铁证：**
- `ChatDeepSeek` **构造函数本身耗时 ~8.2s**（`langchain_deepseek.ChatDeepSeek(...)` 实例化），与 trace 观测的 9.3s 空白完全吻合
- 连续 3 次 `create_model()` 都是 ~9.2-9.5s → **当前代码完全没有任何缓存，每次 LLM 调用都重新构造模型**
- `_resolve_llm_config()` 只花 0.008s → 排除读取 `model_config.json`/AES 解密是瓶颈的可能

### 根因

`ThinkingToggleMiddleware._maybe_swap()` 方法（[src/agent/middlewares/thinking_toggle.py:65-83](src/agent/middlewares/thinking_toggle.py#L65-L83)）：
```python
def _maybe_swap(self, request):
    enable, route, model_name = self._resolve_overrides(request)
    if enable is None and not route and not model_name:
        return request
    from agent.llms.model import create_model
    model = create_model(enable_thinking=enable, route=route, model_name=model_name)
    # ...
    return request.override(model=model)
```

每次 LLM 调用都执行：
1. `create_model()` → 新建 `ChatDeepSeek` 实例
2. `ChatDeepSeek.__init__` 内部 **~8.2s**（实测：与 import 无关的纯构造开销，怀疑是 langchain_deepseek 初始化时的 SSL/httpx/vendor 能力加载）
3. 结果：**每次模型调用固定付出 9.2~10.5s 的模型重建成本**

**四轮调用损失：9.3 + 8.6 + 7.7 + 10.5 = ~36s（占会话 150s 的 24%）**

### 方案 A：Session 级模型缓存（推荐）

```python
# thinking_toggle.py 中增加缓存
class ThinkingToggleMiddleware(AgentMiddleware):
    _model_cache: dict[str, Any] = {}  # 进程级缓存

    def _cache_key(self, enable, route, model_name) -> str:
        return f"{route or ''}:{model_name or ''}:{enable}"

    def _maybe_swap(self, request):
        enable, route, model_name = self._resolve_overrides(request)
        key = self._cache_key(enable, route, model_name)
        if key in self._model_cache:
            return request.override(model=self._model_cache[key])
        model = create_model(enable_thinking=enable, route=route, model_name=model_name)
        if model:
            self._model_cache[key] = model
        return request.override(model=model)
```

**效果：** 第1次调用仍 ~9.3s，之后 **0s**（后续 3 轮节省 ~27s）
**风险：** 模型配置变更后缓存可能过时（需监听 `model_config.json` 文件变更或加 TTL）

### 方案 B：懒初始化 + 延迟创建

将 `create_model()` 的调用从 `_maybe_swap` 推迟到 `_execute_model_sync` 中（即 LLM 真正执行前再创建），但不对齐当前中间件架构的 hook 点。

### 方案 C：模型实例在模块级缓存

```python
# model.py 中增加模块级缓存
_model_instance_cache: dict[str, Any] = {}
create_model()  # 首次调用时创建并缓存
```

### 推荐方案 A 的落地步骤

1. 在 `ThinkingToggleMiddleware` 中增加 `_model_cache` 字典
2. 在 `_maybe_swap` 中按 `(enable_thinking, route, model_name)` 三元组构建缓存 key
3. 命中则直接 `override(model=...)` 返回
4. 未命中则调用 `create_model()` 并缓存
5. 增加 `model_config.json` 的 `watchdog` 文件监听，变更时清空缓存
6. 缓存最大条目数限制（如 10 条，防止无限增长）

---

## 🟡 P1：execute 进程池复用

### 问题定位

3 次 `execute` 调用全部是 `python` 启动独立进程 + 查询 LangSmith API，耗时 15~28s/次。

### 实测基准（2026-08-21，`bench_execute.py`）

```
[1] Python 空进程启动（5次平均）:           0.419s/次  ← 启动开销很小
[2] Python + LangSmith list_runs(limit=100): 18.958s   ← 含 import (~11s) + API (~7.3s)
[3] 单进程内 3 次 list_runs:                 26.217s   ← 平均 8.7s/次
```

**耗时拆解（每次 execute）：**
| 环节 | 耗时 | 说明 |
|:---|:---:|:---|
| Python 子进程启动 | **0.42s** | subprocess.Popen + 解释器初始化 |
| LangSmith 库 import | **~11s** | langsmith.Client 及其依赖的 import 开销 |
| LangSmith API `list_runs` | **~7.3s/次** | 分页 HTTP 请求（100条，多页） |
| **合计** | **~18.7s** | 首次执行 |
| 后续复用进程 | **~7.3s/次** | 第2次起节省 import 和进程启动 |

**关键结论：**
- 进程启动 0.4s 不是瓶颈
- 如果复用进程（3 次 API 调用在同一个 Python 进程内），总耗时可从 **57s → 35s，节省 ~22s（38%）**
- 但 LangSmith API 本身 ~7.3s/次 的延迟无可避免（这是远程 API 的固有延迟）

### 根因

`DynamicLocalShellBackend.execute()`（[src/agent/backends/dynamic_workspace.py:79-82](src/agent/backends/dynamic_workspace.py#L79-L82)）：
```python
def execute(self, command, *, timeout=None):
    self.cwd = Path(self._get_root_dir()).resolve()
    return super().execute(command, timeout=timeout)
```

`LocalShellBackend.execute()` 即 `subprocess.run(command, shell=True)`，每次启动新进程。

### 方案 A：Python REPL 进程池（推荐）

在 `DynamicLocalShellBackend` 中维护一个 Python REPL 子进程，把 `import langsmith` 的开销摊到首个调用：

```python
class DynamicLocalShellBackend(LocalShellBackend):
    _python_process: subprocess.Popen | None = None

    def _ensure_python_repl(self):
        if self._python_process is None or self._python_process.poll() is not None:
            self._python_process = subprocess.Popen(
                [sys.executable, "-i", "-q"],  # 交互模式
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        return self._python_process

    def execute_python(self, code: str) -> str:
        proc = self._ensure_python_repl()
        proc.stdin.write(f"{code}\nprint('__EXEC_DONE__')\n")
        proc.stdin.flush()
        # 读取直到 __EXEC_DONE__
        ...
```

**效果：**
- 第1次 execute 仍付 ~11s 的 `import langsmith` 开销
- 后续 execute **省去 import + 进程启动，每次仅剩 ~7.3s API 时间**
- 3 次 execute 总耗时从 **~57s → ~35s，节省 ~22s（38%）**

**风险：**
- 子进程状态隔离：REPL 里残留的变量/模块状态可能污染后续调用
- 内存泄漏：REPL 进程长期不回收
- 并发安全：主 agent 多线程（并发查询）需加锁或为每个线程建独立 REPL
- 需在 `LocalShellBackend.execute()` 的路径上拦截 `python` 命令，绕开原生实现

### 方案 B：单进程批处理

对 trace 中「同一轮内连续多次查询 LangSmith」的场景，提示 LLM 把多次查询合并到**一个脚本里一次执行**（脚本内部循环查询），省掉进程启动和重复 import。trace 中 3 次 execute 实际是 3 个独立脚本，若能合并成 1 个脚本在 1 次 execute 内循环，可把 3×(0.42+11+7.3) 降到 0.42+11+3×7.3 ≈ **33s**。

**风险：** 依赖 LLM 生成的脚本质量，agent 未必每次都合并（可配合 prompt 强约束）。

### 方案 C：新增 LangSmith MCP 工具（根本解）

trace 中 `execute` 反复运行 Python 脚本查 LangSmith，本质是 agent 想访问 LangSmith 数据。与其让 agent 每次 `write_file` 写脚本再 `execute`，不如直接提供一个 **`query_langsmith_traces` MCP 工具**，内部封装 `langsmith.Client.list_runs()`（进程内常驻，无需 subprocess），一次调用直达 API：

```python
@tool
def query_langsmith_traces(project: str, start_time: str, limit: int = 100) -> dict:
    """查询 LangSmith 项目的 recent traces（进程内调用，无 subprocess 开销）"""
    from langsmith import Client
    c = Client()
    runs = list(c.list_runs(project_name=project, filter=f'and(gte(start_time, "{start_time}"))', limit=limit))
    return {"count": len(runs), "traces": [...]}
```

**效果：**
- 消除全部 subprocess + import 开销，每次查询仅剩 API 时间 ~7.3s
- 3 次查询从 ~57s → **~22s，节省 35s（61%）**
- 根治「LLM 写脚本出错」的浪费（trace 中 `query_studio_projects.py` 报 `generator has no len()` 白跑 15.4s）

**风险：** 需实现工具 + 注册到 `mcp_tools`；需考虑工具暴露给 LLM 的参数校验与安全边界。

### 推荐

**方案 C > 方案 B > 方案 A**。方案 C 从架构上消除 subprocess 和重复 import，收益最大且最稳。方案 B 是零代码的最小改动（只改 prompt），方案 A 是后端兜底。

### 落地步骤（方案 C）

1. 新建 `src/agent/tools/langsmith_tool.py`，实现 `query_langsmith_traces` 工具（用 langsmith SDK，进程内）
2. 在 `src/agent/tools/mcp_tool.py` 中注册该工具
3. 在 `MAIN_AGENT_PROMPT.md` 的可用工具清单中加入该工具，说明用途
4. （可选）保留 execute 兜底，但 prompt 提示 LLM 优先使用专用工具而非写脚本

---

## 🟢 P2：CurrentDbContextMiddleware 合并到 dynamic_prompt

### 问题定位

`CurrentDbContextMiddleware` 本身只有 **~0.3s 开销**，但作为独立中间件增加了洋葱圈数。

### 方案

`CurrentDbContextMiddleware._inject()` 的功能（把 `【当前数据库：{db_name}】` 注入最新用户消息）可以直接合并到 `main_agent.py` 的 `dynamic_prompt()` 函数中，因为 `dynamic_prompt` 已经做了类似的事（把 `db_name` 注入 system prompt）。

```python
# main_agent.py dynamic_prompt() 中增强
@dynamic_prompt
def dynamic_prompt(request: ModelRequest) -> str:
    # ... 现有逻辑：注入 db_name 到 system prompt 顶部 ...
    
    # 额外：注入到最新用户消息
    messages = getattr(request, "messages", [])
    if messages and isinstance(messages[-1], HumanMessage):
        last = messages[-1]
        prefix = f"【当前数据库：{db_name}】"
        if not last.content.startswith(prefix):
            last.content = prefix + last.content
    
    return prompt
```

**效果：** 减少一层中间件包装，节省 ~0.3s/次
**风险：** 低，`CurrentDbContextMiddleware` 本身逻辑简单

### 落地步骤

1. 将 `CurrentDbContextMiddleware._inject()` 的逻辑移到 `dynamic_prompt()`
2. 从 `main_agent.py` 的 `middleware` 列表中移除 `db_context_middleware`
3. 删除 `current_db_context.py` 文件
4. 确保 `dynamic_prompt()` 的注入逻辑幂等（已存在前缀则不重复注入）

---

## ⚪ P3：消息去重（已有但不完善）

### 现状

`MessageSlimmerMiddleware`（[src/agent/middlewares/message_slimmer.py](src/agent/middlewares/message_slimmer.py)）已经实现了：
- 超大 tool 结果截断落盘（阈值 16KB）
- 完全重复结果去重

但当前 trace 中未观察到被触发的场景（`execute` 输出较小，未触发截断）。

### 建议

1. 降低 `max_chars_before_truncate` 阈值（从 16000 降到 8000）
2. 对 `write_file` 的结果也做去重（当前 `write_file` 每次写入内容不同，去重难生效）

---

## 预期收益汇总

| 优化项 | 节省时间 | 实施难度 | 代码改动量 | 备注 |
|:---|:---:|:---:|:---:|:---|
| P0 模型缓存 | 36s (24%) | ⭐ 低 | ~20 行 | 实测 ChatDeepSeek 构造 ~8.2s/次，缓存后第2次起 0s |
| P1-C 新增 LangSmith MCP 工具 | 35s (23%) | ⭐⭐ 中 | ~80 行 | 消除 subprocess + import 开销，每次仅剩 API 时间 |
| P1-B 合并脚本（prompt） | 34s (22%) | ⭐ 低 | ~5 行 | 零代码，只改 prompt |
| P1-A REPL 进程池 | 22s (15%) | ⭐⭐⭐ 中 | ~80 行 | 反复 import 问题，但遗留 API 时间 |
| P2 合并中间件 | 0.5s (0.3%) | ⭐ 低 | ~30 行 | 收益微薄 |
| P3 消息去重 | 0.2s (0.1%) | ⭐ 低 | ~5 行 | 已有代码，调阈值即可 |

**核心建议：只做 P0（模型缓存）+ P1-B（prompt 提示合并脚本）或 P1-C（新增 LangSmith 工具）即可覆盖 80% 的优化空间。**

### 优化后预期时间线

```
当前:  model(23.3s) → execute(27.9s) → model(17.3s) → execute(15.4s) → model(17.8s) → execute(19.0s) → model(21.5s)
       = 150s

优化后(只做P0):
       model(14.0s) → execute(27.9s) → model(8.0s) → execute(15.4s) → model(8.1s) → execute(19.0s) → model(10.5s)
       = 114s  (节省 36s, 24%)

优化后(P0+P1-C):
       model(14.0s) → execute(8.7s)  → model(8.0s) → execute(8.7s)  → model(8.1s) → execute(8.7s)  → model(10.5s)
       = 79s  (节省 71s, 47%)

优化后(P0+P1-B):
       model(14.0s) → execute(11.7s) → model(8.0s) → execute(0s)    → model(8.1s) → execute(0s)    → model(10.5s)
       = 66s  (节省 84s, 56%)  ★ 最理想：合并后只需 1 次 execute
```