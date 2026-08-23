Synthetic, derived from parser expectations on 2026-08-24, provider CLI
version unknown.

These files model the shape of `GET /api/oauth/usage` and
`GET /api/oauth/profile` responses as consumed by
`parse_claude_usage(data, profile)` in `scripts/ai-cli-usage.py`. No real
token, email, org id, or account id appears anywhere; `tester@example.invalid`
is a placeholder address on a reserved-for-documentation domain (RFC 2606).
