# Chinook 数据库 NL2SQL 验证问题列表

> 数据库：Chinook (MySQL/AutoIncrement)，11 张表，3503 首曲目，275 位艺术家，59 位客户
> 用途：测试 NL2SQL 流水线从简单到复杂的 SQL 生成能力
> 难度递进：L1 单表查询 → L2 条件过滤 → L3 多表 JOIN → L4 聚合分组 → L5 子查询 → L6 复杂综合

---

## L1 — 单表简单查询（问题 1-5）

### 问题 1
**自然语言：** 查询所有艺术家的姓名。

**预期涉及：** `Artist` 表，`Name` 列
**预期 SQL 模式：** `SELECT Name FROM Artist`
**难度：** L1

---

### 问题 2
**自然语言：** 列出所有客户的姓氏和电子邮件地址。

**预期涉及：** `Customer` 表，`LastName`、`Email` 列
**预期 SQL 模式：** `SELECT LastName, Email FROM Customer`
**难度：** L1

---

### 问题 3
**自然语言：** 显示所有员工的工号、名字、姓氏和职位。

**预期涉及：** `Employee` 表，`EmployeeId`、`FirstName`、`LastName`、`Title` 列
**预期 SQL 模式：** `SELECT EmployeeId, FirstName, LastName, Title FROM Employee`
**难度：** L1

---

### 问题 4
**自然语言：** 查询一共有多少张专辑。

**预期涉及：** `Album` 表，`COUNT`
**预期 SQL 模式：** `SELECT COUNT(*) FROM Album`
**难度：** L1

---

### 问题 5
**自然语言：** 列出所有音乐流派的名称，按字母顺序排列。

**预期涉及：** `Genre` 表，`Name` 列，`ORDER BY`
**预期 SQL 模式：** `SELECT Name FROM Genre ORDER BY Name`
**难度：** L1

---

## L2 — 条件过滤查询（问题 6-10）

### 问题 6
**自然语言：** 查询来自巴西（Brazil）的所有客户姓名和城市。

**预期涉及：** `Customer` 表，`FirstName`、`LastName`、`City`、`Country` 列，`WHERE`
**预期 SQL 模式：** `SELECT FirstName, LastName, City FROM Customer WHERE Country = 'Brazil'`
**难度：** L2

---

### 问题 7
**自然语言：** 查询单价为 1.99 的所有曲目名称和对应的专辑 ID。

**预期涉及：** `Track` 表，`Name`、`AlbumId`、`UnitPrice` 列，`WHERE`
**预期 SQL 模式：** `SELECT Name, AlbumId FROM Track WHERE UnitPrice = 1.99`
**难度：** L2

---

### 问题 8
**自然语言：** 查找 2025 年之后生成的发票 ID、客户 ID 和发票日期。

**预期涉及：** `Invoice` 表，`InvoiceId`、`CustomerId`、`InvoiceDate` 列，`WHERE`
**预期 SQL 模式：** `SELECT InvoiceId, CustomerId, InvoiceDate FROM Invoice WHERE InvoiceDate > '2025-01-01'`
**难度：** L2

---

### 问题 9
**自然语言：** 查询时长超过 5 分钟（300000 毫秒）的曲目名称和时长，按时长从长到短排列。

**预期涉及：** `Track` 表，`Name`、`Milliseconds` 列，`WHERE`，`ORDER BY DESC`
**预期 SQL 模式：** `SELECT Name, Milliseconds FROM Track WHERE Milliseconds > 300000 ORDER BY Milliseconds DESC`
**难度：** L2

---

### 问题 10
**自然语言：** 查询姓氏以 'M' 开头且所在国家为 'USA' 的客户全名和公司名称。

**预期涉及：** `Customer` 表，`FirstName`、`LastName`、`Company`、`Country` 列，`LIKE`，`AND`
**预期 SQL 模式：** `SELECT FirstName, LastName, Company FROM Customer WHERE LastName LIKE 'M%' AND Country = 'USA'`
**难度：** L2

---

## L3 — 多表 JOIN 查询（问题 11-15）

### 问题 11
**自然语言：** 列出所有专辑的标题及其对应艺术家的姓名。

**预期涉及：** `Album`、`Artist` 表，`JOIN`，`Album.ArtistId = Artist.ArtistId`
**预期 SQL 模式：** `SELECT Album.Title, Artist.Name FROM Album JOIN Artist ON Album.ArtistId = Artist.ArtistId`
**难度：** L3

---

### 问题 12
**自然语言：** 查询每首曲目的名称及其所属专辑的标题和音乐流派名称。

**预期涉及：** `Track`、`Album`、`Genre` 表，多表 `JOIN`
**预期 SQL 模式：** `SELECT Track.Name, Album.Title, Genre.Name FROM Track JOIN Album ON Track.AlbumId = Album.AlbumId JOIN Genre ON Track.GenreId = Genre.GenreId`
**难度：** L3

---

### 问题 13
**自然语言：** 查询每位客户的全名及其对应的支持代表（SupportRep）的姓名。

**预期涉及：** `Customer`、`Employee` 表，`JOIN`，`Customer.SupportRepId = Employee.EmployeeId`
**预期 SQL 模式：** `SELECT Customer.FirstName, Customer.LastName, Employee.FirstName, Employee.LastName FROM Customer JOIN Employee ON Customer.SupportRepId = Employee.EmployeeId`
**难度：** L3

---

### 问题 14
**自然语言：** 查找每个播放列表的名称及其包含的曲目数量。

**预期涉及：** `Playlist`、`PlaylistTrack` 表，`JOIN`，`GROUP BY`，`COUNT`
**预期 SQL 模式：** `SELECT Playlist.Name, COUNT(PlaylistTrack.TrackId) AS track_count FROM Playlist JOIN PlaylistTrack ON Playlist.PlaylistId = PlaylistTrack.PlaylistId GROUP BY Playlist.PlaylistId, Playlist.Name`
**难度：** L3

---

### 问题 15
**自然语言：** 查询每张发票的 ID、发票日期、客户全名和发票总金额。

**预期涉及：** `Invoice`、`Customer` 表，`JOIN`
**预期 SQL 模式：** `SELECT Invoice.InvoiceId, Invoice.InvoiceDate, Customer.FirstName, Customer.LastName, Invoice.Total FROM Invoice JOIN Customer ON Invoice.CustomerId = Customer.CustomerId`
**难度：** L3

---

## L4 — 聚合与分组查询（问题 16-20）

### 问题 16
**自然语言：** 统计每个国家的客户数量，按客户数量从高到低排列。

**预期涉及：** `Customer` 表，`GROUP BY`，`COUNT`，`ORDER BY DESC`
**预期 SQL 模式：** `SELECT Country, COUNT(*) AS customer_count FROM Customer GROUP BY Country ORDER BY customer_count DESC`
**难度：** L4

---

### 问题 17
**自然语言：** 计算每个音乐流派的曲目数量和平均单价。

**预期涉及：** `Track`、`Genre` 表，`JOIN`，`GROUP BY`，`COUNT`，`AVG`
**预期 SQL 模式：** `SELECT Genre.Name, COUNT(*) AS track_count, AVG(Track.UnitPrice) AS avg_price FROM Track JOIN Genre ON Track.GenreId = Genre.GenreId GROUP BY Genre.GenreId, Genre.Name`
**难度：** L4

---

### 问题 18
**自然语言：** 查找每位销售支持代表（Title 包含 'Sales Support Agent'）所服务的客户数量。

**预期涉及：** `Employee`、`Customer` 表，`JOIN`，`WHERE`，`GROUP BY`，`COUNT`
**预期 SQL 模式：** `SELECT Employee.FirstName, Employee.LastName, COUNT(Customer.CustomerId) AS customer_count FROM Employee JOIN Customer ON Employee.EmployeeId = Customer.SupportRepId WHERE Employee.Title LIKE '%Sales Support Agent%' GROUP BY Employee.EmployeeId, Employee.FirstName, Employee.LastName`
**难度：** L4

---

### 问题 19
**自然语言：** 找出消费总额最高的前 5 位客户的全名和消费总额。

**预期涉及：** `Customer`、`Invoice` 表，`JOIN`，`GROUP BY`，`SUM`，`ORDER BY DESC`，`LIMIT`
**预期 SQL 模式：** `SELECT Customer.FirstName, Customer.LastName, SUM(Invoice.Total) AS total_spent FROM Customer JOIN Invoice ON Customer.CustomerId = Invoice.CustomerId GROUP BY Customer.CustomerId, Customer.FirstName, Customer.LastName ORDER BY total_spent DESC LIMIT 5`
**难度：** L4

---

### 问题 20
**自然语言：** 查询 2024 年每个月的发票总金额，按月份排序。

**预期涉及：** `Invoice` 表，`YEAR`/`MONTH` 函数，`GROUP BY`，`SUM`，`ORDER BY`
**预期 SQL 模式：** `SELECT MONTH(InvoiceDate) AS month, SUM(Total) AS monthly_total FROM Invoice WHERE YEAR(InvoiceDate) = 2024 GROUP BY MONTH(InvoiceDate) ORDER BY month`
**难度：** L4

---

## L5 — 子查询（问题 21-25）

### 问题 21
**自然语言：** 查询消费总额超过所有客户平均消费总额的客户姓名和消费总额。

**预期涉及：** `Customer`、`Invoice` 表，`JOIN`，`GROUP BY`，`HAVING`，子查询
**预期 SQL 模式：** `SELECT Customer.FirstName, Customer.LastName, SUM(Invoice.Total) AS total_spent FROM Customer JOIN Invoice ON Customer.CustomerId = Invoice.CustomerId GROUP BY Customer.CustomerId, Customer.FirstName, Customer.LastName HAVING SUM(Invoice.Total) > (SELECT AVG(total) FROM (SELECT SUM(Total) AS total FROM Invoice GROUP BY CustomerId) AS t)`
**难度：** L5

---

### 问题 22
**自然语言：** 查询没有产生过任何发票的客户姓名和电子邮件。

**预期涉及：** `Customer`、`Invoice` 表，`NOT IN` 子查询
**预期 SQL 模式：** `SELECT FirstName, LastName, Email FROM Customer WHERE CustomerId NOT IN (SELECT DISTINCT CustomerId FROM Invoice)`
**难度：** L5

---

### 问题 23
**自然语言：** 查找包含曲目数量超过所有播放列表平均曲目数量的播放列表名称和曲目数。

**预期涉及：** `Playlist`、`PlaylistTrack` 表，`JOIN`，`GROUP BY`，`HAVING`，子查询
**预期 SQL 模式：** `SELECT Playlist.Name, COUNT(PlaylistTrack.TrackId) AS track_count FROM Playlist JOIN PlaylistTrack ON Playlist.PlaylistId = PlaylistTrack.PlaylistId GROUP BY Playlist.PlaylistId, Playlist.Name HAVING COUNT(PlaylistTrack.TrackId) > (SELECT AVG(cnt) FROM (SELECT COUNT(TrackId) AS cnt FROM PlaylistTrack GROUP BY PlaylistId) AS t)`
**难度：** L5

---

### 问题 24
**自然语言：** 查询至少购买过一次 'Rock' 流派曲目的所有客户全名（去重）。

**预期涉及：** `Customer`、`Invoice`、`InvoiceLine`、`Track`、`Genre` 表，多表 `JOIN`，`DISTINCT`
**预期 SQL 模式：** `SELECT DISTINCT Customer.FirstName, Customer.LastName FROM Customer JOIN Invoice ON Customer.CustomerId = Invoice.CustomerId JOIN InvoiceLine ON Invoice.InvoiceId = InvoiceLine.InvoiceId JOIN Track ON InvoiceLine.TrackId = Track.TrackId JOIN Genre ON Track.GenreId = Genre.GenreId WHERE Genre.Name = 'Rock'`
**难度：** L5

---

### 问题 25
**自然语言：** 查找那些其员工直接汇报给 'Andrew Adams' 的员工姓名和职位。

**预期涉及：** `Employee` 表，自引用（`ReportsTo`），子查询
**预期 SQL 模式：** `SELECT FirstName, LastName, Title FROM Employee WHERE ReportsTo = (SELECT EmployeeId FROM Employee WHERE FirstName = 'Andrew' AND LastName = 'Adams')`
**难度：** L5

---

## L6 — 复杂综合查询（问题 26-30）

### 问题 26
**自然语言：** 为每种音乐流派找出消费总额最高的客户全名、流派名称和该流派对应的消费金额。

**预期涉及：** `Genre`、`Track`、`InvoiceLine`、`Invoice`、`Customer` 表，多表 `JOIN`，`GROUP BY`，`SUM`，窗口函数
**预期 SQL 模式：** 按流派和客户分组求和后用窗口函数 ROW_NUMBER 取每组第一
**难度：** L6

---

### 问题 27
**自然语言：** 查询购买了所有 'Jazz' 流派曲目的客户全名。

**预期涉及：** `Customer`、`Invoice`、`InvoiceLine`、`Track`、`Genre` 表，除法关系（`NOT EXISTS` 双重否定）
**预期 SQL 模式：** `SELECT FirstName, LastName FROM Customer c WHERE NOT EXISTS (SELECT TrackId FROM Track t JOIN Genre g ON t.GenreId = g.GenreId WHERE g.Name = 'Jazz' AND NOT EXISTS (SELECT 1 FROM Invoice i JOIN InvoiceLine il ON i.InvoiceId = il.InvoiceId WHERE i.CustomerId = c.CustomerId AND il.TrackId = t.TrackId))`
**难度：** L6

---

### 问题 28
**自然语言：** 分析客户购买行为：查询每位客户的总消费额、订单数量、平均每笔订单金额，以及占总消费额的比例，按总消费额从高到低排列，仅显示前 10 名。

**预期涉及：** `Customer`、`Invoice` 表，`JOIN`，`GROUP BY`，`COUNT`，`SUM`，`AVG`，子查询，`LIMIT`
**预期 SQL 模式：** `SELECT Customer.FirstName, Customer.LastName, SUM(Invoice.Total) AS total_spent, COUNT(Invoice.InvoiceId) AS order_count, AVG(Invoice.Total) AS avg_order, ROUND(SUM(Invoice.Total) * 100.0 / (SELECT SUM(Total) FROM Invoice), 2) AS percentage FROM Customer JOIN Invoice ON Customer.CustomerId = Invoice.CustomerId GROUP BY Customer.CustomerId, Customer.FirstName, Customer.LastName ORDER BY total_spent DESC LIMIT 10`
**难度：** L6

---

### 问题 29
**自然语言：** 找出存在曲目价格不一致的专辑：同一专辑内同时包含 0.99 和 1.99 两种单价的曲目。返回专辑标题、艺术家姓名，以及两种单价的曲目分别有多少首。

**预期涉及：** `Album`、`Artist`、`Track` 表，`JOIN`，`GROUP BY`，`HAVING`，`CASE WHEN` 条件聚合
**预期 SQL 模式：** `SELECT Album.Title, Artist.Name, SUM(CASE WHEN Track.UnitPrice = 0.99 THEN 1 ELSE 0 END) AS price_099_count, SUM(CASE WHEN Track.UnitPrice = 1.99 THEN 1 ELSE 0 END) AS price_199_count FROM Album JOIN Artist ON Album.ArtistId = Artist.ArtistId JOIN Track ON Album.AlbumId = Track.AlbumId GROUP BY Album.AlbumId, Album.Title, Artist.Name HAVING COUNT(DISTINCT Track.UnitPrice) > 1`
**难度：** L6

---

### 问题 30
**自然语言：** 查找员工层级结构：显示每位员工的姓名、职位、直接上级的姓名，以及该员工管理的直接下属人数，按层级从上到下排列。

**预期涉及：** `Employee` 表，自连接（`LEFT JOIN`），`GROUP BY`，`COUNT`
**预期 SQL 模式：** `SELECT e1.FirstName, e1.LastName, e1.Title, e2.FirstName AS manager_first, e2.LastName AS manager_last, COUNT(e3.EmployeeId) AS subordinate_count FROM Employee e1 LEFT JOIN Employee e2 ON e1.ReportsTo = e2.EmployeeId LEFT JOIN Employee e3 ON e1.EmployeeId = e3.ReportsTo GROUP BY e1.EmployeeId, e1.FirstName, e1.LastName, e1.Title, e2.FirstName, e2.LastName ORDER BY e1.ReportsTo, e1.EmployeeId`
**难度：** L6

---

## 难度分布总结

| 难度 | 问题编号 | 数量 | 考察能力 |
|------|---------|------|---------|
| L1 | 1-5 | 5 | 单表 SELECT、COUNT、ORDER BY |
| L2 | 6-10 | 5 | WHERE、LIKE、AND、比较运算符 |
| L3 | 11-15 | 5 | 两表/三表 JOIN、简单 GROUP BY |
| L4 | 16-20 | 5 | 多表 JOIN + 聚合、HAVING、YEAR/MONTH、LIMIT |
| L5 | 21-25 | 5 | 子查询、NOT IN、NOT EXISTS、自连接、DISTINCT |
| L6 | 26-30 | 5 | 窗口函数、除法关系、条件聚合、自连接+聚合、复合分析 |

## 评估维度

每个问题按以下维度评分：

| 结果 | 说明 |
|------|------|
| **PASS** | SQL 语法正确且执行结果与预期一致 |
| **SYNTAX_ERR** | SQL 语法错误，无法执行 |
| **LOGIC_ERR** | SQL 可执行但结果与预期不符（意图不匹配） |
| **SCHEMA_ERR** | 表名或列名错误（Schema Linking 失败） |
