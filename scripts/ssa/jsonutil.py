"""Reading JSON out of a worker log that is not pure JSON.

A worker log is whatever the CLI wrote plus whatever leaked onto the same fd:
a node stack trace, a shell warning, a progress line. Both the session scraper
and the digest have to find the real objects in that, so the ladder lives here
once instead of drifting in two copies.
"""

from __future__ import annotations

import json
from typing import Iterable, List, Optional


def iter_json_lines(text: str) -> Iterable[dict]:
    """Every line that is a JSON object, in order. Non-JSON lines are skipped."""
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            yield obj


def non_json_lines(text: str) -> List[str]:
    """The non-empty lines that are not JSON objects, in order.

    With 2>&1 these are the crash: a stack trace, a resolver error, a "command
    not found". Dropping them silently is how a run that never started reads
    as a run that produced nothing.
    """
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("{"):
            try:
                if isinstance(json.loads(stripped), dict):
                    continue
            except ValueError:
                pass
        out.append(stripped)
    return out


def load_whole_json(text: str, keys: Optional[List[str]] = None) -> Optional[dict]:
    """The log's JSON object, tolerating noise around it.

    Ladder: the whole text, then from the first "{", then from the last "{",
    then a per-line scan taking the last object that carries one of `keys`
    (any object, when no keys are named). One trailing stderr line after a
    single-line JSON object used to defeat every rung but the last.
    """
    stripped = text.strip()
    candidates = [stripped]
    if "{" in stripped:
        candidates.append(stripped[stripped.find("{"):])
        candidates.append(stripped[stripped.rfind("{"):])
    for candidate in candidates:
        if not candidate:
            continue
        try:
            obj = json.loads(candidate)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        if keys and not any(k in obj for k in keys):
            continue
        return obj
    found = None
    for obj in iter_json_lines(text):
        if not keys or any(k in obj for k in keys):
            found = obj
    return found
