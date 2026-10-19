"""`ci.yml` and `make ci` must be the same set of checks, by construction.

Four PR runs went red on 2026-10-19 for checks the local run had skipped, because local checks
were retyped by hand and drifted from `ci.yml`. The fix is one definition per check: a make
target. So every `run:` step in ci.yml must be `make <target>` (CI-only plumbing, the pinned
linter installers, is the one named exception), every such target must be listed in the
Makefile's `CHECKS`, which is exactly what `make ci` runs, and every CHECK must be called by
ci.yml. The pinned tool versions in the installer steps must equal the Makefile's, because
`make ci` refuses a local linter of a different version.

Run:
    python3 tests/test_ci_make_targets.py     (or: python3 -m pytest tests/ -q)
"""
import os
import re
import sys

import yaml

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
CI_YML = os.path.join(ROOT, ".github", "workflows", "ci.yml")
MAKEFILE = os.path.join(ROOT, "Makefile")
PLUMBING_PREFIX = "install pinned "


def run_steps():
    with open(CI_YML, encoding="utf-8") as fh:
        wf = yaml.safe_load(fh)
    steps = [s for job in wf["jobs"].values() for s in job["steps"] if "run" in s]
    assert steps, "ci.yml has no run: steps; the parser is looking in the wrong place"
    return steps


def makefile_text():
    assert os.path.exists(MAKEFILE), "no Makefile: `make ci` does not exist yet"
    with open(MAKEFILE, encoding="utf-8") as fh:
        return fh.read()


def make_var(text, name):
    m = re.search(rf"^{name}\s*[:?]?=\s*(\S+)", text, re.M)
    assert m, f"Makefile does not define {name}"
    return m.group(1)


def checks(text):
    m = re.search(r"^CHECKS\s*:?=\s*((?:.*\\\n)*.*)$", text, re.M)
    assert m, "Makefile has no CHECKS list (the targets `make ci` runs)"
    return m.group(1).replace("\\\n", " ").split()


# Targets that call shellcheck/actionlint from PATH: in CI the pinned installers must already have run.
NEEDS_PINNED_TOOLS = {"tools", "actionlint", "canary", "shellcheck", "composite"}


def installer_after_tool_step(jobs):
    """(job, installer, target) for a pinned-tool install step that follows a make step needing it.

    The first CI run of this gate went red exactly so: `make shellcheck` ran before the pinned
    actionlint install, and `make tools` could not find the tool (exit 127)."""
    bad = []
    for job_name, job in jobs.items():
        seen = None
        for step in job["steps"]:
            if "run" not in step:
                continue
            m = re.fullmatch(r"make ([\w-]+)", step["run"].strip())
            if m and m.group(1) in NEEDS_PINNED_TOOLS and seen is None:
                seen = m.group(1)
            elif seen and step.get("name", "").startswith(PLUMBING_PREFIX):
                bad.append((job_name, step["name"], seen))
    return bad


def test_pinned_check_fails_on_a_path_that_does_not_exist():
    """A mistyped root used to be skipped silently while the other roots still passed."""
    import subprocess
    import tempfile
    script = os.path.join(ROOT, "scripts", "check_pinned_actions.py")
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "a.yml"), "w", encoding="utf-8") as fh:
            fh.write("jobs:\n  a:\n    steps:\n      - uses: actions/checkout@" + "a" * 40 + "\n")
        ok = subprocess.run([sys.executable, script, d], capture_output=True, text=True)
        assert ok.returncode == 0, ok.stdout
        r = subprocess.run([sys.executable, script, d, os.path.join(d, "no-such.yml")],
                           capture_output=True, text=True)
        assert r.returncode == 1 and "no-such.yml" in r.stdout, r.stdout


def test_pinned_installers_run_before_the_make_steps_that_need_them():
    with open(CI_YML, encoding="utf-8") as fh:
        jobs = yaml.safe_load(fh)["jobs"]
    bad = installer_after_tool_step(jobs)
    assert not bad, f"pinned-tool installer after a make step that needs it: {bad}"
    late = {"j": {"steps": [{"run": "make shellcheck"},
                            {"name": "install pinned actionlint", "run": "true"}]}}
    assert installer_after_tool_step(late) == [("j", "install pinned actionlint", "shellcheck")]


def targets_called():
    called, plumbing = [], 0
    for step in run_steps():
        name = step.get("name", "")
        body = step["run"].strip()
        m = re.fullmatch(r"make ([\w-]+)", body)
        if m:
            called.append(m.group(1))
        elif name.startswith(PLUMBING_PREFIX):
            plumbing += 1
        else:
            raise AssertionError(
                f"ci.yml step {name or body.splitlines()[0]!r} is not `make <target>`; "
                "move the check into the Makefile (or name it 'install pinned ...' if it is "
                "only a pinned-tool installer)")
    return called, plumbing


def test_every_check_step_is_a_make_target_listed_in_make_ci():
    text = makefile_text()
    listed = checks(text)
    called, _ = targets_called()
    assert called, "no make steps found in ci.yml"
    missing = [t for t in called if t not in listed]
    assert not missing, f"ci.yml calls {missing}, which `make ci` does not run (CHECKS={listed})"
    for t in called:
        assert re.search(rf"^{re.escape(t)}\s*:", text, re.M), f"no Makefile target {t!r}"


def test_make_ci_runs_nothing_ci_does_not():
    called, _ = targets_called()
    extra = [t for t in checks(makefile_text()) if t not in called]
    assert not extra, f"`make ci` runs {extra}, which ci.yml never calls"


def test_pinned_tool_versions_match_the_makefile():
    text = makefile_text()
    with open(CI_YML, encoding="utf-8") as fh:
        raw = fh.read()
    sc, al = make_var(text, "SHELLCHECK_VERSION"), make_var(text, "ACTIONLINT_VERSION")
    assert re.search(rf"SC_VERSION:\s*v{re.escape(sc)}\b", raw), \
        f"ci.yml SC_VERSION differs from Makefile SHELLCHECK_VERSION={sc}"
    assert re.search(rf"AL_VERSION:\s*{re.escape(al)}\b", raw), \
        f"ci.yml AL_VERSION differs from Makefile ACTIONLINT_VERSION={al}"


def test_checker_catches_a_retyped_step():
    """Self-test: a raw command step, and a make target outside CHECKS, are both detected."""
    assert re.fullmatch(r"make ([\w-]+)", "python3 -m pytest tests/ -q") is None
    assert checks("CHECKS := a b \\\n  c\nfoo:\n") == ["a", "b", "c"]


def test_pinned_check_flags_a_mutable_ref_and_a_sha_passes():
    """The check ran as `grep -P`, which BSD grep lacks: it errored and the step still passed."""
    import subprocess
    import tempfile
    script = os.path.join(ROOT, "scripts", "check_pinned_actions.py")
    with tempfile.TemporaryDirectory() as d:
        wf = os.path.join(d, "a.yml")

        def run(line):
            with open(wf, "w", encoding="utf-8") as fh:
                fh.write(f"jobs:\n  a:\n    steps:\n      {line}\n")
            return subprocess.run([sys.executable, script, d], capture_output=True, text=True)

        assert run("- uses: actions/checkout@v4").returncode == 1
        assert run("- uses: actions/checkout@" + "a" * 40 + " # v5").returncode == 0
        assert run("- uses: ./local-action").returncode == 1   # examined nothing third-party
        # a single file argument (action.yml) is scanned too, not only directories
        r = subprocess.run([sys.executable, script, wf], capture_output=True, text=True)
        assert r.returncode == 1   # wf still holds the ./local-action line: nothing examined
        with open(wf, "w", encoding="utf-8") as fh:
            fh.write("runs:\n  steps:\n    - uses: actions/checkout@v4\n")
        assert subprocess.run([sys.executable, script, wf], capture_output=True, text=True).returncode == 1


if __name__ == "__main__":
    test_pinned_check_flags_a_mutable_ref_and_a_sha_passes()
    test_every_check_step_is_a_make_target_listed_in_make_ci()
    test_make_ci_runs_nothing_ci_does_not()
    test_pinned_tool_versions_match_the_makefile()
    test_checker_catches_a_retyped_step()
    test_pinned_check_fails_on_a_path_that_does_not_exist()
    test_pinned_installers_run_before_the_make_steps_that_need_them()
    print("ci make targets: OK")
    sys.exit(0)
