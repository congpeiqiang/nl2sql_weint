# -*- coding: utf-8 -*-
import sqlglot
import json

out = {}
out['version'] = sqlglot.__version__
out['dialects'] = sorted(sqlglot.dialects.DIALECTS)

# 测试解析能力
sql = "SELECT * FROM users u JOIN orders o ON u.id = o.user_id WHERE u.age > 30"
try:
    ast = sqlglot.parse_one(sql)
    out['parse_ok'] = True
    out['ast_type'] = type(ast).__name__
except Exception as e:
    out['parse_ok'] = False
    out['parse_error'] = str(e)

# 测试方言转译
try:
    mysql_sql = "SELECT * FROM users LIMIT 10"
    transpiled = sqlglot.transpile(mysql_sql, read='mysql', write='postgres')[0]
    out['transpile_mysql_to_pg'] = transpiled
except Exception as e:
    out['transpile_error'] = str(e)

# 测试优化器
try:
    from sqlglot.optimizer import optimize
    opt_sql = "SELECT * FROM (SELECT * FROM users) t WHERE t.id = 1"
    optimized = optimize(sqlglot.parse_one(opt_sql), schema=None)
    out['optimizer_ok'] = True
    out['optimized_sql'] = optimized.sql()
except Exception as e:
    out['optimizer_error'] = str(e)

with open('sqlglot_probe_result.json', 'w', encoding='utf-8') as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print('DONE')
