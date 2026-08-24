"""Shared test helpers for the smart-subagents characterization suite.

Everything here is hermetic: no real credentials, no network, no writes
outside a temp dir the caller owns. Import this module, never copy it.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
SSA_SH = ROOT / "scripts" / "smart-subagents.sh"
USAGE_PY = ROOT / "scripts" / "ai-cli-usage.py"
SSA_CLI_PY = ROOT / "scripts" / "ssa" / "cli.py"
WORKERS_JSON = ROOT / "scripts" / "workers.json"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
BIN_DIR = FIXTURES_DIR / "bin"
PROVIDERS_DIR = FIXTURES_DIR / "providers"

# Implement dispatch refuses a one-line brief. Tests that launch a worker
# must carry this section unless they are asserting the refuse.
FIXTURE_BRIEF = (
    "Complete the fixture task.\n\n"
    "## Structural discovery\n"
    "CGC-SKIP: fixture; route=none; evidence=characterization-test\n"
)


def load_ssa(name: str):
    """Import ssa.<name> (registry / adapters / state) from scripts/."""
    scripts = str(ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import importlib

    return importlib.import_module("ssa." + name)


def run_ssa_cli(*args: str, env: Optional[dict] = None, timeout: int = 60):
    """Run scripts/ssa/cli.py. Returns (returncode, stdout, stderr)."""
    proc = subprocess.run(
        [sys.executable, str(SSA_CLI_PY), *args],
        env=env if env is not None else dict(os.environ),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


def load_usage_module():
    """Import scripts/ai-cli-usage.py under a normal module name.

    The filename is hyphenated so it cannot be `import`ed directly; this
    mirrors the loader tests/smoke.sh already uses inline.
    """
    spec = importlib.util.spec_from_file_location("ai_cli_usage", str(USAGE_PY))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


# ---------------------------------------------------------------------------
# Synthetic builders
# ---------------------------------------------------------------------------


def make_window(module, **overrides) -> Any:
    """A Window with sane defaults, overridden per test."""
    defaults = dict(
        name="test_window",
        used_pct=10.0,
        remaining_pct=90.0,
    )
    defaults.update(overrides)
    return module.Window(**defaults)


def make_status(module, cli: str = "codex", **overrides) -> Any:
    """A CliStatus with sane defaults, overridden per test."""
    defaults = dict(
        cli=cli,
        available=True,
        eligible=True,
        score=50.0,
    )
    defaults.update(overrides)
    return module.CliStatus(**defaults)


# ---------------------------------------------------------------------------
# Hermetic environment
# ---------------------------------------------------------------------------


class TempEnv:
    """Isolated HOME / XDG_* / SSA_WORK_DIR / PATH for one test.

    Use as a context manager. `env` is the dict to hand to subprocess calls
    (run_ssa uses it automatically when passed as `env=`). Nothing here
    touches the real user's HOME, cache, state or work dir.
    """

    def __init__(self, extra_env: Optional[dict] = None, prepend_bin: bool = True):
        self._tmp: Optional[tempfile.TemporaryDirectory] = None
        self._extra_env = extra_env or {}
        self._prepend_bin = prepend_bin
        self.root: Optional[Path] = None
        self.home: Optional[Path] = None
        self.env: dict = {}

    def __enter__(self) -> "TempEnv":
        self._tmp = tempfile.TemporaryDirectory(prefix="ssa-test-")
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        xdg_state = self.root / "state"
        xdg_cache = self.root / "cache"
        work_dir = self.root / "work"
        for d in (xdg_state, xdg_cache, work_dir):
            d.mkdir(parents=True, exist_ok=True)

        path = os.environ.get("PATH", "")
        if self._prepend_bin:
            path = f"{BIN_DIR}:{path}"

        env = dict(os.environ)
        env.update(
            {
                "HOME": str(self.home),
                "XDG_STATE_HOME": str(xdg_state),
                "XDG_CACHE_HOME": str(xdg_cache),
                "SSA_WORK_DIR": str(work_dir),
                "PATH": path,
                # Never let a test reach the network for a quota snapshot.
                "SSA_NO_QUOTA_SNAPSHOT": "1",
            }
        )
        env.update(self._extra_env)
        self.env = env
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._tmp is not None:
            self._tmp.cleanup()
        return False

    @property
    def work_dir(self) -> Path:
        return Path(self.env["SSA_WORK_DIR"])

    @property
    def state_dir(self) -> Path:
        return Path(self.env["XDG_STATE_HOME"]) / "smart-subagents"

    @property
    def cache_dir(self) -> Path:
        return Path(self.env["XDG_CACHE_HOME"]) / "smart-subagents"


@contextlib.contextmanager
def temp_env(extra_env: Optional[dict] = None, prepend_bin: bool = True):
    with TempEnv(extra_env=extra_env, prepend_bin=prepend_bin) as te:
        yield te


# ---------------------------------------------------------------------------
# git fixtures
# ---------------------------------------------------------------------------


def make_git_repo(path: Path, files: Optional[dict] = None) -> Path:
    """Init a throwaway git repo at `path` with one commit. Returns `path`."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test Suite"], cwd=path, check=True)
    files = files or {"README.md": "fixture\n"}
    for name, content in files.items():
        fp = path / name
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=path, check=True)
    return path


# ---------------------------------------------------------------------------
# smart-subagents.sh subprocess runner
# ---------------------------------------------------------------------------


def run_ssa(*args: str, env: dict, timeout: int = 60, input_text: Optional[str] = None):
    """Run scripts/smart-subagents.sh with the given args and env.

    Returns (returncode, stdout, stderr). Never touches the network: caller's
    env is expected to carry SSA_NO_QUOTA_SNAPSHOT=1 (TempEnv sets it).
    """
    proc = subprocess.run(
        ["bash", str(SSA_SH), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        input=input_text,
    )
    return proc.returncode, proc.stdout, proc.stderr


def read_argv_file(path: Path) -> list:
    """Read a fake binary's recorded argv file: one arg per line."""
    if not path.exists():
        return []
    text = path.read_text()
    return text.split("\n")[:-1] if text.endswith("\n") else text.split("\n")


def load_fixture_json(*parts: str):
    import json

    p = PROVIDERS_DIR.joinpath(*parts)
    return json.loads(p.read_text())
