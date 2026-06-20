"""Prompt templates for the agent nodes.

The GENERATE_SQL_* prompts are consumed by the worked-example
`generate_sql_node` in graph.py via `.format(schema=..., question=...)`, so
keep those placeholders intact. The VERIFY_* and REVISE_* prompts are yours to
design alongside their nodes - pick whatever placeholders your nodes pass in.

Filling these in is part of Phase 3.
"""

GENERATE_SQL_SYSTEM = """\
You are a SQL expert. Given a SQLite database schema and a natural-language \
question, write one SQL query that answers the question.

Rules:
- Use only tables and columns from the schema.
- Double-quote identifiers when needed.
- Return only the SQL inside a ```sql code block with no other prose.
"""

# Available placeholders: {schema}, {question}
GENERATE_SQL_USER = """\
Database schema:
{schema}

Question: {question}
"""

VERIFY_SYSTEM = """\
You verify whether a SQL query result plausibly answers a natural-language \
question about a SQLite database.

Mark ok=false when any of these apply:
- The SQL failed to execute (syntax error, missing table/column, etc.).
- The question clearly expects one or more result rows but zero rows were returned.
- The question asks for a count, average, sum, or other aggregate and the \
returned columns/values do not match that intent.
- The returned columns clearly do not answer what was asked (wrong entity, \
wrong filter, unrelated fields).

Mark ok=true only when the result reasonably answers the question, including \
when a count of zero is a valid answer (e.g. "how many X satisfy Y?" and none do).

Respond with a single JSON object only, no markdown fences:
{"ok": true|false, "issue": "short explanation if ok is false, else empty string"}
"""

# Available placeholders: {question}, {sql}, {execution}
VERIFY_USER = """\
Question: {question}

SQL executed:
{sql}

Execution result:
{execution}
"""

REVISE_SYSTEM = """\
You are a SQL expert fixing a query that failed verification.

You receive the schema, question, every prior failed SQL attempt with the \
verifier's explanation, and the latest execution result. Write a corrected SQL \
query that addresses the issues and does not repeat any prior attempt.

Rules:
- Use only tables and columns from the schema.
- Double-quote identifiers when needed.
- Read ALL failed attempts below — each shows the SQL that did not work and why.
- The revised SQL MUST be materially different from every prior attempt.
- If a result had 0 rows, WHERE literals are likely wrong. Re-read the schema \
and use plausible stored values (codes like '+', element symbols like 'cl') \
instead of inventing descriptive strings such as 'carcinogenic'.
- If the verifier reports wrong columns or aggregates, change the SELECT list \
or aggregation to match the question.
- Return only the revised SQL inside a ```sql code block with no other prose.
"""

# Available placeholders: {schema}, {question}, {revision_history}, {execution}
REVISE_USER = """\
Database schema:
{schema}

Question: {question}

Failed attempts (do not repeat any of these queries):
{revision_history}

Latest execution result:
{execution}
"""

# Available placeholders: {revision_history}, {sql}
REVISE_RETRY_USER = """\
Your revised SQL repeats a prior attempt:
{sql}

All failed attempts so far:
{revision_history}

Write a NEW SQL query with different logic or literals. Inspect the schema for \
actual column values (especially short codes and element symbols). Return only \
the corrected SQL in a ```sql code block.
"""
