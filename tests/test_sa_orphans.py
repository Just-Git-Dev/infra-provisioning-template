"""sa_audit orphan check: a live SA that config neither declares, retires, nor names in a
`deployed_by` list. Run: python3 tests/test_sa_orphans.py   (or: python3 -m pytest tests/ -q)"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _retire_fakes import P, PROJECT, call, config_root, install, model_gout, runner  # noqa: E402

CFG = {"service_accounts": [{"name": "keeper"}, {"name": "api-run", "deployed_by": ["api-deployer"]},
                            {"name": "old-bot", "retire": True}]}
# Everything that must NOT be reported, as `service-accounts list` would return it.
EXCLUDED = ["123-compute@developer.gserviceaccount.com",
            "service-123@gcp-sa-pubsub.iam.gserviceaccount.com",
            f"{PROJECT}@appspot.gserviceaccount.com",
            f"firebase-adminsdk-abc12@{PROJECT}.iam.gserviceaccount.com",
            f"{P.PROVISIONER_SA}@{PROJECT}.iam.gserviceaccount.com",
            "service-123@containerregistry.iam.gserviceaccount.com"]


def _live(*extra):
    config_root()
    return install(model_gout(live=["keeper", "api-run", "api-deployer", "old-bot", *extra],
                              extra_listed=EXCLUDED))


def test_orphan_reported_for_undeclared_live_sa():
    _live("ghost")
    out, exc, ret = call(P._sa_audit_findings, CFG, PROJECT)
    assert exc is None, exc
    findings, _ = ret
    assert any("ghost" in f and "orphan" in f.lower() for f in findings), (findings, out)
    assert "ORPHAN ghost" in out, out


def test_google_managed_and_default_sas_not_orphans():
    # known = declared, retire:true (even while live), and named in deployed_by; excluded =
    # Google-managed agents, default compute / App Engine SAs, firebase-adminsdk, the anchor SA.
    _live("ghost")
    _, exc, ret = call(P._sa_audit_findings, CFG, PROJECT)
    orphans = [f for f in ret[0] if "orphan" in f.lower()]
    assert exc is None and len(orphans) == 1 and "ghost" in orphans[0], orphans


def test_retired_absent_sa_is_ok_not_skip():
    config_root()
    install(model_gout(live=["keeper", "api-run", "api-deployer"], extra_listed=EXCLUDED))
    out, exc, ret = call(P._sa_audit_findings, CFG, PROJECT)
    assert exc is None and ret[0] == [], (exc, ret)
    assert "old-bot" in out and "deleted, drop the entry" in out, out
    assert "no readable policy" not in out, out


def test_orphan_fails_prune_variant_and_warns_in_report_variant():
    _live("ghost")
    out, exc, _ = call(P.report_service_accounts, CFG, PROJECT)
    assert exc is None, exc
    assert "::warning::" in out and "ghost" in out, out
    _live("ghost")
    out, exc, _ = call(P.audit_service_accounts, CFG, PROJECT)
    assert exc is not None and "ghost" in str(exc.code), (exc, out)


def test_orphan_summary_states_examined_count():
    _live("ghost")
    out, _, _ = call(P.report_service_accounts, CFG, PROJECT)
    # 4 declared/retired/deployer SAs + ghost are live in the project; the excluded ones are listed too
    assert f"listed {5 + len(EXCLUDED)} live service account(s), 1 orphan(s)" in out, out


if __name__ == "__main__":
    runner(globals())
