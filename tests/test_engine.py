"""Engine plan tests — stub the gcloud seam, assert the computed plan.

No GCP access needed: we monkeypatch the gcp provider's gout (reads) and gcloud
(mutations), and core.DRY (the shared dry-run gate), then assert what the handlers
print in dry-run. Run: python3 -m pytest tests/ -q   (or: python3 tests/test_engine.py)

Scope: the gcp provider is the access/identity broker — three subsystems,
`service_accounts`, `act_as`, and `wif`. Resources (apis/secrets/pubsub/alerts) are
app-owned and have no handlers here (see DECISIONS 2026-07-01 + ADR-001). The
core/provider split is ADR-003.
"""
import io
import os
import sys
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engine"))
import core  # noqa: E402
from providers import gcp as P  # noqa: E402  (handlers + seam live in the gcp provider)

_ORIG_GOUT = P.gout   # drive() monkeypatches P.gout; keep the real one for gout tests


def drive(handler, cfg, gout_map, project="p"):
    """Run a handler in dry-run with gout stubbed from gout_map(args)->str."""
    core.DRY = True   # the dry-run gate lives in core (shared across providers)
    P.gout = lambda args, project=None: gout_map(args)
    P.gcloud = lambda *a, **k: (_ for _ in ()).throw(AssertionError("mutation in dry-run"))
    buf = io.StringIO()
    with redirect_stdout(buf):
        handler(cfg, project)
    return buf.getvalue()


def test_service_account_role_present_vs_missing():
    cfg = {"service_accounts": [{"name": "sa1", "roles": ["run.admin", "pubsub.admin"]}]}

    def gmap(args):
        j = " ".join(args)
        if "describe" in args:
            return "exists"                       # SA exists
        if "run.admin" in j:
            return "roles/run.admin"              # role present
        return ""                                  # pubsub.admin missing

    out = drive(P.ensure_service_accounts, cfg, gmap)
    assert "✓ sa sa1@p.iam.gserviceaccount.com" in out
    assert "✓   role roles/run.admin" in out
    assert "would   bind roles/pubsub.admin → sa1" in out


def test_service_account_created_when_missing():
    cfg = {"service_accounts": [{"name": "newsa", "roles": []}]}
    out = drive(P.ensure_service_accounts, cfg, lambda args: "")
    assert "would create sa newsa@p.iam.gserviceaccount.com" in out


def test_wif_binding_present_vs_missing():
    cfg = {
        "project": {"github_org": "AutoMahn", "gcp_number": "42"},
        "wif": {"pool": "github-actions", "provider": "github"},
        "service_accounts": [{"name": "rot", "wif_repos": ["AutoMahn/api", "AutoMahn/ui"]}],
    }

    def gmap(args):
        j = " ".join(args)
        if "workload-identity-pools" in args and "describe" in args:
            return "exists"                        # pool + provider exist
        if "AutoMahn/api" in j:
            return "roles/iam.workloadIdentityUser"  # api binding present
        return ""                                    # ui binding missing

    out = drive(P.ensure_wif, cfg, gmap)
    assert "✓ pool github-actions" in out
    assert "✓   wif AutoMahn/api → rot" in out
    assert "would   bind wif AutoMahn/ui → rot" in out


def test_wif_pool_and_provider_created_when_missing():
    cfg = {
        "project": {"github_org": "AutoMahn", "gcp_number": "42"},
        "wif": {"pool": "github-actions", "provider": "github"},
        "service_accounts": [],
    }
    out = drive(P.ensure_wif, cfg, lambda args: "")
    assert "would create pool github-actions" in out
    assert "would create provider github (org-scoped: AutoMahn)" in out


def test_wif_skipped_when_no_pool():
    cfg = {"project": {"github_org": "AutoMahn", "gcp_number": "42"}, "wif": {}, "service_accounts": []}
    out = drive(P.ensure_wif, cfg, lambda args: "")
    assert "no wif.pool configured" in out


def test_handlers_are_access_only():
    # Guard the scope: only the three access subsystems are registered.
    assert set(P.HANDLERS) == {"service_accounts", "act_as", "wif"}
    assert set(P.PRUNERS) == {"service_accounts", "act_as", "provisioner"}


# ── act_as (SA→SA impersonation) ─────────────────────────────────────────────
def test_act_as_binding_present_vs_missing():
    cfg = {"service_accounts": [
        {"name": "releaser", "act_as": ["run-a", "run-b"]},
        {"name": "run-a"}, {"name": "run-b"},
    ]}

    def gmap(args):
        j = " ".join(args)
        # run-a already bound to releaser; run-b not yet
        return "roles/iam.serviceAccountUser" if "run-a@" in j else ""

    out = drive(P.ensure_act_as, cfg, gmap)
    assert "act_as releaser → run-a" in out
    assert "would bind act_as releaser → run-b" in out


def test_act_as_none_declared():
    cfg = {"service_accounts": [{"name": "run-a"}, {"name": "run-b"}]}
    out = drive(P.ensure_act_as, cfg, lambda args: "")
    assert "no act_as declared" in out


def test_prune_act_as_removes_own_undeclared_keeps_external():
    # releaser SHOULD act as run-a (wanted). Live on run-a: releaser (wanted, keep),
    # a stale owned SA `old` (undeclared → remove), and an external member (keep).
    cfg = {"service_accounts": [
        {"name": "releaser", "act_as": ["run-a"]},
        {"name": "run-a"}, {"name": "old"},
    ]}

    def gmap(args):
        j = " ".join(args)
        if "run-a@" in j:
            return ("serviceAccount:releaser@p.iam.gserviceaccount.com\n"
                    "serviceAccount:old@p.iam.gserviceaccount.com\n"
                    "serviceAccount:svc-123@gcp-sa-pubsub.iam.gserviceaccount.com")
        return ""

    out = drive(P.prune_act_as, cfg, gmap)
    assert "would unbind act_as old → run-a" in out          # owned + undeclared → pruned
    assert "unbind act_as releaser" not in out               # wanted → kept
    assert "svc-123" not in out                              # external/Google-managed → untouched


# ── gout fail-loud ───────────────────────────────────────────────────────────
def _raise_gcloud(stderr):
    import subprocess
    def boom(args, project=None, check=True):
        raise subprocess.CalledProcessError(1, ["gcloud"], stderr=stderr)
    return boom


def test_gout_absent_returns_empty():
    P.gout = _ORIG_GOUT
    P.gcloud = _raise_gcloud("ERROR: (gcloud...) NOT_FOUND: ... does not exist")
    assert P.gout(["iam", "service-accounts", "describe", "x"]) == ""


def test_gout_real_failure_is_loud():
    P.gout = _ORIG_GOUT
    P.gcloud = _raise_gcloud("ERROR: (gcloud...) PERMISSION_DENIED: caller lacks permission")
    try:
        P.gout(["projects", "get-iam-policy", "x"])
        raise AssertionError("expected SystemExit on non-absent failure")
    except SystemExit:
        pass


# ── prune (subtractive) ──────────────────────────────────────────────────────
def test_prune_service_accounts_removes_only_extra():
    cfg = {"service_accounts": [{"name": "sa1", "roles": ["run.admin"]}]}
    # live: the wanted role + one extra config no longer declares
    out = drive(P.prune_service_accounts, cfg,
                lambda args: "roles/run.admin\nroles/monitoring.editor")
    assert "would unbind roles/monitoring.editor from sa1" in out
    assert "unbind roles/run.admin" not in out          # wanted role is kept


def test_prune_service_accounts_noop_when_matching():
    cfg = {"service_accounts": [{"name": "sa1", "roles": ["run.admin"]}]}
    out = drive(P.prune_service_accounts, cfg, lambda args: "roles/run.admin")
    assert "no extra roles" in out
    assert "unbind" not in out


def test_prune_provisioner_keeps_kept_removes_rest():
    live = "\n".join([
        "roles/iam.serviceAccountAdmin",
        "roles/resourcemanager.projectIamAdmin",
        "roles/iam.workloadIdentityPoolAdmin",
        "roles/monitoring.editor",          # stale over-grant
        "roles/pubsub.editor",              # stale over-grant
    ])
    out = drive(P.prune_provisioner, {}, lambda args: live)
    assert "would unbind roles/monitoring.editor from infra-provisioner" in out
    assert "would unbind roles/pubsub.editor from infra-provisioner" in out
    assert "keep roles/resourcemanager.projectIamAdmin" in out
    # never removes projectIamAdmin — it is what lets the prune finish
    assert "unbind roles/resourcemanager.projectIamAdmin" not in out


def test_provisioner_kept_roles_reads_shared_file():
    assert P.provisioner_kept_roles() == {
        "roles/iam.serviceAccountAdmin",
        "roles/resourcemanager.projectIamAdmin",
        "roles/iam.workloadIdentityPoolAdmin",
    }


# ── mutation-path regressions (ported from the infra-provisioning consumer) ──
def _raise_on_call(stderr):
    """0-arg callable that fails the way a real `gcloud` mutation does."""
    import subprocess
    def boom():
        raise subprocess.CalledProcessError(1, ["gcloud"], stderr=stderr)
    return boom


def test_do_surfaces_gcloud_stderr_on_failure():
    """A failed mutation must exit with gcloud's stderr, not swallow it — `read`
    has been fail-loud since ADR-003; `do`/`undo` were not, so a rejected create
    printed a bare traceback-free '+ create sa …' and moved on."""
    core.DRY = False
    try:
        for op in (core.do, core.undo):
            try:
                op(_raise_on_call("ERROR: INVALID_ARGUMENT: display name too long"), "create sa x")
                raise AssertionError(f"expected SystemExit from core.{op.__name__}")
            except SystemExit as e:
                assert "INVALID_ARGUMENT" in str(e), str(e)
    finally:
        core.DRY = True


def test_service_account_display_name_truncated_by_bytes():
    """GCP's displayName limit is 100 BYTES, not characters. Truncating the
    description by character count let a multibyte description (e.g. an em-dash)
    overflow, and `service-accounts create` failed with INVALID_ARGUMENT."""
    desc = "\u2014" * 60                  # em-dash: 1 char, 3 bytes → 60 chars / 180 bytes
    cfg = {"service_accounts": [{"name": "sa1", "description": desc, "roles": []}]}
    seen = []
    core.DRY = False
    P.gout = lambda args, project=None: ""          # SA absent → take the create path
    P.gcloud = lambda args, project=None, check=True: seen.append(args)
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            P.ensure_service_accounts(cfg, "p")
    finally:
        core.DRY = True
    flag = next(a for a in seen[0] if a.startswith("--display-name="))
    value = flag.split("=", 1)[1]
    n = len(value.encode("utf-8"))
    assert n <= 100, f"--display-name is {n} bytes, over GCP's 100-byte limit"
    assert value == desc[:33], "expected 33 em-dashes (99 bytes); the 34th would overflow"


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
    print(f"\n{len(fns)-failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
