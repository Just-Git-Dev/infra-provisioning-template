"""Kubernetes provider plan tests — stub the kubectl seam, assert the computed plan.

No cluster needed: we monkeypatch the kubernetes provider's kout (reads) and kubectl
(mutations), plus the gcp provider's gout/gcloud for the ksa WI-binding half, and
core.DRY (the shared dry-run gate), then assert what the handlers print in dry-run.
Run: python3 tests/test_kubernetes.py   (or: python3 -m pytest tests/)

Scope: the kubernetes provider is the tenancy/identity broker — namespace, quota,
network_policy, rbac, ksa (ADR-003 Phase 2). Exposure infra (Service/Ingress/TLS/DNS/
HPA/PDB) is Phase 3 and has no handlers yet.
"""
import io
import os
import sys
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engine"))
import core  # noqa: E402
import provision  # noqa: E402
from providers import kubernetes as P  # noqa: E402
from providers import gcp as G  # noqa: E402  (ksa delegates the GCP-side WI grant here)

_BOOM = lambda *a, **k: (_ for _ in ()).throw(AssertionError("mutation in dry-run"))


def drive(handler, cfg, kout_map, ns="app", gcp_map=None):
    """Run a handler in dry-run with kout (and optionally gcp.gout) stubbed."""
    core.DRY = True   # the dry-run gate lives in core (shared across providers)
    P.kout = lambda args: kout_map(args)
    P.kubectl = _BOOM
    G.gout = lambda args, project=None: (gcp_map(args) if gcp_map else "")
    G.gcloud = _BOOM
    buf = io.StringIO()
    with redirect_stdout(buf):
        handler(cfg, ns)
    return buf.getvalue()


# ── namespace ────────────────────────────────────────────────────────────────
def test_namespace_created_when_missing():
    out = drive(P.ensure_namespace, {"labels": {"team": "platform"}}, lambda args: "")
    assert "would create namespace app" in out
    assert "would   set label app.kubernetes.io/managed-by=jgd-provisioner" in out
    assert "would   set label team=platform" in out


def test_namespace_exists_label_present_vs_missing():
    def kmap(args):
        j = " ".join(args)
        if "-o name" in j:
            return "namespace/app"                    # namespace exists
        if "managed-by" in j:
            return "jgd-provisioner"                  # managed label already set
        return ""                                     # team label missing

    out = drive(P.ensure_namespace, {"labels": {"team": "platform"}}, kmap)
    assert "✓ namespace app" in out
    assert "✓   label app.kubernetes.io/managed-by=jgd-provisioner" in out
    assert "would   set label team=platform" in out


# ── quota ────────────────────────────────────────────────────────────────────
def test_quota_created_when_missing():
    out = drive(P.ensure_quota, {"quota": {"requests.cpu": "4", "limits.memory": "16Gi"}},
                lambda args: "")
    assert "would create resourcequota app-quota" in out
    assert "requests.cpu=4" in out


def test_quota_drift_when_value_differs():
    def kmap(args):
        j = " ".join(args)
        if "-o name" in j:
            return "resourcequota/app-quota"          # quota exists
        if "requests\\.cpu" in j:
            return "2"                                # live 2, want 4 → drift
        return ""

    out = drive(P.ensure_quota, {"quota": {"requests.cpu": "4"}}, kmap)
    assert "would update resourcequota app-quota" in out
    assert "requests.cpu 2→4" in out


def test_quota_noop_when_matching():
    def kmap(args):
        j = " ".join(args)
        if "-o name" in j:
            return "resourcequota/app-quota"
        if "requests\\.cpu" in j:
            return "4"                                # live == want
        return ""

    out = drive(P.ensure_quota, {"quota": {"requests.cpu": "4"}}, kmap)
    assert "✓   requests.cpu=4" in out
    assert "would" not in out


def test_quota_skipped_when_none():
    out = drive(P.ensure_quota, {}, lambda args: "")
    assert "no quota configured" in out


# ── network_policy ───────────────────────────────────────────────────────────
def test_network_policy_applied_when_isolation():
    out = drive(P.ensure_network_policies, {"network_isolation": True}, lambda args: "")
    assert "would create networkpolicy default-deny-ingress" in out


def test_network_policy_skipped_by_default():
    out = drive(P.ensure_network_policies, {}, lambda args: "")
    assert "network_isolation not enabled" in out


# ── rbac ─────────────────────────────────────────────────────────────────────
def test_rbac_role_and_binding_created():
    cfg = {"rbac": {
        "roles": [{"name": "app-reader", "rules": [{"apiGroups": [""], "resources": ["pods"],
                                                     "verbs": ["get", "list"]}]}],
        "bindings": [{"name": "app-reader-b", "role": "app-reader",
                      "subjects": [{"kind": "ServiceAccount", "name": "app"}]}],
    }}
    out = drive(P.ensure_rbac, cfg, lambda args: "")
    assert "would create role app-reader" in out
    assert "would create rolebinding app-reader-b → app-reader" in out


def test_rbac_skipped_when_none():
    out = drive(P.ensure_rbac, {}, lambda args: "")
    assert "no rbac configured" in out


# ── ksa (both WI halves) ─────────────────────────────────────────────────────
def test_ksa_created_and_annotated_and_gcp_bound():
    cfg = {"gcp_project": "auto-mahn", "service_accounts": [
        {"name": "api", "gcp_service_account": "api-run@auto-mahn.iam.gserviceaccount.com"}]}
    # kout: everything absent (KSA missing, no annotation). gcp: no WI member yet.
    out = drive(P.ensure_ksa, cfg, lambda args: "", gcp_map=lambda args: "")
    assert "would create ksa api" in out
    assert "would   annotate api wi → api-run@auto-mahn.iam.gserviceaccount.com" in out
    assert "would   bind gcp wi api → api-run" in out


def test_ksa_annotation_and_binding_present():
    gsa = "api-run@auto-mahn.iam.gserviceaccount.com"
    cfg = {"gcp_project": "auto-mahn",
           "service_accounts": [{"name": "api", "gcp_service_account": gsa}]}

    def kmap(args):
        j = " ".join(args)
        if "-o name" in j:
            return "serviceaccount/api"               # KSA exists
        return gsa                                    # annotation already set

    # gcp side: the WI member already holds workloadIdentityUser
    out = drive(P.ensure_ksa, cfg, kmap,
                gcp_map=lambda args: "roles/iam.workloadIdentityUser")
    assert "✓ ksa api" in out
    assert "✓   wi-annotation → " + gsa in out
    assert "✓   gcp wi-binding api → api-run" in out
    assert "would" not in out


def test_ksa_skipped_when_none():
    out = drive(P.ensure_ksa, {}, lambda args: "")
    assert "no ksa configured" in out


# ── exposure infra (Phase 3) ─────────────────────────────────────────────────
def test_service_created_when_missing():
    out = drive(P.ensure_service, {"service": {"name": "api", "port": 80, "target_port": 8080}},
                lambda args: "")
    assert "would create service api :80 → 8080" in out


def test_service_skipped_when_none():
    assert "no service configured" in drive(P.ensure_service, {}, lambda args: "")


def test_ingress_created_with_tls_and_dns():
    cfg = {"ingress": {"name": "api", "host": "api.automahn.app", "service": "api",
                       "tls_secret": "api-tls", "external_dns": "api.automahn.app"}}
    out = drive(P.ensure_ingress, cfg, lambda args: "")
    assert "would create ingress api → api.automahn.app" in out


def test_ingress_skipped_when_none():
    assert "no ingress configured" in drive(P.ensure_ingress, {}, lambda args: "")


def test_certificate_created_when_missing():
    cfg = {"certificate": {"name": "api-tls", "issuer": "letsencrypt-prod",
                           "dns_names": ["api.automahn.app"]}}
    out = drive(P.ensure_certificate, cfg, lambda args: "")
    assert "would create certificate api-tls (api.automahn.app)" in out


def test_hpa_created_when_missing():
    out = drive(P.ensure_hpa, {"hpa": {"name": "api", "min": 2, "max": 10}}, lambda args: "")
    assert "would create hpa api (2-10)" in out


def test_hpa_drift_on_max_replicas():
    def kmap(args):
        j = " ".join(args)
        if "-o name" in j:
            return "horizontalpodautoscaler.autoscaling/api"   # exists
        if "minReplicas" in j:
            return "2"                                         # min matches
        if "maxReplicas" in j:
            return "5"                                         # max drift: live 5, want 10
        return ""

    out = drive(P.ensure_hpa, {"hpa": {"name": "api", "min": 2, "max": 10}}, kmap)
    assert "would update hpa api" in out
    assert "maxReplicas 5→10" in out


def test_hpa_noop_when_matching():
    def kmap(args):
        j = " ".join(args)
        if "-o name" in j:
            return "horizontalpodautoscaler.autoscaling/api"
        if "minReplicas" in j:
            return "2"
        if "maxReplicas" in j:
            return "10"
        return ""

    out = drive(P.ensure_hpa, {"hpa": {"name": "api", "min": 2, "max": 10}}, kmap)
    assert "✓ hpa api (2-10)" in out
    assert "would" not in out


def test_pdb_created_when_missing():
    out = drive(P.ensure_pdb, {"pdb": {"name": "api", "min_available": 1}}, lambda args: "")
    assert "would create pdb api" in out


def test_prune_ingress_removes_managed_undeclared():
    # config declares no ingress; a managed one is live → prune it
    out = drive(P.prune_ingress, {}, lambda args: "ingress.networking.k8s.io/stale-ing")
    assert "would delete ingress stale-ing" in out


# ── registry guard ───────────────────────────────────────────────────────────
def test_handlers_are_tenancy_identity_and_exposure():
    assert set(P.HANDLERS) == {"namespace", "quota", "network_policy", "rbac", "ksa",
                               "service", "certificate", "ingress", "hpa", "pdb"}
    # Prune covers every removable object EXCEPT namespace + quota (destructive tenancy
    # objects that need explicit human action).
    assert set(P.PRUNERS) == {"network_policy", "rbac", "ksa",
                              "service", "certificate", "ingress", "hpa", "pdb"}
    assert "namespace" not in P.PRUNERS
    assert "quota" not in P.PRUNERS


# ── prune (subtractive, managed-label scoped) ────────────────────────────────
def test_prune_ksa_removes_only_managed_undeclared():
    cfg = {"service_accounts": [{"name": "api"}]}
    # live managed KSAs: api (declared, keep) + stale (undeclared, remove)
    out = drive(P.prune_ksa, cfg,
                lambda args: "serviceaccount/api\nserviceaccount/stale")
    assert "would delete ksa stale" in out
    assert "delete ksa api" not in out                # declared → kept


def test_prune_ksa_noop_when_matching():
    out = drive(P.prune_ksa, {"service_accounts": [{"name": "api"}]},
                lambda args: "serviceaccount/api")
    assert "no extra ksa" in out
    assert "delete" not in out


# ── multi-target dispatch (provision.targets_of) ─────────────────────────────
def test_targets_autowrap_bare_config_is_gcp():
    cfg = {"project": {"gcp_id": "auto-mahn"}, "service_accounts": []}
    targets = provision.targets_of(cfg)
    assert len(targets) == 1
    assert targets[0]["kind"] == "gcp"
    assert targets[0]["project"]["gcp_id"] == "auto-mahn"   # whole config is the target


def test_targets_explicit_are_used_as_is():
    cfg = {"targets": [
        {"kind": "gcp", "project": {"gcp_id": "auto-mahn"}},
        {"kind": "kubernetes", "namespace": "app", "cluster": "prod"},
    ]}
    targets = provision.targets_of(cfg)
    assert [t["kind"] for t in targets] == ["gcp", "kubernetes"]
    assert targets[1]["namespace"] == "app"


def test_gcp_target_defaults_kind():
    cfg = {"targets": [{"project": {"gcp_id": "x"}}]}   # kind omitted → gcp
    assert provision.targets_of(cfg)[0]["kind"] == "gcp"


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
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
