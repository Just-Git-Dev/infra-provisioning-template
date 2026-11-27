"""`foreign_members`: a PROJECT role for a service account that lives in ANOTHER project.

Every member the engine built before this was `serviceAccount:<name>@<this project>`, so one
project could not grant a role to another project's identity. The first need: a staging
project's Cloud Run service agent must pull images from the prod project's registry (staging
runs prod's exact image digests). That is one project-level `artifactregistry.reader` on the
prod project for a member the prod config does not own.

Narrow by construction, and each rule is checked at PLAN time:
  - `serviceAccount:` members only (a human gets SA-scoped grants via `principals:`);
  - never this project's own SAs (declare those under `service_accounts:`);
  - every role in the config root's grantable-roles.txt (the same list IAM enforces), no
    capabilities;
  - a non-empty `reason` (a cross-project grant is invisible from the member's own project).
Ensure writes unconditional bindings, one role per call. Prune removes only an unconditional
role a DECLARED foreign member holds and the config no longer lists.
Run: python3 tests/test_foreign_members.py   (or: python3 -m pytest tests/ -q)
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engine"))
import core  # noqa: E402,F401
from providers import gcp as P  # noqa: E402
from test_repo_links import _run_mode  # noqa: E402

AGENT = "serviceAccount:service-111@serverless-robot-prod.iam.gserviceaccount.com"
OTHER = "serviceAccount:someone@elsewhere.iam.gserviceaccount.com"
READER = "roles/artifactregistry.reader"
ALLOW = "roles/artifactregistry.reader\nroles/run.developer\nroles/monitoring.viewer\n"


def _cfg(member=AGENT, roles=("artifactregistry.reader",), reason="staging pulls prod images",
         extra=None):
    entry = {"member": member, "roles": list(roles)}
    if reason is not None:
        entry["reason"] = reason
    entry.update(extra or {})
    return {"service_accounts": [{"name": "keeper", "roles": ["run.developer"]}],
            "foreign_members": [entry]}


NUM = "999"                          # this project's number, as `projects describe` reports it
NARROW = {READER, "roles/monitoring.viewer"}   # the engine's foreign list, widened for tests


def _reads(bindings):
    """bindings: [(role, [members], condition-or-None), ...] -> the project's JSON policy."""
    P._PROJECT_NUMBERS.clear()
    pol = {"bindings": [dict({"role": r, "members": m}, **({"condition": c} if c else {}))
                        for r, m, c in bindings]}

    def reads(args):
        if args[:2] == ["projects", "get-iam-policy"] and "--format=json" in args:
            return json.dumps(pol)
        if args[:2] == ["projects", "describe"]:
            return NUM
        if "describe" in args:
            return "exists"
        return ""
    return reads


def _with_allow(fn, body=ALLOW, narrow=NARROW):
    prev = os.environ.get("PROVISION_CONFIG_ROOT")
    real_narrow = P.foreign_grantable_roles
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "bootstrap"))
        with open(os.path.join(tmp, "bootstrap", "grantable-roles.txt"), "w") as fh:
            fh.write(body)
        os.environ["PROVISION_CONFIG_ROOT"] = tmp
        P.foreign_grantable_roles = lambda: set(narrow)
        core.STRAY = []
        try:
            return fn()
        finally:
            P.foreign_grantable_roles = real_narrow
            if prev is None:
                os.environ.pop("PROVISION_CONFIG_ROOT", None)
            else:
                os.environ["PROVISION_CONFIG_ROOT"] = prev


def _ensure(cfg, bindings, dry=False):
    return _with_allow(lambda: _run_mode(P.HANDLERS["foreign_members"], cfg, _reads(bindings), dry))


def _prune(cfg, bindings, dry=False):
    return _with_allow(lambda: _run_mode(P.PRUNERS["foreign_members"], cfg, _reads(bindings), dry))


def _muts(rec, verb):
    return [c for c in rec.calls if verb in c]


# ── registry ──────────────────────────────────────────────────────────────────

def test_registered_as_handler_and_pruner_before_the_audits():
    keys = list(P.HANDLERS)
    assert "foreign_members" in keys and "foreign_members" in P.PRUNERS
    assert keys.index("foreign_members") < keys.index("sa_audit")


# ── ensure ────────────────────────────────────────────────────────────────────

def test_binds_a_missing_role_unconditionally_one_role_per_call():
    cfg = _cfg(roles=("artifactregistry.reader", "monitoring.viewer"))
    rec, out, msg = _ensure(cfg, [])
    assert msg is None, (msg, out)
    adds = _muts(rec, "add-iam-policy-binding")
    assert len(adds) == 2, rec.calls
    for call in adds:
        assert call[:3] == ["projects", "add-iam-policy-binding", "p"], call
        assert f"--member={AGENT}" in call and "--condition=None" in call, call
        assert sum(a.startswith("--role=") for a in call) == 1, call
    assert {a for c in adds for a in c if a.startswith("--role=")} == {
        f"--role={READER}", "--role=roles/monitoring.viewer"}


def test_idempotent_when_held_unconditionally():
    rec, out, msg = _ensure(_cfg(), [(READER, [AGENT], None)])
    assert msg is None, (msg, out)
    assert rec.calls == [], rec.calls
    assert READER in out and AGENT in out


def test_a_conditional_binding_does_not_count_as_held():
    cond = {"title": "narrow", "expression": "resource.name.startsWith('x')"}
    rec, _out, msg = _ensure(_cfg(), [(READER, [AGENT], cond)])
    assert msg is None
    assert len(_muts(rec, "add-iam-policy-binding")) == 1, rec.calls


def test_dry_run_plans_and_mutates_nothing():
    rec, out, msg = _ensure(_cfg(), [], dry=True)
    assert msg is None
    assert rec.calls == []
    assert "would" in out and READER in out and AGENT in out


def test_no_block_is_a_skip():
    rec, out, msg = _with_allow(lambda: _run_mode(
        P.HANDLERS["foreign_members"], {"service_accounts": []}, _reads([]), False))
    assert msg is None and rec.calls == [] and "no foreign members declared" in out


# ── plan-time refusals ────────────────────────────────────────────────────────

def _refused(cfg, *needles):
    rec, out, msg = _ensure(cfg, [])
    assert msg is not None, f"expected a loud exit:\n{out}"
    for n in needles:
        assert n in msg, (n, msg)
    assert rec.calls == [], rec.calls


def test_refuses_a_human_member():
    _refused(_cfg(member="user:someone@example.com"), "serviceAccount:", "principals")


def test_refuses_a_bare_member():
    _refused(_cfg(member="someone@elsewhere.iam.gserviceaccount.com"), "serviceAccount:")


def test_refuses_this_projects_own_service_account():
    _refused(_cfg(member="serviceAccount:keeper@p.iam.gserviceaccount.com"), "service_accounts")


def test_refuses_every_other_identity_of_this_project():
    """Default compute (its auto-granted editor would become a permanent stray), App Engine,
    this project's own Google agents, and the provisioner (prune_provisioner would fight it)."""
    for email, needle in (
            (f"{NUM}-compute@developer.gserviceaccount.com", "default compute"),
            ("p@appspot.gserviceaccount.com", "App Engine"),
            (f"service-{NUM}@serverless-robot-prod.iam.gserviceaccount.com", "service agents"),
            (f"{NUM}@cloudbuild.gserviceaccount.com", "service agents"),
            ("infra-provisioner@p.iam.gserviceaccount.com", "own service accounts")):
        _refused(_cfg(member=f"serviceAccount:{email}"), needle)
    fleet = _cfg(member="serviceAccount:fleet@host.iam.gserviceaccount.com")
    fleet["provisioner"] = {"service_account": "fleet@host.iam.gserviceaccount.com"}
    _refused(fleet, "provisioner")


def test_refuses_a_malformed_member():
    for m in ("serviceAccount:", "serviceAccount:no-domain", "serviceAccount:a b@x.com"):
        _refused(_cfg(member=m), "well-formed")


def test_refuses_a_role_outside_the_foreign_list_even_if_project_grantable():
    _refused(_cfg(roles=("run.developer",)), "roles/run.developer", "foreign-grantable-roles.txt")


def test_engine_foreign_list_is_seeded_with_exactly_image_read():
    """Widening this file is the risk surface: it starts at the one known need."""
    assert P.foreign_grantable_roles() == {READER}


def test_refuses_a_missing_or_empty_reason():
    _refused(_cfg(reason=None), "reason")
    _refused(_cfg(reason="  "), "reason")


def test_refuses_a_role_outside_the_grantable_list():
    _refused(_cfg(roles=("owner",)), "roles/owner", "grantable-roles.txt")


def test_refuses_a_capability():
    _refused(_cfg(roles=("capability:observability-read",)), "capability")


def test_refuses_an_empty_role_list():
    _refused(_cfg(roles=()), "roles")


def test_refuses_an_unknown_key():
    _refused(_cfg(extra={"condition": {"title": "x"}}), "condition")


def test_refuses_a_member_declared_twice():
    cfg = _cfg()
    cfg["foreign_members"].append(dict(cfg["foreign_members"][0]))
    _refused(cfg, "twice")


# ── prune ─────────────────────────────────────────────────────────────────────

def test_prune_removes_an_undeclared_role_of_a_declared_member_only():
    live = [(READER, [AGENT, OTHER], None),
            ("roles/run.developer", [AGENT, OTHER], None)]
    rec, out, msg = _prune(_cfg(), live)
    assert msg is None, (msg, out)
    rms = _muts(rec, "remove-iam-policy-binding")
    assert len(rms) == 1, rec.calls
    assert f"--member={AGENT}" in rms[0] and "--role=roles/run.developer" in rms[0]
    assert "--condition=None" in rms[0]


def test_prune_keeps_conditional_bindings_and_strays_it_cannot_remove():
    cond = {"title": "narrow", "expression": "true"}
    live = [(READER, [AGENT], None),
            ("roles/monitoring.viewer", [AGENT], cond),
            ("roles/owner", [AGENT], None)]
    def run():
        got = _run_mode(P.PRUNERS["foreign_members"], _cfg(), _reads(live), False)
        return got + (list(core.STRAY),)
    rec, out, msg, stray = _with_allow(run)
    assert _muts(rec, "remove-iam-policy-binding") == [], rec.calls
    assert stray == [f"{AGENT}: roles/owner"], stray      # this is what fails the prune run
    assert "roles/monitoring.viewer" in out, out


# ── revoke: true (the tombstone that withdraws a grant) ──────────────────────

def _tomb(**extra):
    return _cfg(roles=(), extra=dict({"revoke": True}, **extra))


def test_revoke_tombstone_binds_nothing_on_ensure():
    rec, out, msg = _ensure(_tomb(), [])
    assert msg is None, (msg, out)
    assert rec.calls == [] and "revoke: true" in out


def test_revoke_tombstone_prune_removes_every_unconditional_role():
    live = [(READER, [AGENT, OTHER], None), ("roles/monitoring.viewer", [AGENT], None)]
    cfg = _tomb()
    del cfg["foreign_members"][0]["roles"]
    rec, out, msg = _prune(cfg, live)
    assert msg is None, (msg, out)
    rms = _muts(rec, "remove-iam-policy-binding")
    assert sorted(a for c in rms for a in c if a.startswith("--role=")) == [
        f"--role={READER}", "--role=roles/monitoring.viewer"], rec.calls
    assert all(f"--member={AGENT}" in c for c in rms), rms


def test_revoke_tombstone_refuses_roles_and_a_non_boolean():
    _refused(_cfg(extra={"revoke": True}), "tombstone")
    _refused(_cfg(extra={"revoke": "yes"}), "true or false")
    _refused(_cfg(roles=()), "revoke: true")


def test_prune_with_everything_declared_is_clean():
    rec, out, msg = _prune(_cfg(), [(READER, [AGENT], None)])
    assert msg is None and rec.calls == [], (msg, rec.calls)


def test_service_account_prune_never_touches_a_foreign_member():
    live = [("roles/run.developer", ["serviceAccount:keeper@p.iam.gserviceaccount.com"], None),
            (READER, [AGENT], None)]
    rec, out, msg = _with_allow(lambda: _run_mode(
        P.PRUNERS["service_accounts"], _cfg(), _reads(live), False))
    assert not any(f"--member={AGENT}" in c for c in rec.calls), rec.calls


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
        except BaseException as e:
            failed += 1
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns)-failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
