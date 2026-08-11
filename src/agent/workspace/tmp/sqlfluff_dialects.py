# -*- coding: utf-8 -*-
import json
from sqlfluff.core import Linter

out = {}

# 测试不同方言
dialects = ['mysql', 'postgres', 'sqlite', 'ansi']

test_sqls = {
    "select_star": "SELECT * FROM users WHERE age > 30",
    "no_limit": "SELECT name FROM users",
    "cartesian_join": "SELECT * FROM users u JOIN orders o",
    "func_on_col": "SELECT * FROM users WHERE YEAR(created_at) = 2024",
    "not_in": "SELECT * FROM users WHERE id NOT IN (SELECT user_id FROM orders)",
    "good_sql": "SELECT u.name, o.total FROM users u JOIN orders o ON u.id = o.user_id WHERE u.age > 30 LIMIT 10"
}

for dialect in dialects:
    linter = Linter(dialect=dialect)
    out[dialect] = {}
    for name, sql in test_sqls.items():
        try:
            result = linter.lint_string(sql)
            violations = [
                {
                    'rule': v.rule_code(),
                    'desc': v.description,
                    'line': v.line_no
                }
                for v in result.violations
            ]
            out[dialect][name] = violations
        except Exception as e:
            out[dialect][name] = {'error': str(e)}

with open('sqlfluff_dialects_result.json', 'w', encoding='utf-8') as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print('DONE')
