"""Advisory decision history and aggregate outcome joins, never task text."""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

FIELDS = {
    "classify": ("size", "difficulty", "kind", "confidence", "effective", "runner_up", "low_confidence"),
    "lint": ("scores", "missing"),
    "review": ("scores", "flags"),
}


def state_path(variable, filename):
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    return Path(os.environ.get(variable) or base / "smart-subagents" / filename)


def append_decision(task_id, stage, action, result):
    if not task_id or not result.get("available") or result.get("reviewed") is False:
        return
    try:
        from .jev import DEFAULT_MODEL
        row = {"schema_version": 1, "ts": datetime.now(timezone.utc).isoformat(),
               "task_id": task_id, "stage": stage,
               "model": os.environ.get("SSA_JEV_MODEL", DEFAULT_MODEL)}
        row.update({key: result[key] for key in FIELDS[stage]})
        row["action"] = action
        line = json.dumps(row) + "\n"
        path = state_path("SSA_JEV_DECISIONS", "jev-decisions.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as stream:
            stream.write(line)
    except Exception:
        # Observability must never affect the judgment or its exit status.
        pass


def rows(path):
    try:
        with path.open() as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    if isinstance(row, dict):
                        yield row
                except ValueError:
                    continue
    except OSError:
        return


def timestamp(row):
    try:
        value = datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
        return value if value.tzinfo else None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def summarize(outcomes):
    passed = sum(row.get("outcome") == "verified-pass" and row.get("verification_passed") is True
                 for row in outcomes)
    return {"joined_outcomes": len(outcomes), "verified_pass": passed,
            "partial": sum(row.get("outcome") == "partial" for row in outcomes),
            "verified_pass_rate": passed / len(outcomes) if outcomes else None}


def tune(days=30):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    latest = {}
    for row in rows(state_path("SSA_JEV_DECISIONS", "jev-decisions.jsonl")):
        ts = timestamp(row)
        stage, task = row.get("stage"), row.get("task_id")
        if stage not in FIELDS or not isinstance(task, str) or not ts or ts < cutoff:
            continue
        key = (stage, task)
        if key not in latest or ts >= latest[key][0]:
            latest[key] = (ts, row)
    outcomes = {}
    for row in rows(state_path("SSA_LEDGER", "outcomes.jsonl")):
        task = row.get("task_id")
        if isinstance(task, str):
            # record is append-only; the last record is the current outcome.
            outcomes[task] = row
    result = {"days": days, "stages": {}}
    for stage in FIELDS:
        decisions = [row for (s, _), (_, row) in latest.items() if s == stage]
        joined = [(row, outcomes[row["task_id"]]) for row in decisions if row["task_id"] in outcomes]
        stats = {"decisions": len(decisions), "joined_outcomes": len(joined)}
        def group(predicate):
            return summarize([outcome for row, outcome in joined if predicate(row)])
        if stage == "classify":
            difficulties = sorted({row.get("effective", {}).get("difficulty", "unknown") for row in decisions})
            stats["effective_difficulty"] = {
                key: group(lambda row: row.get("effective", {}).get("difficulty", "unknown") == key)
                for key in difficulties}
            stats["low_confidence"] = {str(flag).lower(): group(
                lambda row: bool(row.get("low_confidence")) == flag) for flag in (True, False)}
        elif stage == "lint":
            elements = sorted({name for row in decisions for name in
                               list(row.get("scores", {})) + row.get("missing", [])})
            stats["elements"] = {name: {
                "missing": group(lambda row: name in row.get("missing", [])),
                "not_missing": group(lambda row: name not in row.get("missing", []))}
                for name in elements}
        else:
            stats["flagged"] = {str(flag).lower(): group(
                lambda row: bool(row.get("flags")) == flag) for flag in (True, False)}
        result["stages"][stage] = stats
    return result


def format_report(report):
    if not any(stage["decisions"] for stage in report["stages"].values()):
        return "no decisions"
    lines = ["stage decisions joined-outcomes"]
    for name, stage in report["stages"].items():
        lines.append("%s %d %d" % (name, stage["decisions"], stage["joined_outcomes"]))
    lines.append("group joined-outcomes verified-pass partial pass-rate")
    def emit(prefix, node):
        if "verified_pass_rate" in node:
            rate = node["verified_pass_rate"]
            lines.append("%s %d %d %d %s" % (prefix, node["joined_outcomes"],
                         node["verified_pass"], node["partial"],
                         "n/a" if rate is None else "%.1f%%" % (rate * 100)))
        else:
            for key, value in node.items():
                if isinstance(value, dict):
                    emit(prefix + "/" + key, value)
    for name, stage in report["stages"].items():
        emit(name, stage)
    return "\n".join(lines)
