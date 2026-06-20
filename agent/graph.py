"""LangGraph agent: text-to-SQL with verify+revise loop.

Graph shape:

    START -> attach_schema -> generate_sql -> execute -> verify
                                                          |
                                              ok=true ----+----> END
                                                          |
                                              ok=false ---+----> revise -> execute -> verify (loop)

Loop is capped at MAX_ITERATIONS total generate/revise calls.

The execute node and the graph wiring are provided. `generate_sql_node` is
filled in as a worked example; you implement `verify`, `revise`, and the
conditional router following the same shape.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from pydantic import SecretStr

from agent import prompts
from agent.config import MAX_ITERATIONS, MAX_VERIFY_ISSUE_CHARS, MAX_VERIFY_TOKENS
from agent.execution import ExecutionResult, execute_sql
from agent.schema import render_schema

VLLM_BASE_URL = os.environ.get("VLLM_BASE_URL", "http://localhost:8000/v1")
VLLM_MODEL = os.environ.get("VLLM_MODEL", "Qwen/Qwen3-30B-A3B-Instruct-2507")
# vLLM ignores the key, but a hosted OpenAI-compatible provider needs a real one.
# Lets you point the agent at e.g. OpenAI while iterating without a running vLLM.
LLM_API_KEY = os.environ.get("OPENAI_API_KEY", "not-needed")


@dataclass
class AgentState:
    """State threaded through the graph. Extend with fields you need."""

    question: str
    db_id: str
    schema: str = ""
    sql: str = ""
    execution: ExecutionResult | None = None
    verify_ok: bool = False
    verify_issue: str = ""
    iteration: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)


def llm() -> ChatOpenAI:
    """Chat client pointed at VLLM_BASE_URL (your local vLLM by default)."""
    return ChatOpenAI(
        model=VLLM_MODEL,
        base_url=VLLM_BASE_URL,
        api_key=SecretStr(LLM_API_KEY),
        temperature=0.0,
    )


def verify_llm():
    """Bounded verifier client — short JSON only."""
    return llm().bind(max_tokens=MAX_VERIFY_TOKENS)


_COUNT_QUESTION = re.compile(
    r"\b(how many|number of|count of|total number|what is the count)\b",
    re.IGNORECASE,
)


def _question_expects_count(question: str) -> bool:
    return bool(_COUNT_QUESTION.search(question))


def _truncate_issue(issue: str) -> str:
    issue = issue.strip()
    if len(issue) <= MAX_VERIFY_ISSUE_CHARS:
        return issue
    return issue[: MAX_VERIFY_ISSUE_CHARS - 3] + "..."


def _rule_based_verify(
    question: str,
    execution: ExecutionResult | None,
) -> tuple[bool, str] | None:
    """Return a verify decision without LLM, or None to defer to LLM verify."""
    if execution is None:
        return False, "no execution result"
    if execution.row_count == 0:
        if _question_expects_count(question):
            return True, ""
        return False, "0 rows returned; check filters, joins, or literal values"
    return None


def _verify_result(
    verify_ok: bool,
    verify_issue: str,
    *,
    mode: str,
) -> dict:
    verify_issue = _truncate_issue(verify_issue)
    return {
        "verify_ok": verify_ok,
        "verify_issue": verify_issue,
        "history_entry": {
            "node": "verify",
            "ok": verify_ok,
            "issue": verify_issue,
            "mode": mode,
        },
    }

# ---- Nodes ------------------------------------------------------------

def _attach_schema(state: AgentState) -> dict:
    """Provided. Render the DB schema once at the start of the run."""
    return {"schema": render_schema(state.db_id)}


def _message_text(content: str | list[str | dict[str, Any]]) -> str:
    """Normalize AIMessage.content to plain text (str or multimodal blocks)."""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def _extract_sql(text: str) -> str:
    """Pull a SQL statement out of an LLM reply, stripping markdown fences/prose.

    Intentionally simple: take the first ```sql ... ``` block if there is one,
    otherwise the whole reply. You may need to harden this for your prompts.
    """
    fenced = re.search(r"```(?:sql)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    return (fenced.group(1) if fenced else text).strip()


def _normalize_sql(sql: str) -> str:
    """Collapse whitespace for same-query detection."""
    return re.sub(r"\s+", " ", sql.strip().rstrip(";")).lower()


def _format_revision_history(history: list[dict[str, Any]]) -> str:
    """Build a prompt section listing every failed SQL attempt and verifier feedback."""
    blocks: list[str] = []
    last_sql = ""
    attempt = 0

    for entry in history:
        node = entry.get("node")
        if node in ("generate_sql", "revise"):
            last_sql = str(entry.get("sql", ""))
        elif node == "verify" and not entry.get("ok", True):
            attempt += 1
            issue = str(entry.get("issue", "") or "(no issue provided)")
            blocks.append(
                f"Attempt {attempt}:\n"
                f"SQL:\n{last_sql}\n\n"
                f"Verifier issue:\n{issue}"
            )

    if not blocks:
        return "No prior failed attempts."
    return "\n\n".join(blocks)


def _prior_sql_attempts(history: list[dict[str, Any]]) -> list[str]:
    """Return every SQL query tried so far (generate + revise)."""
    return [
        str(entry["sql"])
        for entry in history
        if entry.get("node") in ("generate_sql", "revise") and entry.get("sql")
    ]


def _parse_verify_response(text: str) -> tuple[bool, str]:
    """Parse {"ok": bool, "issue": str} from an LLM reply, tolerating fences/prose."""
    stripped = text.strip()
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end <= start:
            return False, f"Verifier returned unparseable output: {stripped[:200]}"
        try:
            data = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError:
            return False, f"Verifier returned unparseable output: {stripped[:200]}"

    ok = bool(data.get("ok", False))
    issue = _truncate_issue(str(data.get("issue", "") or ""))
    return ok, issue


def generate_sql_node(state: AgentState) -> dict:
    """Worked example - the other LLM nodes follow this same shape.

    Build messages from the prompts, call the shared llm(), extract the SQL,
    and return only the state fields you changed. `iteration` is bumped here
    (and in revise) so route_after_verify can enforce MAX_ITERATIONS.

    This node is wired and ready; fill in GENERATE_SQL_SYSTEM / GENERATE_SQL_USER
    in prompts.py to make it produce real queries.
    """
    response = llm().invoke([
        ("system", prompts.GENERATE_SQL_SYSTEM),
        ("user", prompts.GENERATE_SQL_USER.format(
            schema=state.schema,
            question=state.question,
        )),
    ])
    sql = _extract_sql(_message_text(response.content))
    return {
        "sql": sql,
        "iteration": state.iteration + 1,
        "history": state.history + [{"node": "generate_sql", "sql": sql}],
    }


def execute_node(state: AgentState) -> dict:
    """Provided. Runs the SQL and stores the result."""
    return {"execution": execute_sql(state.db_id, state.sql)}


def verify_node(state: AgentState) -> dict:
    """Decide whether state.execution plausibly answers state.question.

    Rule-based fast path for SQL errors and 0-row results; LLM verify only
    for ambiguous non-empty results. Verifier output is token- and length-capped.
    """
    execution = state.execution
    ruled = _rule_based_verify(state.question, execution)
    if ruled is not None:
        verify_ok, verify_issue = ruled
        result = _verify_result(verify_ok, verify_issue, mode="rules")
        return {
            "verify_ok": result["verify_ok"],
            "verify_issue": result["verify_issue"],
            "history": state.history + [result["history_entry"]],
        }

    execution_text = execution.render() if execution is not None else "ERROR: no execution result"
    response = verify_llm().invoke([
        ("system", prompts.VERIFY_SYSTEM),
        ("user", prompts.VERIFY_USER.format(
            question=state.question,
            sql=state.sql,
            execution=execution_text,
        )),
    ])
    verify_ok, verify_issue = _parse_verify_response(_message_text(response.content))
    result = _verify_result(verify_ok, verify_issue, mode="llm")
    return {
        "verify_ok": result["verify_ok"],
        "verify_issue": result["verify_issue"],
        "history": state.history + [result["history_entry"]],
    }


def revise_node(state: AgentState) -> dict:
    """Produce a revised SQL query given state.verify_issue and the prior attempt.

    Same shape as generate_sql_node, but the prompt should include the failing
    SQL, its execution result, and the verifier's complaint so the model can fix
    it. Bump the iteration counter the same way generate_sql_node does so the
    loop terminates.

    Return: {"sql": <str>, "iteration": state.iteration + 1, ...}.
    """
    execution = state.execution
    execution_text = execution.render() if execution is not None else "ERROR: no execution result"
    revision_history = _format_revision_history(state.history)
    prior_sqls = _prior_sql_attempts(state.history)

    messages: list[tuple[str, str]] = [
        ("system", prompts.REVISE_SYSTEM),
        ("user", prompts.REVISE_USER.format(
            schema=state.schema,
            question=state.question,
            revision_history=revision_history,
            execution=execution_text,
        )),
    ]

    model = llm()
    response = model.invoke(messages)
    reply = _message_text(response.content)
    sql = _extract_sql(reply)

    def _repeats_prior_attempt(candidate: str) -> bool:
        norm = _normalize_sql(candidate)
        return any(norm == _normalize_sql(prior) for prior in prior_sqls)

    if _repeats_prior_attempt(sql):
        messages.append(("assistant", reply))
        messages.append(("user", prompts.REVISE_RETRY_USER.format(
            revision_history=revision_history,
            sql=sql,
        )))
        response = model.invoke(messages)
        sql = _extract_sql(_message_text(response.content))

    return {
        "sql": sql,
        "iteration": state.iteration + 1,
        "history": state.history + [{
            "node": "revise",
            "sql": sql,
            "issue": state.verify_issue,
            "unchanged": _repeats_prior_attempt(sql),
        }],
    }


def route_after_verify(state: AgentState) -> str:
    """Conditional router: return "revise" to loop, "end" to terminate.

    Two reasons to end: the verifier was happy (state.verify_ok), or you've hit
    the iteration cap (state.iteration >= MAX_ITERATIONS). Otherwise, revise.
    """
    if state.verify_ok or state.iteration >= MAX_ITERATIONS:
        return "end"
    return "revise"


# ---- Graph wiring -----------------------------------------------------

def build_graph():
    g = StateGraph(AgentState)
    g.add_node("attach_schema", _attach_schema)
    g.add_node("generate_sql", generate_sql_node)
    g.add_node("execute", execute_node)
    g.add_node("verify", verify_node)
    g.add_node("revise", revise_node)

    g.add_edge(START, "attach_schema")
    g.add_edge("attach_schema", "generate_sql")
    g.add_edge("generate_sql", "execute")
    g.add_edge("execute", "verify")
    g.add_conditional_edges(
        "verify",
        route_after_verify,
        {"revise": "revise", "end": END},
    )
    g.add_edge("revise", "execute")
    return g.compile()


graph = build_graph()
