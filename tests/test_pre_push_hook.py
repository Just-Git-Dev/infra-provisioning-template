"""The pre-push hook must gate the COMMIT BEING PUSHED, and must not leak git's env into checks.

`make ci` reads the working tree. A hook that just runs it validates whatever is checked out,
which can differ from the sha being pushed; so the hook refuses a dirty tree and a pushed ref
whose sha is not HEAD. It also unsets every `git rev-parse --local-env-vars` name, because a
test that calls git inside a hook otherwise acts on the hooked repo.

Run:
    python3 tests/test_pre_push_hook.py     (or: python3 -m pytest tests/ -q)
"""
import os
import subprocess
import sys
import tempfile

HOOK = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", ".githooks", "pre-push"))
ZERO = "0" * 40


def _clean_env(**extra):
    out = subprocess.run(["git", "rev-parse", "--local-env-vars"], capture_output=True, text=True)
    env = {k: v for k, v in os.environ.items() if k not in out.stdout.split()}
    env.update(extra)
    return env


def git(cwd, *args):
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True,
                          env=_clean_env(GIT_CONFIG_GLOBAL="/dev/null"), check=True).stdout.strip()


def repo():
    d = tempfile.mkdtemp()
    git(d, "init", "-q", "-b", "main")
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        git(d, "config", k, v)
    for name in ("a", "b"):
        with open(os.path.join(d, name), "w") as fh:
            fh.write(name)
        git(d, "add", name)
        git(d, "commit", "-q", "-m", name)
    return d


def fake_make(gate):
    """A dir holding a `make` whose body is `gate`; first on PATH it stands in for `make ci`.

    The hook has no env seam that replaces the gate (a stray `PRE_PUSH_GATE=true` would have
    silently disabled it), so the tests substitute the command the hook runs instead."""
    d = tempfile.mkdtemp()
    path = os.path.join(d, "make")
    with open(path, "w") as fh:
        fh.write(f"#!/usr/bin/env bash\n{gate}\n")
    os.chmod(path, 0o755)
    return d


def hook(d, stdin, gate="true", **env):
    env.setdefault("PATH", fake_make(gate) + os.pathsep + os.environ["PATH"])
    return subprocess.run([HOOK], cwd=d, input=stdin, capture_output=True, text=True,
                          env=_clean_env(**env))


def test_pre_push_gate_env_var_does_not_replace_the_gate():
    d = repo()
    head = git(d, "rev-parse", "HEAD")
    r = hook(d, f"refs/heads/main {head} refs/heads/main {ZERO}\n", gate="false", PRE_PUSH_GATE="true")
    assert r.returncode == 1 and "gate FAILED" in r.stderr


def _push_tag(gate, at):
    """Real `git push` of an annotated tag to a local bare remote, hook installed."""
    d = repo()
    bare = tempfile.mkdtemp()
    git(bare, "init", "-q", "--bare")
    git(d, "remote", "add", "origin", bare)
    git(d, "config", "core.hooksPath", os.path.dirname(HOOK))
    git(d, "tag", "-a", "-m", "release", "v1.0.0", at)
    path = fake_make(gate) + os.pathsep + os.environ["PATH"]
    return subprocess.run(["git", "-C", d, "push", "origin", "refs/tags/v1.0.0"],
                          capture_output=True, text=True,
                          env=_clean_env(GIT_CONFIG_GLOBAL="/dev/null", PATH=path))


def test_annotated_tag_at_head_passes_and_runs_the_gate():
    r = _push_tag("echo GATE-RAN >&2", "HEAD")
    assert r.returncode == 0 and "GATE-RAN" in r.stderr, r.stderr


def test_annotated_tag_at_another_commit_is_refused():
    r = _push_tag("true", "HEAD~1")
    assert r.returncode != 0 and "HEAD is" in r.stderr, r.stderr


def test_passes_when_clean_head_and_gate_green():
    d = repo()
    head = git(d, "rev-parse", "HEAD")
    assert hook(d, f"refs/heads/main {head} refs/heads/main {ZERO}\n").returncode == 0


def test_gate_failure_blocks_the_push():
    d = repo()
    head = git(d, "rev-parse", "HEAD")
    r = hook(d, f"refs/heads/main {head} refs/heads/main {ZERO}\n", gate="false")
    assert r.returncode == 1 and "gate FAILED" in r.stderr


def test_refuses_a_pushed_ref_that_is_not_head():
    d = repo()
    older = git(d, "rev-parse", "HEAD~1")
    r = hook(d, f"refs/heads/old {older} refs/heads/old {ZERO}\n")
    assert r.returncode == 1 and "HEAD is" in r.stderr


def test_refuses_a_dirty_tree():
    d = repo()
    head = git(d, "rev-parse", "HEAD")
    with open(os.path.join(d, "a"), "w") as fh:
        fh.write("changed")
    r = hook(d, f"refs/heads/main {head} refs/heads/main {ZERO}\n")
    assert r.returncode == 1 and "dirty" in r.stderr


def test_branch_deletion_needs_no_gate():
    d = repo()
    assert hook(d, f"(delete) {ZERO} refs/heads/x abc\n", gate="false").returncode == 0


def test_git_env_is_scrubbed_before_the_gate_runs():
    d = repo()
    head = git(d, "rev-parse", "HEAD")
    gate = 'test -z "${GIT_DIR:-}" && test -z "${GIT_INDEX_FILE:-}"'
    r = hook(d, f"refs/heads/main {head} refs/heads/main {ZERO}\n", gate=gate,
             GIT_DIR=os.path.join(d, ".git"), GIT_INDEX_FILE=os.path.join(d, ".git", "index"))
    assert r.returncode == 0, r.stderr


if __name__ == "__main__":
    test_passes_when_clean_head_and_gate_green()
    test_gate_failure_blocks_the_push()
    test_refuses_a_pushed_ref_that_is_not_head()
    test_refuses_a_dirty_tree()
    test_branch_deletion_needs_no_gate()
    test_git_env_is_scrubbed_before_the_gate_runs()
    test_pre_push_gate_env_var_does_not_replace_the_gate()
    test_annotated_tag_at_head_passes_and_runs_the_gate()
    test_annotated_tag_at_another_commit_is_refused()
    print("pre-push hook: OK")
    sys.exit(0)
