"""Bootstrap-anchor dry-run tests — stub the gcloud seam, assert what a DRY_RUN=1 run says.

No GCP access needed: a stub `gcloud` on PATH answers every read as "nothing exists yet",
so both anchors take their create/grant branches. In dry-run those branches must ANNOUNCE
(`~ would:`) and must NOT claim (`+`), and must invoke no mutation at all.

Run: python3 -m pytest tests/ -q   (or: python3 tests/test_anchor.py)
"""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Reads answer empty/absent so every branch is a would-create; anything else is a mutation
# and gets logged, which is how we assert dry-run touched nothing.
STUB = r"""#!/usr/bin/env bash
args="$*"
case "$args" in
  *"config get-value account"*)  echo tester@example.com; exit 0 ;;
  *"projects describe"*)         case "$args" in *projectNumber*) echo 123456789 ;; esac; exit 0 ;;
  *describe*)                    exit 1 ;;
  *"get-iam-policy"*|*"services list"*) exit 0 ;;
  *)                             echo "MUTATION: $args" >> "$STUB_LOG"; exit 0 ;;
esac
"""


def run_anchor(script, env_extra=None, dry="1"):
    """Run <script> with a stub gcloud; returns (stdout+stderr, logged mutations)."""
    with tempfile.TemporaryDirectory() as tmp:
        stub = os.path.join(tmp, "gcloud")
        with open(stub, "w") as fh:
            fh.write(STUB)
        os.chmod(stub, 0o755)
        log = os.path.join(tmp, "mutations.log")
        env = dict(os.environ, PATH=tmp + os.pathsep + os.environ["PATH"],
                   DRY_RUN=dry, STUB_LOG=log)
        env.update(env_extra or {})
        proc = subprocess.run(["bash", os.path.join(ROOT, "bootstrap", script)],
                              capture_output=True, text=True, env=env)
        mutations = open(log).read() if os.path.exists(log) else ""
    assert proc.returncode == 0, f"{script} exited {proc.returncode}\n{proc.stdout}{proc.stderr}"
    return proc.stdout + proc.stderr, mutations


ANCHORS = [("anchor.sh", {}), ("fleet-anchor.sh", {"HOST_PROJECT": "host-project"})]


def test_dry_run_never_claims_it_added_anything():
    """A `+` line is the vocabulary of a real run. In dry-run it reads as "changed" and is a lie."""
    for script, env in ANCHORS:
        out, _ = run_anchor(script, env)
        offenders = [ln for ln in out.splitlines() if "\033[33m+\033[0m" in ln]
        assert not offenders, f"{script} printed {len(offenders)} '+' line(s) in dry-run: {offenders[:3]}"


def test_dry_run_announces_every_would_change():
    """`do_or_dry ... >/dev/null` used to swallow the `~ would:` line — the redirect is the
    call site's, and it took the announcement with it. Nothing may be silently planned."""
    for script, env in ANCHORS:
        out, _ = run_anchor(script, env)
        would = [ln for ln in out.splitlines() if "would:" in ln]
        assert would, f"{script} planned nothing at all — the stub should make everything absent"
        for needle in ("roles create jgdSecretIamAdmin", "add-iam-policy-binding"):
            assert any(needle in ln for ln in would), \
                f"{script}: no `~ would:` line for '{needle}' (swallowed by a redirect?)"


def test_dry_run_mutates_nothing():
    for script, env in ANCHORS:
        _, mutations = run_anchor(script, env)
        assert not mutations, f"{script} invoked gcloud mutations in dry-run:\n{mutations}"


def test_a_real_run_still_claims_and_mutates():
    """The dry-run guard in `add` must not silence a REAL run — that would trade one
    misleading output for another."""
    for script, env in ANCHORS:
        out, mutations = run_anchor(script, env, dry="0")
        assert "\033[33m+\033[0m" in out, f"{script}: a real run printed no '+' line"
        assert "MUTATION:" in mutations, f"{script}: a real run invoked no gcloud mutation"


# ── regression: folder/org reach must still bind the project-scoped custom role ──────
#
# `grant_scope()` skips project-scoped custom roles ("granted per-project"), but the run loop
# only called `grant_project` in the `grant` branch — under folder/org reach it enabled APIs
# and nothing else, so the fleet SA was never granted `jgdSecretIamAdmin` on any target.
# `resource_roles.secrets` then fails PERMISSION_DENIED fleet-wide after cutover, and a
# dry-run cannot see it: the engine reports zero drift over a subsystem it cannot read.
REACH_MODES = [
    ("org", {"REACH": "org", "ORG_ID": "250926570441"}),
    # folder reach needs ORG_ID too: a custom role cannot be created at folder scope, so
    # jgdScopeIamViewer is defined at the org and granted at the folder.
    ("folder", {"REACH": "folder", "FOLDER_ID": "123456789", "ORG_ID": "250926570441"}),
]

# The shipped template carries one placeholder row; keep the assertion derived from it
# rather than hard-coding, so a consumer editing PROJECTS does not break the test.
TARGET = "your-gcp-project"


def test_scope_reach_still_binds_project_scoped_custom_role():
    for mode, env in REACH_MODES:
        out, _ = run_anchor("fleet-anchor.sh", dict(env, HOST_PROJECT="host-project"))
        needle = "projects/%s/roles/jgdSecretIamAdmin" % TARGET
        assert needle in out, (
            "REACH=%s: no per-project jgdSecretIamAdmin binding planned. A project-scoped "
            "custom role cannot be granted at folder/org scope, so it must still be bound "
            "per-project — otherwise resource_roles.secrets fails PERMISSION_DENIED "
            "fleet-wide after cutover.\n%s" % (mode, out))


def test_scope_reach_does_not_grant_custom_role_at_scope():
    """The converse guard: granting a project-scoped custom role AT org/folder scope would
    fail at apply time (the role does not exist there). Per-project binding is the fix."""
    for mode, env in REACH_MODES:
        out, _ = run_anchor("fleet-anchor.sh", dict(env, HOST_PROJECT="host-project"))
        for ln in out.splitlines():
            if "jgdSecretIamAdmin" in ln and ("organizations add-iam-policy-binding" in ln
                                              or "folders add-iam-policy-binding" in ln):
                raise AssertionError("REACH=%s: planned a project-scoped custom role at "
                                     "%s scope: %s" % (mode, mode, ln))


def test_scope_reader_role_is_read_only_and_granted():
    """The pruner must be able to SEE scope-level grants, and must never be able to change
    them: a policed identity that can rewrite its own bindings polices nothing. Guards both
    halves — the role is created and granted, and it carries no setIamPolicy."""
    for mode, env in REACH_MODES:
        out, _ = run_anchor("fleet-anchor.sh", dict(env, HOST_PROJECT="host-project"))
        assert "jgdScopeIamViewer" in out, (
            "REACH=%s: no jgdScopeIamViewer planned — --prune provisioner would read no "
            "scope bindings at all and report a clean run over nothing.\n%s" % (mode, out))
        for ln in out.splitlines():
            if "jgdScopeIamViewer" in ln and "roles create" in ln:
                # inspect the --permissions VALUE only: the description legitimately
                # contains the word "setIamPolicy" while promising not to grant it.
                perms = ln.split("--permissions=", 1)[1].split(" ")[0]
                bad = [x for x in perms.split(",") if x.endswith("setIamPolicy")]
                assert not bad, ("REACH=%s: scope viewer role defined with %s — a policed "
                                 "identity must not rewrite its own bindings" % (mode, bad))
                assert any(x.endswith("getIamPolicy") for x in perms.split(",")), (
                    "REACH=%s: the scope viewer role cannot read an IAM policy, which is "
                    "its entire purpose: %s" % (mode, perms))


def test_engine_and_script_agree_on_the_scope_reader_role():
    """The role id is spelled in two places: the script CREATES it, the engine EXPECTS it in
    the allowed set. If they drift, the audit reports the provisioner's own read role as an
    undeclared grant and fails every prune."""
    script = open(os.path.join(ROOT, "bootstrap", "fleet-anchor.sh")).read()
    line = [l for l in script.splitlines() if l.startswith("SCOPE_READER_ROLE_ID=")]
    assert len(line) == 1, "expected exactly one SCOPE_READER_ROLE_ID in fleet-anchor.sh"
    from_script = line[0].split("=", 1)[1].strip().strip('"')
    sys.path.insert(0, os.path.join(ROOT, "engine"))
    from providers import gcp
    assert gcp.SCOPE_READER_ROLE_ID == from_script, (
        "engine says %r, fleet-anchor.sh says %r" % (gcp.SCOPE_READER_ROLE_ID, from_script))


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
