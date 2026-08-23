Synthetic, derived from parser expectations on 2026-08-24, provider CLI
version unknown.

These files model the shape of the `api.kimi.com/coding/v1/usages` and
`.../v1/me` responses as consumed by `parse_kimi_usage(data, me)` in
`scripts/ai-cli-usage.py`. `healthy.json` covers both the weekly quota window
and a throughput sub-window (`limits[]`), matching the "weekly plus
throughput windows" shape the real API returns. No real token, email, or
account id appears anywhere; `"tester"` is a placeholder nickname, not a real
username.
