"""`human_managed_roles`: a conditional project role a human owns (README; DECISIONS 2026-11-14).

Declared per SA as `[{role, condition: {title, expression}}]`; the engine NEVER grants or removes it.
sa_audit and `prune service_accounts` recognise an exact match instead of calling it a stray.
Every cloud seam is stubbed. Run: python3 tests/test_human_managed.py   (or: python3 -m pytest tests/ -q)"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _retire_fakes import (P, PROJECT, call, config_root, core, email,  # noqa: E402
                           install, runner)

ROLE = "roles/secretmanager.admin"
TITLE = "base-secrets-only"
EXPR = 'resource.name.startsWith("projects/42/secrets/base-")'
HM = {"role": ROLE, "condition": {"title": TITLE, "expression": EXPR}}
MEMBER = f"serviceAccount:{email('releaser')}"


def _cfg(hm=(HM,)):
    return {"service_accounts": [{"name": "releaser", "roles": [], "human_managed_roles": list(hm)}]}


def _gout(live):
    """live: [(role, title|None, expression|None)] held by the SA on the PROJECT. One policy feeds
    BOTH readers, like gcloud: the flattened role list includes conditional bindings too."""
    em = email("releaser")

    def gout(args, project=None):
        j = " ".join(args)
        if args[:2] == ["iam", "service-accounts"] and args[2] == "list":
            return em
        if args[:2] == ["iam", "service-accounts"] and args[2] == "describe":
            return "x\t" + P._described("") if args[3] == em else ""
        if args[:2] == ["iam", "service-accounts"] and args[2] == "get-iam-policy":
            return json.dumps({"bindings": []}) if args[3] == em else ""
        if args[:2] == ["projects", "get-iam-policy"]:
            if "--format=json" in args:
                bs = []
                for role, title, expr in live:
                    b = {"role": role, "members": [MEMBER]}
                    if title is not None:
                        b["condition"] = {"title": title, "expression": expr}
                    bs.append(b)
                return json.dumps({"bindings": bs})
            return "\n".join(r for r, _, _ in live) if f"serviceAccount:{em}" in j else ""
        if args[:2] == ["projects", "describe"]:
            return "42"
        return ""

    return gout


def _setup(live, allow=("roles/run.developer",), dry=True):
    config_root(allow=allow)
    return install(_gout(live), dry=dry)


def _role_writes(rec, role=ROLE):
    return [a for a in rec.calls
            if ("add-iam-policy-binding" in a or "remove-iam-policy-binding" in a)
            and f"--role={role}" in a]


def test_t8_exact_match_is_ok_not_stray_and_not_extra_for_prune():
    _setup([(ROLE, TITLE, EXPR)], allow=(ROLE,))
    out, exc, ret = call(P._sa_audit_findings, _cfg(), PROJECT)
    assert exc is None, (exc, out)
    findings, _ = ret
    assert findings == [], findings
    assert f"ok human-managed {ROLE} ({TITLE})" in out, out
    assert "STRAY" not in out and "stray" not in out, out
    # prune: even with the role grantable (so an "extra" WOULD be removed), it is neither extra nor stray
    rec = _setup([(ROLE, TITLE, EXPR)], allow=(ROLE,))
    out, exc, _ = call(P.prune_service_accounts, _cfg(), PROJECT)
    assert exc is None, (exc, out)
    assert "would unbind" not in out and "stray" not in out, out
    assert core.MUTATIONS == 0 and core.STRAY == [], (core.MUTATIONS, core.STRAY)
    assert rec.calls == [], rec.calls


def test_t9_same_role_with_a_different_condition_is_drift():
    _setup([(ROLE, TITLE, 'resource.name.startsWith("projects/42/secrets/other-")')])
    out, exc, ret = call(P._sa_audit_findings, _cfg(), PROJECT)
    assert exc is None, (exc, out)
    findings, _ = ret
    assert any("human-managed binding drifted" in f and ROLE in f for f in findings), (findings, out)
    assert not any("ok human-managed" in ln for ln in out.splitlines()), out


def test_t10_same_role_held_unconditionally_is_still_stray():
    # live holds BOTH the exact conditional binding and the same role unconditionally: the
    # declaration covers only the conditional form, so the unconditional one is stray.
    _setup([(ROLE, TITLE, EXPR), (ROLE, None, None)])
    out, exc, ret = call(P._sa_audit_findings, _cfg(), PROJECT)
    assert exc is None, (exc, out)
    findings, _ = ret
    assert f"ok human-managed {ROLE} ({TITLE})" in out, out          # the conditional one is recognised
    assert any("stray role" in f and ROLE in f for f in findings), (findings, out)


def test_t11_declared_but_absent_live_is_a_finding_that_fails_the_audit():
    _setup([])
    out, exc, ret = call(P._sa_audit_findings, _cfg(), PROJECT)
    assert exc is None, (exc, out)
    findings, _ = ret
    assert any("human-managed binding missing" in f and ROLE in f for f in findings), (findings, out)
    _setup([])
    out, exc, _ = call(P.audit_service_accounts, _cfg(), PROJECT)
    assert exc is not None and "human-managed binding missing" in str(exc.code), (exc, out)


MALFORMED = {
    "not a list": "roles/secretmanager.admin",
    "entry not a mapping": ["roles/secretmanager.admin"],
    "short role name": [{"role": "secretmanager.admin", "condition": {"title": TITLE, "expression": EXPR}}],
    "role not a string": [{"role": 7, "condition": {"title": TITLE, "expression": EXPR}}],
    "missing condition": [{"role": ROLE}],
    "condition not a mapping": [{"role": ROLE, "condition": "base"}],
    "empty title": [{"role": ROLE, "condition": {"title": "", "expression": EXPR}}],
    "empty expression": [{"role": ROLE, "condition": {"title": TITLE, "expression": ""}}],
    "title not a string": [{"role": ROLE, "condition": {"title": 3, "expression": EXPR}}],
    "missing expression": [{"role": ROLE, "condition": {"title": TITLE}}],
}


def test_t12_malformed_human_managed_roles_exit_loudly():
    for label, bad in MALFORMED.items():
        cfg = {"service_accounts": [{"name": "releaser", "roles": [], "human_managed_roles": bad}]}
        for fn in (P.prune_service_accounts, P._sa_audit_findings):
            _setup([])
            out, exc, _ = call(fn, cfg, PROJECT)
            assert exc is not None and "human_managed_roles" in str(exc.code), \
                (label, fn.__name__, exc, out)


def test_t13_no_add_or_remove_call_for_a_human_managed_role():
    # the role is grantable here, so a pruner that mistook it for "extra" WOULD remove it.
    for live in ([(ROLE, TITLE, EXPR)], []):         # present exactly, and absent
        rec = _setup(live, allow=(ROLE,), dry=False)
        out1, exc1, _ = call(P.ensure_service_accounts, _cfg(), PROJECT)
        out2, exc2, _ = call(P.prune_service_accounts, _cfg(), PROJECT)
        assert exc1 is None and exc2 is None, (exc1, exc2, out1, out2)
        assert "releaser" in out1 and "releaser" in out2, (out1, out2)      # both actually ran
        assert _role_writes(rec) == [], (live, rec.calls)


def test_t13b_declaring_it_does_not_need_the_role_in_grantable_roles():
    # grantable-roles.txt is untouched by this feature: the role need not be in it to be declared.
    _setup([(ROLE, TITLE, EXPR)], allow=("roles/run.developer",))
    out, exc, ret = call(P._sa_audit_findings, _cfg(), PROJECT)
    assert exc is None and ret[0] == [], (exc, ret, out)


WIDER = (ROLE, "wider", 'resource.name.startsWith("projects/42/secrets/")')


def test_t14_exact_binding_plus_a_wider_conditional_binding_is_drift():
    # review fix 1: the exact declared binding is live, AND another conditional binding of the same
    # role (a wider scope) exists: that second one is not declared, so it is drift, never "covered".
    _setup([(ROLE, TITLE, EXPR), WIDER], allow=(ROLE,))
    out, exc, ret = call(P._sa_audit_findings, _cfg(), PROJECT)
    assert exc is None, (exc, out)
    findings, _ = ret
    assert any("human-managed binding drifted" in f and ROLE in f and "wider" in f
               for f in findings), (findings, out)
    assert f"ok human-managed {ROLE} ({TITLE})" in out, out      # the exact one is still recognised


def test_t15_overlap_of_human_managed_and_wanted_role_exits_at_plan_time():
    # review fix 2: ensure would bind the role with --condition=None, so config is refused up front.
    for sa_roles in (["secretmanager.admin"], [ROLE]):
        cfg = {"service_accounts": [{"name": "releaser", "roles": sa_roles,
                                     "human_managed_roles": [HM]}]}
        for fn in (P.ensure_service_accounts, P.prune_service_accounts, P._sa_audit_findings):
            rec = _setup([], allow=(ROLE,), dry=False)
            out, exc, _ = call(fn, cfg, PROJECT)
            assert exc is not None and ROLE in str(exc.code) and "human_managed_roles" in str(exc.code), \
                (sa_roles, fn.__name__, exc, out)
            assert _role_writes(rec) == [], rec.calls


def test_t16_prune_never_plans_removal_of_a_conditional_binding():
    # review fix 3: the engine only writes unconditional bindings, so it removes only those.
    for live in ([(ROLE, TITLE, EXPR), WIDER],                    # declared + drifted wider
                 [(ROLE, TITLE, 'resource.name.startsWith("projects/42/secrets/other-")')],
                 [("roles/run.developer", "t", "true")]):          # any other role, conditional
        rec = _setup(live, allow=(ROLE, "roles/run.developer"))
        out, exc, _ = call(P.prune_service_accounts, _cfg(), PROJECT)
        assert exc is None, (exc, out)
        assert "would unbind" not in out and core.MUTATIONS == 0 and rec.calls == [], (live, out)
    assert "conditional binding" in out and "not engine-owned, kept" in out, out
    assert "roles/run.developer" in out and "(t)" in out, out


def test_t17_role_held_conditionally_and_unconditionally_plans_one_unbind():
    for live in ([(ROLE, TITLE, EXPR), (ROLE, None, None)],
                 [(ROLE, None, None), (ROLE, None, None), (ROLE, "wider", "true")]):
        _setup(live, allow=(ROLE,))
        out, exc, _ = call(P.prune_service_accounts, _cfg(), PROJECT)
        assert exc is None, (exc, out)
        assert out.count("would unbind") == 1 and core.MUTATIONS == 1, (live, out)


if __name__ == "__main__":
    runner(globals())
