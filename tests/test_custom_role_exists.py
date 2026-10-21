"""A custom role must EXIST before the engine plans to bind it (dry-run AND apply).

Bug 2026-10-21: dry-run printed `~ would bind organizations/<o>/roles/repoAdmin` while the role did
not exist; the apply then failed INVALID_ARGUMENT "Role ... does not exist in the resource's
hierarchy". Spec (AGENTS.md "Dry-run is ... accurate"): a `~ would` line is a change apply can make.

The lookup goes through the real `gcloud` seam (P.gcloud) so error classification is exercised;
the stub replays RECORDED gcloud output from tests/fixtures/ (commands in fixtures/README.md).
"""
import io
import os
import subprocess
import sys
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engine"))
import core  # noqa: E402
from providers import gcp as P  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
ORG_ROLE = "organizations/250926570441/roles/repoAdmin"
MISSING = "organizations/250926570441/roles/noSuchRoleXyz"
PROJ_ROLE = "projects/no-such-proj-zz9/roles/repoAdmin"


def _fx(name):
    with open(os.path.join(FIX, name)) as f:
        return f.read()


def _run(roles, describe, dry=True):
    """Run ensure_service_accounts; `describe(args)` -> stdout or raises CalledProcessError."""
    P._ROLE_STATE.clear()
    lookups, mutations = [], []

    def fake_gcloud(args, project=None, check=True):
        if args[:3] == ["iam", "roles", "describe"]:
            lookups.append(list(args))
            return subprocess.CompletedProcess(args, 0, describe(args), "")
        mutations.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    P.gcloud = fake_gcloud
    P.gout = lambda args, project=None: ""      # SA absent, nothing bound: everything is a plan
    core.DRY = dry
    cfg = {"project": {"github_org": "Example-Org"},
           "service_accounts": [{"name": "repo-admin", "roles": roles}]}
    buf, msg = io.StringIO(), None
    try:
        with redirect_stdout(buf):
            try:
                P.ensure_service_accounts(cfg, "p")
            except SystemExit as e:
                msg = str(e)
    finally:
        core.DRY = False
    return lookups, mutations, buf.getvalue(), msg


def _exists(args):
    return _fx("role_describe_org_exists.out")


def _not_found(args):
    raise subprocess.CalledProcessError(1, args, "", _fx("role_describe_org_missing.err"))


def _denied(args):
    raise subprocess.CalledProcessError(1, args, "", _fx("role_describe_denied.err"))


def test_missing_custom_role_fails_loudly_and_plans_no_would_line_in_dry_run():
    _, muts, out, msg = _run([MISSING], _not_found, dry=True)
    assert msg and MISSING in msg and "manage-roles.sh" in msg and "manage-custom-roles.yml" in msg, msg
    assert f"bind {MISSING}" not in out, out


def test_missing_custom_role_fails_in_apply_before_any_binding():
    _, muts, out, msg = _run([MISSING], _not_found, dry=False)
    assert msg and MISSING in msg, msg
    assert not [m for m in muts if "add-iam-policy-binding" in m], muts


def test_existing_custom_role_still_plans_would_and_describe_uses_org_flag():
    lookups, _, out, msg = _run([ORG_ROLE], _exists, dry=True)
    assert msg is None, msg
    assert f"bind {ORG_ROLE}" in out, out
    assert any("--organization=250926570441" in a for a in lookups[0]) and "repoAdmin" in lookups[0], lookups


def test_predefined_role_triggers_no_lookup():
    lookups, _, out, msg = _run(["roles/run.admin"], _exists, dry=True)
    assert msg is None and lookups == [], (lookups, msg)


def test_project_custom_role_is_looked_up_with_project_flag():
    lookups, _, _, msg = _run([PROJ_ROLE], _exists, dry=True)
    assert msg is None and "--project=no-such-proj-zz9" in lookups[0], (lookups, msg)


def test_lookup_error_other_than_not_found_fails_closed_with_that_error():
    _, _, out, msg = _run([PROJ_ROLE], _denied, dry=True)
    assert msg and "PERMISSION_DENIED" in msg, msg
    assert f"bind {PROJ_ROLE}" not in out, out


def test_one_lookup_per_role_per_run():
    lookups, _, _, msg = _run([ORG_ROLE, ORG_ROLE], _exists, dry=True)
    assert msg is None and len(lookups) == 1, lookups


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"FAIL {name}: {type(e).__name__}: {e}")
    sys.exit(1 if fails else 0)
