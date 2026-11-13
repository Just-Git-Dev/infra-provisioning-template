"""`retire: true` + the `--retire` deleter mode (SA retirement, engine half).

Seams stubbed (see _retire_fakes): nothing here can reach gcloud.
Run: python3 tests/test_retire.py   (or: python3 -m pytest tests/ -q)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _retire_fakes import (P, PROJECT, call, config_root, core, deletes, email,  # noqa: E402
                           install, model_gout, runner)

import provision  # noqa: E402

RETIRED = {"name": "old-bot", "retire": True}
LIVE = {"name": "keeper", "roles": ["run.developer"]}


def test_retired_sa_is_never_created():
    install(model_gout(live=[]))
    out, exc, _ = call(P.ensure_service_accounts, {"service_accounts": [RETIRED]}, PROJECT)
    assert exc is None, exc
    assert "create sa old-bot" not in out, out


def test_retired_sa_gets_no_role_binding():
    cfg = {"service_accounts": [RETIRED, LIVE]}
    assert [sa["name"] for sa, _ in P._sa_roles(cfg)] == ["keeper"]


def test_prune_strips_every_role_from_retired_sa():
    config_root(allow=["roles/run.developer", "roles/artifactregistry.writer"])
    install(model_gout(live=["old-bot"], roles={"old-bot": ["roles/run.developer",
                                                            "roles/artifactregistry.writer"]}))
    out, exc, _ = call(P.prune_service_accounts, {"service_accounts": [RETIRED]}, PROJECT)
    assert exc is None, exc
    assert "would unbind roles/run.developer from old-bot" in out, out
    assert "would unbind roles/artifactregistry.writer from old-bot" in out, out
    assert core.MUTATIONS == 2


def test_retire_mode_deletes_only_retired_live_roleless_sa():
    other = {"name": "other-bot", "retire": True}
    cfg = {"service_accounts": [RETIRED, other, LIVE]}
    install(model_gout(live=["old-bot", "keeper"]))      # other-bot already gone
    out, exc, _ = call(P.retire_service_accounts, cfg, PROJECT)
    assert exc is None, exc
    assert f"would delete sa {email('old-bot')}" in out, out
    assert f"delete sa {email('keeper')}" not in out, out
    assert core.MUTATIONS == 1, core.MUTATIONS


def test_retire_mode_deletes_nothing_if_any_retired_sa_still_holds_a_role():
    cfg = {"service_accounts": [RETIRED, {"name": "busy-bot", "retire": True}]}
    rec = install(model_gout(live=["old-bot", "busy-bot"], roles={"busy-bot": ["roles/run.developer"]}),
                  dry=False)
    out, exc, _ = call(P.retire_service_accounts, cfg, PROJECT)
    assert exc is not None, "must refuse"
    assert "busy-bot" in str(exc.code) + out and "roles/run.developer" in str(exc.code) + out
    assert deletes(rec) == [], rec.calls
    assert core.MUTATIONS == 0 and "delete sa" not in out, out


def test_retire_mode_absent_sa_reports_drop_entry():
    install(model_gout(live=[]))
    out, exc, _ = call(P.retire_service_accounts, {"service_accounts": [RETIRED]}, PROJECT)
    assert exc is None, exc
    assert "deleted, drop the entry" in out, out
    assert core.MUTATIONS == 0


def test_retire_dry_run_counts_but_withholds_delete():
    rec = install(model_gout(live=["old-bot"]))
    out, exc, _ = call(P.retire_service_accounts, {"service_accounts": [RETIRED]}, PROJECT)
    assert exc is None, exc
    assert core.MUTATIONS == 1 and "would delete sa" in out, out
    assert rec.calls == [], rec.calls
    # and under apply the delete really goes out, once
    rec = install(model_gout(live=["old-bot"]), dry=False)
    call(P.retire_service_accounts, {"service_accounts": [RETIRED]}, PROJECT)
    assert len(deletes(rec)) == 1 and email("old-bot") in deletes(rec)[0], rec.calls


def _boom(*a, **k):
    raise AssertionError("an add handler / pruner ran in retire mode")


def test_retire_mode_runs_no_handler_or_pruner():
    config_root(cfg={"service_accounts": [RETIRED]})
    install(model_gout(live=["old-bot"]))
    saved = dict(P.HANDLERS), dict(P.PRUNERS)
    try:
        for reg in (P.HANDLERS, P.PRUNERS):
            for k in reg:
                reg[k] = _boom
        out, exc, _ = call(provision.run, "k", apply=False, retire=True)
    finally:
        P.HANDLERS.update(saved[0])
        P.PRUNERS.update(saved[1])
    assert exc is None, (exc, out)
    assert "retire dry-run complete — 1 deletion planned." in out, out


def test_add_and_prune_paths_never_emit_service_accounts_delete():
    cfg = {"service_accounts": [RETIRED, LIVE]}
    config_root(allow=["roles/run.developer"], cfg=cfg)
    for kw in ({}, {"prune": True}):
        rec = install(model_gout(live=["old-bot", "keeper"], roles={"old-bot": ["roles/run.developer"]}),
                      dry=False)
        call(provision.run, "k", apply=True, **kw)
        assert rec.calls, f"scanner saw no gcloud calls for {kw}"
        assert deletes(rec) == [], (kw, deletes(rec))
    # positive control: the scanner CAN see a delete, so the empties above mean something
    rec = install(model_gout(live=["old-bot"]), dry=False)
    out, exc, _ = call(provision.run, "k", apply=True, retire=True)
    assert exc is None, (exc, out)
    assert len(deletes(rec)) == 1, rec.calls


def test_retired_sa_with_roles_or_wif_or_act_as_fails_validation():
    for key, val in (("roles", ["run.developer"]), ("wif_repos", ["o/r"]), ("act_as", ["keeper"]),
                     ("wif_subjects", [{"repo": "o/r", "environment": "e"}]),
                     ("resource_roles", {"secrets": {"s": ["x"]}}), ("deployed_by", ["a-deployer"]),
                     ("service_agents", ["pubsub"])):
        cfg = {"service_accounts": [{**RETIRED, key: val}, LIVE]}
        _, exc, _ = call(P.validate_retired, cfg)
        assert exc is not None and "old-bot" in str(exc.code), key
    ok = {"service_accounts": [{**RETIRED, "description": "d", "reason": "r"}, LIVE]}
    assert call(P.validate_retired, ok)[1] is None


def test_retired_sa_named_in_act_as_or_deployed_by_fails_validation():
    for ref in ({"name": "x", "act_as": ["old-bot"]},):
        _, exc, _ = call(P.validate_retired, {"service_accounts": [RETIRED, ref]})
        assert exc is not None and "old-bot" in str(exc.code)
    dep = {"name": "api-deployer", "retire": True}
    run = {"name": "api-run", "deployed_by": ["api-deployer"]}
    _, exc, _ = call(P.validate_retired, {"service_accounts": [dep, run]})
    assert exc is not None and "api-deployer" in str(exc.code)
    # validation is wired into the handlers, not only callable
    install(model_gout())
    _, exc, _ = call(P.ensure_service_accounts, {"service_accounts": [dep, run]}, PROJECT)
    assert exc is not None


def test_retire_refuses_provisioner_identity():
    cfg = {"service_accounts": [{"name": P.PROVISIONER_SA, "retire": True}]}
    install(model_gout(live=[P.PROVISIONER_SA]))
    out, exc, _ = call(P.retire_service_accounts, cfg, PROJECT)
    assert exc is not None and P.PROVISIONER_SA in str(exc.code), (exc, out)
    assert core.MUTATIONS == 0


def test_retire_cli_mutually_exclusive_with_prune():
    old = sys.argv
    try:
        sys.argv = ["provision.py", "k", "--retire", "--prune"]
        out, exc, _ = call(provision.main)
        assert exc is not None and exc.code == 2 and "not allowed with" in out, out
        sys.argv = ["provision.py", "k", "--retire", "--only", "service_accounts"]
        out, exc, _ = call(provision.main)
        assert exc is not None and exc.code == 2 and "--only" in out and "retire" in out, out
    finally:
        sys.argv = old


if __name__ == "__main__":
    runner(globals())
