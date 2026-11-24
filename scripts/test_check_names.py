#!/usr/bin/env python3
# VENDORED from claude-config skills/public-name-guard @ 41f1f42f3b34 — edit canonical, not this copy
"""Tests for check_names.py: run as a plain script (`python3 test_check_names.py`), stdlib only.

Every test builds a throwaway git repo and a throwaway pattern list. The words used here are
invented (zorblax, quuxcorp, ...): this file is vendored into PUBLIC repos, so it must never
hold a real listed word.
"""
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "check_names.py")
SECRET_WORDS = ("zorblax", "quuxcorp", "frobnitz-helper", "Plughcase")
LIST = r"""# test list
\bzorblax\b

\bquuxcorp\b
\bfrobnitz-helper\b
\bplughcase\b
"""


def git(cwd, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True,
                          capture_output=True, text=True).stdout.strip()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q", "-b", "main")
        self.listfile = os.path.join(self.tmp.name, "list.txt")
        with open(self.listfile, "w") as f:
            f.write(LIST)
        self.write("README.md", "hello\nnothing to see\n")
        self.commit("initial")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text, mode="w"):
        p = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, mode) as f:
            f.write(text)

    def commit(self, msg):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", msg)
        return git(self.repo, "rev-parse", "HEAD")

    def run_check(self, *args, env_extra=None, listfile=True):
        env = {k: v for k, v in os.environ.items()
               if k not in ("NAME_DENYLIST", "NAME_DENYLIST_FILE", "PR_TITLE", "PR_BODY", "NAMES_RANGE")
               and not k.startswith("GIT_")}
        env["HOME"] = self.tmp.name   # the default ~/.config path must not find the real list
        if listfile:
            env["NAME_DENYLIST_FILE"] = self.listfile
        env.update(env_extra or {})
        r = subprocess.run([sys.executable, SCRIPT, "--root", self.repo, *args], env=env,
                           capture_output=True, text=True)
        return r.returncode, r.stdout + r.stderr

    def assertNoSecret(self, out):
        for w in SECRET_WORDS:
            self.assertNotIn(w.lower(), out.lower(), f"matched text leaked into output: {out!r}")


class Files(Base):
    def test_clean_tree_passes_with_nonzero_counts(self):
        rc, out = self.run_check()
        self.assertEqual(rc, 0, out)
        self.assertIn("names: examined 1 files, 0 commit messages, 0 PR fields against 4 patterns", out)

    def test_file_hit_reports_path_line_pattern_and_never_the_text(self):
        self.write("docs/a.md", "one\ntwo mentions Zorblax here\n")
        self.commit("add doc")
        rc, out = self.run_check()
        self.assertEqual(rc, 1, out)
        self.assertIn("docs/a.md:2: pattern #1", out)
        self.assertNoSecret(out)

    def test_word_boundary_fatally_not_tally_style(self):
        self.write("x.txt", "this failed frobnitz-helperish and fatally-frobnitz-helpers\n")
        self.commit("x")
        rc, out = self.run_check()
        # `\bfrobnitz-helper\b`: "frobnitz-helperish" and "frobnitz-helpers" are not word-bounded hits
        self.assertEqual(rc, 0, out)

    def test_case_insensitive(self):
        self.write("x.txt", "PLUGHCASE\n")
        self.commit("x")
        rc, out = self.run_check()
        self.assertEqual(rc, 1, out)
        self.assertIn("x.txt:1: pattern #4", out)

    def test_codeowners_exempt(self):
        self.write("CODEOWNERS", "* @zorblax\n")
        self.write(".github/CODEOWNERS", "* @zorblax\n")
        self.commit("owners")
        rc, out = self.run_check()
        self.assertEqual(rc, 0, out)

    def test_untracked_file_not_scanned(self):
        self.write("scratch.txt", "zorblax\n")
        rc, out = self.run_check()
        self.assertEqual(rc, 0, out)

    def test_binary_file_skipped(self):
        self.write("img.bin", b"\x00\x01zorblax\x00".decode("latin-1"), mode="w")
        self.commit("bin")
        rc, out = self.run_check()
        self.assertEqual(rc, 0, out)

    def test_file_path_hit(self):
        self.write("docs/quuxcorp-notes.md", "fine\n")
        self.commit("p")
        rc, out = self.run_check()
        self.assertEqual(rc, 1, out)
        self.assertIn("pattern #2", out)
        self.assertIn("(file path)", out)
        self.assertNoSecret(out)


class ListSource(Base):
    def test_missing_list_fails_with_message(self):
        rc, out = self.run_check(listfile=False)
        self.assertEqual(rc, 1, out)
        self.assertIn("name list not found: create ~/.config/jgd/public-denylist.txt or set NAME_DENYLIST", out)

    def test_empty_env_secret_falls_through_and_fails(self):
        rc, out = self.run_check(listfile=False, env_extra={"NAME_DENYLIST": ""})
        self.assertEqual(rc, 1, out)
        self.assertIn("name list not found", out)

    def test_named_file_missing_fails(self):
        rc, out = self.run_check(listfile=False,
                                 env_extra={"NAME_DENYLIST_FILE": os.path.join(self.tmp.name, "nope")})
        self.assertEqual(rc, 1, out)
        self.assertIn("name list not found", out)

    def test_empty_list_fails(self):
        with open(self.listfile, "w") as f:
            f.write("# only a comment\n\n")
        rc, out = self.run_check()
        self.assertEqual(rc, 1, out)
        self.assertIn("0 patterns", out)

    def test_bad_regex_exit_2_names_line_only(self):
        with open(self.listfile, "w") as f:
            f.write("\\bzorblax\\b\n(quuxcorp\n")
        rc, out = self.run_check()
        self.assertEqual(rc, 2, out)
        self.assertIn("line 2", out)
        self.assertNoSecret(out)

    def test_env_list_wins_over_file(self):
        self.write("x.txt", "quuxcorp\n")
        self.commit("x")
        rc, out = self.run_check(env_extra={"NAME_DENYLIST": "\\bzorblax\\b\n"})
        self.assertEqual(rc, 0, out)
        self.assertIn("against 1 patterns", out)

    def test_default_home_path_used(self):
        cfg = os.path.join(self.tmp.name, ".config", "jgd")
        os.makedirs(cfg)
        with open(os.path.join(cfg, "public-denylist.txt"), "w") as f:
            f.write(LIST)
        rc, out = self.run_check(listfile=False)
        self.assertEqual(rc, 0, out)
        self.assertIn("against 4 patterns", out)


class Commits(Base):
    def test_commit_message_in_range_caught_outside_ignored(self):
        old = self.commit_empty("mention zorblax long ago")
        base = git(self.repo, "rev-parse", "HEAD")
        self.commit_empty("clean one")
        bad = self.commit_empty("subject\n\nbody says quuxcorp")
        rc, out = self.run_check("--range", f"{base}..HEAD")
        self.assertEqual(rc, 1, out)
        self.assertIn(f"commit {bad[:12]}: pattern #2", out)
        self.assertNotIn(old[:12], out)
        self.assertIn("2 commit messages", out)
        self.assertNoSecret(out)

    def test_range_from_env(self):
        base = git(self.repo, "rev-parse", "HEAD")
        self.commit_empty("zorblax")
        rc, out = self.run_check(env_extra={"NAMES_RANGE": f"{base}..HEAD"})
        self.assertEqual(rc, 1, out)

    def test_empty_range_scans_no_commits(self):
        self.commit_empty("zorblax")
        rc, out = self.run_check(env_extra={"NAMES_RANGE": ""})
        self.assertEqual(rc, 0, out)
        self.assertIn("0 commit messages", out)

    def test_bad_range_is_an_error_not_a_pass(self):
        rc, out = self.run_check("--range", "nosuchref..HEAD")
        self.assertEqual(rc, 2, out)

    def commit_empty(self, msg):
        git(self.repo, "commit", "-q", "--allow-empty", "-m", msg)
        return git(self.repo, "rev-parse", "HEAD")


class PullRequest(Base):
    def test_pr_title_hit(self):
        rc, out = self.run_check(env_extra={"PR_TITLE": "Add Zorblax support", "PR_BODY": "clean"})
        self.assertEqual(rc, 1, out)
        self.assertIn("PR title: pattern #1", out)
        self.assertIn("2 PR fields", out)
        self.assertNoSecret(out)

    def test_pr_body_hit_reports_line(self):
        rc, out = self.run_check(env_extra={"PR_TITLE": "ok", "PR_BODY": "a\nb\nfrobnitz-helper"})
        self.assertEqual(rc, 1, out)
        self.assertIn("PR body:3: pattern #3", out)

    def test_clean_pr_fields_pass(self):
        rc, out = self.run_check(env_extra={"PR_TITLE": "ok", "PR_BODY": ""})
        self.assertEqual(rc, 0, out)
        self.assertIn("1 PR fields", out)


if __name__ == "__main__":
    unittest.main(verbosity=1)
