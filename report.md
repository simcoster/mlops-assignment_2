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
- Scope: documentation only in this iteration; code/config changes to follow separately.
