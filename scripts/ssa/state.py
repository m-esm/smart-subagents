"""Task state as data: one authoritative task.json, one append-only
events.jsonl, and an explicit transition table.

Before this, a task's phase was inferred from which files happened to exist,
which cannot tell "the worker is thinking" from "the worker was killed and
nobody wrote anything". The inference still runs as a fallback for task dirs
minted by an older build, but a task that goes through this module carries its
own history.

Every write is atomic (temp file in the same directory, then os.replace) and
serialized by a per-task lock directory, so an interrupted write leaves the
previous record intact rather than a truncated one.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 1

TASK_FILE = "task.json"
EVENTS_FILE = "events.jsonl"
LOCK_DIR = ".ssa-state.lock"

# The lifecycle, spelled out. Anything not on this table is a bug in the
# caller, not a state to invent at runtime.
TRANSITIONS: Dict[str, List[str]] = {
    "minted": ["preflighted", "aborted"],
    "preflighted": ["picked", "aborted"],
    # A verdict straight from "picked" is the baseline verify: the supervisor
    # runs the task's own verify commands against the untouched tree before
    # any worker has touched it.
    "picked": ["running", "aborted", "verified", "failed", "inconclusive"],
    "running": ["exited", "aborted", "stalled"],
    # "reported" is bookkeeping, not work, so it is reachable from any end of
    # a run: an env-blocked or stalled dispatch still owes the ledger a line,
    # and losing those lines would quietly bias the learned fit.
    # "running" is the re-dispatch edge: the same task dir runs a second time
    # after a failed or empty first attempt, and the record has to follow it.
    "exited": ["verified", "failed", "inconclusive", "picked", "running",
                "aborted", "reported"],
    # A verdict can be revisited: verify runs again after a retry, and the
    # second answer is allowed to differ from the first.
    "verified": ["reported", "picked", "failed", "inconclusive"],
    "failed": ["reported", "picked", "verified", "inconclusive"],
    "inconclusive": ["reported", "picked", "verified", "failed"],
    "reported": [],
    # A killed or stalled run still exits: the watchdog stamps the state, then
    # dispatch writes the exit code and the diff for the same attempt. Both can
    # also be retried, which goes back through "picked" (task 1788291814-77216
    # sat at "running" forever because neither edge existed).
    "aborted": ["reported", "picked", "exited"],
    "stalled": ["reported", "picked", "running", "exited"],
}
STATES = tuple(TRANSITIONS)
TERMINAL = tuple(s for s, nxt in TRANSITIONS.items() if not nxt)
INITIAL = "minted"


class StateError(Exception):
    """An illegal transition, or a task record that cannot be trusted."""


def _utc(ts: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts if ts is not None else time.time()))


def task_path(task_dir: str) -> Path:
    return Path(task_dir) / TASK_FILE


def events_path(task_dir: str) -> Path:
    return Path(task_dir) / EVENTS_FILE


# ---------------------------------------------------------------------------
# Locking and atomic writes
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def lock(task_dir: str, timeout: float = 10.0, poll: float = 0.05):
    """A per-task lock. mkdir is the portable atomic primitive here."""
    path = Path(task_dir) / LOCK_DIR
    deadline = time.time() + timeout
    while True:
        try:
            os.mkdir(str(path), 0o700)
            break
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise StateError("cannot lock %s: %s" % (task_dir, exc))
            if time.time() >= deadline:
                raise StateError(
                    "task %s is locked by another writer (%s); "
                    "remove it if no writer is alive" % (task_dir, path)
                )
            time.sleep(poll)
    try:
        yield
    finally:
        try:
            os.rmdir(str(path))
        except OSError:
            pass


def _atomic_write_json(path: Path, doc: dict) -> None:
    """Serialize first, then replace. A failure here must not touch `path`."""
    text = json.dumps(doc, indent=2) + "\n"
    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=str(path.parent), prefix=path.name + ".", suffix=".tmp",
            delete=False,
        ) as fh:
            tmp_name = fh.name
            os.chmod(tmp_name, 0o600)
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, str(path))
        tmp_name = ""
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


def _read1(task_dir: str, name: str, default: str = "") -> str:
    try:
        text = (Path(task_dir) / name).read_text(errors="replace")
    except OSError:
        return default
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line
    return default


def infer_state(task_dir: str) -> str:
    """Best guess for a task dir minted before task.json existed."""
    d = Path(task_dir)
    if (d / "outcome-record.json").exists():
        return "reported"
    if (d / "outcome.json").exists():
        try:
            verdict = (
                json.loads((d / "outcome.json").read_text()).get("verify") or {}
            ).get("verdict")
        except Exception:
            verdict = None
        return {"pass": "verified", "fail": "failed"}.get(verdict, "inconclusive")
    if (d / "exit-code.txt").exists():
        return "exited"
    if (d / "brief.md").exists() and _read1(task_dir, "worker.txt"):
        return "picked"
    if _read1(task_dir, "wt.txt"):
        return "preflighted"
    return INITIAL


def load(task_dir: str) -> Optional[dict]:
    try:
        doc = json.loads(task_path(task_dir).read_text())
    except OSError:
        return None
    except ValueError as exc:
        raise StateError("%s is not valid JSON: %s" % (task_path(task_dir), exc))
    if not isinstance(doc, dict):
        raise StateError("%s is not an object" % task_path(task_dir))
    return doc


def _new_record(task_dir: str, state: str) -> dict:
    now = _utc()
    return {
        "schema_version": SCHEMA_VERSION,
        "task_id": _read1(task_dir, "task-id.txt", Path(task_dir).name),
        "repo": _read1(task_dir, "repo.txt"),
        "base_sha": _read1(task_dir, "base-sha.txt"),
        "worker": _read1(task_dir, "worker.txt"),
        "class": {
            "size": _read1(task_dir, "size.txt", "?"),
            "difficulty": _read1(task_dir, "difficulty.txt", "?"),
            "kind": _read1(task_dir, "kind.txt", "?"),
        },
        "state": state,
        "created_at": now,
        "updated_at": now,
        "attempts": [],
    }


def ensure(task_dir: str, state: Optional[str] = None) -> dict:
    """Load the record, creating one from the dir's artifacts when absent."""
    d = Path(task_dir)
    if not d.is_dir():
        raise StateError("no such task dir: %s" % task_dir)
    doc = load(task_dir)
    if doc is not None:
        return doc
    with lock(task_dir):
        doc = load(task_dir)
        if doc is None:
            doc = _new_record(task_dir, state or infer_state(task_dir))
            _atomic_write_json(task_path(task_dir), doc)
    return doc


def _refresh(task_dir: str, doc: dict) -> None:
    """Re-read the flat files a later command may have written."""
    for field, name in (
        ("worker", "worker.txt"),
        ("repo", "repo.txt"),
        ("base_sha", "base-sha.txt"),
    ):
        value = _read1(task_dir, name)
        if value and not doc.get(field):
            doc[field] = value
    worker = _read1(task_dir, "worker.txt")
    if worker:
        doc["worker"] = worker


def transition(task_dir: str, to: str, force: bool = False, **fields: Any) -> dict:
    """Move the task to `to`, or raise. Records an attempt on the way.

    `force` records an edge the table refuses and stamps `"desync": true` on
    the record. It exists so a task whose real state has drifted from the
    record (a killed watchdog, a hand-run retry) ends up with a record that
    says what happened and admits the table was bypassed, rather than a record
    frozen at a state the task left hours ago. An unknown state name is still
    refused: force bypasses the edge, never the vocabulary.
    """
    if to not in TRANSITIONS:
        raise StateError(
            "unknown state %r; known: %s" % (to, ", ".join(sorted(TRANSITIONS)))
        )
    ensure(task_dir)
    with lock(task_dir):
        doc = load(task_dir) or _new_record(task_dir, INITIAL)
        current = str(doc.get("state") or INITIAL)
        if current not in TRANSITIONS:
            raise StateError("task %s is in unknown state %r" % (task_dir, current))
        if to != current and to not in TRANSITIONS[current]:
            if not force:
                raise StateError(
                    "illegal transition %s -> %s (legal from %s: %s)"
                    % (current, to, current, ", ".join(TRANSITIONS[current]) or "nothing")
                )
            doc["desync"] = True
            desyncs = list(doc.get("desyncs") or [])
            desyncs.append({"from": current, "to": to, "at": _utc()})
            doc["desyncs"] = desyncs[-20:]
        _refresh(task_dir, doc)
        changed = to != current
        doc["state"] = to
        doc["updated_at"] = _utc()
        if to == "running" and changed:
            attempt = {
                "n": len(doc.get("attempts") or []) + 1,
                "worker": doc.get("worker") or fields.get("worker") or "",
                "started_at": _utc(),
                "ended_at": None,
                "exit": None,
                "failure_class": None,
                "session_id": None,
            }
            if fields.get("pid") is not None:
                attempt["pid"] = fields["pid"]
            doc.setdefault("attempts", []).append(attempt)
        elif to in ("exited", "aborted", "stalled"):
            attempts = doc.get("attempts") or []
            if attempts:
                last = attempts[-1]
                last["ended_at"] = _utc()
                for key in ("exit", "failure_class", "session_id"):
                    if fields.get(key) is not None:
                        last[key] = fields[key]
        for key in ("verdict", "outcome", "notes"):
            if fields.get(key) is not None:
                doc[key] = fields[key]
        _atomic_write_json(task_path(task_dir), doc)
    return doc


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def read_events(task_dir: str) -> List[dict]:
    out: List[dict] = []
    try:
        text = events_path(task_dir).read_text(errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _next_seq(task_dir: str) -> int:
    highest = 0
    for rec in read_events(task_dir):
        try:
            highest = max(highest, int(rec.get("seq") or 0))
        except (TypeError, ValueError):
            continue
    return highest + 1


def append_event(task_dir: str, phase: str, **fields: Any) -> dict:
    """Append one lifecycle event. seq is monotonic per task."""
    if not phase:
        raise StateError("an event needs a phase")
    d = Path(task_dir)
    if not d.is_dir():
        raise StateError("no such task dir: %s" % task_dir)
    with lock(task_dir):
        rec = {
            "seq": _next_seq(task_dir),
            "ts": _utc(),
            "phase": phase,
            "worker": fields.get("worker") or _read1(task_dir, "worker.txt"),
            "pid": fields.get("pid"),
            "exit": fields.get("exit"),
            "failure_class": fields.get("failure_class"),
            "artifacts": [str(a) for a in (fields.get("artifacts") or [])],
        }
        for key, value in fields.items():
            if key in rec or key == "artifacts":
                continue
            rec[key] = value
        path = events_path(task_dir)
        existed = path.exists()
        with open(str(path), "a") as fh:
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
        if not existed:
            try:
                os.chmod(str(path), 0o600)
            except OSError:
                pass
    return rec


def summary(task_dir: str) -> dict:
    """What `status` and `ls` read: the record, or the inference behind it."""
    doc = load(task_dir)
    if doc is None:
        return {
            "state": infer_state(task_dir),
            "recorded": False,
            "attempts": 0,
            "events": 0,
        }
    return {
        "state": doc.get("state"),
        "recorded": True,
        "attempts": len(doc.get("attempts") or []),
        "events": len(read_events(task_dir)),
        "worker": doc.get("worker"),
        "task_id": doc.get("task_id"),
    }
