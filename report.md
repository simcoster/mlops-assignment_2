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
