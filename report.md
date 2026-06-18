## Phase 1 — vLLM serving config
Model: Qwen/Qwen3-30B-A3B-Instruct-2507
Hardware: 1× H100 80GB
| Flag | Value | Justification |
|------|-------|---------------|
| --gpu-memory-utilization | 0.9 | Leave headroom for KV cache while maximizing concurrent agent runs |
| --max-model-len | 8192 | Prompts ~2K tokens; shorter max frees KV for more seqs |
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
