# 自定义 API 接入与扩展方案

> 日期：2026-08-12
> 状态：已实施（db-config API 已按此方案合并进 langgraph API）
> 涉及项目：nl2sql 后端 (`D:\code_work_space\llm\nl2sql`)，前端不涉及
> 关联文档：[`前端配置数据库接入方案.md`](前端配置数据库接入方案.md)（首个落地实例：db-config 管理 API 迁移记录）

---

## 1. 背景与目标

历史上 db-config 管理 API 独立跑在一个 uvicorn 进程（端口 8008），前端分别直连两个端口
（chat→2026、db-config→8008）。两套服务带来：

- 启动/运维成本：多一个进程要守护、多一个端口要放通；
- 前端心智负担：两套 base URL、两套 CORS；
- 扩展成本：后期每加一个自定义 API 就得再起一个服务。

**目标**：利用 langgraph 官方 **`LANGGRAPH_HTTP.app` 自定义 app 钩子**，把自定义 API 挂进
langgraph API 同一进程/同一端口（**2026**），**不改 langgraph-api 源码**；并以
「组合根（custom_app.py）+ 每 API 一模块（暴露 `routes` 列表）」的结构落地，让**后续新增
自定义 API 只加一个文件 + 一行注册**。

## 2. 总体架构

```
浏览器 (localhost:3000)
      │  chat: /api/runs、/threads ...（原样）
      │  db-config: /api/db-configs、/api/wren-projects ...（原 8008，已合并）
      ▼
┌─────────────────────────────────────────────────────┐
│ langgraph_api.server:app  （uvicorn, 端口 2026）      │
│   langgraph 原生路由（/ok /info /threads /runs ...）  │
│   + 自定义 app 路由（来自 LANGGRAPH_HTTP.app 钩子）     │
│      └─ src/api/custom_app.py:app （组合根）          │
│            ├─ *api.db_config.routes  （7 条）         │
│            └─ *api.<future>.routes   （新增一行）      │
└─────────────────────────────────────────────────────┘
```

- **合并机制**：`.env` 里 `LANGGRAPH_HTTP={"app": "src/api/custom_app.py:app"}` →
  `HttpConfig.app` 指向用户 Starlette 实例；`langgraph_api/server.py` 把该 app 的路由 +
  langgraph 原生路由合并进同一个 app（同时合并 lifespan 与 exception handlers）。
- **两种启动方式都生效**：`python start_server.py`（`load_dotenv(.env)`）与
  `langgraph dev --port 2026`（读 langgraph.json 的 `"env": ".env"`）都会加载 `.env`。
- **必须是 Starlette**：langgraph 运行时仅依赖 Starlette 1.3.1，**FastAPI 未安装**，
  自定义 app 一律用 Starlette，零新增依赖。

## 3. 核心约束（务必遵守）

| 约束 | 原因 |
|------|------|
| **禁全局 JSON 中间件**（BaseHTTPMiddleware 包全 app） | 会吞掉整个 app 的 JSON body，并干扰 langgraph SSE 流式路由。正确做法是 handler 内 `await request.json()`（用 `api/_common.parse_body`） |
| **自定义 app 只挂路由，不挂中间件** | 中间件会影响同 app 里的 langgraph 原生路由 |
| **用 Starlette 而非 FastAPI** | 运行时无 FastAPI；`FastAPI()` 实例也可合并，但引入新依赖且与官方合并路径不一致 |
| **路由路径避免冲突** | Starlette 按注册顺序匹配，先注册的先命中；新 API 路径不要与现有 `/api/db-configs*`、`/healthz` 等重叠 |
| **路径锚点重算** | `Path(__file__).resolve().parents[N]` 随文件位置变化，迁移/新建模块时要重算（如 db_config 迁移时 parents[3]→parents[2]） |
| **改代码后必须重启 2026** | 旧进程不会热加载；`/api/wren-projects` 404 即说明进程还跑着未挂自定义 app 的旧代码 |

## 4. 现有基础设施（已就位，直接用）

### 4.1 共享工具 `src/api/_common.py`

```python
from starlette.requests import Request
from starlette.responses import JSONResponse

async def parse_body(request: Request) -> dict:
    """安全解析 JSON body：空 body / 非法 JSON / 非 JSON 内容 → {}。"""
    try:
        return await request.json() or {}
    except Exception:
        return {}

def json_response(data: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status)
```

### 4.2 组合根 `src/api/custom_app.py`（注册表）

```python
import os, sys
# 防御性插 src 进 sys.path：start_server.py 已插；此处兜底 langgraph dev 等未插的启动方式
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from starlette.applications import Starlette
from starlette.routing import BaseRoute
import api.db_config

ROUTES: list[BaseRoute] = [
    *api.db_config.routes,
    # 后期新增：import api.<name> + 展开 *api.<name>.routes
]

app = Starlette(routes=ROUTES)   # 唯一被 LANGGRAPH_HTTP 加载的对象
```

### 4.3 `.env`

```
LANGGRAPH_HTTP={"app": "src/api/custom_app.py:app"}
```

## 5. 新增一个自定义 API（步骤）

以下为「hello world」级最小示例，按此模板套即可。

**步骤 1：在 `src/api/` 新建模块 `src/api/ping.py`**

```python
"""示例 API：每模块暴露 routes 列表，custom_app.py 聚合。"""
from __future__ import annotations

from starlette.requests import Request
from starlette.routing import BaseRoute, Route

from api._common import json_response, parse_body


async def ping(request: Request) -> ...:
    body = await parse_body(request)          # 需要读 body 时用 parse_body（禁全局中间件）
    return json_response({"pong": True, "echo": body.get("echo")})


routes: list[BaseRoute] = [
    Route("/api/ping", ping, methods=["GET", "POST"]),
]
```

**步骤 2：在 `custom_app.py` 注册一行**

```python
import api.db_config
import api.ping                       # ← 新增：import

ROUTES: list[BaseRoute] = [
    *api.db_config.routes,
    *api.ping.routes,                 # ← 新增：展开
]
```

**步骤 3：重启 2026 生效**

```bash
# 1) 找到并 kill 旧进程（监听 2026 的 PID）
netstat -ano | grep ":2026" | grep LISTEN
# 2) 从仓库根启动
python start_server.py
```

**步骤 4：验证**

```bash
curl -s http://localhost:2026/api/ping                     # → {"pong":true,"echo":null}
curl -s -X POST -H "Content-Type: application/json" \
  -d '{"echo":"hi"}' http://localhost:2026/api/ping         # → {"pong":true,"echo":"hi"}
# CORS 预检（前端 3000 跨域）
curl -s -i -X OPTIONS http://localhost:2026/api/ping \
  -H "Origin: http://localhost:3000" -H "Access-Control-Request-Method: POST" \
  | grep -i access-control-allow-origin                     # → http://localhost:3000
```

> 前端侧无需代理：直接 `fetch(\`${config.deploymentUrl}/api/ping\`)`，CORS 与聊天共用一份配置。

## 6. 参考实现：`src/api/db_config.py`

首个落地实例，作为「带逻辑/带依赖」的完整参考：

- 7 条路由：`/api/db-configs`（GET 列表/POST 新增）、`/api/db-configs/{name}`（GET/DELETE）、
  `/api/db-configs/{name}/test`（POST）、`/api/wren-projects`（GET）、`/healthz`（GET）；
- handler 内部 `await parse_body(request)` 读 JSON body；
- 列表/单条响应做密码脱敏 + `semantic` 标记（`_masked_with_semantic`）；
- `upsert_config` / `delete_config` 后调 `get_detector().invalidate()`，保证语义标记即时更新；
- 模块尾部导出 `routes: list[BaseRoute]`。

## 7. 验证方法与回归清单

改动后按需执行（对应 `src/api/db_config.py` 的既有验证路径）：

1. **双路由共存**：`/api/<自定义>` 200 且 `/ok`、`/info`、`/threads`（langgraph 原生）不受影响；
2. **SSE 流式回归**：发一条聊天消息，确认 `messages/partial` 增量事件正常流动——
   证明无全局中间件干扰；
3. **CORS**：`OPTIONS` 预检 + 带 `Origin` 的真实请求都应返回 `access-control-allow-origin`；
4. **state 更新**：若新 API 改配置类数据，验证下游读取（如 semantic 标记）免重启即反映。

## 8. 回滚

- 新增 API 回滚：删 `custom_app.py` 里的注册行 + 删模块文件，重启 2026；
- 整体回滚到独立服务模式：删 `.env` 的 `LANGGRAPH_HTTP` + 前端 `apiBase()` 还原为 8008 →
  8008 shim（`src/mcp_server/db_mcp_server/db_config_api.py`，从 `src/` 目录启动）照常独立运行。

## 9. 相关文件索引

| 层 | 文件 |
|----|------|
| 钩子配置 | `.env`（`LANGGRAPH_HTTP`） |
| 组合根（注册表） | `src/api/custom_app.py` |
| 共享工具 | `src/api/_common.py`（`parse_body` / `json_response`） |
| 参考实现 | `src/api/db_config.py`（db-config 管理 API） |
| 8008 过渡 shim | `src/mcp_server/db_mcp_server/db_config_api.py`（回滚/过渡期用） |
| 合并机制（langgraph 官方） | `.venv/Lib/site-packages/langgraph_api/config/schemas.py`（`HttpConfig.app`）、`langgraph_api/server.py`（路由合并） |
| 迁移记录 | [前端配置数据库接入方案.md](前端配置数据库接入方案.md) §13 |

---

## 附：合并机制来源（langgraph 官方，已探明）

- `LANGGRAPH_HTTP`（JSON env）→ `HttpConfig.app` 指向用户 Starlette/FastAPI 实例；
- `server.py` 把该 app 的路由 + langgraph 自己的路由合并进同一 app，并合并 lifespan 与 exception handlers；
- 因此自定义 app **不要**重复实现 lifespan 里的业务（如 db-config 无自建连接池，直接复用即可）。
