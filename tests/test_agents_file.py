"""init writes $WT/.claude/agents/ssa-worker.md from the same payload as --agents.

Parse both sides. Do not assert a golden markdown string.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import (  # noqa: E402
    load_ssa,
    make_git_repo,
    run_ssa,
    run_ssa_cli,
    temp_env,
)
from test_registry import write_registry  # noqa: E402
from test_shell import find_only_task_dir, write_usage_stub  # noqa: E402


def parse_agent_markdown(text: str):
    """Split a Claude Code agent file into (frontmatter dict, prompt body)."""
    if not text.startswith("---\n"):
        raise AssertionError("agent file does not start with YAML frontmatter")
    rest = text[4:]
    marker = "\n---\n"
    idx = rest.find(marker)
    if idx < 0:
        raise AssertionError("agent file is missing closing frontmatter fence")
    fm_text = rest[:idx]
    body = rest[idx + len(marker) :]
    if body.endswith("\n"):
        body = body[:-1]
    fm = {}
    for line in fm_text.splitlines():
        if not line.strip():
            continue
        key, sep, raw = line.partition(":")
        if not sep:
            raise AssertionError("bad frontmatter line: %r" % line)
        raw = raw.strip()
        try:
            fm[key.strip()] = json.loads(raw)
        except ValueError:
            fm[key.strip()] = raw
    return fm, body


def _init_env(te, recommendation: dict) -> dict:
    stub = te.root / "usage-stub.py"
    write_usage_stub(stub, recommendation)
    env = dict(te.env)
    env["SSA_USAGE_PY"] = str(stub)
    env["SSA_STUB_ARGV"] = str(te.root / "usage-argv.txt")
    return env


def _claude_ctx(task_dir: Path) -> dict:
    brief = task_dir / "brief.md"
    args_file = task_dir / "worker-args-claude.txt"
    args = []
    if args_file.exists():
        args = [line for line in args_file.read_text().splitlines() if line.strip()]
    return {
        "brief": str(brief) if brief.exists() else "",
        "args": args,
    }


CLAUDE_ARGS = ["--effort", "high", "--model", "fable"]
GROK_ARGS = ["--reasoning-effort", "high"]


def _rec(primary: str, **worker_args) -> dict:
    args = {"codex": [], "grok": list(GROK_ARGS), "claude": list(CLAUDE_ARGS), "kimi": []}
    args.update(worker_args)
    ranked = [{"cli": primary, "score": 90}]
    for name in args:
        if name != primary:
            ranked.append({"cli": name, "score": 40})
    return {
        "primary_worker": primary,
        "fallback_workers": [n for n in args if n != primary],
        "local_labor_ok": True,
        "worker_args": args,
        "ranked": ranked,
        "reasons": [],
    }


class AgentsFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.adapters = load_ssa("adapters")
        cls.registry = load_ssa("registry")
        cls.reg = cls.registry.load()
        cls.spec = cls.reg.get("claude")

    def _assert_file_matches_json(self, agent_path: Path, ctx: dict):
        fm, body = parse_agent_markdown(agent_path.read_text())
        payload = json.loads(self.adapters.agents_json(self.spec, ctx))
        self.assertEqual(list(payload.keys()), [fm["name"]])
        json_body = payload[fm["name"]]
        for key, value in json_body.items():
            if key == "prompt":
                self.assertEqual(body, value)
            else:
                self.assertEqual(fm[key], value, key)
        self.assertEqual(fm["name"], next(iter(payload)))
        self.assertTrue(fm["name"])
        self.assertTrue(fm["description"])
        self.assertEqual(fm["memory"], "project")
        self.assertIs(fm["background"], True)
        self.assertNotIn("memory", json_body)
        self.assertNotIn("background", json_body)
        return fm, body, json_body

    def test_init_writes_ssa_worker_md_matching_agents_json(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            env = _init_env(te, _rec("claude"))
            rc, out, err = run_ssa("init", "--repo", str(repo), env=env)
            self.assertEqual(rc, 0, err)
            task_dir = find_only_task_dir(te.work_dir)
            wt = Path((task_dir / "wt.txt").read_text().strip())
            agent = wt / ".claude" / "agents" / "ssa-worker.md"
            self.assertTrue(agent.is_file(), "missing %s" % agent)
            self._assert_file_matches_json(agent, _claude_ctx(task_dir))

    def test_agents_file_model_comes_from_worker_args_claude_not_a_literal(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            env = _init_env(te, _rec("claude"))
            rc, out, err = run_ssa("init", "--repo", str(repo), env=env)
            self.assertEqual(rc, 0, err)
            task_dir = find_only_task_dir(te.work_dir)
            wt = Path((task_dir / "wt.txt").read_text().strip())
            agent = wt / ".claude" / "agents" / "ssa-worker.md"

            (task_dir / "worker-args-claude.txt").write_text("--model\nsonnet\n")
            written = self.adapters.write_worktree_claude_agent(str(task_dir))
            self.assertEqual(written, str(agent))
            fm, body, json_body = self._assert_file_matches_json(
                agent, _claude_ctx(task_dir)
            )
            self.assertEqual(fm["model"], "sonnet")
            self.assertEqual(json_body["model"], "sonnet")
            self.assertNotEqual(fm["model"], "fable")

            (task_dir / "worker-args-claude.txt").write_text("")
            self.adapters.write_worktree_claude_agent(str(task_dir))
            fm, body, json_body = self._assert_file_matches_json(
                agent, _claude_ctx(task_dir)
            )
            self.assertNotIn("model", fm)
            self.assertNotIn("model", json_body)

            payload = self.adapters.agents_payload(
                self.spec, {"brief": "", "args": ["--effort", "high"]}
            )
            md = self.adapters.agents_markdown(payload)
            js = json.loads(self.adapters.agents_json(self.spec, {"brief": "", "args": ["--effort", "high"]}))
            fm, _body = parse_agent_markdown(md)
            self.assertNotIn("model", fm)
            self.assertNotIn("model", js["ssa-worker"])

    def test_missing_name_or_description_refuses_nonzero(self):
        with temp_env() as te:
            for field in ("name", "description"):
                with self.subTest(field=field):
                    def mutate(doc, key=field):
                        doc["workers"]["claude"]["agents"][key] = ""

                    path = write_registry(te.root / ("bad-%s.json" % field), mutate=mutate)
                    env = dict(te.env)
                    env["SSA_WORKERS_JSON"] = str(path)
                    rc, out, err = run_ssa_cli("registry-validate", env=env)
                    self.assertNotEqual(rc, 0)
                    self.assertIn("agents.%s" % field, err)
                    with self.assertRaises(self.registry.RegistryError) as cm:
                        self.registry.load(str(path), cache=False)
                    self.assertIn("agents.%s" % field, str(cm.exception))

            dest = te.root / "no-write" / "ssa-worker.md"
            for payload, field in (
                ({"description": "x", "prompt": "p", "disallowedTools": []}, "name"),
                ({"name": "ssa-worker", "prompt": "p", "disallowedTools": []}, "description"),
                ({"name": "", "description": "x", "prompt": "p"}, "name"),
                ({"name": "ssa-worker", "description": "", "prompt": "p"}, "description"),
            ):
                with self.subTest(writer=field, payload=payload):
                    with self.assertRaises(self.adapters.AdapterError) as cm:
                        self.adapters.write_agents_file(str(dest), payload)
                    self.assertIn("agents.%s" % field, str(cm.exception))
                    self.assertFalse(dest.exists())
                    self.assertFalse(dest.parent.exists())

    def test_non_git_init_does_not_write_the_file(self):
        with temp_env() as te:
            repo = te.root / "nongit"
            repo.mkdir()
            env = _init_env(te, _rec("claude"))
            rc, out, err = run_ssa("init", "--repo", str(repo), env=env)
            self.assertEqual(rc, 0, err)
            task_dir = find_only_task_dir(te.work_dir)
            self.assertEqual((task_dir / "wt.txt").read_text().strip(), "NOT_GIT")
            self.assertFalse((repo / ".claude").exists())
            wt_root = te.work_dir / "wt"
            if wt_root.exists():
                leftover = list(wt_root.rglob("ssa-worker.md"))
                self.assertEqual(leftover, [])
            written = self.adapters.write_worktree_claude_agent(str(task_dir))
            self.assertEqual(written, "")
            self.assertFalse((repo / ".claude").exists())

    def test_grok_pick_still_writes_the_claude_agent_file(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            env = _init_env(
                te,
                _rec(
                    "grok",
                    grok=["--reasoning-effort", "high", "--model", "grok-4"],
                    claude=["--effort", "high", "--model", "sonnet"],
                ),
            )
            rc, out, err = run_ssa("init", "--repo", str(repo), env=env)
            self.assertEqual(rc, 0, err)
            task_dir = find_only_task_dir(te.work_dir)
            self.assertEqual((task_dir / "worker.txt").read_text().strip(), "grok")
            self.assertEqual(
                (task_dir / "worker-args.txt").read_text().strip(),
                "--reasoning-effort\nhigh\n--model\ngrok-4",
            )
            wt = Path((task_dir / "wt.txt").read_text().strip())
            agent = wt / ".claude" / "agents" / "ssa-worker.md"
            self.assertTrue(agent.is_file(), "grok pick must still write %s" % agent)
            fm, body, json_body = self._assert_file_matches_json(
                agent, _claude_ctx(task_dir)
            )
            self.assertEqual(fm["model"], "sonnet")
            self.assertEqual(json_body["model"], "sonnet")
            self.assertNotEqual(fm["model"], "grok-4")
            self.assertNotEqual(fm["model"], "fable")

    def test_init_agent_file_does_not_make_gc_keep_the_dir(self):
        """The file is a launch artifact. gc must still list the task as safe."""
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            env = _init_env(te, _rec("claude"))
            rc, out, err = run_ssa("init", "--repo", str(repo), env=env)
            self.assertEqual(rc, 0, err)
            task_dir = find_only_task_dir(te.work_dir)
            wt = Path((task_dir / "wt.txt").read_text().strip())
            self.assertTrue((wt / ".claude" / "agents" / "ssa-worker.md").is_file())
            rc, out, err = run_ssa("gc", "--older-than", "0", env=env)
            self.assertEqual(rc, 0, err)
            self.assertIn("safe  task %s" % task_dir.name, out)
            self.assertTrue(task_dir.is_dir(), "dry-run must not delete")


if __name__ == "__main__":
    unittest.main()
