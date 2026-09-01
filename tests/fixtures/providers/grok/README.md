Synthetic, derived from parser expectations on 2026-08-24, provider CLI
version unknown.

These files model the shape of the `cli-chat-proxy.grok.com/v1/billing` and
`.../v1/user` responses as consumed by `parse_grok_usage(billing, user)` in
`scripts/ai-cli-usage.py`. `healthy_root.json` and `healthy_nested.json`
cover the two billing shapes the real API is known to return (fields at the
root vs. nested under `"config"`). No real token, email, or account id
appears anywhere; `tester@example.invalid` is a placeholder address on a
reserved-for-documentation domain (RFC 2606).

`zero_limit.json` is the shape the live account actually returns
(`monthlyLimit.val = 0`, `used.val = 0`): a meter that reports nothing, not a
month with everything still to spend. It must read as "usage data missing".
