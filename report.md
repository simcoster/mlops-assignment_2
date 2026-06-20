## Phase 1 — vLLM serving config
Model: Qwen/Qwen3-30B-A3B-Instruct-2507 (from `VLLM_MODEL` in `.env`, read by `scripts/start_vllm.sh`)
Hardware: 1× H100 80GB

| Flag | Value | Justification |
|------|-------|---------------|
| VLLM_MODEL | Qwen/Qwen3-30B-A3B-Instruct-2507 | Assignment target model; must match agent/eval requests |
| --gpu-memory-utilization | 0.9 | Weights use ~57 GiB; higher util leaves enough KV cache headroom on H100 |
| --max-model-len | 8192 | Default 40960 OOMs on KV cache; prompts ~2K tokens so 8K is sufficient |
| --max-num-seqs | 64 | Tuned for 10+ RPS with ~2-3 calls/request |
| --enable-prefix-caching | true | Schema prefix reused across generate/verify/revise |
| --reasoning-parser | qwen3 | Qwen3 family |
| --default-chat-template-kwargs | {"enable_thinking": false} | Agent needs SQL, not reasoning tokens |

## Phase 2 — Grafana dashboard (`serving.json`)

Three row groups, metrics scraped from vLLM `/metrics` via Prometheus:

| Category | Panels | Key metrics |
|----------|--------|-------------|
| **Throughput** | running / waiting / req/s / gen & prompt tokens/s / tokens per engine step P50–P95 | `num_requests_running`, `num_requests_waiting`, `request_success_total`, `generation_tokens_total`, `prompt_tokens_total`, `iteration_tokens_total` |
| **Latency** | E2E & TTFT P50–P99; lifecycle queue/prefill/decode P95; TPOT & inter-token P50–P95 | `e2e_request_latency_seconds`, `time_to_first_token_seconds`, `request_queue_time_seconds`, `request_prefill_time_seconds`, `request_decode_time_seconds`, `time_per_output_token_seconds` |
| **KV cache** | usage %, prefix hit rate, preemptions/s | `kv_cache_usage_perc`, `prefix_cache_hits_total` / `prefix_cache_queries_total`, `num_preemptions_total` |

Read order under load: queue growing → KV usage near 100% → preemptions → which lifecycle phase P95 moved (queue vs prefill vs decode).

## Phase 3 — Agent graph

| Setting | Value | Justification |
|---------|-------|---------------|
| MAX_ITERATIONS | 2 (iteration 1 target) | `eval_baseline.json` shows identical pass rate for `iter_1` and `iter_2` (0.3333), so the 3rd loop adds latency risk without observed quality gain |
| MAX_RESULT_ROWS | 100 | Hard cap on open-ended list queries at execute time; aggregates and explicit LIMIT exempt; reduces huge fetch/JSON cost under load |
| generate/revise LIMIT rules | prompt-driven | Aggregates: no LIMIT; explicit N: LIMIT N; "all"/"every": no LIMIT; open-ended lists: ORDER BY + LIMIT 100 |
| verify truncation | TRUNCATED flag in execution | Verifier judges SQL logic on truncated previews, not full row enumeration |
| verify → revise loop | conditional edge | Re-executes after failed verification |
| verify targets | SQL error, 0 rows, wrong columns | Obvious failure modes routed to revise |
| revise | temp 0.2 + unchanged-SQL retry | Avoid repeating the same failing query at temp 0 |
| thinking | disabled at vLLM server | Agent needs short SQL/JSON, not reasoning tokens |

## Phase 4 — SLA iteration log

### Baseline stress test (before new tuning)

| Load test run | Requested RPS | Achieved RPS | OK | Timeouts | HTTP errors | Client errors | Latency p50 | Latency p95 | Latency p99 | Notes |
|---------------|---------------|--------------|----|----------|-------------|---------------|-------------|-------------|-------------|-------|
| run A | 5 | 4.3069 | 1291 | 5 | 195 | 9 | 2.3417s | 17.9090s | 27.8481s | Mostly stable, but p95 above SLA target |
| run B | 10 | 8.3332 | 452 | 1697 | 246 | 605 | 22.8806s | 113.1670s | 118.6886s | Severe saturation and timeout spike |
| run C | 20 | 16.6664 | 298 | 4370 | 116 | 1216 | 28.4747s | 112.6996s | 117.4194s | Intentional overload confirms compute bottleneck |

### Iteration 1 (agent loop reduction)

- Observation: `results/eval_baseline.json` shows `iter_1` and `iter_2` have identical success rate (`0.3333`).
- Decision: reduce `MAX_ITERATIONS` from 3 to 2 to lower per-request LLM work and reduce timeout risk.

**10 RPS load test (run D — MAX_ITERATIONS=2, no row cap):**

| Metric | Value |
|--------|-------|
| Achieved RPS | 8.3332 |
| OK | 916 (30.5%) |
| Timeouts | 1475 |
| HTTP errors | 1 |
| Client errors | 608 |
| Latency p50 / p95 / p99 | 49.7s / 97.2s / 103.9s |

vs baseline run B (MAX_ITERATIONS=3): OK roughly doubled (452 → 916), timeouts down (1697 → 1475), HTTP errors fixed (246 → 1). Still far from SLA — ~63% of requests fail at 10 RPS.

### Iteration 2 (result row limiting)

- Observation: open-ended list queries (e.g. financial accounts before 1997 with balance > 3000 USD) can return thousands of rows, inflating SQLite fetch, JSON response size, and client timeouts.
- Decision: three-layer defense — (1) prompt LIMIT rules in generate/revise, (2) execution hard cap via `COUNT(*)` + `LIMIT 100` subquery wrapper for non-aggregate queries without LIMIT, (3) verifier aware of `TRUNCATED` previews.
- Eval safety: agent stores uncapped SQL in history; eval re-executes `final_sql` without the wrapper.

**10 RPS load test (run E — MAX_ITERATIONS=2 + row cap):**

| Metric | Value | vs run D (iter 1) |
|--------|-------|-------------------|
| Achieved RPS | 8.3332 | flat |
| OK | 1117 (37.2%) | +201 (+22%) |
| Timeouts | 1276 | −199 (−13%) |
| HTTP errors | 1 | flat |
| Client errors | 606 | flat |
| Latency p50 / p95 / p99 | 81.9s / 117.1s / 119.5s | p50 worse (+32s); p95/p99 slightly worse |

**Interpretation:** row cap improved reliability (more requests finish within the 120s client timeout) but did not improve latency percentiles. Likely causes: (1) `COUNT(*)` subquery adds a full scan on uncapped list queries, (2) agent-side wall time still dominated by 2× generate/verify LLM round-trips per request, not vLLM decode alone. Grafana during run E showed vLLM E2E P95 ~3–4s and P99 ~5s with queue at 0 and ~15–25 req/s at the serving layer — bottleneck is the full agent graph under concurrency, not raw model throughput.

**Next candidates:** replace `COUNT(*)` + `LIMIT` with single `LIMIT N+1` probe; push LIMIT into generated SQL more consistently; reduce LLM calls (rule-based verify for obvious failures).
