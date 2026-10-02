import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import make_git_repo, run_ssa, temp_env  # noqa: E402


def git(cwd, *args):
    return subprocess.check_output(["git", "-C", str(cwd), *args], text=True).strip()


class SuperprojectDriftTests(unittest.TestCase):
    def _layout(self, te):
        sup = make_git_repo(te.root / "super")
        sub = make_git_repo(sup / "vendor" / "ssa")
        old = git(sub, "rev-parse", "HEAD")
        (sub / "fix.txt").write_text("fix\n")
        git(sub, "add", "fix.txt")
        git(sub, "commit", "-qm", "fix")
        return sup, sub, old, git(sub, "rev-parse", "HEAD")

    def _record(self, sup, sha):
        git(sup, "update-index", "--add", "--cacheinfo", "160000,%s,vendor/ssa" % sha)

    def _stderr(self, te, sub, cmd):
        env = dict(te.env)
        env["CLAUDE_PLUGIN_ROOT"] = str(sub)
        return run_ssa(cmd, env=env)[2]

    def test_a_checkout_behind_the_recorded_commit_warns(self):
        with temp_env() as te:
            sup, sub, old, new = self._layout(te)
            self._record(sup, new)
            git(sub, "checkout", "-q", old)
            err = self._stderr(te, sub, "plan")
            self.assertIn("WARNING", err)
            self.assertIn("at %s, behind %s" % (old[:7], new[:7]), err)
            self.assertIn("submodule update -- vendor/ssa", err)

    def test_a_checkout_at_or_ahead_of_the_recorded_commit_is_quiet(self):
        with temp_env() as te:
            sup, sub, old, new = self._layout(te)
            for recorded in (new, old):
                self._record(sup, recorded)
                self.assertNotIn("WARNING", self._stderr(te, sub, "plan"))

    def test_read_only_commands_skip_the_check(self):
        with temp_env() as te:
            sup, sub, old, new = self._layout(te)
            self._record(sup, new)
            git(sub, "checkout", "-q", old)
            self.assertNotIn("WARNING", self._stderr(te, sub, "help"))


if __name__ == "__main__":
    unittest.main()
