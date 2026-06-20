"""Shared configuration values for agent and evaluation code."""

# Total generate + revise calls before the loop is forced to stop.
MAX_ITERATIONS = 2

# Hard cap on rows fetched/returned for open-ended list queries (aggregates exempt).
MAX_RESULT_ROWS = 100
