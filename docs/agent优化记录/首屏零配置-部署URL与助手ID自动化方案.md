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
