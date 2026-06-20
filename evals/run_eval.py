"""Eval runner using execution accuracy.

Reads evals/eval_set.jsonl, calls the agent at AGENT_URL on each question,
then compares the agent's SQL output to the gold SQL by *executed rows*
(canonicalized: sorted, stringified, None-coerced to empty).

Helpers (run_sql / canonicalize / matches) are provided. You implement
eval_one() and summarize().

Run:
    uv run python evals/run_eval.py --out results/eval_baseline.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path

import httpx
from agent.config import MAX_ITERATIONS

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EVAL_FILE = ROOT / "evals" / "eval_set.jsonl"
DEFAULT_OUT_FILE = ROOT / "results" / "eval_baseline.json"
DB_DIR = ROOT / "data" / "bird"
AGENT_URL_DEFAULT = "http://localhost:8001/answer"


# ---------- Helpers (provided) -----------------------------------------

def run_sql(db_id: str, sql: str, timeout: float = 5.0) -> tuple[bool, list[tuple] | None, str | None]:
    """Run sql against db_id in read-only mode. Returns (ok, rows, error)."""
    path = DB_DIR / f"{db_id}.sqlite"
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=timeout) as conn:
            cur = conn.execute(sql)
            rows = cur.fetchall()
            return True, rows, None
    except Exception as e:  # noqa: BLE001
        return False, None, f"{type(e).__name__}: {e}"


def canonicalize(rows: list[tuple] | None) -> list[tuple] | None:
    """Sort rows; coerce cells to str; None -> ''."""
    if rows is None:
        return None
    return sorted(tuple("" if c is None else str(c) for c in row) for row in rows)


def matches(gold_rows: list[tuple] | None, pred_rows: list[tuple] | None) -> bool:
    if gold_rows is None or pred_rows is None:
        return False
    return canonicalize(gold_rows) == canonicalize(pred_rows)


# ---------- Implement these (Phase 5) ----------------------------------

AGENT_TIMEOUT = 300.0


def _sql_attempts(history: list[dict]) -> list[str]:
    """SQL emitted at each generate/revise step, in order."""
    return [
        str(entry["sql"])
        for entry in history
        if entry.get("node") in ("generate_sql", "revise") and entry.get("sql")
    ]


def _score_sql(db_id: str, sql: str, gold_rows: list[tuple] | None, gold_ok: bool) -> dict:
    pred_ok, pred_rows, pred_err = run_sql(db_id, sql)
    correct = bool(gold_ok and pred_ok and matches(gold_rows, pred_rows))
    return {
        "sql": sql,
        "correct": correct,
        "pred_ok": pred_ok,
        "error": pred_err,
    }


def eval_one(question: dict, agent_url: str) -> dict:
    """Score one question. Return a dict capturing per-iteration correctness."""
    db_id = question["db_id"]
    gold_sql = question["gold_sql"]
    gold_ok, gold_rows, gold_err = run_sql(db_id, gold_sql)

    agent_result: dict
    try:
        with httpx.Client(timeout=AGENT_TIMEOUT) as client:
            response = client.post(
                agent_url,
                json={"question": question["question"], "db": db_id},
            )
            response.raise_for_status()
            agent_result = response.json()
    except httpx.HTTPError as exc:
        return {
            "question": question["question"],
            "db_id": db_id,
            "gold_sql": gold_sql,
            "gold_ok": gold_ok,
            "gold_error": gold_err,
            "iterations": 0,
            "attempt_count": 0,
            "final_sql": "",
            "final_correct": False,
            "per_iteration": [],
            "agent_ok": False,
            "agent_error": str(exc),
            "history": [],
        }

    history = agent_result.get("history", [])
    attempt_sqls = _sql_attempts(history)
    per_iteration = [
        _score_sql(db_id, sql, gold_rows, gold_ok) for sql in attempt_sqls
    ]

    final_sql = agent_result.get("sql", "") or (attempt_sqls[-1] if attempt_sqls else "")
    if final_sql:
        final_score = _score_sql(db_id, final_sql, gold_rows, gold_ok)
        final_correct = final_score["correct"]
    else:
        final_correct = False

    return {
        "question": question["question"],
        "db_id": db_id,
        "gold_sql": gold_sql,
        "gold_ok": gold_ok,
        "gold_error": gold_err,
        "iterations": agent_result.get("iterations", 0),
        "attempt_count": len(attempt_sqls),
        "final_sql": final_sql,
        "final_correct": final_correct,
        "per_iteration": per_iteration,
        "agent_ok": agent_result.get("ok"),
        "agent_error": agent_result.get("error"),
        "history": history,
    }


def _carried_correctness(per_iteration: list[dict]) -> list[bool]:
    """Expand attempt scores with carry-forward for early termination."""
    if not per_iteration:
        return [False] * MAX_ITERATIONS

    attempt_flags = [p["correct"] for p in per_iteration]
    terminal = len(attempt_flags) - 1
    return [attempt_flags[min(k, terminal)] for k in range(MAX_ITERATIONS)]


def summarize(results: list[dict]) -> dict:
    """Aggregate per-question results.

    Per-iteration carry-forward: if the agent terminated at iteration j < k
    (verify said ok at j, or it hit MAX_ITERATIONS at j < k), treat the
    question's iteration-k result as identical to its iteration-j result.
    The agent stopped emitting; whatever it had at termination is what
    would have been served had we polled at iteration k.
    """
    total = len(results)
    if total == 0:
        return {
            "total": 0,
            "overall_pass_rate": 0.0,
            "overall_passed": 0,
            "per_iteration_pass_rate": {},
            "avg_iterations": 0.0,
        }

    carried = [_carried_correctness(r["per_iteration"]) for r in results]
    per_iteration_pass_rate: dict[str, float] = {}
    for k in range(MAX_ITERATIONS):
        passed = sum(1 for flags in carried if flags[k])
        per_iteration_pass_rate[f"iter_{k}"] = passed / total

    overall_passed = sum(1 for r in results if r["final_correct"])
    return {
        "total": total,
        "overall_pass_rate": overall_passed / total,
        "overall_passed": overall_passed,
        "per_iteration_pass_rate": per_iteration_pass_rate,
        "avg_iterations": sum(r["iterations"] for r in results) / total,
    }


# ---------- Main (provided) --------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--eval-set", type=Path, default=DEFAULT_EVAL_FILE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_FILE)
    parser.add_argument("--agent-url", default=AGENT_URL_DEFAULT)
    args = parser.parse_args()

    questions = [json.loads(line) for line in args.eval_set.read_text().splitlines() if line.strip()]
    print(f"Loaded {len(questions)} eval questions from {args.eval_set}")

    results: list[dict] = []
    t0 = time.monotonic()
    for i, q in enumerate(questions, 1):
        print(f"[{i}/{len(questions)}] {q['db_id']}: {q['question'][:60]}...", flush=True)
        results.append(eval_one(q, args.agent_url))
    elapsed = time.monotonic() - t0

    summary = summarize(results)
    out = {
        "summary": summary,
        "wall_clock_seconds": elapsed,
        "results": results,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2))
    print(f"Wrote {args.out}")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
