# -*- coding: utf-8 -*-
import json
import sqlglot
from sqlglot import exp
from sqlglot.optimizer import optimize, simplify, qualify

out = {}

# ============ sqlglot optimizer 能力 ============
out['optimizer'] = {}

# 1. 简化表达式
try:
    from sqlglot.optimizer.simplify import simplify
    sql = "SELECT * FROM t WHERE 1=1 AND a = a"
    ast = sqlglot.parse_one(sql)
    simplified = simplify(ast)
    out['optimizer']['simplify'] = simplified.sql()
except Exception as e:
    out['optimizer']['simplify_error'] = str(e)

# 2. 消除冗余子查询
try:
    sql = "SELECT * FROM (SELECT * FROM users) t WHERE t.id = 1"
    ast = sqlglot.parse_one(sql)
    optimized = optimize(ast)
    out['optimizer']['eliminate_subquery'] = optimized.sql()
except Exception as e:
    out['optimizer']['eliminate_subquery_error'] = str(e)

# 3. 谓词下推
try:
    sql = "SELECT * FROM (SELECT * FROM users WHERE age > 30) t JOIN orders o ON t.id = o.user_id"
    ast = sqlglot.parse_one(sql)
    optimized = optimize(ast)
    out['optimizer']['predicate_pushdown'] = optimized.sql()
except Exception as e:
    out['optimizer']['predicate_pushdown_error'] = str(e)

# 4. 列裁剪
try:
    sql = "SELECT * FROM users u JOIN orders o ON u.id = o.user_id"
    ast = sqlglot.parse_one(sql)
    optimized = optimize(ast)
    out['optimizer']['column_pruning'] = optimized.sql()
except Exception as e:
    out['optimizer']['column_pruning_error'] = str(e)

# ============ sqlglot 表达式分析能力 ============
out['expression_analysis'] = {}

# 检测函数包裹列
def find_func_wrapped_columns(sql):
    ast = sqlglot.parse_one(sql)
    issues = []
    for func in ast.find_all(exp.Func):
        for arg in func.args.values():
            if isinstance(arg, exp.Column):
                issues.append(f"{func.sql()} 包裹列 {arg.sql()}")
    return issues

out['expression_analysis']['func_wrapped_columns'] = find_func_wrapped_columns(
    "SELECT * FROM users WHERE YEAR(created_at) = 2024 AND LOWER(name) = 'john'"
)

# 检测隐式类型转换
def find_implicit_cast(sql):
    ast = sqlglot.parse_one(sql)
    issues = []
    for cast in ast.find_all(exp.Cast):
        issues.append(f"显式 CAST: {cast.sql()}")
    return issues

out['expression_analysis']['casts'] = find_implicit_cast(
    "SELECT * FROM users WHERE CAST(id AS VARCHAR) = '123'"
)

# 检测 OR 条件（可能无法用索引）
def find_or_conditions(sql):
    ast = sqlglot.parse_one(sql)
    issues = []
    for or_expr in ast.find_all(exp.Or):
        issues.append(f"OR 条件: {or_expr.sql()}")
    return issues

out['expression_analysis']['or_conditions'] = find_or_conditions(
    "SELECT * FROM users WHERE age > 30 OR status = 'active'"
)

# 检测 LIKE 前缀通配符
def find_like_wildcards(sql):
    ast = sqlglot.parse_one(sql)
    issues = []
    for like in ast.find_all(exp.Like):
        pattern = like.args.get('expression')
        if pattern:
            pattern_str = pattern.sql()
            if pattern_str.startswith("'%") or pattern_str.startswith("'_"):
                issues.append(f"LIKE 前缀通配符（索引失效）: {like.sql()}")
    return issues

out['expression_analysis']['like_wildcards'] = find_like_wildcards(
    "SELECT * FROM users WHERE name LIKE '%john%'"
)

# 检测 NOT 条件
def find_not_conditions(sql):
    ast = sqlglot.parse_one(sql)
    issues = []
    for not_expr in ast.find_all(exp.Not):
        issues.append(f"NOT 条件（可能无法用索引）: {not_expr.sql()}")
    return issues

out['expression_analysis']['not_conditions'] = find_not_conditions(
    "SELECT * FROM users WHERE NOT (age > 30)"
)

with open('sqlglot_advanced_result.json', 'w', encoding='utf-8') as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print('DONE')
