Synthetic, derived from parser expectations on 2026-08-24, provider CLI
version unknown.

These files model the shape of the `chatgpt.com/backend-api/wham/usage`
response as consumed by `parse_codex_usage(data)` in
`scripts/ai-cli-usage.py`. No real token, email, or account id appears
anywhere; `tester@example.invalid` is a placeholder address on a
reserved-for-documentation domain (RFC 2606). `epoch_reset.json` uses a fixed
far-future Unix timestamp (2030) purely so the fixture never goes stale.
