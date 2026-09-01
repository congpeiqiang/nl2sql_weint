# Chinook_Aliyun artists 艺术家数量统计报告

> 生成时间：2026-08-21
> 查询通道：直连通道 dbmcp_run_sql（本库未在语义层建模）

## 1. 查询状态：❌ 失败（基础设施故障）

本次实时查询**未能执行成功**，原因如下：

- 数据库 `chinook_aliyun` 未在已配置库列表中（可用库：`aix_report`、`Chinook_AutoIncrement`、`imdb`）。
- 对最接近的 Chinook 库 `Chinook_AutoIncrement` 执行 `SELECT COUNT(*) FROM artists;` 时报错：
  `(2003, "Can't connect to MySQL server on 'mysql-master' ([Errno 11001] getaddrinfo failed)")`
- 全部 3 个已配置库（aix_report / Chinook_AutoIncrement / imdb）均指向同一 MySQL 主机 `mysql-master`，DNS 解析全部失败，属**持久性基础设施故障**（已重试 3 次确认）。

## 2. 历史参考值（非本次实时查询结果）

⚠️ 以下数值来自历史报告存档，**不代表本次实时查询结果**，仅供参考：

| 项目 | 内容 |
|------|------|
| 数据库 | Chinook_Aliyun（PostgreSQL，直连通道） |
| 实际表名 | `artist`（注意：不是 `artists`，字段 `artistid` 主键、`name`） |
| 历史记录艺术家总数 | **275 位** |
| 历史 SQL | `SELECT COUNT(*) AS artist_count FROM artist;` |

## 3. 结论

- **本次实时统计无法完成**：数据库基础设施当前不可用，未获得实时数据。
- 历史存档显示该库 `artist` 表共 **275 位艺术家**（2026-08-17 记录），待基础设施恢复后应重新执行 `SELECT COUNT(*) FROM artist;` 确认。
