# 05 · Prompt 管理

> Langfuse 的 Prompt Management 功能让你集中管理、版本化、灰度发布 LLM 提示词。

---

## 1. 为什么需要 Prompt 管理

直接在代码中硬编码 prompt 的问题：

- ❌ 每次改 prompt 都要改代码、发版
- ❌ 无法回滚到旧版本
- ❌ 无法做 A/B 测试
- ❌ 多人协作时容易冲突
- ❌ 不知道线上跑的是哪个版本

Langfuse Prompt Management 解决以上所有问题。

## 2. 核心概念

```
Prompt Name（如 "nl2sql_system_prompt"）
├── v1 (label: archived)
├── v2 (label: staging)
├── v3 (label: production)   ← 线上使用的版本
└── v4 (label: canary)       ← 灰度测试的版本
```

| 概念 | 说明 |
|------|------|
| **Prompt Name** | Prompt 的唯一标识（如 `"main_system_prompt"`） |
| **Version** | 自动递增的版本号（每次更新 +1） |
| **Label** | 标签（`production` / `staging` / `canary` / 自定义） |
| **Content** | Prompt 文本内容 |
| **Config** | 可选的 JSON 配置（如 temperature、model 等） |

## 3. 通过 SDK 管理 Prompt

### 3.1 创建 / 更新 Prompt

```python
from langfuse import Langfuse

langfuse = Langfuse()

# 创建新版本的 prompt
langfuse.create_prompt(
    name="nl2sql_system_prompt",
    prompt="""你是一个专业的 SQL 助手。请根据用户的问题和数据库 schema，
生成正确的 SQL 查询语句。

规则：
1. 只生成 SELECT 语句
2. 必须使用提供的表和字段
3. 注意 JOIN 条件的正确性""",
    labels=["staging"],  # 新版本的标签
)
```

### 3.2 获取 Prompt

```python
# 获取指定标签的最新版本
prompt = langfuse.get_prompt(
    name="nl2sql_system_prompt",
    label="production",  # 获取 production 标签的版本
)

# 使用 prompt 文本
system_message = prompt.prompt  # 或 prompt.get_prompt()

# 查看版本信息
print(f"版本: {prompt.version}, 标签: {prompt.labels}")
```

### 3.3 更新标签（发布 / 回滚）

```python
# 将某个版本标记为 production（发布）
langfuse.update_prompt_labels(
    name="nl2sql_system_prompt",
    version=3,                  # 目标版本号
    new_labels=["production"],  # 新标签（会移除旧的 production 标签）
)

# 回滚：将旧版本重新标记为 production
langfuse.update_prompt_labels(
    name="nl2sql_system_prompt",
    version=2,
    new_labels=["production"],
)
```

### 3.4 带配置的 Prompt

```python
langfuse.create_prompt(
    name="nl2sql_system_prompt",
    prompt="...",
    labels=["staging"],
    config={
        "model": "gpt-4o",
        "temperature": 0,
        "max_tokens": 4000,
    },
)

# 获取时读取配置
prompt = langfuse.get_prompt(name="nl2sql_system_prompt", label="production")
model = prompt.config.get("model", "gpt-4o")  # 带默认值
temperature = prompt.config.get("temperature", 0)
```

## 4. 通过 UI 管理 Prompt

### 4.1 查看 Prompt 列表

进入 **Prompts** 页面：

- 所有已注册的 prompt 名称
- 每个 prompt 的当前 production 版本号
- 最后更新时间

### 4.2 查看版本历史

点击一个 prompt 进入详情页：

- 所有版本列表
- 每个版本的内容、标签、创建时间
- 版本间差异对比

### 4.3 编辑与发布

1. 点击 **New Version** 创建新版本
2. 编辑 prompt 内容
3. 设置初始标签（如 `staging`）
4. 保存
5. 验证通过后，将标签改为 `production`

## 5. 在应用中使用 Prompt

### 5.1 启动时拉取

```python
from langfuse import Langfuse

langfuse = Langfuse()

def load_prompts():
    """服务启动时从 Langfuse 拉取 prompt，失败则本地兜底"""
    try:
        main_prompt = langfuse.get_prompt(
            name="main_system_prompt",
            label="production",
        )
        print(f"[langfuse] prompt main_system_prompt(label=production) v{main_prompt.version} 生效")
        return main_prompt.prompt
    except Exception as e:
        print(f"[langfuse] prompt 拉取失败，本地兜底: {e}")
        return load_local_fallback("main_system_prompt")
```

### 5.2 带缓存的拉取

```python
_prompt_cache = {}

def get_cached_prompt(name: str, label: str = "production") -> str:
    """带本地缓存的 prompt 获取（减少 API 调用）"""
    cache_key = f"{name}:{label}"
    
    if cache_key in _prompt_cache:
        return _prompt_cache[cache_key]
    
    try:
        prompt = langfuse.get_prompt(name=name, label=label)
        _prompt_cache[cache_key] = prompt.prompt
        return prompt.prompt
    except Exception:
        return load_local_fallback(name)
```

## 6. A/B 灰度发布

### 6.1 标签切换

最简单的灰度方式：通过标签控制流量。

```bash
# 全量使用 production
LANGFUSE_PROMPT_LABEL=production

# 切换到 staging 版本测试
LANGFUSE_PROMPT_LABEL=staging
```

### 6.2 按比例灰度

```python
import random

CANARY_RATIO = 0.1  # 10% 流量走 canary

def get_prompt_with_canary(name: str):
    if random.random() < CANARY_RATIO:
        return langfuse.get_prompt(name=name, label="canary")
    else:
        return langfuse.get_prompt(name=name, label="production")
```

### 6.3 发布流程

```
1. 修改 prompt → 创建新版本（label: staging）
2. 在 staging 环境验证
3. 创建 canary 版本（label: canary），小比例灰度
4. 观察 canary 的评估指标
5. 通过 → 将 staging 版本标记为 production（全量发布）
6. 未通过 → 修复后重复步骤 1
```

## 7. 最佳实践

### 7.1 命名规范

| Prompt 用途 | 建议命名 |
|-------------|----------|
| 主 Agent 系统 prompt | `main_system_prompt` |
| 子 Agent 系统 prompt | `nl2sql_system_prompt` |
| 评估用 prompt | `eval_sql_correctness_prompt` |
| 特定任务 prompt | `report_generation_prompt` |

### 7.2 版本管理

- 每次修改都创建新版本（不要覆盖）
- `production` 标签只给验证通过的版本
- 保留历史版本方便回滚
- 在 commit message 中注明 prompt 版本号

### 7.3 本地兜底

**始终**准备本地兜底文件。Langfuse 不可用时（网络故障、服务重启），应用仍能正常运行：

```python
def get_prompt(name, label="production", fallback_path=None):
    try:
        prompt = langfuse.get_prompt(name=name, label=label)
        return prompt.prompt
    except Exception as e:
        logger.warning(f"Langfuse prompt 拉取失败 ({name}): {e}，使用本地兜底")
        if fallback_path and os.path.exists(fallback_path):
            with open(fallback_path, "r", encoding="utf-8") as f:
                return f.read()
        raise
```

## 8. 下一步

- [06-数据集与实验](06-数据集与实验.md)：基于数据集测试 prompt 变更的效果
- [07-反馈闭环](07-反馈闭环.md)：用评估结果驱动 prompt 迭代
