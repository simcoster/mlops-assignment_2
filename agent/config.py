"""Shared configuration values for agent and evaluation code."""

# Total generate + revise calls before the loop is forced to stop.
MAX_ITERATIONS = 2

# Hard cap on rows fetched/returned for open-ended list queries (aggregates exempt).
MAX_RESULT_ROWS = 100

# Verifier LLM output bounds (prevents rambling issue text under load).
MAX_VERIFY_TOKENS = 128
MAX_VERIFY_ISSUE_CHARS = 200
