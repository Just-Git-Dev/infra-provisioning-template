"""Environment-scoped WIF link + detect-only SA audit (RED until the engine implements them).

Plan: ADR-010 (project-owned repo links); plan rulings 13 and 16 of repos-as-code. Seam: the same
`P.gout` (reads) / `P.gcloud` (mutations) stubs as test_engine.py. These tests RUN IN APPLY MODE
(core.DRY = False) with a RECORDING gcloud stub, so "never fixes / never binds X" is proven against
every call that would really have reached gcloud, not against a dry-run line.

CONTRACT the implementer must meet
==================================

(e) Config shape — a service account may declare environment-scoped links:

        service_accounts:
          - name: repo-admin
            wif_subjects:                      # NEW key, sibling of `wif_repos`
              - {repo: Example-Org/project, environment: repo-admin}

    `ensure_wif(cfg, project)` binds, per entry, the member

        principal://iam.googleapis.com/projects/<num>/locations/global/workloadIdentityPools/<pool>
            /subject/repo:<Org>/<repo>:environment:<env>        (no whitespace; <Org>/<repo> = `repo`)

    with role `roles/iam.workloadIdentityUser` ON the SA (`iam service-accounts
    add-iam-policy-binding`, one call per entry). It NEVER binds the repo-wide
    `.../attribute.repository/<repo>` principalSet for a `wif_subjects` entry (only `wif_repos` does
    that). It is idempotent: when the live SA policy already has that member+role it issues no call.
    `wif_subjects` and `wif_repos` may coexist on one SA; each is bound only by its own key.

(f) Detect-only audit — `audit_service_accounts(cfg, project)` in engine/providers/gcp.py (and
    reachable from P.PRUNERS values, so Provision runs it). For each DECLARED service account it
    reads the SA's own IAM policy with `iam service-accounts get-iam-policy <email> ... --format=json`
    (stdout = the policy JSON `{"bindings": [{"role": ..., "members": [...]}]}`), and for every
    member that is NOT (1) a `principalSet://` / `principal://` member of the SAME org's pool
    entries, i.e. whose repository belongs to `project.github_org`, nor (2) a declared grant (an
    `act_as` holder, or a `principals:` member), it prints an `err` line containing `UNDECLARED`
    and, after examining ALL SAs, signals failure with `sys.exit("<message containing
    'undeclared'>")` (the `_audit_scope_grants` / prune_provisioner shape). It issues NO mutating
    gcloud call of any kind: no remove-iam-policy-binding, no set-iam-policy, no add-*.
"""
import io
import os
import sys
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engine"))
import core  # noqa: E402
from providers import gcp as P  # noqa: E402

import json  # noqa: E402
import subprocess  # noqa: E402

POOL = "github-actions"
NUM = "42"
POOLBASE = f"//iam.googleapis.com/projects/{NUM}/locations/global/workloadIdentityPools/{POOL}"
ROLE_WIF = "roles/iam.workloadIdentityUser"
ENV_MEMBER = (f"principal:{POOLBASE}/subject/repo:Example-Org/project:environment:repo-admin")
REPOWIDE_PROJECT = f"principalSet:{POOLBASE}/attribute.repository/Example-Org/project"


class Recorder:
    """Stub seam: reads answered by `reads(args)`, every gcloud call recorded."""

    def __init__(self, reads):
        self.calls = []
        self.reads = reads

    def install(self):
        core.DRY = False
        P.gout = lambda args, project=None: self.reads(args)

        def fake_gcloud(args, project=None, check=True):
            self.calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, "", "")
        P.gcloud = fake_gcloud


def _run(handler, cfg, reads):
    rec = Recorder(reads)
    rec.install()
    buf, exit_msg = io.StringIO(), None
    saved_dry = core.DRY
    try:
        with redirect_stdout(buf):
            try:
                handler(cfg, "p")
            except SystemExit as e:
                exit_msg = str(e)
    finally:
        core.DRY = saved_dry
    return rec, buf.getvalue(), exit_msg


def _cfg(sas):
    return {"project": {"github_org": "Example-Org", "gcp_number": NUM},
            "wif": {"pool": POOL, "provider": "github"}, "service_accounts": sas}


def _pool_exists(extra=None):
    def reads(args):
        if "workload-identity-pools" in args and "describe" in args:
            return "exists"
        return extra(args) if extra else ""
    return reads


def _adds(rec):
    return [c for c in rec.calls if "add-iam-policy-binding" in c]


# ── (e) environment-scoped link ──────────────────────────────────────────────
def test_wif_subjects_binds_the_environment_principal_and_nothing_repo_wide():
    cfg = _cfg([{"name": "repo-admin",
                 "wif_subjects": [{"repo": "Example-Org/project", "environment": "repo-admin"}]}])
    rec, out, exit_msg = _run(P.ensure_wif, cfg, _pool_exists())
    assert exit_msg is None, exit_msg
    adds = _adds(rec)
    assert len(adds) == 1, f"expected exactly one binding call, got {rec.calls}"
    call = " ".join(adds[0])
    assert f"--member={ENV_MEMBER}" in adds[0], call
    assert f"--role={ROLE_WIF}" in adds[0], call
    assert "repo-admin@p.iam.gserviceaccount.com" in adds[0], call
    assert not any("attribute.repository" in a for c in rec.calls for a in c), \
        f"a repo-wide principalSet was bound: {rec.calls}"


def test_wif_subjects_is_idempotent_when_the_member_is_already_bound():
    # a second, UNBOUND entry is the positive control: only it may be bound (not vacuous on a no-op)
    cfg = _cfg([{"name": "repo-admin", "wif_subjects": [
        {"repo": "Example-Org/project", "environment": "repo-admin"},
        {"repo": "Example-Org/project", "environment": "break-glass"}]}])

    def bound(args):
        j = " ".join(args)
        if "get-iam-policy" in j and ENV_MEMBER in j:
            return ROLE_WIF          # live policy already has member+role
        return ""
    rec, out, exit_msg = _run(P.ensure_wif, cfg, _pool_exists(bound))
    assert exit_msg is None, exit_msg
    members = [a for c in _adds(rec) for a in c if a.startswith("--member=")]
    assert members == [
        f"--member=principal:{POOLBASE}/subject/repo:Example-Org/project:environment:break-glass"], members


def test_wif_subjects_does_not_create_the_repo_wide_binding_but_wif_repos_still_does():
    cfg = _cfg([{"name": "repo-admin",
                 "wif_subjects": [{"repo": "Example-Org/project", "environment": "repo-admin"}]},
                {"name": "deployer", "wif_repos": ["Example-Org/payments-api"]}])
    rec, out, exit_msg = _run(P.ensure_wif, cfg, _pool_exists())
    assert exit_msg is None, exit_msg
    members = [a for c in _adds(rec) for a in c if a.startswith("--member=")]
    assert sorted(members) == sorted([
        f"--member={ENV_MEMBER}",
        f"--member=principalSet:{POOLBASE}/attribute.repository/Example-Org/payments-api"]), members


def test_wif_subjects_two_environments_bind_two_distinct_members():
    cfg = _cfg([{"name": "repo-admin", "wif_subjects": [
        {"repo": "Example-Org/project", "environment": "repo-admin"},
        {"repo": "Example-Org/project", "environment": "break-glass"}]}])
    rec, out, exit_msg = _run(P.ensure_wif, cfg, _pool_exists())
    members = sorted(a for c in _adds(rec) for a in c if a.startswith("--member="))
    assert members == sorted([
        f"--member={ENV_MEMBER}",
        f"--member=principal:{POOLBASE}/subject/repo:Example-Org/project:environment:break-glass"]), members


# ── (f) detect-only SA audit ─────────────────────────────────────────────────
SA_EMAIL = "svc-a@p.iam.gserviceaccount.com"
SAME_ORG_SET = f"principalSet:{POOLBASE}/attribute.repository/Example-Org/payments-api"
FOREIGN_SET = f"principalSet:{POOLBASE}/attribute.repository/Other-Org/payments-api"
DECLARED_ACTOR = "serviceAccount:deployer@p.iam.gserviceaccount.com"


def _audit_cfg():
    return _cfg([{"name": "svc-a"},
                 {"name": "deployer", "act_as": ["svc-a"]}])


def _policy_reads(members_by_role):
    pol = json.dumps({"bindings": [{"role": r, "members": m} for r, m in members_by_role.items()]})

    def reads(args):
        j = " ".join(args)
        if "get-iam-policy" in j and "--format=json" in j and SA_EMAIL in j:
            return pol
        if "get-iam-policy" in j and "--format=json" in j:
            return json.dumps({"bindings": []})
        return ""
    return reads


def test_audit_exists_and_is_run_by_provision():
    assert hasattr(P, "audit_service_accounts"), "gcp.audit_service_accounts is the audit entry point"
    assert P.audit_service_accounts in P.PRUNERS.values() or P.audit_service_accounts in P.HANDLERS.values()


def test_audit_is_clean_for_same_org_links_and_declared_grants():
    reads = _policy_reads({
        ROLE_WIF: [SAME_ORG_SET, ENV_MEMBER],
        "roles/iam.serviceAccountUser": [DECLARED_ACTOR]})
    rec, out, exit_msg = _run(P.audit_service_accounts, _audit_cfg(), reads)
    assert exit_msg is None, f"clean SA failed the audit: {exit_msg}\n{out}"
    assert "UNDECLARED" not in out
    assert rec.calls == []


def test_audit_reports_every_undeclared_binding_and_exits_nonzero():
    reads = _policy_reads({
        ROLE_WIF: [SAME_ORG_SET, FOREIGN_SET],
        "roles/iam.serviceAccountTokenCreator": ["user:stranger@example.com", "allUsers"],
        "roles/iam.serviceAccountUser": [DECLARED_ACTOR,
                                         "serviceAccount:rogue@p.iam.gserviceaccount.com"]})
    rec, out, exit_msg = _run(P.audit_service_accounts, _audit_cfg(), reads)
    assert exit_msg is not None, f"undeclared bindings did not fail the run:\n{out}"
    assert "undeclared" in exit_msg.lower(), exit_msg
    for needle in ("Other-Org", "stranger@example.com", "allUsers", "rogue@p."):
        assert needle in out, f"{needle} not reported:\n{out}"
    for ok in ("Example-Org/payments-api", "deployer@p."):
        assert not any(ok in l and "UNDECLARED" in l for l in out.splitlines()), \
            f"declared/same-org member {ok} was flagged:\n{out}"


def test_audit_removes_nothing_even_in_apply_mode():
    reads = _policy_reads({
        "roles/owner": ["user:stranger@example.com"],
        ROLE_WIF: [FOREIGN_SET]})
    rec, out, exit_msg = _run(P.audit_service_accounts, _audit_cfg(), reads)
    assert exit_msg is not None and "undeclared" in exit_msg.lower(), (exit_msg, out)
    assert rec.calls == [], f"the audit reached gcloud with mutating calls: {rec.calls}"


def test_audit_examines_every_declared_sa_not_just_the_first():
    seen = []

    def reads(args):
        j = " ".join(args)
        if "get-iam-policy" in j and "--format=json" in j:
            seen.append(j)
        return json.dumps({"bindings": []})
    rec, out, exit_msg = _run(P.audit_service_accounts, _audit_cfg(), reads)
    assert exit_msg is None, exit_msg
    assert any("svc-a@p." in s for s in seen) and any("deployer@p." in s for s in seen), seen


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except BaseException as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns)-failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
