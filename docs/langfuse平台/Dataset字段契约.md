# Langfuse Dataset 条目字段契约（badcase / goodcase）

> 写入方：`src/api/feedback_annotation.py`（人工确认两条出口 + **点赞自动入集**）、`src/agent/eval/collect_badcase.py`（每日自动采集）
> 读取方：`src/api/feedback_annotation.py` 的 `_dataset_item_to_row`（`GET /api/feedback/datasets`）、Langfuse UI、离线评测（`run_experiment`）
>
> **命名沿革（2026-09-19）**：三个「模型侧留档」键由 `physical_sql` / `cube_spec_original` /
> `cube_spec_readable_original` 改为 `physical_sql_original` / `cube_original` /
> `cube_original_readable` —— 统一 `_original`（=「**模型原本那份**」）后缀。这几个键当时
> **尚未发版**（HEAD 里只有 `gold_sql`/`good_sql`），属干净改名，读路径**不留旧名兼容**。
> 同批把 `MAX_SNAPSHOT_SQL` 由 8000 提到 64000，见下表。
>
> **2026-09-19 同日第二批**：新增 `source=auto-good`（点赞自动入集）、metadata 键
> `message_id`（撤回定位键，**两条出口都写**）与 `GET /api/feedback/dataset-stats`
> （金标缺口度量）。见 §四 与「撤回入集」一节。

---

## 一句话规则：谁是权威

**`expected_output.*` 是机器读的唯一权威；`metadata.*` 里的同名内容是给人看的镜像 + 兜底（自动采集条 / 2026-09-19 前的存量条目）。**

所以：评测、回归、Run 对比**一律读 `expected_output`**；读 `metadata` 只应发生在 ① Langfuse UI 里人浏览 ② `expected_output` 缺席或没有对应槽位时。两组内容由同一次调用产出，**不会漂移**，重复是刻意的（见末尾「为什么不合并」）。

---

## 字段表

### 一、`expected_output`（权威）

| 键 | 含义 | 谁有 | 谁读 |
|---|---|---|---|
| `sql` | **人工认定该跑的那条 SQL**（金标）。Cube 通道为引擎按口径编译出的**物理 SQL** | 人工确认条；**自动采集条恒为 null** | `_dataset_item_to_row` → 前端 `DatasetItem.sql`；离线评测 |
| `cube` | 该题的**金标聚合口径**（`{cube, measures, dimensions, filters…}`，已归一化：列表排序、剥 limit/offset），**对象**形态 | 仅 Cube 通道条 | 同上 → 前端 `DatasetItem.cube_spec`；Experiment Output 列可直接并排 diff |

### 二、`metadata` 中的人工侧镜像（人读；同名内容已在 `expected_output`）

| 键 | 含义 | 备注 |
|---|---|---|
| `gold_sql` | = `expected_output.sql` | **仅 badcase** 有此键名 |
| `good_sql` | = `expected_output.sql` | **仅 goodcase** 有此键名；两个数据集键名不同是历史事实，读方按 dataset 区分 |
| `cube_spec` | = `expected_output.cube`，但为**单行 JSON 串** | Langfuse UI metadata 面板原样展示；也是**自动采集条的唯一口径来源**，故读路径有 `expected_output.cube or metadata.cube_spec` 兜底 |
| `cube_spec_readable` | 同一份口径的**人读多行文本**（`wren_call_extract.spec_readable_text`） | 只在 metadata：`expected_output` 里放文本没有意义 |

> 我们的代码**一处都不读** `gold_sql` / `good_sql`（它们是纯给人看的）。`cube_spec` 只在 `expected_output.cube` 缺席时被读（自动采集条、存量老条目）。

### 三、`metadata` 中的模型侧留档（**只在 metadata**，不属于"期望"）

`_original` 后缀 = 「**模型原本那份**」，与当前生效（人工可能改过）的那份成对。

| 键 | 含义 | 变化规律 |
|---|---|---|
| `physical_sql_original` | 模型**实际下发**的那条 SQL（Cube 通道＝当时口径的进程内复算物理 SQL） | 取 `feedback` 表不可变快照，**人工任何操作都改不动它**；空则不写键。截断线 `MAX_SNAPSHOT_SQL`（2026-09-19 由 8000 提到 64000，实测最长 10.9KB → 正常口径恒为**完整物理 SQL**，可直连库执行） |
| `cube_original` | 模型**原始**那份口径（JSON 串） | 同上，读不可变快照；**没有回退**，拿不到就整个键不写（绝不拿人工改过的冒充原始） |
| `cube_original_readable` | 上面那份的人读文本 | 同上 |

这三个回答的是「模型当时是什么」，与 `expected_output` 的「人工认定该是什么」互为对照——**两者不同才是信息**。
自动采集条没有 `*_original` 两个键：它的 `cube_spec` 本身就取自同一份不可变快照，无人可改，加副本只是噪音。
`cube_original_readable` 的价值是**并排比对口径漂移**：JSON 串在 metadata 面板里挤成一行没法读，这个键把它摊成多行，与 `cube_spec_readable` 一眼可比。

### 四、其余元信息

| 键 | 含义 |
|---|---|
| `source` | `user-annotation` / `auto-collect` / **`auto-good`**（判断一个条目有没有人工金标的依据） |
| `message_id` | 该条目对应的**聊天消息 id**——撤回时的定位键（**badcase 与 goodcase 都写**）。Langfuse Dataset API 没有可读的消息标识，只有 `trace_id`，而一条 trace 可能承载同会话多条反馈。**2026-09-19 之前入集的条目没有这个键**，撤回有兜底规则（见下） |
| `trace_id` / `db_name` / `collected_at` | 溯源 |
| `reasons` / `scores`（自动采集）· `bad_type` / `rating` / `feedback_type`（人工） | 分类与筛选 |
| `note` | 用户在反馈里写的评论（自动采集条取自本地反馈快照） |

### 五、`source` 的三种取值与可信度

| 取值 | 谁写的 | 有什么 | 可信度 |
|---|---|---|---|
| `auto-collect` | 每日采集（`collect_badcase`） | `expected_output` 恒为 `None` | 负样本，**待补金标** |
| `user-annotation` | 标注员点「确认入 Good Set」/「确定入 BadCase」 | 人工认定的 SQL / 口径 | 人工背书 |
| **`auto-good`** | **用户点赞后自动入集**（`maybe_auto_good`，在反馈快照补齐那一刻判定） | 模型当时那条 SQL（外加 Cube 口径） | **无人复核**：门槛宽松（只看「点赞 + 有 SQL + 非闲聊」，**完全不看五维分**），且「用户说对」与「模型说对」是同源证据 |

`auto-good` 必须单独标出来，是因为它是三个来源里唯一**没有任何人做过判断**的：复核、批量筛选、评测集取舍时优先看这批。移除手段有两个，见下。

---

## 撤回入集（goodcase 专有）

`POST /api/feedback/annotations/{thread_id}/{message_id}/revoke-good`
→ **真删** Langfuse 里的 dataset item（`client.api.dataset_items.delete`），本地标注回到 `queued`。

不软删除的理由：数据集是评测基准，留一条错标就是毒化，没有"软删除"的空间。

**不变量：本地 `status=good` ⇔ 数据集里有这条。** 所以删不掉时（定位不了 / 调用失败）**不退本地状态**，而是回 409/502 并说明如何人工处理——退了本地，条目会回到队列被自动入集第二次，写出重复条目。

| 定位情况 | 行为 |
|---|---|
| 该 trace 下有条目带 `message_id == 本条` | 只删这些（精确） |
| 都不带 `message_id`，且该 trace 下**只有 1 条** | 删它（存量条目兜底） |
| 都不带 `message_id`，且该 trace 下有**多条** | **409 拒绝**，一条都不删（宁可让人去 UI 删，也不误删同会话别的条目） |
| Langfuse 未接入 | 200 + `warning`，只做本地回退（此时没有数据集可言） |

**自动收回**：用户**删除点赞反馈**时，只有 `auto_good=1` 的条目会被自动收回（`withdraw_auto_good`，在 `store.revoke_annotations_for_message` **之前**执行——后者会把状态改成 `rejected`，之后就读不到 `status=good` 了）。人工确认过的 Good Set 不动：人的判断依据不止那个 👍。收回同样遵守上面的不变量——删不掉就保持本地 `good`，等人工处理。

> 标记位在本地 SQLite 的 `feedback_annotation.auto_good`（0 = 人工确认或未入集，1 = 自动入集），
> 老库靠 `_migrate_schema` 的逐列增量迁移补上。

---

## 金标缺口度量

`GET /api/feedback/dataset-stats` → `{queue, badcase, goodcase, disabled?, error?}`

回答的是**「坏例集里有多少条还没补金标」**——这个欠债此前完全不可见：`GET /api/feedback/datasets` 把
`expected_output=None` 与 `{"sql":""}` 压成同一个空串，且只取第 1 页（≤100 条），>100 条的数据集会
算出错误的缺口。

| 字段 | 判据 / 来源 |
|---|---|
| `badcase` / `goodcase`.{`total`,`with_gold`,`without_gold`,`by_source`} | Langfuse Dataset **分页全量**（20 页 × 100 上限）；`with_gold` 判据 = `expected_output.sql` 非空（**权威判据**，不看 `metadata.gold_sql` 镜像） |
| `queue` | 本地 SQLite `count_annotations()`，零网络、瞬时；六个状态一律给值 |
| `disabled` / `error` | Langfuse 未接入 / 读失败——此时两个数据集回零，前端显示「—」而不是 0（免得有人对着 0 去补金标） |

Langfuse 侧结果有 **60s 进程内 TTL 缓存**（带锁；只在成功时写缓存），因为前端会反复刷新。
前端把缺口顶在标注页头部（`待判断 N · 标注中 N · BadCase 待补金标 分子/分母 · GoodCase N（自动 M）`），
并在 badcase 列表行给 `!has_gold` 的条打「缺金标」标。

---

## 三个数据集形态速查

| | `expected_output` | metadata 特有 |
|---|---|---|
| `badcase` · 人工确认 | `{sql, cube?}` | `bad_type`、`reasons: ["user_feedback=0","manual_annotation"]`、`gold_sql`、`message_id` |
| `goodcase` · 人工确认 | `{sql, cube?}` | `rating`、`feedback_type`、`good_sql`、`message_id`、`source=user-annotation` |
| `goodcase` · **自动入集** | `{sql, cube?}` | 同上 + `source=auto-good`（**无人复核**，见 §五） |
| `badcase` · 自动采集 | **`None`** | `scores`、`reasons`；口径只在 `metadata.cube_spec` |

---

## 为什么不合并（三条硬约束）

1. **自动采集条 `expected_output` 恒为 `None`**（还没有人工金标）——口径只能放 metadata；读路径的兜底链就是为它写的。
2. **存量条目改不了**：2026-09-19 前的条目只有 metadata 侧字段，改名/删除会让新旧两套并存。
3. **两者服务不同读者**：`expected_output` 走 Langfuse 一等字段（Experiment Output 列、评测器输入），metadata 是人浏览 metadata 面板的位置。

若将来要真合并，**只能新增不能删**：先让读方全部切到 `expected_output.*`，确认无外部消费者后再停写 metadata 侧副本。

---

## 相关

- 测试：`d:\tmp\test_cube_spec_dataset_fields.py`（字段级 + 功能级）、`d:\tmp\verify_goodset.py`、
  `d:\tmp\test_auto_good_commit.py`（自动入集门槛矩阵）、`d:\tmp\test_revoke_good.py`（撤回与自动收回）、
  `d:\tmp\test_dataset_stats.py`（金标缺口）、`d:\tmp\test_autogood_ui.js`（前端接线静态校验）等
- 背景：Cube 通道为什么没有 SQL 快照 → 复算物理 SQL 的来龙去脉，见 `Cube通道报告缺完整数据表与假附件链接.md` 及 memory `cube-channel-goodset-sql-snapshot`
