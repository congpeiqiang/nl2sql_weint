# -*- coding: utf-8 -*-
import sqlfluff
import json

out = {}

# 测试 sqlfluff 版本
out['version'] = sqlfluff.__version__

# 测试 sqlfluff 解析和 lint
from sqlfluff.core import Linter

linter = Linter(dialect='ansi')

# 测试 SQL
test_sql = """
SELECT * FROM users u
JOIN orders o ON u.id = o.user_id
WHERE u.age > 30
"""

try:
    result = linter.lint_string(test_sql)
    violations = result.violations
    out['lint_violations'] = [
        {
            'rule': v.rule_code(),
            'description': v.description,
            'line': v.line_no,
            'pos': v.line_pos
        }
        for v in violations
    ]
except Exception as e:
    out['lint_error'] = str(e)

# 测试 sqlfluff 支持的方言
try:
    from sqlfluff.core.config import FluffConfig
    cfg = FluffConfig()
    out['default_dialect'] = cfg.get('dialect')
except Exception as e:
    out['dialect_error'] = str(e)

with open('sqlfluff_result.json', 'w', encoding='utf-8') as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print('DONE')
