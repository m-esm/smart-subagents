"""Worker registry: load and validate scripts/workers.json.

One entry per worker CLI describes everything that used to be a per-CLI case
arm: how to find its binary, which quota probe reports on it, what it can be
trusted to write, how a prompt reaches it, the exact argv for each mode, how a
session id is scraped out of its output, and how difficulty maps to its own
flags.

Validation fails closed. An argv template that names an unknown placeholder, or
carries a shell metacharacter, is rejected at load time rather than executed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 1

MODES = ("implement", "plan", "resume")
SANDBOXES = ("os", "workspace", "none")
TRANSPORTS = ("stdin", "arg", "file-ref")
SESSION_KINDS = ("jsonl-keys", "json-keys", "jsonl-event")
FINAL_KINDS = ("generic", "json-key", "jsonl-event")
FORMATS = ("jsonl", "json", "text")
OUTPUT_MODES = ("arg", "stdout", "none")
CWD_MODES = ("inherit", "worktree")

# Placeholders an argv template may name. {effort} and {model} splice zero or
# more tokens; every other placeholder resolves to exactly one string.
SCALAR_PLACEHOLDERS = ("worktree", "brief", "output", "session_id", "prompt")
LIST_PLACEHOLDERS = ("effort", "model")
PLACEHOLDERS = SCALAR_PLACEHOLDERS + LIST_PLACEHOLDERS

_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")
# The argv is handed to execve, never to a shell, so a metacharacter in a
# template is either a mistake or an injection attempt. Refuse both.
_META_CHARS = ";|&`\n\r<>"
_META_SEQS = ("$(", "${", "&&", "||", ">>")

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "workers.json"


class RegistryError(Exception):
    """A registry file that cannot be trusted to build an argv."""


def _fail(where: str, msg: str) -> "RegistryError":
    return RegistryError("%s: %s" % (where, msg))


def _check_token(
    where: str, token: Any, allowed: tuple, index: int, standalone_lists: bool = False
) -> None:
    if not isinstance(token, str):
        raise _fail(where, "token %d is %r, not a string" % (index, token))
    for ch in _META_CHARS:
        if ch in token:
            raise _fail(
                where,
                "token %d (%r) contains the shell metacharacter %r"
                % (index, token, ch),
            )
    for seq in _META_SEQS:
        if seq in token:
            raise _fail(
                where,
                "token %d (%r) contains the shell sequence %r" % (index, token, seq),
            )
    for name in _PLACEHOLDER_RE.findall(token):
        if name not in allowed:
            raise _fail(
                where,
                "token %d (%r) uses unknown placeholder {%s}; allowed: %s"
                % (index, token, name, ", ".join(allowed)),
            )
    if standalone_lists and any(
        ("{%s}" % n) in token for n in LIST_PLACEHOLDERS
    ):
        if token not in ["{%s}" % n for n in LIST_PLACEHOLDERS]:
            raise _fail(
                where,
                "token %d (%r) embeds a list placeholder; {effort} and {model} "
                "must stand alone as a whole token" % (index, token),
            )


def _require(where: str, block: dict, key: str, types: tuple) -> Any:
    if key not in block:
        raise _fail(where, "missing required field %r" % key)
    value = block[key]
    if not isinstance(value, types):
        raise _fail(
            where,
            "field %r is %s, want %s"
            % (key, type(value).__name__, "/".join(t.__name__ for t in types)),
        )
    return value


class WorkerSpec:
    """One registry entry, validated."""

    def __init__(self, name: str, block: dict):
        where = "worker %r" % name
        if not isinstance(block, dict):
            raise _fail(where, "entry is not an object")
        self.name = name
        self.display_name = str(_require(where, block, "display_name", (str,)))

        binary = _require(where, block, "binary", (dict,))
        self.binary_env = str(binary.get("env") or "")
        self.binary_candidates = [str(c) for c in (binary.get("candidates") or [])]
        self.binary_path_name = str(binary.get("path_name") or "")
        if not (self.binary_env or self.binary_candidates or self.binary_path_name):
            raise _fail(where, "binary declares no env, candidates or path_name")

        self.auth_file = str(block.get("auth_file") or "")
        self.probe = str(_require(where, block, "probe", (str,)))

        self.sandbox = str(_require(where, block, "sandbox", (str,)))
        if self.sandbox not in SANDBOXES:
            raise _fail(
                where, "sandbox %r not one of %s" % (self.sandbox, ", ".join(SANDBOXES))
            )
        self.write_allowed_default = bool(
            _require(where, block, "write_allowed_default", (bool,))
        )

        run = block.get("run") or {}
        if not isinstance(run, dict):
            raise _fail(where, "run is not an object")
        self.cwd_mode = str(run.get("cwd") or "inherit")
        if self.cwd_mode not in CWD_MODES:
            raise _fail(where, "run.cwd %r not one of %s" % (self.cwd_mode, CWD_MODES))
        self.env_scrub = bool(run.get("env_scrub", False))

        self.prompt: Dict[str, dict] = {}
        prompt = _require(where, block, "prompt", (dict,))
        for mode, cfg in prompt.items():
            if mode not in MODES:
                raise _fail(where, "prompt names unknown mode %r" % mode)
            if not isinstance(cfg, dict):
                raise _fail(where, "prompt.%s is not an object" % mode)
            transport = str(cfg.get("transport") or "")
            if transport not in TRANSPORTS:
                raise _fail(
                    where,
                    "prompt.%s.transport %r not one of %s"
                    % (mode, transport, ", ".join(TRANSPORTS)),
                )
            template = str(cfg.get("template") or "")
            if transport == "file-ref":
                if not template:
                    raise _fail(where, "prompt.%s is file-ref without a template" % mode)
                _check_token(
                    "%s prompt.%s.template" % (where, mode), template, PLACEHOLDERS, 0
                )
            self.prompt[mode] = {"transport": transport, "template": template}

        self.argv: Dict[str, List[str]] = {}
        argv = _require(where, block, "argv", (dict,))
        for mode, tokens in argv.items():
            if mode not in MODES:
                raise _fail(where, "argv names unknown mode %r" % mode)
            if not isinstance(tokens, list) or not tokens:
                raise _fail(where, "argv.%s is not a non-empty array" % mode)
            for i, token in enumerate(tokens):
                _check_token(
                    "%s argv.%s" % (where, mode),
                    token,
                    PLACEHOLDERS,
                    i,
                    standalone_lists=True,
                )
            transport = (self.prompt.get(mode) or {}).get("transport")
            has_prompt = any("{prompt}" in t for t in tokens)
            if transport == "stdin" and has_prompt:
                raise _fail(
                    where,
                    "argv.%s uses {prompt} but its transport is stdin" % mode,
                )
            if transport in ("arg", "file-ref") and not has_prompt:
                raise _fail(
                    where,
                    "argv.%s never uses {prompt} but its transport is %s"
                    % (mode, transport),
                )
            self.argv[mode] = [str(t) for t in tokens]
        for mode in self.argv:
            if mode not in self.prompt:
                raise _fail(where, "argv.%s has no prompt transport" % mode)
        if "implement" not in self.argv:
            raise _fail(where, "argv has no implement mode")

        self.output: Dict[str, str] = {}
        for mode, value in (block.get("output") or {}).items():
            if mode not in MODES:
                raise _fail(where, "output names unknown mode %r" % mode)
            if value not in OUTPUT_MODES:
                raise _fail(
                    where,
                    "output.%s %r not one of %s" % (mode, value, ", ".join(OUTPUT_MODES)),
                )
            self.output[mode] = str(value)
        for mode, tokens in self.argv.items():
            uses_output = any("{output}" in t for t in tokens)
            declared = self.output.get(mode, "none")
            if uses_output and declared != "arg":
                raise _fail(
                    where,
                    "argv.%s uses {output} but output.%s is %r" % (mode, mode, declared),
                )
            if declared == "arg" and not uses_output:
                raise _fail(
                    where, "output.%s is \"arg\" but argv.%s never uses {output}" % (mode, mode)
                )

        # How this worker's log reads per mode. A typo here silently picks the
        # wrong digest path, so an unknown mode or value is a load error.
        fmt_block = block.get("format") or {}
        if not isinstance(fmt_block, dict):
            raise _fail(where, "format is not an object")
        self.format: Dict[str, str] = {}
        for mode, value in fmt_block.items():
            if mode not in MODES:
                raise _fail(where, "format names unknown mode %r" % mode)
            if value not in FORMATS:
                raise _fail(
                    where,
                    "format.%s %r not one of %s" % (mode, value, ", ".join(FORMATS)),
                )
            self.format[str(mode)] = str(value)

        session = _require(where, block, "session", (dict,))
        kind = str(session.get("kind") or "")
        if kind not in SESSION_KINDS:
            raise _fail(
                where,
                "session.kind %r not one of %s" % (kind, ", ".join(SESSION_KINDS)),
            )
        keys = [str(k) for k in (session.get("keys") or [])]
        if not keys:
            raise _fail(where, "session declares no keys")
        self.session = {
            "kind": kind,
            "keys": keys,
            "event_key": str(session.get("event_key") or "type"),
            "event": str(session.get("event") or ""),
        }
        if kind == "jsonl-event" and not self.session["event"]:
            raise _fail(where, "session.kind is jsonl-event but no event is named")

        # Where the worker's last agent message lives in its log. Optional:
        # "generic" scans for assistant text; the two named kinds are exact.
        self.final = self._locator(where, block, "final", default_kind="generic")

        # Where a terminal error lands, when the worker has one. Optional and
        # validated exactly like `final`: a run that ended on turn.failed must
        # not report the last successful message as its outcome.
        self.error = self._locator(where, block, "error", default_kind="")

        self.effort_ladder = [str(r) for r in (block.get("effort_ladder") or [])]
        self.effort_flags = [str(t) for t in (block.get("effort_flags") or [])]
        for i, token in enumerate(self.effort_flags):
            _check_token("%s effort_flags" % where, token, ("effort",), i)
        if self.effort_ladder and not self.effort_flags:
            raise _fail(where, "effort_ladder is set but effort_flags is empty")

        models = block.get("models") or {}
        if not isinstance(models, dict):
            raise _fail(where, "models is not an object")
        self.model_flags = [str(t) for t in (models.get("flag") or [])]
        for i, token in enumerate(self.model_flags):
            _check_token("%s models.flag" % where, token, ("model",), i)
        self.model_rules: List[dict] = []
        for i, rule in enumerate(models.get("rules") or []):
            if not isinstance(rule, dict):
                raise _fail(where, "models.rules[%d] is not an object" % i)
            model = str(rule.get("model") or "")
            if not model:
                raise _fail(where, "models.rules[%d] names no model" % i)
            if not self.model_flags:
                raise _fail(where, "models.rules[%d] set but models.flag is empty" % i)
            self.model_rules.append(
                {
                    "difficulty": [str(d) for d in (rule.get("difficulty") or [])],
                    "size": [str(s) for s in (rule.get("size") or [])],
                    "model": model,
                }
            )

        fit = block.get("fit") or {}
        if not isinstance(fit, dict):
            raise _fail(where, "fit is not an object")
        self.fit: Dict[str, float] = {}
        for kind_name, value in fit.items():
            try:
                self.fit[str(kind_name)] = float(value)
            except (TypeError, ValueError):
                raise _fail(where, "fit.%s is not a number" % kind_name)

    @staticmethod
    def _locator(where: str, block: dict, field: str, default_kind: str):
        """Validate a `final`/`error` rule. Returns None when absent and optional."""
        rule = block.get(field) or {}
        if not isinstance(rule, dict):
            raise _fail(where, "%s is not an object" % field)
        if not rule and not default_kind:
            return None
        kind = str(rule.get("kind") or default_kind)
        if kind not in FINAL_KINDS:
            raise _fail(
                where, "%s.kind %r not one of %s" % (field, kind, ", ".join(FINAL_KINDS))
            )
        keys = [str(k) for k in (rule.get("keys") or [])]
        match = rule.get("match") or {}
        if not isinstance(match, dict):
            raise _fail(where, "%s.match is not an object" % field)
        if kind != "generic" and not keys:
            raise _fail(where, "%s.kind %r declares no keys" % (field, kind))
        return {"kind": kind, "keys": keys, "match": dict(match)}

    # -- lookups ------------------------------------------------------------

    def prompt_for(self, mode: str) -> dict:
        cfg = self.prompt.get(mode)
        if cfg is None:
            raise _fail("worker %r" % self.name, "no prompt transport for mode %r" % mode)
        return cfg

    def argv_for(self, mode: str) -> List[str]:
        tokens = self.argv.get(mode)
        if tokens is None:
            raise _fail("worker %r" % self.name, "no argv template for mode %r" % mode)
        return list(tokens)

    def output_mode(self, mode: str) -> str:
        return self.output.get(mode, "none")

    def model_for(self, difficulty: str, size: str) -> str:
        for rule in self.model_rules:
            if rule["difficulty"] and difficulty not in rule["difficulty"]:
                continue
            if rule["size"] and size not in rule["size"]:
                continue
            return rule["model"]
        return ""

    def resolve_binary(self, env: Optional[dict] = None) -> str:
        """Absolute path to this worker's binary, or "" when nothing resolves.

        An explicitly set env override wins even when it does not exist: an
        operator who points CODEX_BIN somewhere wrong wants to hear about that
        path, not silently get a different binary.
        """
        env = os.environ if env is None else env
        if self.binary_env:
            override = env.get(self.binary_env)
            if override:
                return override
        home = env.get("HOME") or str(Path.home())
        for candidate in self.binary_candidates:
            path = candidate
            if path.startswith("~/"):
                path = os.path.join(home, path[2:])
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path
        if self.binary_path_name:
            found = shutil.which(self.binary_path_name, path=env.get("PATH"))
            if found:
                return found
        return ""

    def auth_path(self, env: Optional[dict] = None) -> str:
        if not self.auth_file:
            return ""
        env = os.environ if env is None else env
        home = env.get("HOME") or str(Path.home())
        if self.auth_file.startswith("~/"):
            return os.path.join(home, self.auth_file[2:])
        return self.auth_file

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "probe": self.probe,
            "sandbox": self.sandbox,
            "write_allowed_default": self.write_allowed_default,
            "env_scrub": self.env_scrub,
            "cwd": self.cwd_mode,
            "modes": sorted(self.argv),
            "effort_ladder": list(self.effort_ladder),
            "fit": dict(self.fit),
        }


class Registry:
    def __init__(self, path: Path, doc: dict):
        self.path = path
        where = str(path)
        version = doc.get("schema_version")
        if version != SCHEMA_VERSION:
            raise _fail(
                where,
                "schema_version is %r, this build understands %d"
                % (version, SCHEMA_VERSION),
            )
        workers = doc.get("workers")
        if not isinstance(workers, dict) or not workers:
            raise _fail(where, "workers is not a non-empty object")
        self.workers: Dict[str, WorkerSpec] = {}
        for name, block in workers.items():
            if not re.match(r"^[a-z][a-z0-9_-]*$", str(name)):
                raise _fail(where, "worker name %r is not [a-z][a-z0-9_-]*" % name)
            self.workers[str(name)] = WorkerSpec(str(name), block)

    @property
    def names(self) -> tuple:
        return tuple(self.workers)

    def get(self, name: str) -> WorkerSpec:
        spec = self.workers.get(name)
        if spec is None:
            raise RegistryError(
                "unknown worker %r; registered: %s"
                % (name, ", ".join(self.names) or "(none)")
            )
        return spec

    def fit_table(self) -> Dict[str, Dict[str, float]]:
        """kind -> {worker: multiplier}. Absent priors default to 1.0."""
        kinds: List[str] = ["default"]
        for spec in self.workers.values():
            for kind in spec.fit:
                if kind not in kinds:
                    kinds.append(kind)
        return {
            kind: {name: spec.fit.get(kind, 1.0) for name, spec in self.workers.items()}
            for kind in kinds
        }

    def effort_ladders(self) -> Dict[str, List[str]]:
        return {name: list(spec.effort_ladder) for name, spec in self.workers.items()}


_CACHE: Dict[str, Registry] = {}


def registry_path(path: Optional[str] = None) -> Path:
    if path:
        return Path(path)
    override = os.environ.get("SSA_WORKERS_JSON")
    if override:
        return Path(override)
    return DEFAULT_PATH


def load(path: Optional[str] = None, cache: bool = True) -> Registry:
    """Load and validate a registry. Raises RegistryError on anything odd."""
    resolved = registry_path(path)
    key = str(resolved)
    if cache and key in _CACHE:
        return _CACHE[key]
    try:
        text = resolved.read_text()
    except OSError as exc:
        raise _fail(key, "cannot read registry: %s" % exc)
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise _fail(key, "not valid JSON: %s" % exc)
    if not isinstance(doc, dict):
        raise _fail(key, "top level is not an object")
    reg = Registry(resolved, doc)
    if cache:
        _CACHE[key] = reg
    return reg
