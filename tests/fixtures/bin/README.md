# fixtures/bin

Fake `codex` / `grok` / `kimi` binaries used by `tests/test_shell.py` to
characterize the shell dispatch contract (`scripts/smart-subagents.sh`)
without ever invoking a real worker CLI or the network.

Synthetic, derived from parser expectations on 2026-08-24, provider CLI
version unknown. Each script records its own argv and cwd under
`$HOME/.ssa-test/<name>/`, then prints output shaped like the corresponding
real CLI's JSON/JSONL output (including a session id, when the test wants
one). Behavior (exit code, whether a session id is emitted) is steered by an
optional `$HOME/.ssa-test-control.json` file the test writes before dispatch.
No real tokens, emails, or account ids appear anywhere in this directory.

`fake-fakecli` is the fourth worker used by `tests/test_registry.py`. Nothing
in `scripts/` knows its name: the conformance test registers it through
`SSA_WORKERS_JSON` alone, which is what proves adding a worker is a registry
entry rather than a code fork. It does not read stdin, because its registry
entry passes the prompt as an argument.
