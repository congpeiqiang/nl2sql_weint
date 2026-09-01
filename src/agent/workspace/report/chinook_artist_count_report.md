# Chinook 数据库 Artist 表统计报告

## 基本信息

| 项目 | 内容 |
| --- | --- |
| 数据库 | chinook_aliyun（PostgreSQL） |
| 数据表 | artist |
| 统计维度 | 艺术家总数 |
| 查询时间 | 当前会话 |

## 统计结果

| 指标 | 数值 |
| --- | --- |
| 艺术家总数 | **275** |

## 查询说明

统计 `artist` 表记录总数，使用以下 SQL：

```sql
SELECT COUNT(*) AS artist_count FROM artist;
```

执行结果：共返回 **1** 行，`artist_count = 275`。

## 结论

`chinook_aliyun` 数据库的 `artist` 表中共有 **275 位艺术家**。
