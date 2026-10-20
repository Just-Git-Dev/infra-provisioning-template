"""`service_agents` (phase 1, owner ruling 21 follow-up 2026-10-20): DECLARE-ONLY.

A runtime SA may name a known Google-managed service agent (`service_agents: [pubsub]`). The
engine derives the member `serviceAccount:service-<projectNumber>@gcp-sa-<x>.iam.gserviceaccount.com`
and adds `(<fixed role>, member)` to the SA's declared grants, so the exact `sa_audit` stops
flagging it. Nothing is written (phase 2 will ensure it) and prune never removes it.
Same seam as test_repo_links.py: recording gcloud stub, scripted reads.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engine"))
import core  # noqa: E402,F401
from providers import gcp as P  # noqa: E402
from test_repo_links import _run  # noqa: E402

NUM = "750513647348"
AGENT = f"serviceAccount:service-{NUM}@gcp-sa-pubsub.iam.gserviceaccount.com"
TC = "roles/iam.serviceAccountTokenCreator"


def _cfg(**extra):
    return {"project": {"github_org": "Example-Org"},
            "service_accounts": [dict({"name": "api-run", "service_agents": ["pubsub"]}, **extra)]}


def _reads(bindings, number=NUM, counter=None):
    P._PROJECT_NUMBERS.clear()      # every test starts with a cold per-run cache
    pol = json.dumps({"bindings": [{"role": r, "members": m} for r, m in bindings.items()]})

    def reads(args):
        if args[:2] == ["projects", "describe"]:
            if counter is not None:
                counter.append(args)
            return number
        if "get-iam-policy" in args and "--format=json" in args:
            return pol
        return ""
    return reads


def test_declared_agent_is_not_flagged():
    rec, out, msg = _run(P.audit_service_accounts, _cfg(), _reads({TC: [AGENT]}))
    assert msg is None, (msg, out)
    assert "UNDECLARED" not in out
    assert rec.calls == []


def test_undeclared_agent_is_still_flagged():
    cfg = {"project": {"github_org": "Example-Org"}, "service_accounts": [{"name": "api-run"}]}
    rec, out, msg = _run(P.audit_service_accounts, cfg, _reads({TC: [AGENT]}))
    assert msg is not None and "undeclared" in msg.lower(), (msg, out)
    assert AGENT in out


def test_declared_agent_with_an_extra_role_is_flagged():
    rec, out, msg = _run(P.audit_service_accounts, _cfg(),
                         _reads({TC: [AGENT], "roles/iam.serviceAccountUser": [AGENT]}))
    assert msg is not None and "undeclared" in msg.lower(), (msg, out)
    assert any("UNDECLARED" in l and "serviceAccountUser" in l for l in out.splitlines()), out
    assert not any("UNDECLARED" in l and "TokenCreator" in l for l in out.splitlines()), out


def test_bad_service_agents_value_fails_at_plan_time():
    for bad in ["nope", "", [], ["pubsub", "nope"], ["PubSub"], "pubsub"]:
        cfg = _cfg(service_agents=bad)
        rec, out, msg = _run(P.audit_service_accounts, cfg, _reads({}))
        assert msg and "service_agents" in msg, (bad, msg, out)
        rec, out, msg = _run(P.ensure_act_as, cfg, _reads({}))     # the apply/plan path too
        assert msg and "service_agents" in msg, (bad, msg, out)


def test_service_agents_on_a_non_run_sa_fails_at_plan_time():
    cfg = {"project": {"github_org": "Example-Org"},
           "service_accounts": [{"name": "api-deployer", "service_agents": ["pubsub"]}]}
    for handler in (P.audit_service_accounts, P.ensure_act_as):
        rec, out, msg = _run(handler, cfg, _reads({}))
        assert msg and "service_agents" in msg and "-run" in msg, (msg, out)


def test_member_derivation_uses_project_number_and_agent_domain():
    P._PROJECT_NUMBERS.clear()
    P.gout = lambda args, project=None: NUM
    got = P._declared_sa_grants(_cfg(), "p", None)
    assert got["api-run"] == {(TC, AGENT)}


def test_unreadable_project_number_fails_loudly_and_never_guesses():
    rec, out, msg = _run(P.audit_service_accounts, _cfg(), _reads({TC: [AGENT]}, number=""))
    assert msg and "project number" in msg.lower(), (msg, out)
    assert "gcp-sa-pubsub" not in out


def test_project_number_is_read_once_per_run():
    seen = []
    cfg = _cfg()
    cfg["service_accounts"].append({"name": "web-run", "service_agents": ["pubsub"]})
    rec, out, msg = _run(P.audit_service_accounts, cfg, _reads({TC: [AGENT]}, counter=seen))
    assert msg is None, (msg, out)
    assert len(seen) == 1, seen


def test_ensure_writes_nothing_in_phase_1():
    rec, out, msg = _run(P.ensure_act_as, _cfg(), _reads({}))
    assert msg is None, (msg, out)
    assert rec.calls == [], rec.calls
    rec, out, msg = _run(P.ensure_service_accounts, _cfg(), _reads({}))
    assert not any(TC in a or AGENT in a for c in rec.calls for a in c), rec.calls


def test_prune_never_removes_the_declared_agent():
    def reads(args):
        j = " ".join(args)
        if args[:2] == ["projects", "describe"]:
            return NUM
        if "get-iam-policy" in j:
            return "\n".join([AGENT, "serviceAccount:old@p.iam.gserviceaccount.com"])
        return ""
    cfg = _cfg()
    cfg["service_accounts"].append({"name": "old"})
    rec, out, msg = _run(P.prune_act_as, cfg, reads)
    assert not any(AGENT in a for c in rec.calls for a in c), rec.calls
    assert not any("remove-iam-policy-binding" in c and AGENT in " ".join(c) for c in rec.calls)


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
