"""WIF pruner `prune_wif` (README "--prune --only wif"; DECISIONS 2026-11-14).

It removes a roles/iam.workloadIdentityUser member from a DECLARED service account only when the
member comes from THIS project's own WIF pool and config no longer declares it. Everything else is
"not ours, kept". Every cloud seam is stubbed; nothing here reaches gcloud.
Run: python3 tests/test_wif_prune.py   (or: python3 -m pytest tests/ -q)"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _retire_fakes import (P, PROJECT, call, config_root, core, email,  # noqa: E402
                           install, runner)

import provision  # noqa: E402

NUM = "42"
POOL = "github-actions"
BASE = f"//iam.googleapis.com/projects/{NUM}/locations/global/workloadIdentityPools/{POOL}"
OTHER_POOL = f"//iam.googleapis.com/projects/{NUM}/locations/global/workloadIdentityPools/other-pool"
OTHER_NUM = f"//iam.googleapis.com/projects/7/locations/global/workloadIdentityPools/{POOL}"
ROLE = "roles/iam.workloadIdentityUser"
SUBJECT = f"principal:{BASE}/subject/repo:Example-Org/demo:environment:ops"
REPOWIDE = f"principalSet:{BASE}/attribute.repository/Example-Org/demo"
WIF = {"pool": POOL, "provider": "github"}


def _cfg(sas, wif=WIF):
    cfg = {"service_accounts": sas}
    if wif:
        cfg["wif"] = wif
    return cfg


def _gout(live, cond=None, number=NUM):
    """live: {sa name: [members holding workloadIdentityUser on it]}. cond: {sa name:
    [(title, [members])]} are CONDITIONAL workloadIdentityUser bindings. number: what
    `projects describe` answers."""
    by_email = {email(n): m for n, m in live.items()}
    cond_by_email = {email(n): m for n, m in (cond or {}).items()}

    def gout(args, project=None):
        j = " ".join(args)
        if args[:2] == ["projects", "describe"]:
            return number
        if args[:3] == ["iam", "service-accounts", "get-iam-policy"] and args[3] in by_email:
            members = by_email[args[3]]
            if "--format=json" in args:
                bs = [{"role": ROLE, "members": members}] if members else []
                bs += [{"role": ROLE, "members": ms, "condition": {"title": t, "expression": "true"}}
                       for t, ms in cond_by_email.get(args[3], [])]
                return json.dumps({"bindings": bs})
            return "\n".join(members) if "workloadIdentityUser" in j else ""
        return ""

    return gout


def _prune(cfg, live, dry=False, **kw):
    assert hasattr(P, "prune_wif"), "gcp.prune_wif does not exist yet (feature missing)"
    assert "wif" in P.PRUNERS, "PRUNERS has no 'wif' entry (feature missing)"
    P._PROJECT_NUMBERS.clear()
    rec = install(_gout(live, **kw), dry=dry)
    out, exc, _ = call(P.PRUNERS["wif"], cfg, PROJECT)
    return rec, out, exc


def _removals(rec):
    return [a for a in rec.calls if "remove-iam-policy-binding" in a]


def _members_removed(rec):
    return sorted(m.split("=", 1)[1] for a in _removals(rec) for m in a if m.startswith("--member="))


def test_t1_repo_wide_to_env_narrowing_removes_repowide_keeps_subject():
    cfg = _cfg([{"name": "ops", "wif_subjects": [{"repo": "Example-Org/demo", "environment": "ops"}]}])
    rec, out, exc = _prune(cfg, {"ops": [SUBJECT, REPOWIDE]})
    assert exc is None, (exc, out)
    assert _members_removed(rec) == [REPOWIDE], (rec.calls, out)
    assert all(f"--role={ROLE}" in a for a in _removals(rec)), rec.calls
    assert email("ops") in _removals(rec)[0], rec.calls


def test_t2_other_pool_user_and_serviceaccount_members_kept_and_reported():
    cfg = _cfg([{"name": "ops"}])
    foreign = [f"principalSet:{OTHER_POOL}/attribute.repository/x/y",
               f"principalSet:{OTHER_NUM}/attribute.repository/x/y",
               "user:someone@example.org", f"serviceAccount:{email('other')}"]
    # positive control: an own-pool undeclared member IS removed, so "kept" is not a no-op run
    rec, out, exc = _prune(cfg, {"ops": foreign + [REPOWIDE]})
    assert exc is None, (exc, out)
    assert _members_removed(rec) == [REPOWIDE], (rec.calls, out)
    for m in foreign:
        assert any(m in ln and "not ours, kept" in ln for ln in out.splitlines()), (m, out)


def test_t3_undeclared_sa_is_never_touched():
    cfg = _cfg([{"name": "ops"}])
    rec, out, exc = _prune(cfg, {"ops": [REPOWIDE], "ghost": [REPOWIDE]})
    assert exc is None, (exc, out)
    assert _members_removed(rec) == [REPOWIDE], (rec.calls, out)     # positive control: ops pruned
    assert not any(email("ghost") in a for c in rec.calls for a in c), rec.calls


def test_t4_retire_true_sa_loses_its_own_pool_members():
    cfg = _cfg([{"name": "old", "retire": True}])
    rec, out, exc = _prune(cfg, {"old": [SUBJECT, REPOWIDE, "user:someone@example.org"]})
    assert exc is None, (exc, out)
    assert _members_removed(rec) == sorted([SUBJECT, REPOWIDE]), (rec.calls, out)
    assert "user:someone@example.org" not in _members_removed(rec)


def test_t5_no_pool_skips_and_touches_nothing():
    cfg = _cfg([{"name": "ops"}], wif=None)
    rec, out, exc = _prune(cfg, {"ops": [REPOWIDE]})
    assert exc is None, (exc, out)
    assert "· " in out and "wif.pool" in out, out      # the engine prints a skip as "· <msg>"
    assert rec.calls == [], rec.calls


def test_t6_prune_only_wif_is_accepted_by_provision_main():
    seen, saved_run, saved_argv = {}, provision.run, sys.argv
    provision.run = lambda project, **kw: seen.update(project=project, **kw)
    sys.argv = ["provision.py", "k", "--prune", "--only", "wif"]
    try:
        out, exc, _ = call(provision.main)
    finally:
        provision.run, sys.argv = saved_run, saved_argv
    assert exc is None, f"--prune --only wif rejected: {exc} {out}"
    assert seen.get("only") == "wif" and seen.get("prune") is True, seen


def test_t7_dry_run_prints_would_unbind_wif_and_trailer_counts_it():
    assert hasattr(P, "prune_wif"), "gcp.prune_wif does not exist yet (feature missing)"
    cfg = _cfg([{"name": "ops", "wif_subjects": [{"repo": "Example-Org/demo", "environment": "ops"}]}])
    config_root(cfg=cfg)
    P._PROJECT_NUMBERS.clear()
    rec = install(_gout({"ops": [SUBJECT, REPOWIDE]}))
    out, exc, _ = call(provision.run, "k", apply=False, prune=True, only="wif")
    assert exc is None, (exc, out)
    line = [ln for ln in out.splitlines() if "would unbind wif" in ln]
    assert len(line) == 1 and REPOWIDE in line[0] and "ops" in line[0].split(REPOWIDE)[1], out
    assert "prune dry-run complete — 1 removal planned." in out, out
    assert _removals(rec) == [], "dry-run must not mutate"


def test_plain_prune_includes_wif_and_nothing_extra_reports_ok():
    # the registry order matters for a plain --prune: wif must be part of it
    assert "wif" in P.PRUNERS, "PRUNERS has no 'wif' entry (feature missing)"
    cfg = _cfg([{"name": "ops", "wif_subjects": [{"repo": "Example-Org/demo", "environment": "ops"}]}])
    rec, out, exc = _prune(cfg, {"ops": [SUBJECT]})
    assert exc is None and "✓ no extra wif bindings" in out, (exc, out)   # ok prints as "✓"
    assert rec.calls == [], rec.calls


# ── review fixes 4-6, 8 ──────────────────────────────────────────────────────
def test_t18_examined_line_is_always_printed():
    cfg = _cfg([{"name": "ops", "wif_subjects": [{"repo": "Example-Org/demo", "environment": "ops"}]},
                {"name": "idle"}])
    rec, out, exc = _prune(cfg, {"ops": [SUBJECT, REPOWIDE], "idle": []})
    assert exc is None and "examined 2 SAs, 2 wif members" in out, (exc, out)
    rec, out, exc = _prune(cfg, {"ops": [SUBJECT], "idle": []})          # nothing extra
    assert exc is None and "no extra wif bindings" in out, (exc, out)
    assert "examined 2 SAs, 1 wif members" in out, out
    rec, out, exc = _prune(_cfg([{"name": "ops"}], wif=None), {"ops": [REPOWIDE]})   # no pool
    assert "examined 0 SAs, 0 wif members" in out, out


def test_t19_pool_name_prefix_is_not_the_own_pool():
    # `github-actions-2` merely STARTS with `github-actions`: the trailing slash must matter.
    cfg = _cfg([{"name": "ops"}])
    near = f"principalSet://iam.googleapis.com/projects/{NUM}/locations/global/workloadIdentityPools/{POOL}-2/attribute.repository/x/y"
    rec, out, exc = _prune(cfg, {"ops": [near, REPOWIDE]})
    assert exc is None, (exc, out)
    assert _members_removed(rec) == [REPOWIDE], (rec.calls, out)        # positive control
    assert any(near in ln and "not ours, kept" in ln for ln in out.splitlines()), out


def test_t20_project_number_prefix_is_not_the_own_project():
    # project 420 merely STARTS with 42
    cfg = _cfg([{"name": "ops"}])
    near = f"principalSet://iam.googleapis.com/projects/420/locations/global/workloadIdentityPools/{POOL}/attribute.repository/x/y"
    rec, out, exc = _prune(cfg, {"ops": [near, REPOWIDE]})
    assert exc is None, (exc, out)
    assert _members_removed(rec) == [REPOWIDE], (rec.calls, out)
    assert any(near in ln and "not ours, kept" in ln for ln in out.splitlines()), out


def test_t21_validated_project_number_wins_over_config_gcp_number():
    # README: the number is `_project_number` (validated describe), never the config's value.
    cfg = _cfg([{"name": "ops", "wif_subjects": [{"repo": "Example-Org/demo", "environment": "ops"}]}])
    cfg["project"] = {"gcp_number": "7"}
    wrong = f"principalSet:{OTHER_NUM}/attribute.repository/x/y"        # project 7's pool
    rec, out, exc = _prune(cfg, {"ops": [SUBJECT, REPOWIDE, wrong]})
    assert exc is None, (exc, out)
    assert _members_removed(rec) == [REPOWIDE], (rec.calls, out)        # SUBJECT stays declared
    assert any(wrong in ln and "not ours, kept" in ln for ln in out.splitlines()), out


def test_t22_non_numeric_project_number_exits_and_removes_nothing():
    cfg = _cfg([{"name": "ops"}])
    for bad in ("not-a-number", ""):
        rec, out, exc = _prune(cfg, {"ops": [REPOWIDE]}, number=bad)
        assert exc is not None and "project number" in str(exc.code), (bad, exc, out)
        assert "service_agents" not in str(exc.code), exc.code           # review fix 8: neutral message
        assert rec.calls == [] and core.MUTATIONS == 0, rec.calls


def test_t23_conditional_wif_binding_is_reported_and_kept():
    cfg = _cfg([{"name": "ops"}])
    rec, out, exc = _prune(cfg, {"ops": [REPOWIDE]},
                           cond={"ops": [("narrow", [SUBJECT, "user:someone@example.org"])]})
    assert exc is None, (exc, out)
    assert _members_removed(rec) == [REPOWIDE], (rec.calls, out)        # unconditional one IS removed
    for m in (SUBJECT, "user:someone@example.org"):
        assert any(m in ln and "conditional" in ln and "(narrow)" in ln and "kept" in ln
                   for ln in out.splitlines()), (m, out)


def test_t24_malformed_tombstone_exits_before_any_removal():
    # same guard prune_service_accounts runs: a retire: true SA carrying other keys is refused
    cfg = _cfg([{"name": "old", "retire": True, "roles": ["run.developer"]}])
    rec, out, exc = _prune(cfg, {"old": [REPOWIDE]})
    assert exc is not None and "retire" in str(exc.code), (exc, out)
    assert rec.calls == [], rec.calls


if __name__ == "__main__":
    runner(globals())
