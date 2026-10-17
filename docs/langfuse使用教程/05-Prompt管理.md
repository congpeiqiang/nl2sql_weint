# 05 · Prompt 管理

> Langfuse Prompt Management 让你集中管理、版本化、灰度发布 LLM 提示词。  
> 参考：[Langfuse Prompts](https://langfuse.com/docs/prompts/get-started)

---

## 1. 为什么需要 Prompt 管理

直接在代码中硬编码 prompt 的问题：

| 痛点 | 后果 | Prompt Management 解决方式 |
|------|------|--------------------------|
| 改 prompt 要改代码 | 每次微调都要发版 | UI 直接编辑，即时生效 |
| 无法回滚 | 改坏了只能再改一次 | 标签切换秒级回滚 |
| 无法 A/B 测试 | 不知道新版比旧版好还是差 | 标签路由 + 流量分配 |
| 多人协作冲突 | 同时改 prompt 互相覆盖 | 版本管理，每次修改自动新版本 |
| 不知道线上跑什么版本 | 出问题无法定位 | 版本号 + 日志可追溯 |
| 评估标准和 prompt 耦合 | 改 rubric 要发版 | rubric 也作为 Prompt 管理 |

## 2. 核心概念

### 2.1 数据模型

```
Prompt（名称唯一标识）
├── v1 (labels: [archived])            ← 初始版本
├── v2 (labels: [archived])            ← 第一次修改
├── v3 (labels: [production])          ← 当前线上版本
├── v4 (labels: [staging])             ← 测试中的版本
└── v5 (labels: [canary])              ← 灰度版本
```

| 概念 | 说明 | 不可变性 |
|------|------|----------|
| **Name** | 唯一标识（如 `"nl2sql_system_prompt"`） | 创建后不可改 |
| **Version** | 自动递增版本号（整数） | 自动分配 |
| **Content** | Prompt 文本（支持变量插值） | 创建后不可改 |
| **Config** | JSON 配置（model, temperature 等） | 创建后不可改 |
| **Labels** | 标签列表（`production`/`staging`/自定义） | **可变**（标签可移动） |

> ⚠️ **版本一旦创建不可修改**。要更新 prompt，必须创建新版本。这保证了版本历史和回滚的可靠性。

### 2.2 标签体系

| 标签 | 用途 | 谁在用 |
|------|------|--------|
| `production` | 线上正式版本 | 所有生产流量 |
| `staging` | 测试版本 | 预发布验证 |
| `canary` | 灰度版本 | 小比例流量测试 |
| `latest` | 自动指向最新版本 | 系统自动管理 |
| 自定义 | 实验用标签 | `run_experiment` 等 |

**标签唯一性**：同一个标签同一时间只能指向一个版本。当把标签移到新版本时，旧版本自动失去该标签。

## 3. SDK API 详解

### 3.1 创建 Prompt

```python
from langfuse import Langfuse

langfuse = Langfuse()

# 创建新版本
prompt = langfuse.create_prompt(
    name="nl2sql_system_prompt",
    prompt="""你是一个专业的 SQL 助手。请根据用户的问题和数据库 schema，
生成正确的 SQL 查询语句。

规则：
1. 只生成 SELECT 语句，禁止 INSERT/UPDATE/DELETE
2. 必须使用提供的表和字段名，不要猜测
3. 注意 JOIN 条件的正确性
4. 聚合查询必须包含 GROUP BY
5. 排序查询必须包含 LIMIT（防止全表扫描）

当前数据库: {{db_name}}
可用表: {{available_tables}}""",
    labels=["staging"],    # 初始标签
    config={
        "model": "gpt-4o",
        "temperature": 0,
        "max_tokens": 4000,
    },
)

print(f"创建成功: v{prompt.version}")
```

### 3.2 获取 Prompt

```python
# 按标签获取
prompt = langfuse.get_prompt(
    name="nl2sql_system_prompt",
    label="production",       # 获取 production 标签的版本
    # fallback="本地兜底内容",  # 可选：获取失败时的兜底
)

# 读取内容
text = prompt.prompt           # 完整文本
version = prompt.version       # 版本号
labels = prompt.labels         # 标签列表
config = prompt.config         # 配置 dict

# 变量插值（如果 prompt 中有 {{var}} 占位符）
compiled = prompt.compile(
    db_name="chinook",
    available_tables="artist, album, track",
)
```

### 3.3 更新标签（发布/回滚）

```python
# 发布：将 staging 版本标记为 production
langfuse.update_prompt_labels(
    name="nl2sql_system_prompt",
    version=4,                  # 要发布的版本号
    new_labels=["production"],  # 新标签（旧 production 自动移除）
)

# 回滚：将旧版本重新标记为 production
langfuse.update_prompt_labels(
    name="nl2sql_system_prompt",
    version=3,                  # 回滚到 v3
    new_labels=["production"],
)

# 同时设置多个标签
langfuse.update_prompt_labels(
    name="nl2sql_system_prompt",
    version=5,
    new_labels=["production", "canary"],
)
```

### 3.4 列出所有版本

```python
# 获取某个 prompt 的所有版本
prompts = langfuse.fetch_prompts(name="nl2sql_system_prompt")
for p in prompts:
    print(f"v{p.version}: labels={p.labels}, created={p.created_at}")
```

## 4. UI 管理

### 4.1 Prompt 列表页

进入 **Prompts** 页面可以看到：

| 列 | 说明 |
|----|------|
| Name | Prompt 名称 |
| Production Version | 当前 production 版本号 |
| Latest Version | 最新版本号 |
| Labels | 各标签指向的版本 |
| Last Updated | 最后更新时间 |

### 4.2 版本详情页

点击一个 prompt 进入详情页：

- **版本历史**：所有版本列表，按版本号倒序
- **版本对比**：选择两个版本，高亮差异
- **标签管理**：拖拽标签到目标版本
- **内容预览**：查看每个版本的完整内容
- **编辑**：基于当前版本创建新版本

### 4.3 创建新版本

1. 点击 **New Version**（或 **Edit**）
2. 编辑 prompt 内容
3. 设置 config（JSON 格式）
4. 选择初始标签（建议先打 `staging`）
5. 保存

## 5. 在应用中使用

### 5.1 启动时拉取（推荐）

```python
from langfuse import Langfuse
import logging

logger = logging.getLogger(__name__)
langfuse = Langfuse()

def load_system_prompt(name: str, label: str = "production") -> str:
    """服务启动时从 Langfuse 拉取 prompt"""
    try:
        prompt = langfuse.get_prompt(name=name, label=label)
        logger.info(
            f"[langfuse] prompt {name}(label={label}) v{prompt.version} 生效"
        )
        return prompt.prompt
    except Exception as e:
        logger.warning(
            f"[langfuse] prompt 拉取失败 ({name}): {e}，使用本地兜底"
        )
        return load_local_fallback(name)

def load_local_fallback(name: str) -> str:
    """从本地文件读取兜底 prompt"""
    fallback_path = f"src/agent/prompts/{name}.txt"
    try:
        with open(fallback_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        raise RuntimeError(f"Langfuse 不可用且本地兜底文件不存在: {fallback_path}")

# 服务启动时
SYSTEM_PROMPT = load_system_prompt("main_system_prompt")
NL2SQL_PROMPT = load_system_prompt("nl2sql_system_prompt")
```

### 5.2 带缓存的获取

```python
from functools import lru_cache

@lru_cache(maxsize=32)
def get_cached_prompt(name: str, label: str = "production") -> str:
    """带进程级缓存的 prompt 获取"""
    prompt = langfuse.get_prompt(name=name, label=label)
    return prompt.prompt

# 每次查询时使用（缓存命中后零网络开销）
system_prompt = get_cached_prompt("nl2sql_system_prompt")
```

> ⚠️ `lru_cache` 是进程级缓存。修改 Langfuse 上的 prompt 后需要**重启服务**才能生效（或清除缓存）。

### 5.3 同步脚本

```bash
# 将本地 prompt 文件上传到 Langfuse
PYTHONPATH=src .venv/Scripts/python.exe -m agent.prompt.sync_prompts \
    --all                    # 同步所有 prompt
    --label staging          # 打 staging 标签
    --commit "修复 JOIN 逻辑" # 版本说明

# 只同步指定 prompt
PYTHONPATH=src .venv/Scripts/python.exe -m agent.prompt.sync_prompts \
    --name nl2sql_system_prompt \
    --label production
```

## 6. A/B 灰度发布

### 6.1 标签路由（最简单）

```python
import os

# 环境变量控制走哪个标签
PROMPT_LABEL = os.getenv("LANGFUSE_PROMPT_LABEL", "production")

def get_prompt(name):
    return langfuse.get_prompt(name=name, label=PROMPT_LABEL)
```

```bash
# 全量 production
LANGFUSE_PROMPT_LABEL=production

# 切到 staging 测试
LANGFUSE_PROMPT_LABEL=staging
```

### 6.2 按比例灰度（Canary）

```python
import os
import random

CANARY_LABEL = os.getenv("LANGFUSE_CANARY_LABEL", "")
CANARY_RATIO = float(os.getenv("LANGFUSE_CANARY_RATIO", "0"))

def get_prompt_with_canary(name: str):
    """按 canary 比例分流"""
    if CANARY_LABEL and CANARY_RATIO > 0 and random.random() < CANARY_RATIO:
        return langfuse.get_prompt(name=name, label=CANARY_LABEL)
    return langfuse.get_prompt(name=name, label="production")
```

```bash
# 10% 流量走 canary
LANGFUSE_CANARY_LABEL=staging
LANGFUSE_CANARY_RATIO=0.1
```

### 6.3 灰度发布完整流程

```
Step 1: 修改 prompt → 创建新版本 → 标签: staging
Step 2: 在 staging 环境验证（run_experiment 离线回归）
Step 3: 通过 → 创建 canary 版本 → 标签: canary
Step 4: 设置 canary 比例（如 10%）→ 重启服务
Step 5: 观察 canary 的评估指标（feedback_gate 在线门禁）
Step 6: 通过 → 将 staging 版本标记为 production（全量发布）
Step 7: 未通过 → 修复后回到 Step 1
```

## 7. Prompt 版本快照（实验可复现性）

### 7.1 为什么需要快照

实验必须保证可复现性：同一个 `run_name` 多次跑，使用的 prompt 必须完全一致。但 Langfuse 上的 prompt 标签可以随时被修改。

### 7.2 快照方案

```python
# 实验前物化 prompt 快照
def snapshot_prompts(label: str, dest_dir: str):
    """将指定标签的 prompt 快照到本地目录"""
    for name in PROMPT_NAMES:
        prompt = langfuse.get_prompt(name=name, label=label)
        snapshot_path = f"{dest_dir}/{name}@v{prompt.version}.txt"
        with open(snapshot_path, "w") as f:
            f.write(prompt.prompt)
        # 写入 .prompts_ok marker
        with open(f"{dest_dir}/.prompts_ok", "w") as f:
            f.write(f"{name}:v{prompt.version}\n")
```

实验 worker 使用 `PROMPT_REFS` 环境变量指向快照目录，保证实验过程中 prompt 不变。

## 8. 最佳实践

### 8.1 命名规范

| 用途 | 命名建议 |
|------|----------|
| 主 Agent 系统 prompt | `main_system_prompt` |
| 子 Agent 系统 prompt | `nl2sql_system_prompt` |
| 评估 rubric | `eval_sql_correctness_rubric` |
| 特定任务 prompt | `report_generation_prompt` |
| 图表生成 prompt | `chart_generation_prompt` |

### 8.2 本地兜底必备

```python
# ✅ 永远有兜底
def get_prompt_safe(name, label="production"):
    try:
        return langfuse.get_prompt(name=name, label=label).prompt
    except Exception:
        return open(f"prompts/{name}.txt").read()

# ❌ 无兜底 — Langfuse 宕机时服务崩溃
def get_prompt_unsafe(name, label="production"):
    return langfuse.get_prompt(name=name, label=label).prompt
```

### 8.3 版本管理纪律

- 每次修改都创建新版本（版本不可修改）
- `production` 标签只给验证通过的版本
- 保留历史版本方便回滚
- 在 git commit message 中注明 prompt 版本号
- 发版前跑 `run_experiment` 回归门禁

## 9. 下一步

- [06-数据集与实验](06-数据集与实验.md)：用实验验证 prompt 变更的效果
- [07-反馈闭环](07-反馈闭环.md)：用评估结果驱动 prompt 迭代
