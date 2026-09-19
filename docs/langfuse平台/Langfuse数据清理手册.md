# Langfuse 数据清理手册（手动删除 / 上线前大扫除）

> **目的**：测试阶段会持续在 Langfuse 里产生 trace / session / dataset / 离线实验记录等数据。上线到生产前，需要按需把它们清掉，避免测试数据与生产数据混在同一个 Langfuse 项目里造成污染。
> **本文档是操作手册**：说清「在哪删、删什么、删完怎么确认」。所有操作**不可逆**，执行前先读 §0 红线。
>
> - 维护日期：2026-09-03
> - 命令基于 `langfuse-cli api`（当前 OpenAPI 快照 4.16.0；Cloud v3 将于 2026-11-16 移除）
> - 覆盖实例：weint 自托管（v4.21）、阿里云自托管（v4.16），命令相同、仅 `BASE_URL`/密钥不同；若走 Langfuse Cloud（SaaS）同样适用

---

## 0. 必读红线（先看这个）

1. **删除不可逆**：删 trace 会连带 observations / scores / 事件与媒体引用一并消失，**没有回收站**。
2. **删除是异步的**：请求返回 ≠ 已删完。Langfuse 把删除放进后台队列，由 worker 消费——
   - Cloud 官方口径：批量删除通常 **~15 分钟内完成**；
   - 自托管 v4：几秒到几分钟（量大更久），看 worker 日志。
   - 所以**删完要复查**，别一看「数据还在」就重复删。
3. **先确认打到了正确的实例/项目**：切换 `BASE_URL`/密钥后，第一件事先跑一条只读 list（§1.3），看到的是预期数据再动手。
4. **先小后大**：先挑 1 条 trace 试删并确认同步生效，再批量。
5. **删前导出留档**（§8），大扫除前务必做。
6. 本文档所有命令均**不输出任何密钥**；密钥按实例从各自的连接说明文档取。

---

## 1. 连接与目标实例

### 1.1 本仓库相关的 Langfuse 实例

| 实例 | Web / API 地址 | 版本 | 项目 | 密钥/项目信息位置 |
|---|---|---|---|---|
| **weint（当前主测试/联调）** | `http://192.168.25.64:3010` | v4.21.0（dual 写） | `default`（proj_6nMyvFmN） | [docs/weint环境/langfuse-连接信息.md](../weint环境/langfuse-连接信息.md) §1 |
| **阿里云（另一台自托管）** | `http://8.163.4.42:3001` | v4.16.0（dual 写） | `default`（proj_7qzwty81） | [docs/langfuse平台/Langfuse连接说明.md](Langfuse连接说明.md) §5 |
| **Langfuse Cloud（如使用 SaaS）** | EU `https://cloud.langfuse.com` / US `https://us.cloud.langfuse.com` | 云端最新 | 各 project 自己 | project Settings → API Keys |

> ⚠️ weint 与阿里云是**两套独立 Langfuse**，各有一对 pk/sk。别把 weint 的 key 配到阿里云 host 上，反之亦然。cloud 项目的 key 只能在 cloud 页面生成。

### 1.2 配置命令行（一次会话一次）

```bash
# ── 选一个实例填 ──────────────────────────────
export LANGFUSE_BASE_URL=http://192.168.25.64:3010   # weint 自托管
# export LANGFUSE_BASE_URL=http://8.163.4.42:3001    # 阿里云自托管
# export LANGFUSE_BASE_URL=https://cloud.langfuse.com # Cloud EU（US 用 us.cloud.langfuse.com）
export LANGFUSE_HOST="$LANGFUSE_BASE_URL"

# 从上面的“密钥位置”文档里复制 pk/sk 填入（自己粘贴到终端，不要贴到聊天里）
export LANGFUSE_PUBLIC_KEY=pk-lf-xxxxxxxx
export LANGFUSE_SECRET_KEY=sk-lf-xxxxxxxx
```

### 1.3 验证连通与鉴权（只读，安全）

```bash
# 能返回 JSON（哪怕只有 0 条）就说明 地址+key 对了
npx langfuse-cli api observations list --limit 1 --json

# 想先看请求长啥样、不真正执行：任意命令加 --curl
npx langfuse-cli api observations list --limit 1 --curl
```

---

## 2. 删除模型：删什么会连带删什么（先建立心智模型）

| 你想清的对象 | 直接删除端点？ | 实际做法 / 连带效果 |
|---|---|---|
| **Trace**（含其 observations、scores） | ✅ 单个 + 批量 | 删 trace 是一切删除的“根” |
| **Session**（一次会话） | ❌ 无 | 删该 session 下的所有 trace，会话视图即消失 |
| **v4 Experiment 页的“实验”**（本项目的离线评估 run） | ❌ 无 delete | 本项目实验条目 = trace/根观测上 `langfuse.experiment.*` 属性**派生**的 → **删承载它的 trace 即自动消失** |
| **Dataset 的 item**（badcase / goodcase 等） | ✅ `dataset-items delete <id>` | 会连带删该 item 的 run items |
| **整个 Dataset** | ❌ 公开 API 没有 | 只能逐个删 item（或重建项目） |
| **旧版 Dataset Run 记录**（Datasets→Runs） | ⚠️ 已废弃 | v4 无 run 级删除端点；删 trace 后可能残留空壳记录（见 §5.3，属已知限制） |
| **Evaluator 配置**（LLM-judge 等） | ✅ `unstable-evaluators delete <id>` | 只删配置；**已产生的评分不删** |
| **Score config** | ❌ 无 delete | 只 create/get/list/update |

**一句话**：想删得干净，99% 的场景都归结为——**先收集到一批 traceId，再批量删 trace**。

---

## 3. 清理流程总览

### 场景 A：只清“某一天 / 某次离线实验 / 某次会话”的测试数据

1. 按 §1.2 配好实例；
2. 用 §4.3 的办法收集要删的 traceId → 先看数量；
3. §4.4 批量删 trace；
4. 复查（§4.5）；
5. 如涉及 badcase/goodcase 数据集残留 → 按需 §6；
6. 如发现旧版 Dataset Run 空壳记录 → 见 §5.3（可接受则忽略）。

### 场景 B：上线前“大扫除”（保留项目、prompts、dataset 结构）

1. §8.1 导出留档（traces/scores CSV）；
2. 选定要保留的时间边界（如“保留 2026-09-01 之后”）；
3. 用 §4.3.1 时间窗收集**边界之前的全部** traceId；
4. §4.4 分批删除；
5. 复查边界前后各一个时间点（§4.5）；
6. 决定是否删 evaluator 配置（§7）与清理数据集（§6）；
7. （强烈建议）正式生产**新建独立 project + environment=production**，让测试与生产物理隔离（§8.3），而不是靠清理维持。

---

## 4. Trace / Session 清理（核心章节）

### 4.1 UI 手动删（少量/定点时最直观）

入口通用（Cloud 与自托管 v4 同一套交互，按钮文案以实际界面为准）：

- **删单条 trace**：Traces 列表 → 行内 `⋯` → Delete；或进入该 trace / session 详情 → Delete。
- **按筛选批量删**：Traces 页按时间/名称/环境筛选 → 表头勾选框「选择当前筛选的全部匹配项」→ 批量 Delete。
- 删 trace 后，从它派生的 Session 视图、打分、Experiment 页条目会一并消失。

### 4.2 CLI 删除（批量主手段）

```bash
# 删单个 trace
npx langfuse-cli api traces delete <traceId>

# 批量删（traceIds 数组必填）
npx langfuse-cli api traces delete-many \
  --body-json '{"traceIds":["traceId1","traceId2"]}'

# 大清单从文件读取（推荐，避免 shell 长度/转义问题）
# 先造 body：python 与 jq 二选一
python -c "import json;ids=[l.strip() for l in open('trace_ids.txt') if l.strip()];open('body.json','w').write(json.dumps({'traceIds':ids}))"
#  或  jq -Rs '{traceIds:(split("\n")|map(select(length>0)))}' trace_ids.txt > body.json
npx langfuse-cli api traces delete-many --body-file body.json
```

> 超大清单建议**分块**（如每 500~1000 个一批循环提交），避免单请求过大 / 触发限流。先看一眼 `wc -l trace_ids.txt`。

### 4.3 收集“要删的 traceId”清单

核心思路：**查询 root observation**（根观测的 `traceId` 就是 trace 的 id，去重后即得到 trace 清单）。

#### 4.3.1 按时间段（整窗全清 / 大扫除最常用）

```bash
npx langfuse-cli api observations list \
  --from-start-time 2026-08-01T00:00:00Z \
  --to-start-time   2026-09-01T00:00:00Z \
  --is-root-observation=true \
  --all --max-items 500000 --json > roots.json

# 提取 traceId（去重；不依赖返回包结构的写法）
jq -r '[..|objects|select(has("traceId"))|.traceId]|unique[]' roots.json > trace_ids.txt
wc -l trace_ids.txt   # 先看条数
```

> ⚠️ 该时间窗是**整个项目**范围。项目里若已混入生产数据，务必先确认边界再全删。

#### 4.3.2 按某个 session（清掉一次会话）

```bash
# 先找到要清会话的 sessionId（Traces 详情页 URL/字段里就有）
npx langfuse-cli api observations list --session-id <sessionId> \
  --is-root-observation=true --all --max-items 5000 --json \
  | jq -r '[..|objects|select(has("traceId"))|.traceId]|unique[]' > trace_ids.txt
```

#### 4.3.3 按离线实验 trace 名前缀（本项目离线评估）

离线评估每条的 trace 名形如 `exp:<臂名>:<序号>`（见 src/agent/eval/run_experiment.py）。可用名字+时间窗组合：

```bash
# ⚠️ --name 多为“等于”匹配，只能精确到单个名字，故必须叠加时间窗，或改用 4.3.4 更准
npx langfuse-cli api observations list --name "exp:ref:0" \
  --from-start-time 2026-09-01T00:00:00Z --to-start-time 2026-09-03T00:00:00Z \
  --is-root-observation=true --all --json
```

#### 4.3.4 按本地 run 文件（**最准**：一次离线实验跑 = 一份本地 JSON）

后端把每次离线实验的状态/结果落在**活动工作区**下：

```
<工作区>/eval/experiment_runs/
├── {stamp}.status.json     # 进度/状态（运行中也可读）
├── run_{stamp}.json        # 完成后的 manifest，含逐条 items[].trace_id
└── {stamp}/                # 逐条明细
```

在跑离线的后端/同工作区机器上：

```bash
# 1) 找到目标 run 的 stamp
ls <工作区>/eval/experiment_runs/ | grep status.json

# 2) 从 manifest 抽出该 run 全部 trace_id（含每个臂）
jq -r '[.items[]? | .[]? | .trace_id // empty] | unique[]' \
  <工作区>/eval/experiment_runs/run_<stamp>.json > trace_ids.txt
wc -l trace_ids.txt
```

> 删除前可顺手把该 manifest 保留副本作为留档（§8.1）。

### 4.4 提交删除

```bash
npx langfuse-cli api traces delete-many --body-file body.json   # body 由 4.2 生成
```

### 4.5 复查确认

```bash
# 等 30s~2 分钟后再查（删除异步）
npx langfuse-cli api observations list \
  --from-start-time <同起点> --to-start-time <同终点> \
  --is-root-observation=true --all --json | head   # 期望明显减少/为 0
# 或单条：get 返回 404 即已删
npx langfuse-cli api traces get <traceId>
```

> 自托管如果删完长时间不生效，看 worker（§9 B3）。

---

## 5. 离线实验 / 评估 run 的清理（“Experment”）

### 5.1 它在 Langfuse 里长什么样

一次离线 run（你从 /experiment 页或 `run_experiment.py` 提交的）在 Langfuse 同时落三处：

1. **每条 trace**（一次查询一条，trace 名 `exp:<臂>:<序号>`）；
2. **v4 Experiment 页的“实验”实体**——由 trace/根观测上的 `langfuse.experiment.*` 属性聚合派生，实体名 = `run_name`（默认形如 `<臂>:<语义库>[:<skill>]@<stamp>`，属性里 `experiment_id=exp-<run_name>`）；
3. **Datasets→Runs 的旧版 dataset run_item**（走 legacy `dataset_run_items.create`，v4 里已废弃）。

### 5.2 清理步骤

```bash
# 先看这个项目都有哪些“实验”（v4 experiments 只有 list，没有 delete）
npx langfuse-cli api experiments list --all --max-items 100 --json
npx langfuse-cli api experiments list --name <run_name> --all --json   # 精确到某个 run_name
```

**没有删除实验的端点** → 按 §4.3.4（本地 run 文件）或 §4.3.1（时间窗）收集该 run 的 traceId → §4.4 批量删 trace。删完：

- **Experiment 页条目自动消失**（它是 trace 派生的，不需要也不能单独删）；
- 该 run 上的 trace/session 级评分一并消失。

### 5.3 已知残留（可接受）

- 删 trace **不会**删除 legacy `dataset_run_items` 记录 → 若还开着旧版「Datasets → Runs」视图，可能看到指向已删 trace 的空壳 run 记录。v4 已把 run 级删除端点废弃（Cloud 2026-11-16 移除；自托管 v4 升级后即不可用），**没有公开删除手段**。影响：只是视图残留，不影响 Experiment 页与新数据；可接受则忽略，洁癖则整项目重建（§8.2）。
- 逐条删 dataset item 会连带删其 run items，但数据集里通常是“题目库”（badcase/goodcase），别为了清 run 误删题目，除非确认那是测试产生的临时数据。

---

## 6. Dataset 清理（badcase / goodcase / 评估题集）

```bash
# 列某个 dataset 的所有 item（先看、先数）
npx langfuse-cli api dataset-items list --dataset-name <dataset名> --all --max-items 1000 --json

# 删单个 item（连带其 run items，不可逆）
npx langfuse-cli api dataset-items delete <itemId>
```

UI：Datasets 页 → 打开数据集 → item 行内 `⋯` → **Delete**（删除）。另有 **Archive**（归档）：只标记“不再参与后续实验”，**保留数据**，别搞混。

- **整个 dataset 没有公开删除 API**：想整库清空只能逐个删 item，或重建项目。
- 本项目数据集：`badcase`（坏例/标注队列）、`goodcase`（点赞好例）等。清空后 `collect_badcase` / 标注页会重新积累，属正常。

---

## 7. Evaluator / 评分配置清理

### 7.1 删 Evaluator 配置（LLM-judge / SQL Semantic Equivalence 等）

```bash
# 列出项目里全部 evaluator（拿 evaluatorId）
npx langfuse-cli api unstable-evaluators list --all --json

# 删（连同它所有版本）
npx langfuse-cli api unstable-evaluators delete <evaluatorId>
```

UI：Evaluation → Evaluators 页 → 行内 `⋯` → Delete。

⚠️ 行为与约束（官方明确）：
- **删配置不删历史已产生的评分**；历史分只随 trace 删除（§4）。
- 若 evaluator 仍被 evaluation rule 引用 → 返回 `409`，先删 rule；
- `scope=managed`（Langfuse 托管）的 evaluator 删不了 → `403`。

### 7.2 Score config

`score-configs` 只支持 create/get/list/update，**无 delete**（为保证历史分数引用完整）。清理历史分只能删对应 trace。

---

## 8. 兜底：导出、项目级重置、自动过期与生产隔离

### 8.1 删前导出留档（大扫除必做）

```bash
# trace/session 主要数据导出（只读查询，安全）
npx langfuse-cli api observations list \
  --from-start-time <起点> --to-start-time <终点> \
  --all --max-items 500000 --json > backup_obs.json
npx langfuse-cli api experiments list --all --max-items 1000 --json > backup_experiments.json
# 本地 run 文件本身也是留档：保留 eval/experiment_runs/ 即可
```

### 8.2 项目级整库重置（最彻底，慎用）

- UI：Project Settings → Danger Zone → Delete project。**会连 traces/prompts/datasets/evaluators/score-configs 全删**，不可逆，删除过程异步。
- 删后重建同名 project 会得到**新的 pk/sk**；需同步后端 `.env` 的 `LANGFUSE_PUBLIC_KEY/SECRET_KEY`（含 `.env.prod`）并重启，否则埋点仍打去旧 project（key 已失效 → 全部 404/丢数据）。
- 保 prompts：删除前先在 Prompts 页把要的 prompt 导出；或删前先 `sync_prompts` 拉一份。

### 8.3 自动过期与生产隔离（比手动清理更好的长期方案）

- **Data Retention**：Project Settings 可设保留天数（≥3 天），Langfuse 每晚自动删超过窗口的 traces/observations/scores/media。Cloud 套餐自带窗口：Hobby 30 天 / Core 90 天 / Pro+ 3 年。
- **强烈建议生产另建 project**：测试与生产用不同 project + 不同 pk/sk + `environment` 标记（本项目已有 DEPLOY_ENV / environment 接线），从根上避免“污染”，而不是依赖事后清理。

---

## 9. 附录

### A. langfuse-cli 速查表

| 想做什么 | 命令 |
|---|---|
| 连通/鉴权探活 | `npx langfuse-cli api observations list --limit 1 --json` |
| 删单条 trace | `npx langfuse-cli api traces delete <traceId>` |
| 批量删 trace | `npx langfuse-cli api traces delete-many --body-file body.json`（body=`{"traceIds":[...]}`） |
| 查某窗口 root 观测 | `… observations list --from-start-time … --to-start-time … --is-root-observation=true --all --max-items 500000 --json` |
| 查某 session | `… observations list --session-id <sid> --is-root-observation=true --all --json` |
| 查“实验” | `… experiments list --all --json`（只读；无删除） |
| 列 dataset items | `… dataset-items list --dataset-name <名> --all --json` |
| 删 dataset item | `… dataset-items delete <itemId>` |
| 列/删 evaluator | `… unstable-evaluators list --all --json` / `… unstable-evaluators delete <id>` |
| 不执行、看请求 | 任意命令加 `--curl` |

### B. 常见问题

- **删完数据还在**：删除异步排队。先等 30s~几分钟复查（§4.5）；自托管长时间不生效查 worker。
  - weint 自托管 worker 日志：SSH 25.64 → `docker-compose -f /home/weint/apps/nl2sql/langfuse/docker-compose.yml logs -f langfuse-worker_1`（容器名以 `docker ps` 为准）。
  - 阿里云：SSH 8.163.4.42 → `sudo docker compose -f /opt/langfuse/docker-compose.yml logs -f langfuse-worker-1`。
- **批量删返回限流/失败**：把清单拆小（500~1000/批），错开时间重试。
- **删 trace 后 UI 还显示旧的 dataset run**：见 §5.3，属 v4 已知残留，可忽略或整项目重建。
- **Cloud 删了 15 分钟还没没**：先确认删除任务真的提交成功（返回非 4xx）；Cloud 有节流，超大清单需分块。
- **误删怎么办**：没有恢复手段——只能靠 §8.1 的导出/本地 run 文件回看，prompts 若被 project 删除一并丢失（删 project 前务必导出）。

### C. 本系统数据形态对照（快速定位）

| 你在界面看到的 | 本质 | 想删它 → 做 |
|---|---|---|
| 一条查询的完整链路（chat-turn trace） | 一条 trace（可能含多轮续跑，复用同 trace） | 删该 trace |
| 一次聊天会话（多轮多问题） | session；每问题一条 trace | 收集该 sessionId 下 trace 批量删（§4.3.2） |
| Experiment 页的某次离线评估 | trace 上的 `langfuse.experiment.*` 派生 | 删承载 trace（§5.2） |
| Datasets → Runs 旧版 run | legacy dataset_run_item | 无删除端点，可忽略（§5.3） |
| badcase / goodcase 数据集条目 | dataset item | `dataset-items delete`（§6） |
| LLM-judge / 语义等价 evaluator | evaluator 配置 | `unstable-evaluators delete`（§7） |
