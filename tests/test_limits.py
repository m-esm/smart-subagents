"""$DIR/limits.txt -> scrubbed env via workers.json run.env_pass.

The resolved env dict is the contract, not a substring of a command line.
A missing file, an empty file, and a worker with no env_pass all produce {}.
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import (  # noqa: E402
    BIN_DIR,
    load_ssa,
    make_git_repo,
    run_ssa,
    temp_env,
)
from test_shell import make_task_dir  # noqa: E402

CLAUDE_LIMIT_VARS = {
    "spawn_depth": "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH",
    "concurrent": "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS",
    "per_session": "CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION",
}


class EnvPassRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reg = load_ssa("registry").load()

    def test_claude_env_pass_names_live_in_the_registry(self):
        self.assertEqual(self.reg.get("claude").env_pass, CLAUDE_LIMIT_VARS)
        for name in ("codex", "grok", "kimi"):
            self.assertEqual(self.reg.get(name).env_pass, {}, msg=name)

    def test_scrub_env_keeps_only_the_four_base_vars(self):
        adapters = load_ssa("adapters")
        kept = adapters.scrub_env(
            {
                "HOME": "/h",
                "PATH": "/p",
                "TMPDIR": "/t",
                "TERM": "x",
                "SSA_TEST_LEAK": "1",
                "SECRET": "nope",
            }
        )
        self.assertEqual(
            kept,
            {"HOME": "/h", "PATH": "/p", "TMPDIR": "/t", "TERM": "x"},
        )


class ResolvedEnvDictTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.adapters = load_ssa("adapters")
        cls.reg = load_ssa("registry").load()

    def _build(self, tmp, worker, limits_text=None, limits_missing=False):
        brief = tmp / "brief.md"
        brief.write_text("Complete the fixture task.\n")
        ctx = {
            "worktree": str(tmp),
            "brief": str(brief),
            "args": [],
        }
        if limits_missing:
            ctx["limits"] = str(tmp / "no-such-limits.txt")
        elif limits_text is not None:
            path = tmp / "limits.txt"
            path.write_text(limits_text)
            ctx["limits"] = str(path)
        return self.adapters.build_command(worker, "implement", ctx, reg=self.reg)

    def test_all_three_keys_resolve_to_the_claude_code_vars(self):
        with temp_env() as te:
            built = self._build(
                te.root,
                "claude",
                "spawn_depth=2\nconcurrent=1\nper_session=0\n",
            )
            self.assertEqual(
                built["env_extra"],
                {
                    "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "2",
                    "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": "1",
                    "CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION": "0",
                },
            )

    def test_one_key_produces_exactly_that_var_not_defaults(self):
        with temp_env() as te:
            built = self._build(te.root, "claude", "concurrent=4\n")
            self.assertEqual(
                built["env_extra"],
                {"CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": "4"},
            )

    def test_absent_limits_file_produces_empty_dict(self):
        with temp_env() as te:
            built = self._build(te.root, "claude")
            self.assertEqual(built["env_extra"], {})
            built = self._build(te.root, "claude", limits_missing=True)
            self.assertEqual(built["env_extra"], {})

    def test_comments_and_blanks_are_not_directives(self):
        with temp_env() as te:
            built = self._build(
                te.root,
                "claude",
                "# spawn_depth=99\n\n  \n# concurrent=1\n",
            )
            self.assertEqual(built["env_extra"], {})

    def test_unknown_key_raises_naming_the_key_and_accepted(self):
        with temp_env() as te:
            with self.assertRaises(self.adapters.AdapterError) as caught:
                self._build(te.root, "claude", "nope=1\n")
            msg = str(caught.exception)
            self.assertIn("nope", msg)
            for key in CLAUDE_LIMIT_VARS:
                self.assertIn(key, msg)

    def test_non_integer_value_raises_naming_the_key(self):
        with temp_env() as te:
            with self.assertRaises(self.adapters.AdapterError) as caught:
                self._build(te.root, "claude", "spawn_depth=two\n")
            self.assertIn("spawn_depth", str(caught.exception))

    def test_grok_with_limits_txt_gets_empty_env_extra(self):
        with temp_env() as te:
            built = self._build(
                te.root,
                "grok",
                "spawn_depth=2\nconcurrent=1\nper_session=0\n",
            )
            self.assertEqual(built["env_extra"], {})


class LimitsDispatchTests(unittest.TestCase):
    def _claude_env(self, te):
        env = dict(te.env)
        env["CLAUDE_BIN"] = str(BIN_DIR / "fake-claude")
        env["SSA_ALLOW_UNSANDBOXED_WRITE"] = "1"
        env["SSA_TEST_LEAK"] = "1"
        env.pop("CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH", None)
        env.pop("CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS", None)
        env.pop("CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION", None)
        return env

    def test_unknown_key_dispatch_exits_nonzero_naming_the_key(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo, worker_args=[])
            (task_dir / "limits.txt").write_text("nope=1\n")
            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "claude",
                env=self._claude_env(te),
            )
            self.assertNotEqual(rc, 0)
            self.assertIn("nope", err)
            for key in CLAUDE_LIMIT_VARS:
                self.assertIn(key, err)

    def test_non_integer_dispatch_exits_nonzero_naming_the_key(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo, worker_args=[])
            (task_dir / "limits.txt").write_text("per_session=-1\n")
            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "claude",
                env=self._claude_env(te),
            )
            self.assertNotEqual(rc, 0)
            self.assertIn("per_session", err)

    def test_declared_limits_reach_the_scrubbed_process_and_parent_leak_does_not(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo, worker_args=[])
            (task_dir / "limits.txt").write_text(
                "spawn_depth=2\nconcurrent=1\nper_session=0\n"
            )
            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "claude",
                env=self._claude_env(te),
            )
            self.assertEqual(rc, 0, err)
            recorded = json.loads(
                (te.home / ".ssa-test" / "fake-claude" / "env.json").read_text()
            )
            self.assertEqual(recorded["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"], "2")
            self.assertEqual(recorded["CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS"], "1")
            self.assertEqual(recorded["CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION"], "0")
            self.assertNotIn("SSA_TEST_LEAK", recorded)
            for base in ("HOME", "PATH", "TMPDIR", "TERM"):
                self.assertIn(base, recorded)

    def test_no_limits_txt_keeps_the_four_base_vars_only(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo, worker_args=[])
            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "claude",
                env=self._claude_env(te),
            )
            self.assertEqual(rc, 0, err)
            recorded = json.loads(
                (te.home / ".ssa-test" / "fake-claude" / "env.json").read_text()
            )
            self.assertNotIn("CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH", recorded)
            self.assertNotIn("CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS", recorded)
            self.assertNotIn("CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION", recorded)
            self.assertNotIn("SSA_TEST_LEAK", recorded)
            for base in ("HOME", "PATH", "TMPDIR", "TERM"):
                self.assertIn(base, recorded)

    def test_grok_dispatch_with_limits_txt_gets_no_extra_env(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo, worker_args=[])
            (task_dir / "limits.txt").write_text(
                "spawn_depth=2\nconcurrent=1\nper_session=0\n"
            )
            env = dict(te.env)
            env["GROK_BIN"] = str(BIN_DIR / "fake-grok")
            env["SSA_TEST_LEAK"] = "1"
            env.pop("CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH", None)
            env.pop("CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS", None)
            env.pop("CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION", None)
            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "grok", env=env
            )
            self.assertEqual(rc, 0, err)
            recorded = json.loads(
                (te.home / ".ssa-test" / "fake-grok" / "env.json").read_text()
            )
            self.assertNotIn("CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH", recorded)
            self.assertNotIn("CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS", recorded)
            self.assertNotIn("CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION", recorded)


if __name__ == "__main__":
    unittest.main()
