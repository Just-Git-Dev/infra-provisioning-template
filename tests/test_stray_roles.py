"""IP3 fallback: GCP cannot express "revoke but never grant", so a project role the provisioner
cannot remove (outside the CONFIG ROOT's grantable-roles.txt) is reported `stray`, never planned
as `~ would unbind`. Run: python3 tests/test_stray_roles.py   (or: python3 -m pytest tests/ -q)"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _retire_fakes import (P, PROJECT, call, config_root, core, install, model_gout,  # noqa: E402
                           runner)

import provision  # noqa: E402

CFG = {"service_accounts": [{"name": "keeper", "roles": ["run.developer"]}]}
HELD = ["roles/run.developer", "roles/artifactregistry.writer", "roles/owner"]


def _model():
    return model_gout(live=["keeper"], roles={"keeper": HELD})


def test_stray_role_outside_allow_list_reported_not_planned_as_unbind():
    config_root(allow=["roles/artifactregistry.writer"])
    install(_model())
    out, exc, _ = call(P.prune_service_accounts, CFG, PROJECT)
    assert exc is None, exc
    assert "would unbind roles/artifactregistry.writer from keeper" in out, out
    assert "stray roles/owner" in out and "provisioner cannot remove" in out, out
    assert "unbind roles/owner" not in out, out


def test_prune_dry_run_mutation_count_excludes_stray():
    config_root(allow=["roles/artifactregistry.writer"])
    install(_model())
    call(P.prune_service_accounts, CFG, PROJECT)
    assert core.MUTATIONS == 1, core.MUTATIONS      # the one grantable extra, not owner


def test_prune_exits_nonzero_at_the_end_when_a_stray_was_found():
    config_root(allow=["roles/artifactregistry.writer"], cfg=CFG)
    rec = install(_model(), dry=False)
    out, exc, _ = call(provision.run, "k", apply=True, prune=True, only="service_accounts")
    assert exc is not None and "stray" in str(exc.code), (exc, out)
    # the removable extra was still removed before the run failed
    assert any("remove-iam-policy-binding" in a and "--role=roles/artifactregistry.writer" in a
               for a in rec.calls), rec.calls
    assert not any("--role=roles/owner" in a for a in rec.calls), rec.calls


def test_allow_list_read_from_config_root_not_engine_root():
    # roles/custom.thing is in the CONFIG ROOT copy only; roles/run.developer is in the engine's
    # copy only (bootstrap/grantable-roles.txt here) and NOT in the config root's.
    root = config_root(allow=["roles/custom.thing"])
    engine_copy = open(os.path.join(core.ROOT, "bootstrap", "grantable-roles.txt")).read()
    assert "roles/run.developer" in engine_copy and "roles/custom.thing" not in engine_copy
    install(model_gout(live=["keeper"], roles={"keeper": ["roles/custom.thing", "roles/run.developer"]}))
    out, exc, _ = call(P.prune_service_accounts, {"service_accounts": [{"name": "keeper"}]}, PROJECT)
    assert exc is None, (exc, root)
    assert "would unbind roles/custom.thing from keeper" in out, out
    assert "stray roles/run.developer" in out, out


def test_sa_audit_reports_undeclared_project_role():
    config_root(allow=["roles/artifactregistry.writer"])
    install(_model())
    out, exc, ret = call(P._sa_audit_findings, CFG, PROJECT)
    assert exc is None, exc
    findings, _ = ret
    assert any("keeper" in f and "roles/owner" in f for f in findings), (findings, out)
    # a role the pruner CAN remove is prune's business, not an audit finding
    assert not any("artifactregistry.writer" in f for f in findings), findings
    assert not any("run.developer" in f for f in findings), findings


if __name__ == "__main__":
    runner(globals())
