"""
Multi-statement SQL execution helpers.

Provides safe SQL splitting (handling semicolons inside string literals)
and result combination for multi-statement execution.
"""

from typing import Any, Dict, List, Tuple

import pandas as pd


def split_sql_statements(sql: str) -> List[str]:
    """Split semicolon-separated SQL into individual statements.

    Handles edge cases:
    - Skips empty statements
    - Preserves semicolons inside single-quoted strings (simple state machine)
    - Preserves escaped quotes ('')

    Args:
        sql: SQL text possibly containing multiple ;-separated statements

    Returns:
        List of non-empty individual SQL statements
    """
    statements: List[str] = []
    current: List[str] = []
    in_single_quote = False
    i = 0

    while i < len(sql):
        ch = sql[i]

        # Toggle quote state (escape-aware: '' inside strings)
        if ch == "'" and not in_single_quote:
            in_single_quote = True
        elif ch == "'" and in_single_quote:
            # Check for escaped quote ''
            if i + 1 < len(sql) and sql[i + 1] == "'":
                current.append("''")
                i += 2
                continue
            in_single_quote = False

        # Semicolon outside strings = statement boundary
        if ch == ";" and not in_single_quote:
            stmt = "".join(current).strip()
            if stmt:
                statements.append(stmt)
            current = []
        else:
            current.append(ch)

        i += 1

    # Trailing statement (no terminating semicolon)
    stmt = "".join(current).strip()
    if stmt:
        statements.append(stmt)

    return statements


def df_to_result(df: pd.DataFrame) -> Dict[str, Any]:
    """Convert a pandas DataFrame to a JSON-serializable dict."""
    return {
        "columns": list(df.columns),
        "rows": df.to_dict(orient="records"),
        "row_count": len(df),
    }


def combine_multi_results(
    all_results: List[Tuple[str, pd.DataFrame]],
) -> Dict[str, Any]:
    """Combine results from multiple SQL statements into a unified response.

    - Single statement: returns original format (backward compatible)
    - Multiple statements: returns last result as primary + execution summary

    Args:
        all_results: List of (sql, dataframe) tuples

    Returns:
        JSON-serializable result dict
    """
    if len(all_results) == 1:
        return df_to_result(all_results[0][1])

    # Multi-statement: use last result as primary
    _, last_df = all_results[-1]
    result = df_to_result(last_df)

    # Build execution summary
    summaries = []
    for idx, (stmt, df) in enumerate(all_results):
        stmt_preview = stmt[:200] + ("..." if len(stmt) > 200 else "")
        summaries.append({
            "index": idx,
            "statement": stmt_preview,
            "rows": len(df),
            "columns": list(df.columns) if not df.empty else [],
        })

    result["statement_count"] = len(all_results)
    result["statements"] = summaries

    return result
