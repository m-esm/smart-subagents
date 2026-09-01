"""Worker adapters: registry entry plus a task context -> a runnable command.

Everything here is pure. It builds an argv, says where stdin comes from, where
the process should run and whether its environment must be scrubbed, then
reads a session id back out of the log and classifies a failure. No process is
ever started from this module, and no argv is ever handed to a shell.
"""

from __future__ import annotations

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
FAILURE_CLASSES = ("rate-limit", "auth", "unknown")

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


def _effort_tokens(spec, ctx: Dict[str, Any]) -> List[str]:
    """Tuning tokens for the {effort} slot.

    An explicit `args` list (what the recommender already emitted, verbatim)
    always wins: the difficulty-to-flag mapping has exactly one owner, and it
    is not this module. Otherwise the rung is rendered through the registry's
    effort_flags template.
    """
    if ctx.get("args") is not None:
        return [str(a) for a in ctx["args"]]
    rung = str(ctx.get("effort") or "")
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


def build_command(worker: str, mode: str, ctx: Dict[str, Any], reg=None) -> Dict[str, Any]:
    """Registry entry + context -> {argv, stdin, cwd, env_scrub, ...}."""
    spec = _spec(worker, reg)
    template = spec.argv_for(mode)
    prompt = _prompt_value(spec, mode, ctx)
    effort_tokens = _effort_tokens(spec, ctx)
    model_tokens = _model_tokens(spec, ctx)

    scalars = {
        "worktree": str(ctx.get("worktree") or ""),
        "brief": str(ctx.get("brief") or ""),
        "output": str(ctx.get("output") or ""),
        "session_id": str(ctx.get("session_id") or ""),
        "prompt": prompt if prompt is not None else "",
    }

    argv: List[str] = []
    for token in template:
        if token == "{effort}":
            argv.extend(effort_tokens)
            continue
        if token == "{model}":
            argv.extend(model_tokens)
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

    return {
        "worker": spec.name,
        "mode": mode,
        "bin": spec.resolve_binary(ctx.get("env")),
        "argv": argv,
        "stdin": stdin_path,
        "cwd": cwd,
        "env_scrub": spec.env_scrub,
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
    """rate-limit | auth | unknown | None (the run did not fail).

    Conservative on purpose: a wrong classification benches a healthy worker
    for every other task, so anything not clearly a limit or a credential
    problem stays "unknown" and sets no cooldown.
    """
    try:
        code = int(exit_code)
    except (TypeError, ValueError):
        code = 1
    if code == 0:
        return None
    text = (log_tail or "").lower()
    if RATE_LIMIT_RE.search(text):
        return "rate-limit"
    if AUTH_RE.search(text):
        return "auth"
    return "unknown"


def _error_text(obj: dict) -> str:
    """The error message an object carries, "" when it carries none.

    Only terminal error envelopes count. Everything else in a worker log is
    the repo's own content passing through: a tool_result holding a grep hit
    on "test_lattices.py:429:", an assistant paragraph quoting an HTTP status,
    a UUID with "429c" in it. None of that says anything about the account.
    """
    t = str(obj.get("type") or "")
    err = obj.get("error")

    def _msg(value) -> str:
        if isinstance(value, dict):
            return str(value.get("message") or "")
        return str(value or "")

    if t in _ERROR_TYPES:
        return (
            _msg(obj.get("message"))
            or _msg(err)
            or str(obj.get("content") or "")
        )
    if t == "result":
        subtype = str(obj.get("subtype") or "")
        if obj.get("is_error") is True or "error" in subtype:
            result = obj.get("result")
            return (result if isinstance(result, str) else "") or _msg(err)
        return ""
    if t.startswith("item."):
        item = obj.get("item") if isinstance(obj.get("item"), dict) else {}
        if str(item.get("type") or "") == "error":
            return str(item.get("message") or "")
        return ""
    # kimi's stream-json has no "type" on most lines; its meta frames do, and
    # only the ones that name an error are evidence (session.resume_hint is
    # a meta frame too, and it is not a failure).
    if "error" in t:
        return _msg(obj.get("message")) or _msg(err) or str(obj.get("content") or "")
    return ""


def error_strings(text: str) -> List[str]:
    """Every terminal error message in a log, in order."""
    return [msg for obj in _iter_json_lines(text) if (msg := _error_text(obj))]


def classify_log(exit_code: int, log_path: str, lines: int = 40) -> Optional[str]:
    """Classify a run from its log: error envelopes first, raw tail only if none.

    A structured log is read structurally. Falling back to the raw tail for a
    JSON log is what let a grep hit set a 24 h cooldown on a healthy worker.
    """
    text = ""
    try:
        with open(log_path, "r", errors="replace") as fh:
            text = fh.read()
    except OSError:
        text = ""
    has_json = any(True for _ in _iter_json_lines(text))
    if has_json:
        return classify_failure(exit_code, "\n".join(error_strings(text)))
    tail = "\n".join(text.splitlines()[-lines:])
    return classify_failure(exit_code, tail)


def scrub_env(env: Optional[dict] = None) -> Dict[str, str]:
    """The four variables a scrubbed worker keeps. Mirrors `env -i` in the shell."""
    env = os.environ if env is None else env
    return {
        "HOME": env.get("HOME", ""),
        "PATH": env.get("PATH", ""),
        "TMPDIR": env.get("TMPDIR", "/tmp"),
        "TERM": env.get("TERM", "dumb"),
    }
