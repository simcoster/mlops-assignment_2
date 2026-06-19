#!/usr/bin/env python3
"""Query vLLM with a single eval question and its database schema.

Run:
    uv run python scripts/query_eval.py 1
    uv run python scripts/query_eval.py --list
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
EVAL_FILE = ROOT / "evals" / "eval_set.jsonl"

SYSTEM_PROMPT = """\
You are a SQL expert. Given a SQLite database schema and a natural-language \
question, write one SQL query that answers the question.

Rules:
- Use only tables and columns from the schema.
- Double-quote identifiers when needed.
- Return only the SQL inside a ```sql code block with no other text.
"""

USER_PROMPT = """\
Database schema:
{schema}

Question: {question}
"""

VLLM_BASE_URL = "http://localhost:8000/v1"
VLLM_MODEL = "Qwen/Qwen3-30B-A3B-Instruct-2507"


def load_questions(path: Path = EVAL_FILE) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"Eval set not found at {path}")
    return [
        json.loads(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]


def extract_sql(text: str) -> str:
    fenced = re.search(r"```(?:sql)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    return (fenced.group(1) if fenced else text).strip()


def query_vllm(
    *,
    schema: str,
    question: str,
    base_url: str,
    model: str,
    api_key: str,
    timeout: float,
) -> str:
    response = httpx.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": USER_PROMPT.format(schema=schema, question=question),
                },
            ],
            "temperature": 0.0,
            "max_tokens": 512,
            "chat_template_kwargs": {"enable_thinking": False}
        },
        timeout=timeout,

    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


def main() -> None:
    load_dotenv(ROOT / ".env")

    parser = argparse.ArgumentParser(description="Query vLLM with an eval question.")
    parser.add_argument(
        "number",
        nargs="?",
        type=int,
        help="1-based question number from evals/eval_set.jsonl",
    )
    parser.add_argument("--list", action="store_true", help="List eval questions")
    parser.add_argument("--eval-set", type=Path, default=EVAL_FILE)
    parser.add_argument(
        "--base-url",
        default=os.environ.get("VLLM_BASE_URL", VLLM_BASE_URL),
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("VLLM_MODEL", VLLM_MODEL),
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("OPENAI_API_KEY", "not-needed"),
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    questions = load_questions(args.eval_set)

    if args.list:
        for i, q in enumerate(questions, 1):
            preview = q["question"][:80]
            suffix = "..." if len(q["question"]) > 80 else ""
            print(f"{i:>2}. [{q['db_id']}] {preview}{suffix}")
        return

    if args.number is None:
        parser.error("question number is required unless --list is used")

    if not 1 <= args.number <= len(questions):
        print(
            f"Question number must be between 1 and {len(questions)}.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    item = questions[args.number - 1]

    # Import here so --list works without sqlite data present.
    from agent.schema import render_schema

    schema = render_schema(item["db_id"])

    print(f"Question {args.number}/{len(questions)}")
    print(f"Database: {item['db_id']}")
    print(f"Question: {item['question']}")
    print(f"Model: {args.model}")
    print("-" * 60)

    content = query_vllm(
        schema=schema,
        question=item["question"],
        base_url=args.base_url,
        model=args.model,
        api_key=args.api_key,
        timeout=args.timeout,
    )

    print("Raw response:")
    print(content)
    print("-" * 60)
    print("Extracted SQL:")
    print(extract_sql(content))


if __name__ == "__main__":
    main()
