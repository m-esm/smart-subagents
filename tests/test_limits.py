"""$DIR/limits.txt -> scrubbed env via workers.json run.env_pass / env_keep.

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

CLAUDE_ENV_PASS = {
    **CLAUDE_LIMIT_VARS,
    "subagent_model": "CLAUDE_CODE_SUBAGENT_MODEL",
    "subagent_model_force": "CLAUDE_CODE_SUBAGENT_MODEL_FORCE",
}

PARENT_SUBAGENT_MODEL = "parent-leak-model"
PARENT_SUBAGENT_FORCE = "parent-leak-force"

# CPython on macOS re-injects these even under `env -i`. They are not a
# scrub leak; a removed scrub would also admit SSA_TEST_LEAK and the rest
# of the parent environment, which this set does not contain.
PYTHON_ENV_INJECTIONS = frozenset(
    {
        "CPATH",
        "LC_CTYPE",
        "LIBRARY_PATH",
        "MANPATH",
        "SDKROOT",
        "__CF_USER_TEXT_ENCODING",
    }
)

PARENT_USER = "ssa-parent-user"


def policy_keys(recorded):
    """Launched env keys minus interpreter noise. Set equality of this
    against the allowlist (plus limits.txt vars) is the scrub contract.
    """
    return set(recorded) - PYTHON_ENV_INJECTIONS


class EnvPassRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reg = load_ssa("registry").load()

    def test_claude_env_pass_names_live_in_the_registry(self):
        self.assertEqual(self.reg.get("claude").env_pass, CLAUDE_ENV_PASS)
        self.assertIn("subagent_model", self.reg.get("claude").env_pass)
        self.assertIn("subagent_model_force", self.reg.get("claude").env_pass)
        self.assertEqual(
            self.reg.get("claude").env_pass["subagent_model"],
            "CLAUDE_CODE_SUBAGENT_MODEL",
        )
        self.assertEqual(
            self.reg.get("claude").env_pass["subagent_model_force"],
            "CLAUDE_CODE_SUBAGENT_MODEL_FORCE",
        )
        for name in ("codex", "grok", "kimi"):
            self.assertEqual(self.reg.get(name).env_pass, {}, msg=name)

    def test_claude_env_keep_includes_user(self):
        self.assertEqual(
            self.reg.get("claude").env_keep,
            ["HOME", "PATH", "TMPDIR", "TERM", "USER"],
        )

    def test_grok_env_keep_defaults_to_the_four(self):
        registry = load_ssa("registry")
        self.assertEqual(
            self.reg.get("grok").env_keep, list(registry.DEFAULT_ENV_KEEP)
        )
        self.assertFalse(self.reg.get("grok").env_scrub)
        for name in ("codex", "kimi"):
            self.assertEqual(
                self.reg.get(name).env_keep,
                list(registry.DEFAULT_ENV_KEEP),
                msg=name,
            )

    def test_scrub_env_keeps_only_the_four_base_vars(self):
        # Deliberate pin of the no-spec default. USER in the parent must
        # not sneak through when no worker spec is supplied.
        adapters = load_ssa("adapters")
        kept = adapters.scrub_env(
            {
                "HOME": "/h",
                "PATH": "/p",
                "TMPDIR": "/t",
                "TERM": "x",
                "USER": "should-not-pass",
                "SSA_TEST_LEAK": "1",
                "SECRET": "nope",
            }
        )
        self.assertEqual(
            kept,
            {"HOME": "/h", "PATH": "/p", "TMPDIR": "/t", "TERM": "x"},
        )

    def test_scrub_env_with_claude_spec_keeps_user(self):
        adapters = load_ssa("adapters")
        kept = adapters.scrub_env(
            {
                "HOME": "/h",
                "PATH": "/p",
                "TMPDIR": "/t",
                "TERM": "x",
                "USER": PARENT_USER,
                "SSA_TEST_LEAK": "1",
                "SECRET": "nope",
            },
            spec=self.reg.get("claude"),
        )
        self.assertEqual(
            kept,
            {
                "HOME": "/h",
                "PATH": "/p",
                "TMPDIR": "/t",
                "TERM": "x",
                "USER": PARENT_USER,
            },
        )

    def test_scrub_env_with_grok_spec_keeps_the_four(self):
        adapters = load_ssa("adapters")
        kept = adapters.scrub_env(
            {
                "HOME": "/h",
                "PATH": "/p",
                "TMPDIR": "/t",
                "TERM": "x",
                "USER": PARENT_USER,
                "SSA_TEST_LEAK": "1",
            },
            spec=self.reg.get("grok"),
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

    def test_subagent_model_accepts_a_model_id(self):
        with temp_env() as te:
            built = self._build(te.root, "claude", "subagent_model=haiku\n")
            self.assertEqual(
                built["env_extra"],
                {"CLAUDE_CODE_SUBAGENT_MODEL": "haiku"},
            )
            built = self._build(
                te.root, "claude", "subagent_model=claude-haiku-4-5-20251001\n"
            )
            self.assertEqual(
                built["env_extra"]["CLAUDE_CODE_SUBAGENT_MODEL"],
                "claude-haiku-4-5-20251001",
            )

    def test_empty_subagent_model_raises_naming_the_key(self):
        with temp_env() as te:
            with self.assertRaises(self.adapters.AdapterError) as caught:
                self._build(te.root, "claude", "subagent_model=\n")
            self.assertIn("subagent_model", str(caught.exception))

    def test_whitespace_subagent_model_raises_naming_the_key(self):
        with temp_env() as te:
            with self.assertRaises(self.adapters.AdapterError) as caught:
                self._build(te.root, "claude", "subagent_model=hai ku\n")
            self.assertIn("subagent_model", str(caught.exception))

    def test_subagent_model_force_must_be_exactly_one(self):
        with temp_env() as te:
            built = self._build(te.root, "claude", "subagent_model_force=1\n")
            self.assertEqual(
                built["env_extra"],
                {"CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1"},
            )
            with self.assertRaises(self.adapters.AdapterError) as caught:
                self._build(te.root, "claude", "subagent_model_force=true\n")
            self.assertIn("subagent_model_force", str(caught.exception))

    def test_grok_with_limits_txt_gets_empty_env_extra(self):
        with temp_env() as te:
            built = self._build(
                te.root,
                "grok",
                "spawn_depth=2\nconcurrent=1\nper_session=0\n",
            )
            self.assertEqual(built["env_extra"], {})
            self.assertEqual(built["env_keep"], {})
            self.assertFalse(built["env_scrub"])

    def test_claude_env_keep_and_env_extra_stay_distinct(self):
        with temp_env() as te:
            parent = {
                "HOME": "/h",
                "PATH": "/p",
                "TMPDIR": "/t",
                "TERM": "x",
                "USER": PARENT_USER,
                "SSA_TEST_LEAK": "1",
            }
            built = self._build(
                te.root,
                "claude",
                "spawn_depth=2\nconcurrent=1\nper_session=0\n",
            )
            # _build does not pass ctx['env']; pin via scrub_env + env_extra.
            adapters = load_ssa("adapters")
            keep = adapters.scrub_env(parent, spec=self.reg.get("claude"))
            self.assertEqual(set(keep) & set(built["env_extra"]), set())
            self.assertIn("USER", keep)
            self.assertNotIn("USER", built["env_extra"])
            self.assertNotIn("SSA_TEST_LEAK", keep)
            for var in CLAUDE_LIMIT_VARS.values():
                self.assertIn(var, built["env_extra"])
                self.assertNotIn(var, keep)


class LimitsDispatchTests(unittest.TestCase):
    def _claude_env(self, te):
        env = dict(te.env)
        env["CLAUDE_BIN"] = str(BIN_DIR / "fake-claude")
        env["SSA_ALLOW_UNSANDBOXED_WRITE"] = "1"
        env["SSA_TEST_LEAK"] = "1"
        env["USER"] = PARENT_USER
        env.pop("CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH", None)
        env.pop("CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS", None)
        env.pop("CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION", None)
        env["CLAUDE_CODE_SUBAGENT_MODEL"] = PARENT_SUBAGENT_MODEL
        env["CLAUDE_CODE_SUBAGENT_MODEL_FORCE"] = PARENT_SUBAGENT_FORCE
        return env

    def _assert_policy_env(self, recorded, expected, parent):
        """Launched env keys == allowlist (+ limits), values from parent/limits.

        Interpreter-injected keys are subtracted so this is set equality of
        the policy, not membership. SSA_TEST_LEAK is not in the injection
        set, so a removed scrub still fails.
        """
        self.assertEqual(policy_keys(recorded), expected)
        self.assertNotIn("SSA_TEST_LEAK", recorded)
        for key in expected:
            self.assertIn(key, recorded)
        if "USER" in expected:
            self.assertEqual(recorded["USER"], parent["USER"])

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

    def test_claude_dispatch_keeps_user_and_not_the_leak(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo, worker_args=[])
            env = self._claude_env(te)
            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "claude",
                env=env,
            )
            self.assertEqual(rc, 0, err)
            recorded = json.loads(
                (te.home / ".ssa-test" / "fake-claude" / "env.json").read_text()
            )
            spec = load_ssa("registry").load().get("claude")
            self._assert_policy_env(recorded, set(spec.env_keep), env)

    def test_declared_limits_reach_the_scrubbed_process_and_parent_leak_does_not(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo, worker_args=[])
            (task_dir / "limits.txt").write_text(
                "spawn_depth=2\nconcurrent=1\nper_session=0\n"
            )
            env = self._claude_env(te)
            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "claude",
                env=env,
            )
            self.assertEqual(rc, 0, err)
            recorded = json.loads(
                (te.home / ".ssa-test" / "fake-claude" / "env.json").read_text()
            )
            self.assertEqual(recorded["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"], "2")
            self.assertEqual(recorded["CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS"], "1")
            self.assertEqual(recorded["CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION"], "0")
            spec = load_ssa("registry").load().get("claude")
            expected = set(spec.env_keep) | set(CLAUDE_LIMIT_VARS.values())
            self._assert_policy_env(recorded, expected, env)

    def test_no_limits_txt_keeps_the_four_base_vars_only(self):
        # Updated: claude's allowlist is now the four plus USER. The
        # launched key set (minus interpreter noise) must equal that
        # allowlist exactly, not merely contain the four.
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo, worker_args=[])
            env = self._claude_env(te)
            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "claude",
                env=env,
            )
            self.assertEqual(rc, 0, err)
            recorded = json.loads(
                (te.home / ".ssa-test" / "fake-claude" / "env.json").read_text()
            )
            self.assertNotIn("CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH", recorded)
            self.assertNotIn("CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS", recorded)
            self.assertNotIn("CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION", recorded)
            self.assertNotIn("CLAUDE_CODE_SUBAGENT_MODEL", recorded)
            self.assertNotIn("CLAUDE_CODE_SUBAGENT_MODEL_FORCE", recorded)
            spec = load_ssa("registry").load().get("claude")
            self._assert_policy_env(recorded, set(spec.env_keep), env)

    def test_kimi_dispatch_without_env_keep_keeps_the_four(self):
        registry = load_ssa("registry")
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo, worker_args=[])
            env = dict(te.env)
            env["KIMI_BIN"] = str(BIN_DIR / "fake-kimi")
            env["SSA_ALLOW_KIMI_WRITE"] = "1"
            env["SSA_TEST_LEAK"] = "1"
            env["USER"] = PARENT_USER
            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "kimi", env=env
            )
            self.assertEqual(rc, 0, err)
            recorded = json.loads(
                (te.home / ".ssa-test" / "fake-kimi" / "env.json").read_text()
            )
            expected = set(registry.DEFAULT_ENV_KEEP)
            self._assert_policy_env(recorded, expected, env)
            self.assertNotIn("USER", recorded)

    def test_scrub_env_and_shell_env_i_agree_on_claude(self):
        adapters = load_ssa("adapters")
        spec = load_ssa("registry").load().get("claude")
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo, worker_args=[])
            env = self._claude_env(te)
            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "claude",
                env=env,
            )
            self.assertEqual(rc, 0, err)
            recorded = json.loads(
                (te.home / ".ssa-test" / "fake-claude" / "env.json").read_text()
            )
            kept = adapters.scrub_env(env, spec)
            for key, value in kept.items():
                self.assertEqual(recorded[key], value, key)
            self.assertEqual(policy_keys(recorded), set(kept))

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
            self.assertNotIn("CLAUDE_CODE_SUBAGENT_MODEL", recorded)
            self.assertNotIn("CLAUDE_CODE_SUBAGENT_MODEL_FORCE", recorded)

    def test_empty_subagent_model_dispatch_exits_nonzero_naming_the_key(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo, worker_args=[])
            (task_dir / "limits.txt").write_text("subagent_model=\n")
            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "claude",
                env=self._claude_env(te),
            )
            self.assertNotEqual(rc, 0)
            self.assertIn("subagent_model", err)
            self.assertFalse(
                (te.home / ".ssa-test" / "fake-claude" / "argv.txt").exists()
            )

    def test_whitespace_subagent_model_dispatch_exits_nonzero_naming_the_key(self):
        with temp_env() as te:
            repo = make_git_repo(te.root / "repo")
            task_dir = make_task_dir(te.work_dir, repo, worker_args=[])
            (task_dir / "limits.txt").write_text("subagent_model=hai ku\n")
            rc, out, err = run_ssa(
                "dispatch", "--dir", str(task_dir), "--worker", "claude",
                env=self._claude_env(te),
            )
            self.assertNotEqual(rc, 0)
            self.assertIn("subagent_model", err)
            self.assertFalse(
                (te.home / ".ssa-test" / "fake-claude" / "argv.txt").exists()
            )


if __name__ == "__main__":
    unittest.main()
