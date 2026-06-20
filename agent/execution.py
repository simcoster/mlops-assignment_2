"""SQL execution helper (provided complete).

execute_sql() runs the agent's SQL against the target DB in read-only mode
and returns a structured ExecutionResult. The verify node consumes this
to decide whether the answer looks plausible.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from agent.config import MAX_RESULT_ROWS
from agent.schema import db_path


@dataclass
class ExecutionResult:
    ok: bool
    rows: list[tuple] | None = None
    columns: list[str] | None = None
    error: str | None = None
    row_count: int = 0
    truncated: bool = False

    def render(self, max_rows: int = 10) -> str:
        """Compact text rendering for prompt context."""
        if not self.ok:
            return f"ERROR: {self.error}"
        if self.row_count == 0:
            return "OK: 0 rows returned."

        cols = ", ".join(self.columns or [])
        preview_rows = (self.rows or [])[:max_rows]
        preview = "\n".join(" | ".join(str(c) for c in row) for row in preview_rows)
        shown = len(preview_rows)

        if self.truncated:
            header = (
                f"OK: {self.row_count} rows total (showing first {shown}).\n"
                f"TRUNCATED: true"
            )
            tail = f"\n... ({self.row_count - shown} more rows not shown)" if self.row_count > shown else ""
        else:
            header = f"OK: {self.row_count} rows."
            tail = (
                f"\n... ({self.row_count - max_rows} more rows)"
                if self.row_count > max_rows
                else ""
            )

        return f"{header}\nCOLUMNS: {cols}\nFIRST ROWS:\n{preview}{tail}"


def _normalize_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.strip().rstrip(";"))


def _has_limit(sql: str) -> bool:
    return bool(re.search(r"\bLIMIT\b", sql, re.IGNORECASE))


def _is_scalar_aggregate(sql: str) -> bool:
    """Heuristic: single-value aggregate queries don't need a row cap."""
    norm = _normalize_sql(sql)
    if re.search(r"\bGROUP\s+BY\b", norm, re.IGNORECASE):
        return False

    select_part = re.split(r"\bFROM\b", norm, maxsplit=1, flags=re.IGNORECASE)[0]
    if not re.match(
        r"SELECT\s+(DISTINCT\s+)?(COUNT|SUM|AVG|MIN|MAX|IIF)\s*\(",
        select_part,
        re.IGNORECASE,
    ):
        return False

    depth = 0
    for ch in select_part[len("SELECT") :]:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            return False
    return True


def _run_query(conn: sqlite3.Connection, sql: str) -> tuple[list[str], list[tuple]]:
    cur = conn.execute(sql)
    cols = [d[0] for d in cur.description] if cur.description else []
    return cols, cur.fetchall()


def execute_sql(
    db_id: str,
    sql: str,
    timeout_seconds: float = 5.0,
    max_rows: int | None = None,
) -> ExecutionResult:
    """Run SQL against db_id's sqlite, return result or error."""
    if max_rows is None:
        max_rows = MAX_RESULT_ROWS

    path = db_path(db_id)
    stripped = sql.strip().rstrip(";")
    try:
        with sqlite3.connect(
            f"file:{path}?mode=ro",
            uri=True,
            timeout=timeout_seconds,
        ) as conn:
            if _has_limit(stripped) or _is_scalar_aggregate(stripped):
                cols, rows = _run_query(conn, sql)
                return ExecutionResult(
                    ok=True,
                    rows=rows,
                    columns=cols,
                    row_count=len(rows),
                    truncated=False,
                )

            count_sql = f"SELECT COUNT(*) FROM ({stripped}) AS _agent_sub"
            total = int(conn.execute(count_sql).fetchone()[0])

            limited_sql = f"SELECT * FROM ({stripped}) AS _agent_sub LIMIT {max_rows}"
            cols, rows = _run_query(conn, limited_sql)
            truncated = total > len(rows)
            return ExecutionResult(
                ok=True,
                rows=rows,
                columns=cols,
                row_count=total,
                truncated=truncated,
            )
    except Exception as e:  # noqa: BLE001
        return ExecutionResult(ok=False, error=f"{type(e).__name__}: {e}")
