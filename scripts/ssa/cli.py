#!/usr/bin/env python3
"""Command line over the smart-subagents runtime.

The shell launcher calls into this for everything it used to know per CLI:
which argv to run, how to read a session id back, how to classify a failure,
and where the task is in its lifecycle.

    python3 scripts/ssa/cli.py registry-validate
    python3 scripts/ssa/cli.py workers [--json]
    python3 scripts/ssa/cli.py build-command --worker W --mode M ... [--nul]
    python3 scripts/ssa/cli.py parse-session --worker W --log PATH
    python3 scripts/ssa/cli.py classify --exit N --log PATH
    python3 scripts/ssa/cli.py event --dir D --phase P [--exit N ...]
    python3 scripts/ssa/cli.py state --dir D
    python3 scripts/ssa/cli.py transition --dir D --to STATE

Exit codes: 0 ok, 1 refused (bad registry, illegal transition, unknown worker).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):  # invoked as a path, not as -m ssa.cli
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from ssa import adapters, registry as registry_mod, state  # type: ignore
    from ssa.registry import RegistryError  # type: ignore
    from ssa.adapters import AdapterError  # type: ignore
    from ssa.state import StateError  # type: ignore
else:
    from . import adapters, registry as registry_mod, state
    from .registry import RegistryError
    from .adapters import AdapterError
    from .state import StateError


def _reg(args):
    return registry_mod.load(getattr(args, "registry", None) or None)


def cmd_registry_validate(args) -> int:
    reg = _reg(args)
    print(
        "registry ok: %s (schema %d, %d worker(s): %s)"
        % (
            reg.path,
            registry_mod.SCHEMA_VERSION,
            len(reg.workers),
            ", ".join(reg.names),
        )
    )
    return 0


def cmd_workers(args) -> int:
    reg = _reg(args)
    rows = []
    for name in reg.names:
        spec = reg.get(name)
        row = spec.to_dict()
        row["bin"] = spec.resolve_binary()
        row["auth_file"] = spec.auth_path()
        rows.append(row)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for row in rows:
        # name, display, sandbox, write, probe, binary, credential file. Tab
        # separated because the shell reads it with IFS=$'\t'.
        print(
            "%s\t%s\t%s\t%s\t%s\t%s\t%s"
            % (
                row["name"],
                row["display_name"],
                row["sandbox"],
                "write" if row["write_allowed_default"] else "no-write",
                row["probe"],
                row["bin"] or "-",
                row["auth_file"] or "-",
            )
        )
    return 0


def cmd_build_command(args) -> int:
    reg = _reg(args)
    ctx = {
        "worktree": args.worktree,
        "brief": args.brief,
        "output": args.output,
        "session_id": args.session_id,
        "prompt": args.prompt,
        "effort": args.effort,
        "model": args.model,
    }
    if args.args_file:
        try:
            text = Path(args.args_file).read_text(errors="replace")
        except OSError as exc:
            print("build-command: cannot read %s: %s" % (args.args_file, exc), file=sys.stderr)
            return 1
        ctx["args"] = [line for line in text.splitlines() if line.strip()]
    elif args.arg:
        ctx["args"] = list(args.arg)
    built = adapters.build_command(args.worker, args.mode, ctx, reg=reg)
    if args.nul:
        # Header fields in a fixed order, then argv. The shell reads this with
        # `read -r -d ''`, which is the only bash 3.2 safe way to move a list
        # of arbitrary strings across a process boundary.
        fields = [
            built["bin"],
            built["cwd"],
            built["stdin"] or "",
            "1" if built["env_scrub"] else "0",
            built["output_mode"],
            "1" if built["write_allowed"] else "0",
            built["sandbox"],
        ]
        out = sys.stdout
        for value in fields + built["argv"]:
            out.write(value)
            out.write("\0")
        out.flush()
        return 0
    print(json.dumps(built, indent=2))
    return 0


def cmd_parse_session(args) -> int:
    reg = _reg(args)
    print(adapters.parse_session(args.worker, args.log, reg=reg))
    return 0


def cmd_classify(args) -> int:
    result = adapters.classify_log(args.exit_code, args.log)
    print(result or "")
    return 0


def cmd_event(args) -> int:
    rec = state.append_event(
        args.dir,
        args.phase,
        worker=args.worker,
        pid=args.pid,
        exit=args.exit_code,
        failure_class=args.failure_class,
        artifacts=args.artifact or [],
    )
    if args.quiet:
        return 0
    print(json.dumps(rec))
    return 0


def cmd_state(args) -> int:
    doc = state.load(args.dir)
    if doc is None:
        if args.strict:
            print("state: no task.json in %s" % args.dir, file=sys.stderr)
            return 1
        doc = {
            "schema_version": state.SCHEMA_VERSION,
            "task_id": Path(args.dir).name,
            "state": state.infer_state(args.dir),
            "recorded": False,
        }
    print(json.dumps(doc, indent=2))
    return 0


def cmd_transition(args) -> int:
    doc = state.transition(
        args.dir,
        args.to,
        worker=args.worker,
        pid=args.pid,
        exit=args.exit_code,
        failure_class=args.failure_class,
        session_id=args.session_id,
    )
    if args.quiet:
        return 0
    print(json.dumps({"task_id": doc.get("task_id"), "state": doc.get("state")}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="ssa", description=__doc__.splitlines()[0])
    ap.add_argument(
        "--registry",
        default="",
        help="registry file (default: $SSA_WORKERS_JSON or scripts/workers.json)",
    )
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("registry-validate", help="load and validate the registry")
    p.set_defaults(func=cmd_registry_validate)

    p = sub.add_parser("workers", help="list registered workers")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_workers)

    p = sub.add_parser("build-command", help="render a worker command")
    p.add_argument("--worker", required=True)
    p.add_argument("--mode", required=True, choices=list(registry_mod.MODES))
    p.add_argument("--worktree", default="")
    p.add_argument("--brief", default="")
    p.add_argument("--output", default="")
    p.add_argument("--session-id", dest="session_id", default="")
    p.add_argument("--prompt", default="")
    p.add_argument("--effort", default="")
    p.add_argument("--model", default="")
    p.add_argument(
        "--arg",
        action="append",
        default=[],
        help="pre-computed tuning token, repeatable; fills the {effort} slot",
    )
    p.add_argument(
        "--args-file",
        default="",
        help="file of pre-computed tuning tokens, one per line (worker-args.txt)",
    )
    p.add_argument("--nul", action="store_true", help="NUL-separated output for the shell")
    p.set_defaults(func=cmd_build_command)

    p = sub.add_parser("parse-session", help="scrape a session id from a worker log")
    p.add_argument("--worker", required=True)
    p.add_argument("--log", required=True)
    p.set_defaults(func=cmd_parse_session)

    p = sub.add_parser("classify", help="classify a failed run from its log tail")
    p.add_argument("--exit", dest="exit_code", type=int, required=True)
    p.add_argument("--log", required=True)
    p.set_defaults(func=cmd_classify)

    p = sub.add_parser("event", help="append one lifecycle event")
    p.add_argument("--dir", required=True)
    p.add_argument("--phase", required=True)
    p.add_argument("--worker", default="")
    p.add_argument("--pid", type=int, default=None)
    p.add_argument("--exit", dest="exit_code", type=int, default=None)
    p.add_argument("--failure-class", dest="failure_class", default=None)
    p.add_argument("--artifact", action="append", default=[])
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_event)

    p = sub.add_parser("state", help="print the task record")
    p.add_argument("--dir", required=True)
    p.add_argument("--strict", action="store_true", help="fail when no task.json exists")
    p.set_defaults(func=cmd_state)

    p = sub.add_parser("transition", help="move the task to a new state")
    p.add_argument("--dir", required=True)
    p.add_argument("--to", required=True, choices=list(state.STATES))
    p.add_argument("--worker", default="")
    p.add_argument("--pid", type=int, default=None)
    p.add_argument("--exit", dest="exit_code", type=int, default=None)
    p.add_argument("--failure-class", dest="failure_class", default=None)
    p.add_argument("--session-id", dest="session_id", default=None)
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_transition)
    return ap


def main(argv=None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return 1
    try:
        return args.func(args)
    except (RegistryError, AdapterError, StateError) as exc:
        print("smart-subagents: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
