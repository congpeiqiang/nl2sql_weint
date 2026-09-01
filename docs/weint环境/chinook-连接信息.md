# Chinook 数据库连接信息（weint 环境）

> 服务器：**192.168.25.64**（Ubuntu 24.04，内网）
> 安装时间：2026-08-27 | 支持远程连接 | PostgreSQL 14（docker.m.daocloud.io/postgres:14）
> 数据来源：`D:\code_work_space\llm\nl2sql\docs\chinook数据库-aliyun\pg`（PG 转换版 SQL）

## 连接参数

| 项目 | 值 |
|------|-----|
| 容器 | `chinook-postgres`（独立实例，全新卷 `chinook_postgres_data`） |
| 数据库 / 用户 | `chinook` / `chinook` |
| 密码 | `chinook123` |
| 监听地址 | `0.0.0.0:5434`（**支持远程连接**，LAN 可达） |
| 连接串 | `postgresql://chinook:chinook123@192.168.25.64:5434/chinook` |
| JDBC | `jdbc:postgresql://192.168.25.64:5434/chinook?user=chinook&password=chinook123` |
| 容器内连接串 | `postgresql://chinook:chinook123@chinook-postgres:5432/chinook` |

## 远程连接方式

```bash
# 任意同网段机器（或经路由可达的机器）
psql -h 192.168.25.64 -p 5434 -U chinook -d chinook
# 密码：chinook123

# 已实测：192.168.25.64:5434 TCP 可达 + scram 密码认证通过
```

> 跨网段/公网访问需在防火墙/安全组放行 TCP 5434；本地调试仍可用 SSH 隧道：`ssh -L 5434:127.0.0.1:5434 weint@192.168.25.64`

## 数据规模（与标准 Chinook 1.4.5 一致）

| 表 | 行数 | 表 | 行数 |
|----|------|----|------|
| Artist | 275 | Invoice | 412 |
| Album | 347 | InvoiceLine | 2240 |
| Track | 3503 | Playlist | 18 |
| Genre | 25 | PlaylistTrack | 8715 |
| MediaType | 5 | Customer | 59 |
| Employee | 8 | | |

11 张表、11 个外键、11 个索引；UTF-8 特殊字符正常（Motörhead、Holý 等）。

## 常用命令

```bash
# 服务器本机
docker exec -it chinook-postgres psql -U chinook -d chinook
# 远程
psql "postgresql://chinook:chinook123@192.168.25.64:5434/chinook"

# 日志 / 状态
docker logs chinook-postgres
docker ps --filter name=chinook-postgres

# SQL 文件位置（服务器）
/home/weint/apps/nl2sql/chinook/   (chinook_schema.sql / chinook_data.sql / verify_chinook.sql)
```

## 备注

- 独立专用实例，与 langfuse 的 PostgreSQL（5433，仍仅本机）及服务器既有 postgres18（5432）/nocobase 等完全隔离
- 容器 `restart=unless-stopped`，数据持久化在 volume `chinook_postgres_data`（重建容器不丢数据）
- 认证方式 scram-sha-256，密码以哈希存储，远程连接安全
