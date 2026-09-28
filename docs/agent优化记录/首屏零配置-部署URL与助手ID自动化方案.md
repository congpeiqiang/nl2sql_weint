# 首屏零配置：登录后直接进聊天页（部署 URL / 助手 ID 自动带上）

> 2026-09-28。**两个交付面**：后端（`src/api/deployment_info.py`，走整包 src 发版）
> + 前端（`harness-deep-agents-ui` 三个文件，走 `release-frontend.ps1` 的 `yarn build`）。
> 前端**不随后端发版**，两边的发版动作要分别做。

## 1. 现象与根因

新账号（或任何新浏览器）首次打开前端，必须先手填「部署 URL」和「助手 ID」才能进聊天页。

根因是**两条**，缺一条都不会有这个问题：

1. **顺序错了**：`src/app/page.tsx` 的 `if (!config)` 分支在 `AuthGuard` **之外**
   ⇒ 未登录就先问配置。用户此刻既没有 cookie，也拿不到任何服务端默认值。
2. **问了两个用户给不出来的值**：
   - *部署 URL*：前端留空即跟随当前访问地址（`src/lib/deploymentUrl.ts` 是唯一解析点，
     2026-09-25 已实现）。**手填反而容易踩坑** —— 填成 `:2026` 那个只绑回环的端口，
     症状就是历史上那两起「模型都没了 + Failed to fetch」（见
     `frontend-deployment-url-must-be-nginx-entry` 那条记录）。
   - *助手 ID*：它其实是本部署的**图名**，写在仓库根 `langgraph.json` 的 `graphs` 里
     （`chat_agent` 主入口 / `nl2sql_agent` 子图），与用户无关。

## 2. 方案

**把配置门挪到登录之后，默认值由服务端给，探测失败才回落手动弹窗。**

```
无配置 → AuthGuard（未登录 → /login；已登录 → 继续）
        → ConfigBootstrap 探测 GET /api/deployment-info
             ├─ 成功 → 落 localStorage {部署URL: "", 助手ID: <graph id>} → 直接进聊天页（零交互）
             └─ 失败 → 原来的「欢迎 + 配置弹窗」（行为与改动前一致，不把人挡在门外）
```

### 2.1 后端（新增，1 个端点）

| 文件 | 内容 |
|---|---|
| `src/api/deployment_info.py` | **新建**：`GET /api/deployment-info`，需登录，返回 `{ok, assistant_id, graph_ids, source}` |
| `src/api/custom_app.py` | import 一行 + `ROUTES` 展开一行 |

- `assistant_id` 从 `langgraph.json` 读（`parents[2]` = 仓库根 = 容器里的 `/app`，
  与本地同构）；主入口优先，**主入口被改名时取第一个图名**（自愈，不用改前端）；
  文件缺失/坏 JSON/graphs 段异常 → 回落常量 `chat_agent` 且 `source="fallback"`。
- **刻意不进 `auth_middleware` 白名单**：登录前没有任何理由暴露图名。
  这也正是前端必须把探测放在 `AuthGuard` 之内的原因。
- `assistant_id` 的语义是 **graph id，不必是 assistant 的 UUID** ——
  前端 `fetchAssistant` 对非 UUID 走 `assistants.search({graphId})`（`src/app/page.tsx`）。

### 2.2 前端（3 个文件）

| 文件 | 改动 |
|---|---|
| `src/lib/deploymentInfo.ts` | **新建**：`fetchDeploymentInfo()` 探针；任何失败返回 `null`，绝不抛 |
| `src/app/components/ConfigBootstrap.tsx` | **新建**：探测 → `onReady`（自动配置）或回落到原欢迎页 + `ConfigDialog` |
| `src/app/page.tsx` | 挂载 effect 不再直接弹窗；`if (!config)` 分支改为 `<AuthGuard><ConfigBootstrap/></AuthGuard>`；移除已无用的 `ConfigDialog` import |

- 部署 URL 写**空串**（不是 `window.location.origin`）：写死 origin 会把配置钉死在当前入口上，
  换域名/端口/https 就要用户自己改；空串语义才是「跟随当前访问地址」。
- 探测走**相对路径** `/api/deployment-info`（同源，nginx 已把 `/api/` 反代到后端）
  —— 此刻还没有配置，而这条请求本就该打到「发下这个页面的那个入口」。

## 3. 边界与降级

| 情形 | 行为 |
|---|---|
| 后端还是老版本（无此端点） | 探测 404 → `null` → 手动弹窗（**与改动前完全一致**） |
| 未登录 / cookie 过期 | `AuthGuard` 先送 `/login`；探测只在已登录后发生 |
| 后端不可达 | 探测异常 → `null` → 手动弹窗 |
| 用户已手动改过配置 | 根本不进这条路径（`getConfig()` 非空） |
| `langgraph.json` 读不到 | 端点回落 `chat_agent` + `source=fallback`，前端照常自动配置 |

## 4. 不改什么

- **不动 `ConfigDialog` / `SettingsDialog` 的字段与校验**：部署 URL 依旧可留空，
  助手 ID 依旧必填（现在有值了）。手动改配置的能力完全保留。
- 不改 `docker/nginx.conf`（`location /api/` 前缀已覆盖新路径；且 nginx.conf 不在发版 tar 里，
  能不动就不动）。
- 不引入构建期常量（`NEXT_PUBLIC_*`）：默认值只住服务端一处，换图/改名不用重发前端。

## 5. 验收

`scripts/verify_deployment_info.py`（**24/24 通过**）：

- ① 图名来源：真实文件读得到且含两个图名、兜底常量与真实图名一致；坏 JSON / 空 graphs /
  缺段 / graphs 非 dict / 文件不存在 **一律 `[]` 且不抛**（负对照）。
- ② 端点行为：未登录 **401**（负对照）；已登录 200 且三字段正确；**主入口改名 → 取第一个图名**；
  读不到 → 回落常量 + `source=fallback`。
- ③ **不在 auth 白名单**（被谁顺手加成白名单 = 图名对未登录访客公开）。
- ④ 组合根**真的注册了**（漏注册只会静默走兜底弹窗，没有任何报错 —— 最容易漏的错）。

前端：`npx tsc --noEmit` 无新增错误（`page.tsx` 那两条 `TS2578` 是 DLP 标记导致的既有错误，
改动前就在）、`yarn build` **exit 0**。

**生产 E2E（需发版后做）**：新开一个无痕窗口 → 打开 `http://192.168.25.64:8080`
→ 应直接落在 `/login`（不再是配置弹窗）→ 登录 → **直接进聊天页**，问一句话能正常出结果；
`curl -b <cookie> http://192.168.25.64:8080/api/deployment-info` 返回 `chat_agent`。

---

## 6. 后续：清浏览器缓存 ⇒ 「尚未配置模型 + Failed to fetch」（2026-09-28 生产复现，已修，待 rebuild 发版）

**现象**：发版后用户「清了下浏览器数据缓存」，界面同时报两件事 —— ①「尚未配置模型，无法发送消息
（模型配置按账号独立，不与其他账号共用）」；②侧栏「加载对话列表失败 / **Failed to fetch**」。
页面本身能开、能登录、能进聊天页。

**根因**：`deep-agent-config` 存在 **localStorage（per 浏览器）**，清缓存 ⇒ 其中的 `deploymentUrl` 归空。
而 §2 那个「唯一解析点」`resolveDeploymentUrl` 当年**只改了 4 个调用点**，全仓另有 **15 处**：

```
modelConfigs / dbConfig / cancelTask / threadRunStatus / semanticApi / feedback /
feedbackLoop / evalFlags / experiment / sqlApproval / threadFork / threadMeta /
threadSearch / workspace / workspaceFiles
```

它们各自写着 `cfg?.deploymentUrl || "http://localhost:2026"`，**从来没走过解析函数**；
`app/hooks/useThreads.ts` 更隐蔽：它把 `config.deploymentUrl`（空串）直接塞给 langgraph-sdk 的
`apiUrl`，SDK 内部默认值是 `http://localhost:8123`。⇒ 这 16 处集体指向**用户自己那台机器**
⇒ 跨源被拒 ⇒ 与「填 `:2026`」事故同一症状（`Failed to fetch` 是浏览器对跨源/连接失败的
`TypeError` 文案）。注意 `ChatInterface` 里模型列表的 `catch { setModelConfigured(false) }`
把「**读不到**」当成「**没配**」⇒ 那句「尚未配置模型」是**假警报**，与后端账号模型隔离无关。

**判据（nginx access log 签名，本次实测，可直接复用）**：清缓存后该浏览器**只**发出同源相对请求
—— `/api/auth/me`、`/assistants/search`（`ClientProvider` 走了解析）、`/api/deployment-info`，全 200；
而 **`/threads/search`、`/api/model-configs`、`/api/threads/*/run-status` 一条都没有**
⇒ 请求根本没到 nginx（不是后端挂、不是模型没了、不是权限）。
反证：清缓存**之前**同一 IP 这些请求都是 200。
同时 `docker exec` 核过：各账号 `users/*/model_config.json` 都有 6~7 个带 key 的 provider，
`model_config_store.py` / `model_required.py` 的 md5 与本地一致 ⇒ 后端一切正常。

**修法（一处改，16 处全修）**：把强制点从「靠自觉调用」挪到**数据出口** —— `src/lib/config.ts`：
- `getConfig()`：返回前 `deploymentUrl: resolveDeploymentUrl(parsed.deploymentUrl)`（空串 → `window.location.origin`）；
- `saveConfig()`：做**逆运算**，值恰好等于当前 origin 时按**空串**存 —— 否则
  `saveConfig({ ...getConfig(), ... })` 这类"读出来改一格再存回"的写法会把解析出的绝对地址
  固化进 localStorage，换个 host/端口访问又失效（旧的 `:2026` 事故正是这样留下的）。

⇒ 那些 `|| "http://localhost:2026"` 从此是**死分支**；将来新写的 API 客户端也自动正确。
**为什么不在那 15 个调用点逐个改**：它们 + `useThreads.ts` 在盘上是 DLP **密文**，Edit 匹配不到
原文（`old_string not found`），只能整文件 Write 重打 ⇒ 出错风险高、且要动 800+ 行。

**立即绕过（无需发版）**：右上角「设置 → 部署 URL」填 nginx 入口 `http://192.168.25.64:8080`
→ 保存 → 刷新，两个症状同时消失。（**09-28 起「留空」也已安全**，见上。）

**验证**：`npx tsc --noEmit` 无新增错误（37 条全是既有的 `TS2578`/`TS7006`，无一涉及
`config.ts`/`deploymentUrl.ts`）· `yarn build` **exit 0**（`Done in 41.22s`）· `git diff` 只含
`import` + `getConfig` + `saveConfig` 三处改动。
**待做**：前端 `release-frontend.ps1` 单独 rebuild 发版（不随后端整包）。
