Synthetic, derived from a real response on 2026-09-22 (limits of the
account then: 500K tokens/min, 30M/h, 720M/day; 1000/60000/1440000
requests). Each file is `{"headers": {...}}` with the lowercased
`x-ratelimit-{limit,remaining}-{tokens,requests}-{minute,hour,day}` headers
as consumed by `parse_cerebras_usage(headers)` in `scripts/ai-cli-usage.py`.
No key, account id or request id appears anywhere. `opencode-run.jsonl` is a
trimmed real `opencode run --format json` log from the same day (session id
and paths rewritten) that pins the registry's session/final/error locators.
