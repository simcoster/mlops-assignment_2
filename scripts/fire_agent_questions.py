#!/usr/bin/env python3
"""Fire eval questions against the agent FastAPI server.

Run (agent must be up on :8001):
    uv run python scripts/fire_agent_questions.py
    uv run python scripts/fire_agent_questions.py --count 5 --start 1
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
EVAL_FILE = ROOT / "evals" / "eval_set.jsonl"
DEFAULT_BASE_URL = "http://localhost:8001"


def load_questions(path: Path = EVAL_FILE) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"Eval set not found at {path}")
    return [
        json.loads(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]


def fire_question(
    *,
    client: httpx.Client,
    base_url: str,
    question: str,
    db_id: str,
    tags: dict[str, str],
) -> dict:
    response = client.post(
        f"{base_url.rstrip('/')}/answer",
        json={"question": question, "db": db_id, "tags": tags},
    )
    response.raise_for_status()
    return response.json()


def main() -> None:
    load_dotenv(ROOT / ".env")

    parser = argparse.ArgumentParser(
        description="POST eval questions to the agent /answer endpoint.",
    )
    parser.add_argument("--base-url", default=os.environ.get("AGENT_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--eval-set", type=Path, default=EVAL_FILE)
    parser.add_argument("--count", type=int, default=10, help="Number of questions to send")
    parser.add_argument(
        "--start",
        type=int,
        default=1,
        help="1-based index in the eval set to start from",
    )
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument(
        "--run-id",
        default="",
        help="Langfuse metadata run id (default: random uuid)",
    )
    args = parser.parse_args()

    questions = load_questions(args.eval_set)
    if args.count < 1:
        parser.error("--count must be at least 1")
    if not 1 <= args.start <= len(questions):
        parser.error(f"--start must be between 1 and {len(questions)}")

    end = min(args.start - 1 + args.count, len(questions))
    batch = questions[args.start - 1 : end]
    run_id = args.run_id or str(uuid.uuid4())

    print(f"Agent: {args.base_url}")
    print(f"Questions: {len(batch)} (eval #{args.start}–#{end})")
    print(f"Run id: {run_id}")
    print("-" * 60)

    ok_count = 0
    revise_count = 0
    total_elapsed = 0.0

    with httpx.Client(timeout=args.timeout) as client:
        try:
            health = client.get(f"{args.base_url.rstrip('/')}/health")
            health.raise_for_status()
        except httpx.HTTPError as exc:
            print(f"Agent health check failed: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc

        for offset, item in enumerate(batch, start=args.start):
            tags = {
                "phase": "4",
                "run_id": run_id,
                "question_index": str(offset),
                "db_id": item["db_id"],
            }
            preview = item["question"][:72]
            suffix = "..." if len(item["question"]) > 72 else ""
            print(f"[{offset}] {item['db_id']}: {preview}{suffix}")

            started = time.perf_counter()
            try:
                result = fire_question(
                    client=client,
                    base_url=args.base_url,
                    question=item["question"],
                    db_id=item["db_id"],
                    tags=tags,
                )
            except httpx.HTTPError as exc:
                elapsed = time.perf_counter() - started
                total_elapsed += elapsed
                print(f"  FAIL ({elapsed:.1f}s): {exc}")
                if hasattr(exc, "response") and exc.response is not None:
                    print(f"  {exc.response.text[:300]}")
                continue

            elapsed = time.perf_counter() - started
            total_elapsed += elapsed

            revised = any(h.get("node") == "revise" for h in result.get("history", []))
            if result.get("ok"):
                ok_count += 1
            if revised:
                revise_count += 1

            row_count = len(result.get("rows") or [])
            status = "ok" if result.get("ok") else "error"
            err = result.get("error")
            err_suffix = f" — {err}" if err else ""
            print(
                f"  {status} ({elapsed:.1f}s) "
                f"iterations={result.get('iterations')} "
                f"rows={row_count} "
                f"revised={'yes' if revised else 'no'}"
                f"{err_suffix}"
            )

    print("-" * 60)
    print(
        f"Done: {ok_count}/{len(batch)} ok, "
        f"{revise_count} with revise, "
        f"{total_elapsed:.1f}s total"
    )


if __name__ == "__main__":
    main()
