"""
SQL runner capability models.

This module contains data models for SQL execution.
"""
from typing import Any, Dict
from pydantic import BaseModel, Field

class RunSqlToolArgs(BaseModel):
    """Arguments for run_sql tool."""

    sql: str = Field(description="SQL query to execute")

class ToolContext(BaseModel):
    """Context passed to all tool executions."""
    user: str = Field(description="User identifier", default="user_001")  # Forward reference to avoid circular import
    metadata: Dict[str, Any] = Field(default_factory=dict)

    class Config:
        arbitrary_types_allowed = True
