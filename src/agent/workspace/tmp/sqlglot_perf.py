# -*- coding: utf-8 -*-
import sqlglot
import json
from sqlglot import exp

out = {}

# ============ 1. 检测 SELECT * ============
def detect_select_star(sql):
    ast = sqlglot.parse_one(sql)
    issues = []
    for star in ast.find_all(exp.Star):
        issues.append("SELECT * 使用（应明确列出所需列）")
    return issues

# ============ 2. 检测缺失 WHERE 的 DML ============
def detect_dml_without_where(sql):
    ast = sqlglot.parse_one(sql)
    issues = []
    for dml in ast.find_all(exp.Update, exp.Delete):
        if not any(isinstance(w, exp.Where) for w in dml.find_all(exp.Where)):
            issues.append(f"{type(dml).__name__} 缺少 WHERE 子句（危险操作）")
    return issues

# ============ 3. 检测 JOIN 类型 ============
def detect_join_types(sql):
    ast = sqlglot.parse_one(sql)
    issues = []
    for join in ast.find_all(exp.Join):
        if isinstance(join, exp.Join):
            # 检查是否有 ON 条件
            if not join.args.get('on'):
                issues.append("存在无 ON 条件的 JOIN（笛卡尔积风险）")
    return issues

# ============ 4. 检测子查询 ============
def detect_subqueries(sql):
    ast = sqlglot.parse_one(sql)
    issues = []
    for subq in ast.find_all(exp.Subquery):
        issues.append("存在子查询（可考虑改写为 JOIN 提升性能）")
    return issues

# ============ 5. 检测 LIMIT 缺失 ============
def detect_missing_limit(sql):
    ast = sqlglot.parse_one(sql)
    issues = []
    if isinstance(ast, exp.Select):
        if not ast.args.get('limit'):
            issues.append("SELECT 无 LIMIT 限制（可能返回大量数据）")
    return issues

# ============ 6. 检测 DISTINCT ============
def detect_distinct(sql):
    ast = sqlglot.parse_one(sql)
    issues = []
    if isinstance(ast, exp.Select) and ast.args.get('distinct'):
        issues.append("使用 DISTINCT（可能可用 GROUP BY 或 EXISTS 优化）")
    return issues

# ============ 7. 检测函数包裹列（索引失效） ============
def detect_func_on_column(sql):
    ast = sqlglot.parse_one(sql)
    issues = []
    for func in ast.find_all(exp.Func):
        for arg in func.args.values():
            if isinstance(arg, exp.Column):
                issues.append(f"函数 {func.sql()} 包裹列 {arg.sql()}（可能导致索引失效）")
    return issues

# ============ 8. 检测 NOT IN ============
def detect_not_in(sql):
    ast = sqlglot.parse_one(sql)
    issues = []
    for notin in ast.find_all(exp.Not, exp.In):
        if isinstance(notin, exp.Not):
            for child in notin.find_all(exp.In):
                issues.append("使用 NOT IN（可考虑改写为 NOT EXISTS 提升性能）")
    return issues

# ============ 综合测试 ============
test_sqls = {
    "select_star": "SELECT * FROM users WHERE age > 30",
    "delete_no_where": "DELETE FROM users",
    "update_no_where": "UPDATE users SET age = 30",
    "cartesian_join": "SELECT * FROM users u JOIN orders o",
    "subquery": "SELECT * FROM (SELECT * FROM users) t WHERE t.id = 1",
    "no_limit": "SELECT name FROM users",
    "distinct": "SELECT DISTINCT name FROM users",
    "func_on_col": "SELECT * FROM users WHERE YEAR(created_at) = 2024",
    "not_in": "SELECT * FROM users WHERE id NOT IN (SELECT user_id FROM orders)",
    "good_sql": "SELECT u.name, o.total FROM users u JOIN orders o ON u.id = o.user_id WHERE u.age > 30 AND u.status = 'active' ORDER BY o.total DESC LIMIT 10"
}

results = {}
for name, sql in test_sqls.items():
    issues = []
    issues += detect_select_star(sql)
    issues += detect_dml_without_where(sql)
    issues += detect_join_types(sql)
    issues += detect_subqueries(sql)
    issues += detect_missing_limit(sql)
    issues += detect_distinct(sql)
    issues += detect_func_on_column(sql)
    issues += detect_not_in(sql)
    results[name] = {"sql": sql, "issues": issues}

out['sqlglot_perf_detection'] = results

with open('sqlglot_perf_result.json', 'w', encoding='utf-8') as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print('DONE')
