"""secret_audit: detect-only audit of the policy of each `<d>-env` secret (owner ruling 24).

CONTRACT (design plans/2026-10-20-repos-as-code-secrets-design.md section 8, owner Q2/Q3/Q4)
=============================================================================================

For every service account `<d>-run` in the JGD config that names `deployed_by: [<d>-deployer]`,
the secret `<d>-env` is expected to hold EXACTLY this policy, derived from the config alone:

    <d>-run       -> roles/secretmanager.secretAccessor
    <d>-deployer  -> roles/secretmanager.secretAccessor, roles/secretmanager.secretVersionManager,
                     roles/secretmanager.viewer          (the predefined sync roles, owner Q3)

Reads are `secrets get-iam-policy <d>-env --project <p> --format=json` (stdout = policy JSON; empty
stdout = no such secret). Anything else is a finding: a missing line, an extra member, an extra
role, any binding carrying a `condition`. The audit is registered as HANDLERS["secret_audit"]
(reports `::warning::`, never fails) and PRUNERS["secret_audit"] (same audit, fails on a finding
only; all secrets absent = `examined 0 secret(s) (N absent: not adopted yet)`, exit 0). It issues NO mutating gcloud call and prints
`examined N secret(s)`. An absent secret is `info`, not examined, not a finding.
"""
import io
import os
import subprocess
import sys
import json
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engine"))
import core  # noqa: E402
from providers import gcp as P  # noqa: E402

ACCESSOR = "roles/secretmanager.secretAccessor"
MANAGER = "roles/secretmanager.secretVersionManager"
VIEWER = "roles/secretmanager.viewer"


def sa(name):
    return f"serviceAccount:{name}@p.iam.gserviceaccount.com"


def good_policy(d):
    return {ACCESSOR: [sa(f"{d}-run"), sa(f"{d}-deployer")],
            MANAGER: [sa(f"{d}-deployer")], VIEWER: [sa(f"{d}-deployer")]}


def cfg(*ds):
    """A JGD config with one `<d>-run` per name (with deployed_by) plus an unrelated SA."""
    sas = [{"name": f"{d}-run", "deployed_by": [f"{d}-deployer"]} for d in ds]
    return {"project": {"github_org": "Example-Org", "gcp_number": "42"},
            "service_accounts": sas + [{"name": "github-rotator"}]}


class Rec:
    def __init__(self, reads):
        self.calls, self.reads = [], reads

    def install(self, dry):
        core.DRY = dry
        P.gout = lambda args, project=None: self.reads(args)

        def fake(args, project=None, check=True):
            self.calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, "", "")
        P.gcloud = fake


def secret_reads(policies, conditions=None):
    """{secret name: {role: [members]}}; a secret not in the map does not exist (empty stdout)."""
    def reads(args):
        if args[:2] == ["secrets", "get-iam-policy"]:
            pol = policies.get(args[2])
            if pol is None:
                return ""
            bindings = []
            for r, m in pol.items():
                b = {"role": r, "members": m}
                if conditions and r in conditions:
                    b["condition"] = {"expression": conditions[r]}
                bindings.append(b)
            return json.dumps({"bindings": bindings})
        return ""
    return reads


def run(handler, config, reads, dry=False):
    rec = Rec(reads)
    rec.install(dry)
    buf, msg = io.StringIO(), None
    try:
        with redirect_stdout(buf):
            try:
                handler(config, "p")
            except SystemExit as e:
                msg = str(e)
    finally:
        core.DRY = False
    return rec, buf.getvalue(), msg


def test_secret_audit_is_registered_as_handler_and_pruner():
    assert "secret_audit" in P.HANDLERS and "secret_audit" in P.PRUNERS
    keys = list(P.HANDLERS)
    assert keys.index("secret_audit") > keys.index("service_accounts")


def test_exact_expected_policy_is_clean_and_counted():
    for h in (P.HANDLERS, P.PRUNERS):
        rec, out, msg = run(h["secret_audit"], cfg("api", "issuer"),
                            secret_reads({"api-env": good_policy("api"),
                                          "issuer-env": good_policy("issuer")}))
        assert msg is None, (msg, out)
        assert "examined 2 secret(s)" in out and "0 finding(s)" in out, out
        assert rec.calls == [], rec.calls


def test_extra_member_extra_role_and_missing_line_are_each_flagged():
    stranger = "user:stranger@example.com"
    extra_member = good_policy("api")
    extra_member[ACCESSOR].append(stranger)
    extra_role = good_policy("api")
    extra_role["roles/secretmanager.admin"] = [sa("api-deployer")]
    missing = good_policy("api")
    del missing[MANAGER]
    for pol, needle in ((extra_member, "stranger@example.com"),
                        (extra_role, "roles/secretmanager.admin"),
                        (missing, "missing")):
        rec, out, msg = run(P.PRUNERS["secret_audit"], cfg("api"), secret_reads({"api-env": pol}))
        assert msg, (needle, out)
        assert needle in out and "api-env" in out, (needle, out)
        assert rec.calls == [], rec.calls


def test_a_conditioned_binding_is_a_finding():
    rec, out, msg = run(P.PRUNERS["secret_audit"], cfg("api"),
                        secret_reads({"api-env": good_policy("api")},
                                     conditions={VIEWER: "request.time < timestamp('2030-01-01T00:00:00Z')"}))
    assert msg and "condition" in out.lower(), (msg, out)


def test_runtime_sa_of_another_service_is_not_expected_on_this_secret():
    pol = good_policy("api")
    pol[ACCESSOR].append(sa("issuer-run"))
    rec, out, msg = run(P.PRUNERS["secret_audit"], cfg("api", "issuer"),
                        secret_reads({"api-env": pol, "issuer-env": good_policy("issuer")}))
    assert msg and "issuer-run" in out, (msg, out)


def test_report_mode_warns_but_never_fails_and_never_mutates_in_both_modes():
    pol = good_policy("api")
    pol[ACCESSOR].append("user:stranger@example.com")
    for dry in (True, False):
        rec, out, msg = run(P.HANDLERS["secret_audit"], cfg("api"),
                            secret_reads({"api-env": pol}), dry=dry)
        assert msg is None, (dry, msg)
        warns = [l for l in out.splitlines() if l.startswith("::warning::")]
        assert warns and "stranger@example.com" in warns[0], out
        assert "examined 1 secret(s)" in out, out
        assert rec.calls == [], (dry, rec.calls)


def test_prune_variant_is_detect_only_in_both_modes():
    pol = good_policy("api")
    pol[ACCESSOR].append("user:stranger@example.com")
    for dry in (True, False):
        rec, out, msg = run(P.PRUNERS["secret_audit"], cfg("api"), secret_reads({"api-env": pol}), dry=dry)
        assert msg, (dry, out)
        assert rec.calls == [], (dry, rec.calls)   # no remove-iam-policy-binding, no set-iam-policy


def test_absent_secret_is_info_and_not_examined():
    # api has a secret, issuer has none yet: 1 examined, no finding
    rec, out, msg = run(P.PRUNERS["secret_audit"], cfg("api", "issuer"),
                        secret_reads({"api-env": good_policy("api")}))
    assert msg is None, (msg, out)
    assert "examined 1 secret(s)" in out and "issuer-env" in out, out


def test_all_absent_is_info_in_both_modes_and_states_the_absent_count():
    # devops ruling 2026-10-20: absent <d>-env = info; prune must not go red for "not adopted yet"
    for h in (P.PRUNERS["secret_audit"], P.HANDLERS["secret_audit"]):
        rec, out, msg = run(h, cfg("api", "issuer"), secret_reads({}))
        assert msg is None, (msg, out)
        assert "examined 0 secret(s) (2 absent: not adopted yet)" in out, out


def test_prune_still_fails_on_drift_when_other_secrets_are_absent():
    pol = good_policy("api")
    pol[ACCESSOR].append("user:stranger@example.com")
    rec, out, msg = run(P.PRUNERS["secret_audit"], cfg("api", "issuer"), secret_reads({"api-env": pol}))
    assert msg and "stranger" in msg, (msg, out)
    assert "examined 1 secret(s) (1 absent: not adopted yet)" in out, out


def test_zero_examined_is_fine_when_no_deployed_by_exists():
    c = {"project": {"github_org": "Example-Org"}, "service_accounts": [{"name": "api-run"}]}
    rec, out, msg = run(P.PRUNERS["secret_audit"], c, secret_reads({}))
    assert msg is None and "examined 0 secret(s)" in out, (msg, out)


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
