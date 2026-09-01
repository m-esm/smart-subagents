"""Bounded views of a worker log.

A worker's stdout.log is a transcript for the disk, not for the supervisor.
Measured on this machine (2026-09-01): grok implement runs leave 1-3 MB of
NDJSON with single lines up to 186 KB (a full assistant message carries the
thinking block and its signature; a tool_result line carries the file the
worker read); codex --json lines reach 420 KB. Anything that hands raw lines
to the caller (`tail -n3`, `tail -f`, cat) therefore pushes hundreds of KB
into the supervisor context per look.

Everything here is pure and byte-capped. `final_message` finds the worker's
last agent message per the registry's `final` rule. `digest` reduces the log
to counts, recent one-line events and that final message. `summarize_line`
turns one NDJSON line into a short line for a live tail.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from . import registry as registry_mod
from .jsonutil import (
    iter_json_lines as _iter_json_lines,
    load_whole_json as _load_whole_json,
    non_json_lines as _non_json_lines,
)

DEFAULT_EVENT_CHARS = 160
DEFAULT_FINAL_CHARS = 800
DEFAULT_MAX_EVENTS = 6
GENERIC_TEXT_KEYS = ("result", "text", "response", "content", "message")


def _spec(worker: str, reg=None):
    reg = reg if reg is not None else registry_mod.load()
    return reg.get(worker)


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _get_path(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _matches(obj: dict, match: Dict[str, Any]) -> bool:
    return all(_get_path(obj, k) == v for k, v in match.items())


def _content_text(content: Any) -> str:
    """Concatenate text blocks of a Claude-style content list (or a string)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(p for p in parts if p)


def _generic_text(obj: dict) -> str:
    """Best-effort agent text from one object of unknown shape."""
    msg = obj.get("message")
    if isinstance(msg, dict):
        role = msg.get("role")
        if role in (None, "assistant"):
            text = _content_text(msg.get("content"))
            if text:
                return text
    if obj.get("role") == "assistant":
        text = _content_text(obj.get("content"))
        if text:
            return text
    # kimi ends every run with {"role":"meta","type":"session.resume_hint",
    # "content":"To resume this session: kimi -r <id>"}. That is transport
    # bookkeeping, and returning it as the final message told the supervisor
    # the worker's last word was a resume hint.
    if obj.get("role") in ("user", "tool", "meta"):
        return ""
    for key in GENERIC_TEXT_KEYS:
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, list):
            text = _content_text(value)
            if text:
                return text
    return ""


def _read(log_path: str) -> str:
    try:
        with open(log_path, "r", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Final message
# ---------------------------------------------------------------------------


def _rule_value(obj: dict, keys: List[str]) -> str:
    for key in keys:
        value = _get_path(obj, key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, dict):
            inner = value.get("message")
            if isinstance(inner, str) and inner.strip():
                return inner
    return ""


def _error_applies(obj: dict, kind: str) -> bool:
    """A json-key error rule only fires on an object that says it failed.

    claude's single result object always carries an "error"-ish shape; only
    is_error / an error subtype distinguishes the run that actually failed.
    """
    if kind != "json-key":
        return True
    if obj.get("is_error") is True:
        return True
    return "error" in str(obj.get("subtype") or "")


def _scan(text: str, fmt: str, rule: Optional[dict], is_error_rule: bool = False):
    """(text, line index) for the last hit of one registry rule.

    The index is the ordinal of the JSON line it came from, so a caller can
    ask whether the error happened after the last agent message or before it.
    """
    rule = rule or {}
    kind = str(rule.get("kind") or "generic")
    keys = [str(k) for k in (rule.get("keys") or [])]
    match = rule.get("match") or {}
    if not keys and kind != "generic":
        return "", -1
    if fmt == "json" or kind == "json-key":
        obj = _load_whole_json(text, keys or None)
        if obj is not None and _error_applies(obj, kind):
            found = _rule_value(obj, keys)
            if not found and not is_error_rule:
                found = _generic_text(obj)
            if found:
                return found.strip(), 0
    if kind == "jsonl-event":
        found, index = "", -1
        for i, obj in enumerate(_iter_json_lines(text)):
            if match and not _matches(obj, match):
                continue
            value = _rule_value(obj, keys)
            if value:
                found, index = value, i
        if found:
            return found.strip(), index
    return "", -1


def error_message_from_text(text: str, fmt: str, rule: Optional[dict]):
    """(message, line index) of the run's terminal error, ("", -1) when none."""
    if not rule:
        return "", -1
    return _scan(text, fmt, rule, is_error_rule=True)


def final_message_from_text(
    text: str, fmt: str, rule: Optional[dict], error_rule: Optional[dict] = None
) -> str:
    """The worker's last agent message, "" when the log has none.

    `rule` is the registry `final` block: {"kind": "json-key"|"jsonl-event"|
    "generic", "keys": [...], "match": {path: value}}. The generic scan is
    always the fallback so a worker without a rule still yields something.
    When `error_rule` matches an event that came after the last agent message,
    the run ended on that error and the message says so first: a stale success
    line from before the failure reads as a completed task.
    """
    found, index = _scan(text, fmt, rule)
    if not found:
        for i, obj in enumerate(_iter_json_lines(text)):
            value = _generic_text(obj)
            if value.strip():
                found, index = value, i
    if not found and fmt != "text":
        # A CLI that reverted to pretty-printed JSON has no parseable lines
        # at all, so the per-line scan above sees nothing.
        obj = _load_whole_json(text)
        if obj is not None:
            value = _generic_text(obj)
            if value.strip():
                found, index = value, 0
    if not found and fmt == "text":
        found, index = text.strip(), 0
    err, err_index = error_message_from_text(text, fmt, error_rule)
    if err and err_index >= index:
        prefix = "[run failed: %s]" % " ".join(err.split())
        tail = found.strip()
        # The error rule and the final rule often read the same object (a
        # result line that is both). Do not print it twice.
        if not tail or tail == err.strip():
            return prefix
        return prefix + "\n\n" + tail
    return found.strip()


def final_message(worker: str, log_path: str, reg=None, mode: str = "implement") -> str:
    spec = _spec(worker, reg)
    fmt = spec.format.get(mode, "text")
    return final_message_from_text(_read(log_path), fmt, spec.final, spec.error)


# ---------------------------------------------------------------------------
# One-line event summaries
# ---------------------------------------------------------------------------


def _tool_use_line(block: dict, limit: int) -> str:
    name = str(block.get("name") or "tool")
    inp = block.get("input")
    detail = ""
    if isinstance(inp, dict):
        for key in ("command", "cmd", "path", "file_path", "pattern", "query", "description"):
            if isinstance(inp.get(key), str):
                detail = inp[key]
                break
        if not detail and inp:
            detail = json.dumps(inp, ensure_ascii=False)
    elif inp is not None:
        detail = str(inp)
    return _clip("tool %s %s" % (name, detail), limit)


def _role_line(obj: dict, limit: int) -> Optional[str]:
    """kimi's stream-json shape: bare role objects, no "type" field.

    {"role":"assistant","content":"<text>","tool_calls":[...]},
    {"role":"tool","tool_call_id":..,"content":..}, and a trailing
    {"role":"meta",...} the supervisor does not need to see.
    """
    role = str(obj.get("role") or "")
    if role == "assistant":
        content = obj.get("content")
        text = content if isinstance(content, str) else _content_text(content)
        out = []
        if text.strip():
            out.append(_clip("assistant: %s" % text, limit))
        for call in obj.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            fn = call.get("function") if isinstance(call.get("function"), dict) else {}
            name = str(fn.get("name") or call.get("name") or "tool")
            detail = fn.get("arguments") or call.get("arguments") or ""
            if not isinstance(detail, str):
                detail = json.dumps(detail, ensure_ascii=False)
            out.append(_clip("tool %s %s" % (name, detail), limit))
        return "\n".join(out) if out else None
    if role == "tool":
        body = obj.get("content")
        if isinstance(body, str):
            size = len(body)
        elif body is None:
            size = 0
        else:
            size = len(json.dumps(body, ensure_ascii=False))
        return "tool_result %d bytes" % size
    return None


def summarize_obj(obj: dict, limit: int = DEFAULT_EVENT_CHARS) -> Optional[str]:
    """One short line for one NDJSON object, or None when it is noise.

    Noise is anything that only exists because the transport streams:
    per-token deltas, message_start/stop framing, progress ticks.
    """
    t = str(obj.get("type") or "")
    if t == "stream_event" or t.startswith("message_") or t.startswith("content_block"):
        return None
    if not t and obj.get("role"):
        return _role_line(obj, limit)
    if t in ("assistant", "user"):
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj
        content = msg.get("content")
        role = msg.get("role") or t
        if isinstance(content, list):
            out = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                bt = block.get("type")
                if bt == "text":
                    out.append(_clip("%s: %s" % (role, block.get("text") or ""), limit))
                elif bt == "tool_use":
                    out.append(_tool_use_line(block, limit))
                elif bt == "tool_result":
                    body = block.get("content")
                    if isinstance(body, str):
                        size = len(body)
                    elif isinstance(body, list):
                        size = len(_content_text(body)) or len(json.dumps(body))
                    else:
                        size = len(json.dumps(body)) if body is not None else 0
                    flag = " error" if block.get("is_error") else ""
                    out.append("tool_result %d bytes%s" % (size, flag))
            return "\n".join(out) if out else None
        if isinstance(content, str) and content.strip():
            return _clip("%s: %s" % (role, content), limit)
        return None
    if t == "result":
        sub = obj.get("subtype") or ("error" if obj.get("is_error") else "done")
        turns = obj.get("num_turns")
        text = obj.get("result") if isinstance(obj.get("result"), str) else ""
        head = "result %s" % sub
        if turns is not None:
            head += " turns=%s" % turns
        return _clip("%s: %s" % (head, text), limit) if text else head
    if t == "system":
        sub = obj.get("subtype") or ""
        return _clip("system %s" % sub, limit)
    if t.startswith("item."):
        item = obj.get("item") if isinstance(obj.get("item"), dict) else {}
        it = str(item.get("type") or "")
        phase = t.split(".", 1)[1]
        if it == "agent_message":
            if phase != "completed":
                return None
            return _clip("assistant: %s" % (item.get("text") or ""), limit)
        if it == "command_execution":
            if phase == "started":
                return _clip("tool bash %s" % (item.get("command") or ""), limit)
            code = item.get("exit_code")
            return "tool_result exit=%s" % code if code is not None else None
        if it == "file_change":
            if phase != "completed":
                return None
            changes = item.get("changes") or []
            paths = [str(c.get("path") or "") for c in changes if isinstance(c, dict)]
            return _clip("file_change %s" % " ".join(paths), limit)
        if it == "error":
            return _clip("error: %s" % (item.get("message") or ""), limit)
        if it == "reasoning":
            return None
        return _clip("%s %s" % (t, it), limit)
    if t in ("turn.failed", "error"):
        err = obj.get("error")
        msg = err.get("message") if isinstance(err, dict) else (err or obj.get("message") or "")
        return _clip("error: %s" % msg, limit)
    if t in ("thread.started", "turn.started", "turn.completed", "session.created",
             "session.resume_hint"):
        return t
    text = _generic_text(obj)
    if text:
        return _clip("assistant: %s" % text, limit)
    return _clip(t or "event", limit) if t else None


def summarize_line(line: str, limit: int = DEFAULT_EVENT_CHARS) -> Optional[str]:
    """For a live tail: one raw line in, one short line (or None) out."""
    stripped = line.strip()
    if not stripped:
        return None
    if not stripped.startswith("{"):
        return _clip(stripped, limit)
    try:
        obj = json.loads(stripped)
    except ValueError:
        return _clip(stripped, limit)
    if not isinstance(obj, dict):
        return _clip(stripped, limit)
    return summarize_obj(obj, limit)


# ---------------------------------------------------------------------------
# Digest
# ---------------------------------------------------------------------------


def _terminal_of(obj: dict) -> str:
    """How the run ended, per this object. "" when it is not a terminal event."""
    t = str(obj.get("type") or "")
    if t == "result":
        sub = str(obj.get("subtype") or "")
        if not sub:
            sub = "error" if obj.get("is_error") else "success"
        return "result:%s" % sub
    if t in ("turn.failed", "turn.completed", "error"):
        return t
    if t.startswith("item.") and isinstance(obj.get("item"), dict):
        if str(obj["item"].get("type") or "") == "error":
            return "item.error"
    return ""


def _is_error_terminal(term: str) -> bool:
    if term in ("turn.failed", "error", "item.error"):
        return True
    # subtypes are free text: "error", "error_during_execution", ...
    return term.startswith("result:") and "error" in term


def digest_text(
    text: str,
    fmt: str,
    rule: Optional[dict],
    max_events: int = DEFAULT_MAX_EVENTS,
    event_chars: int = DEFAULT_EVENT_CHARS,
    final_chars: int = DEFAULT_FINAL_CHARS,
    error_rule: Optional[dict] = None,
) -> Dict[str, Any]:
    lines = text.splitlines()
    doc: Dict[str, Any] = {
        "format": fmt,
        "bytes": len(text.encode("utf-8", errors="replace")),
        "lines": len(lines),
        "max_line": max((len(l) for l in lines), default=0),
        "counts": {"assistant": 0, "tool": 0, "tool_result": 0, "error": 0},
        "recent": [],
        "terminal": "",
        "stderr": [],
        "final": "",
        "final_truncated": False,
    }
    if fmt != "text":
        # 2>&1 merges the crash into the log: a node stack trace, a missing
        # binary, a resolver error. Dropping every non-JSON line meant a run
        # that never started digested to nothing at all.
        doc["stderr"] = [
            _clip(line, event_chars) for line in _non_json_lines(text)[-3:]
        ]
    if fmt == "jsonl":
        summaries: List[str] = []
        for obj in _iter_json_lines(text):
            term = _terminal_of(obj)
            if term:
                doc["terminal"] = term
                # summarize_obj already emits an "error:" line for the typed
                # error events, so only a failed result needs counting here.
                if term.startswith("result:") and _is_error_terminal(term):
                    doc["counts"]["error"] += 1
            summary = summarize_obj(obj, event_chars)
            if not summary:
                continue
            for one in summary.split("\n"):
                if one.startswith("assistant:"):
                    doc["counts"]["assistant"] += 1
                elif one.startswith("tool "):
                    doc["counts"]["tool"] += 1
                elif one.startswith("tool_result"):
                    doc["counts"]["tool_result"] += 1
                elif one.startswith("error"):
                    doc["counts"]["error"] += 1
                summaries.append(one)
        doc["recent"] = summaries[-max_events:] if max_events > 0 else []
    elif fmt == "json":
        doc["recent"] = []
        obj = _load_whole_json(text)
        if obj is not None:
            doc["terminal"] = _terminal_of(obj)
            if _is_error_terminal(doc["terminal"]):
                doc["counts"]["error"] += 1
    else:
        tail = [l for l in lines if l.strip()][-max_events:] if max_events > 0 else []
        doc["recent"] = [_clip(l, event_chars) for l in tail]
    final = final_message_from_text(text, fmt, rule, error_rule)
    if final_chars > 0 and len(final) > final_chars:
        doc["final"] = final[:final_chars].rstrip() + "…"
        doc["final_truncated"] = True
    else:
        doc["final"] = final
    return doc


def digest(
    worker: str,
    log_path: str,
    reg=None,
    mode: str = "implement",
    max_events: int = DEFAULT_MAX_EVENTS,
    event_chars: int = DEFAULT_EVENT_CHARS,
    final_chars: int = DEFAULT_FINAL_CHARS,
) -> Dict[str, Any]:
    spec = _spec(worker, reg)
    fmt = spec.format.get(mode, "text")
    doc = digest_text(
        _read(log_path), fmt, spec.final, max_events, event_chars, final_chars,
        error_rule=spec.error,
    )
    doc["worker"] = worker
    doc["log"] = log_path
    return doc


def render(doc: Dict[str, Any]) -> str:
    c = doc.get("counts") or {}
    head = (
        "log: %s bytes, %s lines (longest %s), assistant=%s tool=%s tool_result=%s error=%s"
        % (doc.get("bytes"), doc.get("lines"), doc.get("max_line"),
           c.get("assistant", 0), c.get("tool", 0), c.get("tool_result", 0),
           c.get("error", 0))
    )
    terminal = doc.get("terminal") or ""
    if terminal:
        head += ", terminal=%s" % terminal
    out = [head]
    recent = doc.get("recent") or []
    if recent:
        out.append("recent:")
        out.extend("  | " + line for line in recent)
    stderr = doc.get("stderr") or []
    if stderr:
        out.append("stderr:")
        out.extend("  | " + line for line in stderr)
    final = doc.get("final") or ""
    if final:
        label = "final (truncated)" if doc.get("final_truncated") else "final"
        out.append("%s:" % label)
        out.extend("  " + line for line in final.splitlines())
    else:
        out.append("final: (no agent message in log)")
    return "\n".join(out)
