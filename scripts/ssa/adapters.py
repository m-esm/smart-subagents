"""Worker adapters: registry entry plus a task context -> a runnable command.

Everything here is pure. It builds an argv, says where stdin comes from, where
the process should run and whether its environment must be scrubbed, then
reads a session id back out of the log and classifies a failure. No process is
ever started from this module, and no argv is ever handed to a shell.
"""

from __future__ import annotations

import json
import re
import os
from typing import Any, Dict, List, Optional

from . import registry as registry_mod
from .jsonutil import iter_json_lines as _iter_json_lines, load_whole_json

# A bare 429 or 401 matches a UUID segment, a grep hit like
# "tests/test_lattices.py:429:", an HTTP status quoted in agent prose, or the
# words "invalid token" inside source the worker was reading. Measured on 60
# real logs, 14 misclassified on any nonzero exit, benching a healthy CLI for
# 15 min or 24 h. Both numbers now need a status-ish word in front of them.
RATE_LIMIT_RE = re.compile(
    r"usage limit|resource_exhausted|rate.?limit|too many requests"
    r"|quota exceeded|insufficient_quota|overloaded"
    r"|(?:status|code|http|error)\D{0,6}429\b"
)
AUTH_RE = re.compile(
    r"unauthori[sz]ed|(?:status|code|http|error)\D{0,6}401\b"
    r"|invalid[ _-](token|credential)|please (log ?in|sign ?in)"
    r"|token (has )?expired"
)
# Task ceiling, not an account problem. Checked before RATE_LIMIT_RE so a
# budget halt cannot be benched as a 429.
BUDGET_RE = re.compile(
    r"error_max_budget|max_budget_usd|budget_exhausted|budget.?exhausted"
)
# Stale session id, not a credential problem. Checked before AUTH_RE so a
# message like "no conversation found, please log in" does not bench the
# worker: the worker is healthy and the task dir holds a dead id.
SESSION_MISSING_RE = re.compile(
    r"no conversation found|session not found|no session found"
)
# Live Claude Code 2.1.267 strings. The assigned phrase "requested subagent
# model is restricted" is not in that binary and must not match.
MODEL_DOWNGRADE_RE = re.compile(
    r"ignored: CLAUDE_CODE_SUBAGENT_MODEL_FORCE is set"
    r"|is restricted by your organization's settings"
)
ENV_SUBAGENT_MODEL = "CLAUDE_CODE_SUBAGENT_MODEL"
ENV_SUBAGENT_MODEL_FORCE = "CLAUDE_CODE_SUBAGENT_MODEL_FORCE"
FAILURE_CLASSES = (
    "rate-limit",
    "auth",
    "budget-exhausted",
    "session-missing",
    "unknown",
)

# Where a worker puts the text of a terminal error. Only these strings are
# classified: a tool_result full of a repo's own source is not evidence about
# the CLI's account.
_ERROR_TYPES = ("error", "turn.failed")


class AdapterError(Exception):
    """A command that cannot be built from the registry and this context."""


def _spec(worker: str, reg=None):
    reg = reg if reg is not None else registry_mod.load()
    return reg.get(worker)


def capabilities(worker: str, reg=None) -> Dict[str, Any]:
    """What this worker is allowed to do, straight from the registry."""
    spec = _spec(worker, reg)
    return {
        "worker": spec.name,
        "display_name": spec.display_name,
        "sandbox": spec.sandbox,
        "write_allowed_default": spec.write_allowed_default,
        "env_scrub": spec.env_scrub,
        "cwd": spec.cwd_mode,
        "probe": spec.probe,
        "modes": sorted(spec.argv),
        "effort_ladder": list(spec.effort_ladder),
    }


def _prompt_value(spec, mode: str, ctx: Dict[str, Any]) -> Optional[str]:
    cfg = spec.prompt_for(mode)
    transport = cfg["transport"]
    if transport == "stdin":
        return None
    brief = ctx.get("brief") or ""
    if transport == "arg":
        if ctx.get("prompt"):
            return str(ctx["prompt"])
        if not brief:
            raise AdapterError(
                "%s %s: transport 'arg' needs a brief path or an explicit prompt"
                % (spec.name, mode)
            )
        try:
            with open(brief, "r") as fh:
                text = fh.read()
        except OSError as exc:
            raise AdapterError("%s %s: cannot read brief: %s" % (spec.name, mode, exc))
        # The shell used to interpolate `$(cat brief)`, which strips trailing
        # newlines. Keep that byte-for-byte.
        return text.rstrip("\n")
    if transport == "file-ref":
        if not brief:
            raise AdapterError(
                "%s %s: transport 'file-ref' needs a brief path" % (spec.name, mode)
            )
        return cfg["template"].replace("{brief}", brief)
    raise AdapterError("%s %s: unknown transport %r" % (spec.name, mode, transport))


def _token_matches_effort_slot(tmpl: str, tok: str) -> bool:
    if "{effort}" not in tmpl:
        return tok == tmpl
    prefix, _, suffix = tmpl.partition("{effort}")
    if prefix and not tok.startswith(prefix):
        return False
    if suffix and not tok.endswith(suffix):
        return False
    if prefix and suffix:
        return len(tok) >= len(prefix) + len(suffix)
    return True


def _effort_group_len(spec, args: List[str], i: int) -> int:
    """Length of an effort-flag group starting at i, or 0 if none."""
    flags = spec.effort_flags
    if not flags or i + len(flags) > len(args):
        return 0
    for j, tmpl in enumerate(flags):
        if not _token_matches_effort_slot(tmpl, args[i + j]):
            return 0
    return len(flags)


def _effort_value_from_group(spec, args: List[str], i: int) -> str:
    for j, tmpl in enumerate(spec.effort_flags):
        if "{effort}" not in tmpl:
            continue
        tok = args[i + j]
        prefix, _, suffix = tmpl.partition("{effort}")
        val = tok
        if prefix:
            val = val[len(prefix) :]
        if suffix:
            val = val[: -len(suffix)]
        return val
    return ""


def effort_rung_from_args(spec, args: List[str]) -> str:
    """The effort rung encoded in worker-args. Last matching group wins."""
    if not spec.effort_flags:
        return ""
    found = ""
    i = 0
    while i < len(args):
        span = _effort_group_len(spec, args, i)
        if span:
            found = _effort_value_from_group(spec, args, i)
            i += span
            continue
        i += 1
    return found


def _replace_effort_tokens(spec, args: List[str], rung: str) -> List[str]:
    """Strip every effort group and emit exactly one pair from effort_flags."""
    flags = spec.effort_flags
    if not flags:
        return list(args)
    new_tokens = [t.replace("{effort}", rung) for t in flags]
    out: List[str] = []
    i = 0
    inserted = False
    while i < len(args):
        span = _effort_group_len(spec, args, i)
        if span:
            if not inserted:
                out.extend(new_tokens)
                inserted = True
            i += span
            continue
        out.append(args[i])
        i += 1
    if not inserted:
        out = new_tokens + out
    return out


def resolve_effort_file(spec, ctx: Dict[str, Any]) -> Optional[str]:
    """Rung from $DIR/effort.txt, or None if absent / no directive / unused.

    Workers with empty effort_flags (kimi) ignore the file so it cannot
    inject a flag they do not have. Unknown values raise AdapterError
    naming the file, the bad value, and the worker's effort_ladder.
    """
    path = str(ctx.get("effort_file") or "")
    if not path:
        return None
    try:
        with open(path, "r") as fh:
            text = fh.read()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AdapterError("%s: cannot read effort file %s: %s" % (spec.name, path, exc))

    values = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        values.append((lineno, line))
    if not values:
        has_comment = any(
            raw.strip().startswith("#") for raw in text.splitlines() if raw.strip()
        )
        if has_comment:
            return None
        raise AdapterError(
            "%s: %s is empty; want one effort rung from the ladder (%s)"
            % (spec.name, path, ", ".join(spec.effort_ladder) or "none")
        )
    if len(values) != 1:
        raise AdapterError(
            "%s: %s has %d directives; want one effort rung"
            % (spec.name, path, len(values))
        )
    _lineno, value = values[0]
    if not spec.effort_flags:
        return None
    if spec.effort_ladder and value not in spec.effort_ladder:
        raise AdapterError(
            "%s: %s value %r is not on the effort ladder; accepted: %s"
            % (spec.name, path, value, ", ".join(spec.effort_ladder) or "none")
        )
    return value


def launched_effort(spec, ctx: Dict[str, Any]) -> str:
    """The rung that will be (or was) launched, not the requested override."""
    override = resolve_effort_file(spec, ctx)
    if override is not None:
        return override
    if ctx.get("args") is not None:
        return effort_rung_from_args(spec, [str(a) for a in ctx["args"]])
    return str(ctx.get("effort") or "")


def launched_effort_for_dir(dir_path: str, worker: str = "", reg=None) -> str:
    """The rung a run actually launched with.

    Prefers $DIR/effort-used.txt, which dispatch writes at launch. That file
    is the only trustworthy answer: effort.txt is an input and may have been
    edited since. Falls back to re-deriving for task dirs written before
    effort-used.txt existed.
    """
    used = _first_line(os.path.join(dir_path, "effort-used.txt")).strip()
    if used:
        return used
    worker = (worker or _first_line(os.path.join(dir_path, "worker.txt"))).strip()
    if not worker:
        return ""
    try:
        spec = _spec(worker, reg)
    except Exception:
        return ""
    args_path = os.path.join(dir_path, "worker-args.txt")
    args: List[str] = []
    try:
        with open(args_path, "r", errors="replace") as fh:
            args = [ln for ln in fh.read().splitlines() if ln.strip()]
    except OSError:
        args = []
    ctx = {"args": args, "effort_file": os.path.join(dir_path, "effort.txt")}
    try:
        return launched_effort(spec, ctx)
    except AdapterError:
        return effort_rung_from_args(spec, args)


def launched_model(spec, ctx: Dict[str, Any]) -> str:
    """The model that will be (or was) launched, not a later re-read of inputs.

    Prefers CLAUDE_CODE_SUBAGENT_MODEL from the resolved env_extra (limits.txt
    mapped through env_pass). Else the --model value already in worker-args.
    """
    try:
        extra = resolve_env_extra(spec, ctx)
    except AdapterError:
        extra = {}
    if ENV_SUBAGENT_MODEL in extra:
        return extra[ENV_SUBAGENT_MODEL]
    return _model_from_ctx(ctx)


def launched_model_for_dir(dir_path: str, worker: str = "", reg=None) -> str:
    """The model a run actually launched with.

    Prefers $DIR/model-used.txt, which dispatch writes at launch. That file
    is the only trustworthy answer: limits.txt is an input and may have been
    edited since. Falls back to re-deriving for task dirs written before
    model-used.txt existed.
    """
    used = _first_line(os.path.join(dir_path, "model-used.txt")).strip()
    if used:
        return used
    worker = (worker or _first_line(os.path.join(dir_path, "worker.txt"))).strip()
    if not worker:
        return ""
    try:
        spec = _spec(worker, reg)
    except Exception:
        return ""
    args_path = os.path.join(dir_path, "worker-args.txt")
    args: List[str] = []
    try:
        with open(args_path, "r", errors="replace") as fh:
            args = [ln for ln in fh.read().splitlines() if ln.strip()]
    except OSError:
        args = []
    ctx = {"args": args, "limits": os.path.join(dir_path, "limits.txt")}
    try:
        return launched_model(spec, ctx)
    except AdapterError:
        return _model_from_ctx({"args": args})


def _first_line(path: str) -> str:
    try:
        with open(path, "r", errors="replace") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped:
                    return stripped
    except OSError:
        return ""
    return ""


def _effort_tokens(spec, ctx: Dict[str, Any]) -> List[str]:
    """Tuning tokens for the {effort} slot.

    $DIR/effort.txt overrides the difficulty-derived value already in
    worker-args. Absent file: the args list is passed through unchanged.
    An override strips every existing effort group and splices exactly one
    pair from the registry's effort_flags. Workers with empty effort_flags
    never gain a flag they cannot use.
    """
    override = resolve_effort_file(spec, ctx)
    if ctx.get("args") is not None:
        args = [str(a) for a in ctx["args"]]
        if override is None:
            return args
        return _replace_effort_tokens(spec, args, override)
    rung = override if override is not None else str(ctx.get("effort") or "")
    if not rung or not spec.effort_flags:
        return []
    if spec.effort_ladder and rung not in spec.effort_ladder:
        raise AdapterError(
            "%s: effort %r is not on its ladder (%s)"
            % (spec.name, rung, ", ".join(spec.effort_ladder) or "none")
        )
    return [t.replace("{effort}", rung) for t in spec.effort_flags]


def _model_tokens(spec, ctx: Dict[str, Any]) -> List[str]:
    if ctx.get("args") is not None:
        # Raw args already carry whatever model flag the recommender chose.
        return []
    model = str(ctx.get("model") or "")
    if not model:
        return []
    if not spec.model_flags:
        raise AdapterError("%s: no model flag template, cannot pass %r" % (spec.name, model))
    return [t.replace("{model}", model) for t in spec.model_flags]


def _model_from_ctx(ctx: Dict[str, Any]) -> str:
    """The --model value the recommender already chose, or ctx['model'].

    worker-args.txt is one token per line, so `--model` and its value are
    adjacent entries. A missing pair means the agents JSON omits `model`.
    """
    args = ctx.get("args")
    if args is not None:
        tokens = [str(a) for a in args]
        for i, tok in enumerate(tokens):
            if tok == "--model" and i + 1 < len(tokens):
                return tokens[i + 1]
            if tok.startswith("--model=") and len(tok) > 8:
                return tok[8:]
        return ""
    return str(ctx.get("model") or "")


def task_label(text: str) -> str:
    """Brief label for the session-agent description.

    First markdown heading line if the first non-empty line is a heading,
    otherwise that first non-empty line. Newlines collapsed, trimmed to 200.
    """
    first = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            first = stripped
            break
    if first.startswith("#"):
        first = first.lstrip("#").strip()
    first = " ".join(first.split())
    return first[:200]


def _task_from_brief(brief_path: str) -> str:
    if not brief_path:
        return ""
    try:
        with open(brief_path, "r") as fh:
            text = fh.read()
    except OSError:
        return ""
    return task_label(text)


def agents_payload(spec, ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Inner `--agents` body plus `name`. Empty dict when the worker has none."""
    cfg = spec.agents
    if not cfg:
        return {}
    description = cfg["description"].replace("{task}", _task_from_brief(str(ctx.get("brief") or "")))
    payload: Dict[str, Any] = {
        "name": cfg["name"],
        "description": description,
        "disallowedTools": list(cfg["disallowedTools"]),
        "prompt": cfg["prompt"],
    }
    model = _model_from_ctx(ctx)
    if model:
        payload["model"] = model
    return payload


def agents_json(spec, ctx: Dict[str, Any]) -> str:
    """Compact `--agents` payload for a worker that declares an agents block."""
    payload = agents_payload(spec, ctx)
    if not payload:
        return ""
    name = payload["name"]
    body = {k: v for k, v in payload.items() if k != "name"}
    return json.dumps({name: body}, separators=(",", ":"), sort_keys=True)


_MARKDOWN_FM_ORDER = (
    "name",
    "description",
    "model",
    "disallowedTools",
    "memory",
    "background",
)


def agents_markdown(payload: Dict[str, Any]) -> str:
    """Claude Code agent file: YAML frontmatter plus prompt body.

    Extra file-only keys `memory: project` and `background: true` live here
    and must not appear in the `--agents` JSON. `disallowedTools` is a
    JSON-style list so a YAML parse and json.loads agree on the value.
    """
    name = str(payload.get("name") or "")
    description = str(payload.get("description") or "")
    if not name:
        raise AdapterError("agents.name is empty")
    if not description:
        raise AdapterError("agents.description is empty")
    fm: Dict[str, Any] = {
        "name": name,
        "description": description,
        "disallowedTools": list(payload.get("disallowedTools") or []),
        "memory": "project",
        "background": True,
    }
    if payload.get("model"):
        fm["model"] = payload["model"]
    lines = ["---"]
    for key in _MARKDOWN_FM_ORDER:
        if key not in fm:
            continue
        val = fm[key]
        if isinstance(val, bool):
            rendered = "true" if val else "false"
        else:
            rendered = json.dumps(val)
        lines.append("%s: %s" % (key, rendered))
    lines.append("---")
    lines.append(str(payload.get("prompt") or ""))
    return "\n".join(lines) + "\n"


def write_agents_file(path: str, payload: Dict[str, Any]) -> None:
    """Write a Claude Code agent markdown file. Refuses empty name/description."""
    text = agents_markdown(payload)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)


def write_worktree_claude_agent(task_dir: str, reg=None) -> str:
    """Write `$WT/.claude/agents/ssa-worker.md` from the claude agents payload.

    Skips (returns "") when wt.txt is missing, empty, or NOT_GIT. Model comes
    from worker-args-claude.txt, never from another CLI's worker-args.
    """
    wt_file = os.path.join(task_dir, "wt.txt")
    try:
        with open(wt_file, "r") as fh:
            wt = fh.read().strip()
    except OSError:
        return ""
    if not wt or wt == "NOT_GIT":
        return ""
    args_path = os.path.join(task_dir, "worker-args-claude.txt")
    args: List[str] = []
    try:
        with open(args_path, "r") as fh:
            args = [line for line in fh.read().splitlines() if line.strip()]
    except OSError:
        args = []
    brief_path = os.path.join(task_dir, "brief.md")
    ctx: Dict[str, Any] = {
        "args": args,
        "brief": brief_path if os.path.isfile(brief_path) else "",
    }
    spec = _spec("claude", reg)
    dest = os.path.join(wt, ".claude", "agents", "ssa-worker.md")
    write_agents_file(dest, agents_payload(spec, ctx))
    return dest


_LIMIT_VALUE_RE = re.compile(r"^[0-9]+$")
_BUDGET_VALUE_RE = re.compile(r"^[0-9]+(\.[0-9]+)?$")
def _check_limit_value(spec_name: str, key: str, value: str) -> None:
    """Validate one limits.txt value. Raises AdapterError naming the key."""
    if key == "subagent_model":
        if not value:
            raise AdapterError(
                "%s: limits key %r value is empty" % (spec_name, key)
            )
        if any(ch.isspace() for ch in value):
            raise AdapterError(
                "%s: limits key %r value %r contains whitespace"
                % (spec_name, key, value)
            )
        for ch in registry_mod._META_CHARS:
            if ch in value:
                raise AdapterError(
                    "%s: limits key %r value %r contains the shell metacharacter %r"
                    % (spec_name, key, value, ch)
                )
        for seq in registry_mod._META_SEQS:
            if seq in value:
                raise AdapterError(
                    "%s: limits key %r value %r contains the shell sequence %r"
                    % (spec_name, key, value, seq)
                )
        return
    if key == "subagent_model_force":
        if value != "1":
            raise AdapterError(
                "%s: limits key %r value %r must be exactly 1"
                % (spec_name, key, value)
            )
        return
    if not _LIMIT_VALUE_RE.fullmatch(value):
        raise AdapterError(
            "%s: limits key %r value %r is not a non-negative integer"
            % (spec_name, key, value)
        )


def resolve_env_extra(spec, ctx: Dict[str, Any]) -> Dict[str, str]:
    """Map $DIR/limits.txt through the worker's env_pass.

    Returns the resolved env dict (env var name -> value). Empty when the
    worker declares no env_pass, the file is absent, or it has no directives.
    Unknown keys and illegal values raise AdapterError: silently dropping
    a typo'd limit is the failure that makes a cap look applied when it is not.
    """
    env_pass = spec.env_pass
    if not env_pass:
        return {}
    path = str(ctx.get("limits") or "")
    if not path:
        return {}
    try:
        with open(path, "r") as fh:
            text = fh.read()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise AdapterError("%s: cannot read limits file: %s" % (spec.name, exc))

    accepted = ", ".join(sorted(env_pass))
    resolved: Dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise AdapterError(
                "%s: limits.txt line %d is not key=value" % (spec.name, lineno)
            )
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key not in env_pass:
            raise AdapterError(
                "%s: unknown limits key %r; accepted: %s"
                % (spec.name, key, accepted or "(none)")
            )
        _check_limit_value(spec.name, key, value)
        resolved[env_pass[key]] = value
    return resolved


def resolve_budget(spec, ctx: Dict[str, Any]) -> str:
    """Positive decimal from $DIR/budget.txt, or "".

    Same lexical rules as limits.txt: blank lines and # comments are not
    directives. Absent file, or comments-only: "". Empty/whitespace-only,
    malformed, or non-positive: AdapterError naming the file. A budget that
    silently does not apply is worse than no budget. Workers whose argv
    never uses {budget} ignore the file.
    """
    uses = any("{budget}" in tokens for tokens in spec.argv.values())
    if not uses:
        return ""
    path = str(ctx.get("budget") or "")
    if not path:
        return ""
    try:
        with open(path, "r") as fh:
            text = fh.read()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise AdapterError("%s: cannot read budget file %s: %s" % (spec.name, path, exc))

    values = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        values.append((lineno, line))
    if not values:
        has_comment = any(
            raw.strip().startswith("#") for raw in text.splitlines() if raw.strip()
        )
        if has_comment:
            return ""
        raise AdapterError(
            "%s: %s is empty; want a positive decimal dollar amount" % (spec.name, path)
        )
    if len(values) != 1:
        raise AdapterError(
            "%s: %s has %d directives; want one positive decimal"
            % (spec.name, path, len(values))
        )
    lineno, value = values[0]
    if not _BUDGET_VALUE_RE.fullmatch(value) or float(value) <= 0:
        raise AdapterError(
            "%s: %s line %d value %r is not a positive decimal dollar amount"
            % (spec.name, path, lineno, value)
        )
    return value


def _budget_tokens(spec, ctx: Dict[str, Any]) -> List[str]:
    """Tuning tokens for the {budget} slot. Zero tokens when there is no cap."""
    value = resolve_budget(spec, ctx)
    if not value:
        return []
    if not spec.budget_flags:
        raise AdapterError(
            "%s: no budget flag template, cannot pass %r" % (spec.name, value)
        )
    return [t.replace("{budget}", value) for t in spec.budget_flags]


def resolve_kind(ctx: Dict[str, Any]) -> str:
    """First non-comment, non-blank line of $DIR/kind.txt, or ctx['kind'].

    Unknown values are returned as-is. `fork` splices argv at build time.
    `fanout` is a dispatch-side kind the shell handles before _ssa_build;
    it does not splice argv. Everything else is recommender input.
    Absent file, empty, or comments-only: "".
    """
    path = str(ctx.get("kind_file") or "")
    if not path:
        return str(ctx.get("kind") or "")
    try:
        with open(path, "r") as fh:
            text = fh.read()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise AdapterError("cannot read kind file %s: %s" % (path, exc))
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        return line
    return ""


def _fork_tokens(spec, ctx: Dict[str, Any]) -> List[str]:
    """Tuning tokens for the {fork} slot. Zero tokens unless kind is fork.

    The flag strings come from the registry's fork_flags. Workers with an
    empty list never gain a flag they cannot use. kind.txt values other
    than fork are ignored.
    """
    if resolve_kind(ctx) != "fork":
        return []
    if not spec.fork_flags:
        return []
    return list(spec.fork_flags)


def build_command(worker: str, mode: str, ctx: Dict[str, Any], reg=None) -> Dict[str, Any]:
    """Registry entry + context -> {argv, stdin, cwd, env_scrub, env_keep, env_extra, ...}."""
    spec = _spec(worker, reg)
    template = spec.argv_for(mode)
    prompt = _prompt_value(spec, mode, ctx)
    effort_tokens = _effort_tokens(spec, ctx)
    model_tokens = _model_tokens(spec, ctx)
    budget_tokens = _budget_tokens(spec, ctx) if "{budget}" in template else []
    fork_tokens = _fork_tokens(spec, ctx) if "{fork}" in template else []

    scalars = {
        "worktree": str(ctx.get("worktree") or ""),
        "brief": str(ctx.get("brief") or ""),
        "output": str(ctx.get("output") or ""),
        "session_id": str(ctx.get("session_id") or ""),
        "prompt": prompt if prompt is not None else "",
    }
    agent_name = spec.agents["name"] if spec.agents else ""
    agents_json_text = agents_json(spec, ctx) if spec.agents else ""

    argv: List[str] = []
    for token in template:
        if token == "{effort}":
            argv.extend(effort_tokens)
            continue
        if token == "{model}":
            argv.extend(model_tokens)
            continue
        if token == "{budget}":
            argv.extend(budget_tokens)
            continue
        if token == "{fork}":
            argv.extend(fork_tokens)
            continue
        if token == "{agents}":
            if not agents_json_text:
                raise AdapterError(
                    "%s %s: template needs {agents} but the worker has no agents block"
                    % (spec.name, mode)
                )
            argv.append(agents_json_text)
            continue
        if token == "{agent}":
            if not agent_name:
                raise AdapterError(
                    "%s %s: template needs {agent} but the worker has no agents block"
                    % (spec.name, mode)
                )
            argv.append(agent_name)
            continue
        rendered = token
        for name in registry_mod.SCALAR_PLACEHOLDERS:
            marker = "{%s}" % name
            if marker not in rendered:
                continue
            value = scalars[name]
            if not value:
                raise AdapterError(
                    "%s %s: template needs %s but the context has none"
                    % (spec.name, mode, marker)
                )
            rendered = rendered.replace(marker, value)
        argv.append(rendered)

    transport = spec.prompt_for(mode)["transport"]
    stdin_path = None
    if transport == "stdin":
        stdin_path = str(ctx.get("brief") or "")
        if not stdin_path:
            raise AdapterError(
                "%s %s: transport 'stdin' needs a brief path" % (spec.name, mode)
            )

    cwd = ""
    if spec.cwd_mode == "worktree":
        cwd = str(ctx.get("worktree") or "")
        if not cwd:
            raise AdapterError("%s %s: run.cwd is worktree but none given" % (spec.name, mode))

    parent_env = ctx.get("env")
    # env_keep: parent-sourced allowlist. env_extra: limits.txt via env_pass.
    # Distinct dicts so a reader does not merge the two concepts; they share
    # one KEY VALUE stream on the way to `env -i`.
    env_keep = scrub_env(parent_env, spec) if spec.env_scrub else {}
    return {
        "worker": spec.name,
        "mode": mode,
        "bin": spec.resolve_binary(parent_env),
        "argv": argv,
        "stdin": stdin_path,
        "cwd": cwd,
        "env_scrub": spec.env_scrub,
        "env_keep": env_keep,
        "env_extra": resolve_env_extra(spec, ctx),
        "output_mode": spec.output_mode(mode),
        "write_allowed": spec.write_allowed_default,
        "sandbox": spec.sandbox,
    }


# ---------------------------------------------------------------------------
# Session ids
# ---------------------------------------------------------------------------


def _last_str(obj: dict, keys: List[str], current: str) -> str:
    out = current
    for key in keys:
        value = obj.get(key)
        if isinstance(value, str) and len(value) > 8:
            out = value
    return out


def parse_session(worker: str, log_path: str, reg=None) -> str:
    """Scrape a resumable session id out of a worker log. "" when there is none."""
    spec = _spec(worker, reg)
    rule = spec.session
    try:
        with open(log_path, "r", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return ""
    kind = rule["kind"]
    keys = rule["keys"]
    sid = ""
    if kind == "jsonl-keys":
        for obj in _iter_json_lines(text):
            sid = _last_str(obj, keys, sid)
        return sid
    if kind == "jsonl-event":
        event_key, event = rule["event_key"], rule["event"]
        for obj in _iter_json_lines(text):
            if obj.get(event_key) == event:
                sid = _last_str(obj, keys, sid)
        if sid:
            return sid
        for obj in _iter_json_lines(text):
            sid = _last_str(obj, keys, sid)
        return sid
    if kind == "json-keys":
        # One trailing stderr line after the object defeated every rung of
        # the old two-candidate ladder, so a perfectly resumable claude run
        # reported no session id while its final message read fine.
        obj = load_whole_json(text, keys)
        if obj is not None:
            for key in keys:
                value = obj.get(key)
                if isinstance(value, str) and value:
                    sid = value
        return sid
    return ""


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------


def classify_failure(exit_code: int, log_tail: str) -> Optional[str]:
    """rate-limit | auth | budget-exhausted | session-missing | unknown | None.

    Conservative on purpose: a wrong classification benches a healthy worker
    for every other task, so anything not clearly a limit or a credential
    problem stays "unknown" and sets no cooldown. A task budget halt and a
    missing session id are their own classes: the worker is healthy and must
    not be benched. session-missing is checked before auth so a stale-id
    message that also says "please log in" is not treated as a dead token.
    """
    try:
        code = int(exit_code)
    except (TypeError, ValueError):
        code = 1
    if code == 0:
        return None
    text = (log_tail or "").lower()
    if BUDGET_RE.search(text):
        return "budget-exhausted"
    if RATE_LIMIT_RE.search(text):
        return "rate-limit"
    if SESSION_MISSING_RE.search(text):
        return "session-missing"
    if AUTH_RE.search(text):
        return "auth"
    return "unknown"


def _error_text(obj: dict) -> str:
    """The error message an object carries, "" when it carries none.

    Only terminal error envelopes count. Everything else in a worker log is
    the repo's own content passing through: a tool_result holding a grep hit
    on "test_lattices.py:429:", an assistant paragraph quoting an HTTP status,
    a UUID with "429c" in it. None of that says anything about the account.

    When is_error is true and result/error carry no message (claude's
    budget halt: result is null, error is absent), fall back to subtype
    and terminal_reason rather than returning "". A silent failure is the
    worst possible output of an error classifier, for any subtype.
    """
    t = str(obj.get("type") or "")
    err = obj.get("error")
    subtype = str(obj.get("subtype") or "")

    def _msg(value) -> str:
        if isinstance(value, dict):
            return str(value.get("message") or "")
        return str(value or "")

    def _fallback() -> str:
        if obj.get("is_error") is not True and "error" not in subtype:
            return ""
        parts = [p for p in (subtype, str(obj.get("terminal_reason") or "")) if p]
        return " ".join(parts)

    if t in _ERROR_TYPES:
        return (
            _msg(obj.get("message"))
            or _msg(err)
            or str(obj.get("content") or "")
            or _fallback()
        )
    if t == "result":
        if obj.get("is_error") is True or "error" in subtype:
            result = obj.get("result")
            return (
                (result if isinstance(result, str) else "")
                or _msg(err)
                or _fallback()
            )
        return ""
    if t.startswith("item."):
        item = obj.get("item") if isinstance(obj.get("item"), dict) else {}
        if str(item.get("type") or "") == "error":
            return str(item.get("message") or "") or _fallback()
        return _fallback()
    # kimi's stream-json has no "type" on most lines; its meta frames do, and
    # only the ones that name an error are evidence (session.resume_hint is
    # a meta frame too, and it is not a failure).
    if "error" in t:
        return (
            _msg(obj.get("message"))
            or _msg(err)
            or str(obj.get("content") or "")
            or _fallback()
        )
    return _fallback()


def error_strings(text: str) -> List[str]:
    """Every terminal error message in a log, in order."""
    return [msg for obj in _iter_json_lines(text) if (msg := _error_text(obj))]


def _is_json_line(line: str) -> bool:
    """True when this log line is a JSON object (so not stderr text)."""
    stripped = line.strip()
    if not stripped.startswith("{"):
        return False
    try:
        return isinstance(json.loads(stripped), dict)
    except ValueError:
        return False


def _classify_evidence(log_path: str, lines: int = 40) -> str:
    """The text classify_log searches: error envelopes first, then non-JSON stderr.

    Never a random grep hit inside a tool_result: those are JSON lines that
    error_strings skips unless they are a terminal error envelope.
    """
    text = ""
    try:
        with open(log_path, "r", errors="replace") as fh:
            text = fh.read()
    except OSError:
        text = ""
    has_json = any(True for _ in _iter_json_lines(text))
    if has_json:
        # Do NOT treat an is_error envelope in an exit-0 log as a failure:
        # 19 successful codex runs in this machine's own history carry a
        # "skills context budget" notice, and promoting those to failures
        # writes a bogus failure_class onto every one of them. A real claude
        # budget halt exits 1 (verified against 2.1.267), so rc is enough.
        msgs = error_strings(text)
        if msgs:
            return "\n".join(msgs)
        # A structured log is read structurally, but stderr is in the same
        # file and is never JSON. When the run failed and no JSON line
        # explains why, the non-JSON lines are the only evidence there is:
        # a dead-session resume that still printed a normal envelope would
        # otherwise classify as unknown. Only reached on a nonzero exit, so
        # a successful run's stray output still says nothing.
        plain = [ln for ln in text.splitlines() if not _is_json_line(ln)]
        return "\n".join(plain[-lines:])
    return "\n".join(text.splitlines()[-lines:])


def classify_log(exit_code: int, log_path: str, lines: int = 40) -> Optional[str]:
    """Classify a run from its log: error envelopes first, raw tail only if none.

    A structured log is read structurally. Falling back to the raw tail for a
    JSON log is what let a grep hit set a 24 h cooldown on a healthy worker.
    """
    return classify_failure(exit_code, _classify_evidence(log_path, lines))


def maybe_mark_model_downgrade(dir_path: str, worker: str = "", reg=None) -> bool:
    """Write $DIR/model-downgraded.txt when FORCE-without-MODEL or a log warning.

    The file existing is the signal. Does not bench the worker, does not
    change exit code, does not change failure_class. Returns True when the
    file is present after this call (written or already there).
    """
    path = os.path.join(dir_path, "model-downgraded.txt")
    reason = ""
    worker = (worker or _first_line(os.path.join(dir_path, "worker.txt"))).strip()
    extra: Dict[str, str] = {}
    if worker:
        try:
            spec = _spec(worker, reg)
            extra = resolve_env_extra(
                spec, {"limits": os.path.join(dir_path, "limits.txt")}
            )
        except Exception:
            extra = {}
    if ENV_SUBAGENT_MODEL_FORCE in extra and ENV_SUBAGENT_MODEL not in extra:
        reason = (
            "FORCE-without-MODEL: CLAUDE_CODE_SUBAGENT_MODEL_FORCE is set "
            "without CLAUDE_CODE_SUBAGENT_MODEL; CLI falls back to the main "
            "session model"
        )
    if not reason:
        evidence = _classify_evidence(os.path.join(dir_path, "stdout.log"))
        if MODEL_DOWNGRADE_RE.search(evidence or ""):
            reason = (
                "worker log reported a subagent model override "
                "(CLAUDE_CODE_SUBAGENT_MODEL_FORCE or org restriction)"
            )
    if not reason:
        return os.path.isfile(path)
    if not os.path.isfile(path):
        try:
            with open(path, "w") as fh:
                fh.write(reason + "\n")
        except OSError:
            return False
    return True


# Fallbacks for names that the historical `env -i` line defaulted when
# unset. Unknown keep names (e.g. USER) fall back to "".
_KEEP_DEFAULTS = {"TMPDIR": "/tmp", "TERM": "dumb"}


def scrub_env(env: Optional[dict] = None, spec=None) -> Dict[str, str]:
    """Parent-env values a scrubbed worker keeps.

    Names come from spec.env_keep, or DEFAULT_ENV_KEEP when no spec is
    given. Values come from `env` (the parent). The shell's `env -i` line
    applies this dict plus any env_extra (limits.txt) pairs; it does not
    hardcode names of its own.
    """
    env = os.environ if env is None else env
    names = (
        list(spec.env_keep)
        if spec is not None
        else list(registry_mod.DEFAULT_ENV_KEEP)
    )
    out: Dict[str, str] = {}
    for name in names:
        out[name] = env.get(name, _KEEP_DEFAULTS.get(name, ""))
    return out
