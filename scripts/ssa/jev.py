"""Typed judgments about a brief, answered by TypeSafe's Jev model.

Two decisions the supervisor used to make by eye, both of which steer real
money: which size/difficulty/kind a brief is (they gate quota floor, reasoning
effort and worker fit), and whether the brief meets the brief contract before a
worker burns a run on it. Jev is a non-generative model: it takes `state` plus
typed questions and returns calibrated probabilities in about a second, so the
answer is a number code can threshold, not prose to parse.

Everything here is advisory and fails open. No key, no network, a 4xx or a
timeout all produce `{"available": false, ...}` and exit code 2; the caller
falls back to the supervisor's own judgment. SSA_JEV=0 turns it off entirely
(the test suite sets that so a developer's real key is never used).

The option KEYS are derived from the routing tables in ai-cli-usage.py
(BASE_FLOOR, DIFFICULTY, FIT); only the level descriptions live here, and
tests/test_jev.py fails when the two drift apart.

Jev reads literally. Each question states the exact condition, gives criteria
for every answer and names the state field in backticks. Counting is kept out
of the model: size levels describe the shape of the change, not a file count.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

API_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"
KEY_ENV = "TYPESAFE_API_KEY"
# State + longest question must stay under 32k tokens; a brief longer than
# this is its own lint finding, and the head carries the contract anyway.
MAX_BRIEF_CHARS = 24000
LOW_CONFIDENCE = 0.5
# Difficulty is the label that moves the quota floor, so a wrong "hard" can
# leave a task with no eligible worker. It gets a stricter bar than the rest
# (measured: hard at 0.72 blocked a dispatch that routine carried).
LOW_CONFIDENCE_BY: dict[str, float] = {"difficulty": 0.8}
LINT_THRESHOLD = 0.5
REVIEW_THRESHOLD = 0.5
# Paths whose hunks a diff review judges. A fact a regex can check.
TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]+$|_test\.[a-z]+$|\.(test|spec)\.[a-z]+$"
)

# Ordered weakest to strongest; keys must equal BASE_FLOOR's.
SIZE_LEVELS: dict[str, str] = {
    "tiny": "A single small edit in one place: one function, one config value, one line of docs",
    "small": "A contained change to one module or a couple of closely related files",
    "medium": "A feature or fix that spans several files across more than one module, usually with tests",
    "large": "A sweeping change across many files or packages: a port, a repo-wide rename, a migration, a new subsystem",
}

# Ordered weakest to strongest; keys must equal DIFFICULTY's.
DIFFICULTY_LEVELS: dict[str, str] = {
    "trivial": "Mechanical work with no decisions: renames, format conversions, regenerating a table, applying a stated pattern",
    "routine": "Ordinary engineering with a known approach: a typical feature, a test to an existing pattern, a straightforward bug",
    "hard": "Needs careful reasoning: concurrency, subtle invariants, a bug with no obvious cause, a tricky algorithm or a risky refactor",
    "frontier": "Open-ended or research-grade: novel design with no known approach, correctness that is hard to verify, high cost if wrong",
}

# Keys must equal FIT's.
KIND_OPTIONS: dict[str, str] = {
    "impl": "Write or change product code to add or alter behaviour",
    "review": "Read existing code or a diff and report findings without changing it",
    "debug": "Find the cause of a failure, crash or wrong result, and fix it",
    "analysis": "Investigate, measure, compare or plan; the deliverable is a report or a document, not a code change",
    "best_of_n": "The brief asks for several independent attempts at the same task so the best one can be chosen",
    "default": "None of the other options describes the brief",
}

# Brief contract (agents/smart-subagents.md). `required` elements missing from
# a brief are findings; the rest are reported but never fail the lint.
LINT_QUESTIONS: dict[str, dict[str, Any]] = {
    "goal": {
        "required": True,
        "instructions": "Does `brief` state one concrete goal: what must exist or behave differently when the work is finished?",
        "true": "A specific outcome is named, such as a behaviour, a file, an endpoint or a passing command",
        "false": "The goal is absent, or is only a vague wish such as improve, clean up or look into",
    },
    "scope_in": {
        "required": True,
        "instructions": "Does `brief` say which files, directories, modules or components the worker is expected to change?",
        "true": "Names paths, modules or components that are in scope",
        "false": "Leaves the worker to guess where to work",
    },
    "scope_out": {
        "required": True,
        "instructions": "Does `brief` say what the worker must not touch or must leave out of the change?",
        "true": "Names files, areas, behaviours or kinds of change that are out of scope or forbidden",
        "false": "States no boundary on what may be changed",
    },
    "acceptance": {
        "required": True,
        "instructions": "Does `brief` give acceptance criteria that a third party could check as true or false?",
        "true": "Lists observable conditions: a test passes, a command prints a given result, a file contains a given thing",
        "false": "Gives no criteria, or only subjective ones such as works well or is clean",
    },
    "verify": {
        "required": True,
        "instructions": "Does `brief` give at least one exact shell command the worker must run to verify its own work?",
        "true": "Contains a runnable command such as a test, build or lint invocation",
        "false": "Mentions testing only in general terms, or not at all",
    },
    "needs_answers": {
        "required": False,
        "instructions": "Does `brief` leave a decision open that the worker would have to ask a person about before it could finish?",
        "true": "Contains an unresolved question, a TBD, or a choice between options with no rule for choosing",
        "false": "Every decision the worker needs is already made in the brief",
    },
}


# Post-run review of what a worker did to EXISTING tests. A green verify only
# proves the commands pass; it cannot tell a fixed bug from a test bent to fit.
REVIEW_QUESTIONS: dict[str, dict[str, str]] = {
    "inverts_assertion": {
        "instructions": "In `diff`, is there a removed line and an added line that assert opposite things about the same subject?",
        "true": "An existing assertion was flipped to its opposite, such as assertNotIn replaced by assertIn, assertFalse by assertTrue, toBe(false) by toBe(true), == by !=, or not-raises by raises",
        "false": "No existing assertion was flipped; assertions were only added, renamed, retargeted at a renamed symbol, or left alone",
    },
    "loosens_threshold": {
        "instructions": "In `diff`, is an existing numeric check replaced by one that is easier to pass, or removed without a numeric replacement?",
        "true": "A bound, tolerance, count or timeout in an existing assertion was widened, or a numeric assertion was deleted or swapped for a non-numeric one",
        "false": "Every existing numeric check is unchanged or stricter, or the diff contains no numeric checks",
    },
    "disables_test": {
        "instructions": "In `diff`, is an existing test deleted, skipped, marked expected-failure, or emptied of its assertions?",
        "true": "A test function or case was removed, given a skip or xfail marker, commented out, or reduced to a body that asserts nothing",
        "false": "Every test that existed before still runs and still asserts something",
    },
}


class JevUnavailable(Exception):
    """No key, disabled, or the service could not be reached in time."""


def enabled() -> bool:
    return os.environ.get("SSA_JEV", "1") not in ("0", "false", "off", "no")


def key_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "typesafe" / "env"


def load_key() -> Optional[str]:
    """TYPESAFE_API_KEY from the environment, else from ~/.config/typesafe/env."""
    key = (os.environ.get(KEY_ENV) or "").strip()
    if key:
        return key
    try:
        for line in key_file().read_text().splitlines():
            m = re.match(rf"\s*(?:export\s+)?{KEY_ENV}\s*=\s*(.+?)\s*$", line)
            if m:
                return m.group(1).strip("'\"") or None
    except OSError:
        return None
    return None


def ask(state: Any, questions: dict[str, dict], timeout: float = 8.0) -> dict:
    """One System One call. Raises JevUnavailable on anything but a 200."""
    if not enabled():
        raise JevUnavailable("disabled by SSA_JEV=0")
    key = load_key()
    if not key:
        raise JevUnavailable(f"no {KEY_ENV} in the environment or {key_file()}")
    body = json.dumps(
        {
            "state": state,
            "model": os.environ.get("SSA_JEV_MODEL", DEFAULT_MODEL),
            "questions": questions,
        }
    ).encode()
    url = os.environ.get("SSA_JEV_URL", API_URL)
    last = "no attempt made"
    for attempt in (1, 2):
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "User-Agent": "smart-subagents-jev/1",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
            if exc.code in (429, 503, 529) and attempt == 1:
                try:
                    wait = float(exc.headers.get("retry-after") or 1.0)
                except ValueError:
                    wait = 1.0
                time.sleep(min(max(wait, 0.2), 2.0))
                continue
            break
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            last = f"{type(exc).__name__}: {exc}"
            break
    raise JevUnavailable(last)


def _score_question(instructions: str, levels: dict[str, str]) -> dict:
    return {"type": "score", "instructions": instructions, "criteria": list(levels.values())}


def _score_label(answer: dict, levels: dict[str, str]) -> tuple[str, float]:
    """The most probable level's key. The expectation can land between two
    levels, and Jev's scores are not numerically calibrated, so argmax it is."""
    names = list(levels)
    probs = answer.get("probabilities") or {}
    if probs:
        idx = max(range(len(names)), key=lambda i: float(probs.get(str(i), 0.0)))
    else:
        idx = min(len(names) - 1, max(0, round(float(answer.get("score", 0)))))
    return names[idx], float(answer.get("confidence", 0.0))


def _runner_up(answer: dict, levels: dict[str, str]) -> Optional[str]:
    """The second most probable level, so a shaky label comes with its rival."""
    names = list(levels)
    probs = answer.get("probabilities") or {}
    ranked = sorted(range(len(names)), key=lambda i: float(probs.get(str(i), 0.0)), reverse=True)
    if len(ranked) < 2 or float(probs.get(str(ranked[1]), 0.0)) <= 0.0:
        return None
    return names[ranked[1]]


def _clip(text: str) -> str:
    return text if len(text) <= MAX_BRIEF_CHARS else text[:MAX_BRIEF_CHARS]


def classify_brief(text: str) -> dict:
    """size / difficulty / kind for a brief, with the confidence of each."""
    questions = {
        "size": _score_question(
            "How much of the codebase does the work described in `brief` change?", SIZE_LEVELS
        ),
        "difficulty": _score_question(
            "How much reasoning does the work described in `brief` demand, regardless of how many files it touches?",
            DIFFICULTY_LEVELS,
        ),
        "kind": {
            "type": "choice",
            "instructions": "Which kind of task does `brief` describe?",
            "criteria": KIND_OPTIONS,
        },
    }
    out = ask({"brief": _clip(text)}, questions)
    ans = out.get("answers") or {}
    size, size_c = _score_label(ans.get("size") or {}, SIZE_LEVELS)
    diff, diff_c = _score_label(ans.get("difficulty") or {}, DIFFICULTY_LEVELS)
    kind_a = ans.get("kind") or {}
    kind = kind_a.get("choice") if kind_a.get("choice") in KIND_OPTIONS else "default"
    kind_c = float(kind_a.get("confidence", 0.0))
    conf = {"size": round(size_c, 3), "difficulty": round(diff_c, 3), "kind": round(kind_c, 3)}
    low = sorted(k for k, v in conf.items() if v < LOW_CONFIDENCE_BY.get(k, LOW_CONFIDENCE))
    rivals = {
        "size": _runner_up(ans.get("size") or {}, SIZE_LEVELS),
        "difficulty": _runner_up(ans.get("difficulty") or {}, DIFFICULTY_LEVELS),
    }
    return {
        "available": True,
        "size": size,
        "difficulty": diff,
        "kind": kind,
        "confidence": conf,
        "low_confidence": low,
        "runner_up": {k: rivals[k] for k in low if rivals.get(k)},
        "flags": f"--size {size} --difficulty {diff} --kind {kind}",
        "model": out.get("model"),
        "input_tokens": (out.get("usage") or {}).get("input_tokens"),
    }


def lint_brief(text: str) -> dict:
    """Which brief-contract elements are missing. Code answers what code can."""
    questions = {
        qid: {
            "type": "noul",
            "instructions": q["instructions"],
            "criteria": {"true": q["true"], "false": q["false"]},
        }
        for qid, q in LINT_QUESTIONS.items()
    }
    out = ask({"brief": _clip(text)}, questions)
    ans = out.get("answers") or {}
    scores = {qid: round(float((ans.get(qid) or {}).get("noul", 0.0)), 3) for qid in LINT_QUESTIONS}
    missing = [
        qid
        for qid, q in LINT_QUESTIONS.items()
        if q["required"] and scores[qid] < LINT_THRESHOLD
    ]
    warnings = []
    if scores["needs_answers"] >= LINT_THRESHOLD:
        warnings.append("needs_answers: the worker cannot ask mid-run; decide it in the brief")
    # An absolute workdir is a fact a regex can check; no model needed.
    if not re.search(r"(?m)(^|[\s`'\"(])(/[\w.@+-]+){2,}", text):
        missing.append("workdir")
    # dispatch refuses a brief without this section (_ssa_require_structural);
    # say so here, so a brief that lints clean is a brief that dispatches.
    if os.environ.get("SSA_STRUCTURAL_LEGACY") != "1" and not (
        re.search(r"(?m)^## Structural (discovery|context)[ \t]*$", text)
        and re.search(r"(?m)^CGC(-SKIP)?:[ \t]+\S", text)
    ):
        missing.append("structural")
    if len(text) > MAX_BRIEF_CHARS:
        warnings.append(f"brief is {len(text)} chars; only the first {MAX_BRIEF_CHARS} were judged")
    return {
        "available": True,
        "ok": not missing,
        "missing": missing,
        "warnings": warnings,
        "scores": scores,
        "model": out.get("model"),
        "input_tokens": (out.get("usage") or {}).get("input_tokens"),
    }


def test_hunks(diff: str) -> tuple[str, list[str]]:
    """The per-file sections of a unified diff that touch EXISTING test files."""
    kept, paths = [], []
    for section in re.split(r"(?m)^(?=diff --git )", diff):
        m = re.match(r"diff --git a/(\S+) b/(\S+)", section)
        if not m or not TEST_PATH_RE.search(m.group(2)):
            continue
        # A brand new test file cannot weaken a test that did not exist.
        if re.search(r"(?m)^new file mode ", section) or not re.search(r"(?m)^-(?!--)", section):
            continue
        kept.append(section)
        paths.append(m.group(2))
    return "".join(kept), paths


def review_diff(diff: str) -> dict:
    """Did the worker bend existing tests? Advisory; never a verdict."""
    hunks, paths = test_hunks(diff)
    if not hunks:
        return {"available": True, "reviewed": False, "ok": True, "flags": [],
                "reason": "no existing test file lost a line"}
    questions = {
        qid: {
            "type": "noul",
            "instructions": q["instructions"],
            "criteria": {"true": q["true"], "false": q["false"]},
        }
        for qid, q in REVIEW_QUESTIONS.items()
    }
    out = ask({"diff": _clip(hunks)}, questions)
    ans = out.get("answers") or {}
    scores = {qid: round(float((ans.get(qid) or {}).get("noul", 0.0)), 3) for qid in REVIEW_QUESTIONS}
    flags = [qid for qid in REVIEW_QUESTIONS if scores[qid] >= REVIEW_THRESHOLD]
    warnings = []
    if len(hunks) > MAX_BRIEF_CHARS:
        warnings.append(f"test diff is {len(hunks)} chars; only the first {MAX_BRIEF_CHARS} were judged")
    return {
        "available": True,
        "reviewed": True,
        "ok": not flags,
        "flags": flags,
        "scores": scores,
        "files": paths,
        "warnings": warnings,
        "model": out.get("model"),
        "input_tokens": (out.get("usage") or {}).get("input_tokens"),
    }


def unavailable(reason: str) -> dict:
    return {"available": False, "reason": reason}
