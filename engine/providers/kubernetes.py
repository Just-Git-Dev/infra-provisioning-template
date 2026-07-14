#!/usr/bin/env python3
"""Kubernetes provider — TENANCY + IDENTITY for a Just-Git-Dev GKE namespace.

Manages the standing, rarely-changing objects that carve out a tenant and give it an
identity (idempotent, plan-first):

  - namespace        : the tenant Namespace + its labels
  - quota            : a ResourceQuota (value-aware on the declared hard limits)
  - network_policy   : a default-deny-ingress baseline (opt-in via `network_isolation`)
  - rbac             : namespaced Role(s) + RoleBinding(s)
  - ksa              : Kubernetes ServiceAccount(s) + GKE Workload-Identity, BOTH halves —
                       the KSA `iam.gke.io/gcp-service-account` annotation (k8s side) AND
                       the reciprocal GCP `roles/iam.workloadIdentityUser` grant on the GSA
                       (delegated to the gcp provider's gcloud seam, not duplicated here).

And the standing EXPOSURE infra (ADR-003 Phase 3):

  - service          : ClusterIP Service selecting the workload by label
  - certificate      : cert-manager Certificate (produces the TLS secret)
  - ingress          : Ingress (host → service, TLS, external-dns annotation for DNS)
  - hpa              : HorizontalPodAutoscaler (value-aware on min/max replicas)
  - pdb              : PodDisruptionBudget selecting the workload by label

Deliberately NOT owned here: the Deployment/workload + image (CI/CD deploy reusables)
and ConfigMap/Secret (`manage-config-secrets.yml`). Every exposure object above selects
the workload by label or references it by name from OUTSIDE, so no field is shared with
the Deployment — the invariant that keeps the engine's drift-detection honest. See ADR-003 §3.

This module owns the `kubectl` seam; the dry-run gate, fail-loud read, and printer live
in `engine/core.py`. Tests stub `kubectl`/`kout` here (no cluster needed).

Honest-drift note: namespace/network_policy/rbac reconcile is existence-based (spec-level
drift on a NetworkPolicy or Role's rules is not yet detected — server-side-apply diffing
is Phase-3 hardening, tracked in ADR-003 open questions). quota (hard limits), namespace
labels, and the KSA WI annotation ARE value-aware.
"""
import subprocess

try:
    import yaml
except ImportError:                      # provision.py guards this with a friendly message
    yaml = None

import core
from core import c
from providers import gcp                 # reuse the gcloud seam for the GCP-side WI binding


# All engine-created objects carry this label so the (opt-in) pruners can find exactly
# what the engine owns and never touch hand-created or other-system objects.
MANAGED = {"app.kubernetes.io/managed-by": "jgd-provisioner"}
_MANAGED_SELECTOR = "app.kubernetes.io/managed-by=jgd-provisioner"
_WI_ANNOTATION = "iam.gke.io/gcp-service-account"


# ── kubectl seam (stubbed in tests) ──────────────────────────────────────────
def kubectl(args, stdin=None, check=True):
    cmd = ["kubectl"] + args
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, check=check)


# kubectl stderr fragments that genuinely mean "the object is absent" (an expected result
# → '') as opposed to a real failure (auth/RBAC-forbidden/network/transient).
_ABSENT_MARKERS = ("NotFound", "(NotFound)", "not found", "No resources found")


def kout(args):
    """Read via kubectl; '' means the object is ABSENT (fail-loud otherwise)."""
    return core.read(lambda: kubectl(args, check=True).stdout, _ABSENT_MARKERS,
                     label=" ".join(args))


def do(args, describe, stdin=None):
    """Mutating kubectl call — withheld in dry-run."""
    core.do(lambda: kubectl(args, stdin=stdin), describe)


def undo(args, describe):
    """Destructive kubectl call — withheld in dry-run. Used by --prune."""
    core.undo(lambda: kubectl(args), describe)


# ── helpers ──────────────────────────────────────────────────────────────────
def _jsonpath(dotted):
    """Escape dots in a label/annotation KEY so jsonpath treats it as one map key."""
    return dotted.replace(".", "\\.")


def _apply(body, describe):
    """Server-apply a manifest dict (built here, so we own its exact spec)."""
    core.do(lambda: kubectl(["apply", "-f", "-"], stdin=yaml.safe_dump(body)), describe)


def _managed_names(kind, ns):
    """Basenames of engine-managed objects of `kind` live in `ns` (via the managed label)."""
    out = kout(["get", kind, "-n", ns, "-l", _MANAGED_SELECTOR, "-o", "name"])
    return [line.split("/", 1)[-1] for line in out.splitlines() if line]


# ── subsystem handlers ───────────────────────────────────────────────────────
def ensure_namespace(cfg, ns):
    print("namespace:")
    labels = {**MANAGED, **(cfg.get("labels") or {})}
    if kout(["get", "namespace", ns, "-o", "name"]):
        c("ok", f"namespace {ns}")
    else:
        do(["create", "namespace", ns], f"create namespace {ns}")
    for k, v in labels.items():
        live = kout(["get", "namespace", ns, "-o",
                     "jsonpath={.metadata.labels." + _jsonpath(k) + "}"])
        if live == str(v):
            c("ok", f"  label {k}={v}")
        else:
            do(["label", "namespace", ns, f"{k}={v}", "--overwrite"], f"  set label {k}={v}")


def ensure_quota(cfg, ns):
    print("quota:")
    hard = cfg.get("quota") or {}
    if not hard:
        c("skip", "no quota configured")
        return
    name = f"{ns}-quota"
    exists = kout(["get", "resourcequota", name, "-n", ns, "-o", "name"])
    drift = []
    for key, want in hard.items():
        want = str(want)
        live = kout(["get", "resourcequota", name, "-n", ns, "-o",
                     "jsonpath={.spec.hard." + _jsonpath(key) + "}"]) if exists else ""
        if live == want:
            c("ok", f"  {key}={want}")
        else:
            drift.append((key, live, want))
    body = {"apiVersion": "v1", "kind": "ResourceQuota",
            "metadata": {"name": name, "namespace": ns, "labels": MANAGED},
            "spec": {"hard": {k: str(v) for k, v in hard.items()}}}
    if not exists:
        _apply(body, f"create resourcequota {name} ({', '.join(f'{k}={v}' for k, v in hard.items())})")
    elif drift:
        _apply(body, "update resourcequota %s (%s)" % (
            name, ", ".join(f"{k} {l or '∅'}→{w}" for k, l, w in drift)))


def ensure_network_policies(cfg, ns):
    print("network_policy:")
    if not cfg.get("network_isolation"):
        c("skip", "network_isolation not enabled")
        return
    name = "default-deny-ingress"
    if kout(["get", "networkpolicy", name, "-n", ns, "-o", "name"]):
        c("ok", f"networkpolicy {name}")
    else:
        body = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                "metadata": {"name": name, "namespace": ns, "labels": MANAGED},
                "spec": {"podSelector": {}, "policyTypes": ["Ingress"]}}
        _apply(body, f"create networkpolicy {name}")


def ensure_rbac(cfg, ns):
    print("rbac:")
    rbac = cfg.get("rbac") or {}
    roles, bindings = rbac.get("roles") or [], rbac.get("bindings") or []
    if not roles and not bindings:
        c("skip", "no rbac configured")
        return
    for r in roles:
        rn = r["name"]
        if kout(["get", "role", rn, "-n", ns, "-o", "name"]):
            c("ok", f"role {rn}")
        else:
            body = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role",
                    "metadata": {"name": rn, "namespace": ns, "labels": MANAGED},
                    "rules": r.get("rules") or []}
            _apply(body, f"create role {rn}")
    for b in bindings:
        bn = b["name"]
        if kout(["get", "rolebinding", bn, "-n", ns, "-o", "name"]):
            c("ok", f"rolebinding {bn}")
        else:
            body = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
                    "metadata": {"name": bn, "namespace": ns, "labels": MANAGED},
                    "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role",
                                "name": b["role"]},
                    "subjects": b.get("subjects") or []}
            _apply(body, f"create rolebinding {bn} → {b['role']}")


def ensure_ksa(cfg, ns):
    """KSA + GKE Workload-Identity (both halves). The KSA annotation is the k8s side; the
    reciprocal `roles/iam.workloadIdentityUser` grant on the GSA (member is the KSA's WI
    principal) is delegated to the gcp provider so we reuse ONE gcloud seam. Needs
    `gcp_project` (the Workload-Identity host project) for the WI principal string."""
    print("ksa:")
    sas = cfg.get("service_accounts") or []
    if not sas:
        c("skip", "no ksa configured")
        return
    gcp_project = cfg.get("gcp_project")
    for sa in sas:
        name = sa["name"]
        if kout(["get", "serviceaccount", name, "-n", ns, "-o", "name"]):
            c("ok", f"ksa {name}")
        else:
            do(["create", "serviceaccount", name, "-n", ns], f"create ksa {name}")
        gsa = sa.get("gcp_service_account")
        if not gsa:
            continue
        live = kout(["get", "serviceaccount", name, "-n", ns, "-o",
                     "jsonpath={.metadata.annotations." + _jsonpath(_WI_ANNOTATION) + "}"])
        if live == gsa:
            c("ok", f"  wi-annotation → {gsa}")
        else:
            do(["annotate", "serviceaccount", name, "-n", ns,
                f"{_WI_ANNOTATION}={gsa}", "--overwrite"], f"  annotate {name} wi → {gsa}")
        # Reciprocal GCP-side grant: the KSA's WI principal may impersonate the GSA.
        if not gcp_project:
            c("skip", f"  no gcp_project — skipping GCP wi-binding for {name}")
            continue
        member = f"serviceAccount:{gcp_project}.svc.id.goog[{ns}/{name}]"
        short = gsa.split("@")[0]
        if gcp.sa_has_member(gcp_project, gsa, "roles/iam.workloadIdentityUser", member):
            c("ok", f"  gcp wi-binding {name} → {short}")
        else:
            gcp.do(["iam", "service-accounts", "add-iam-policy-binding", gsa,
                    "--project", gcp_project, "--role=roles/iam.workloadIdentityUser",
                    f"--member={member}"], gcp_project, f"  bind gcp wi {name} → {short}")


# ── exposure infra (ADR-003 Phase 3) ─────────────────────────────────────────
# Standing exposure objects the engine can fully own: they select the workload by LABEL
# (Service/PDB) or reference it by name from OUTSIDE (Ingress→Service, HPA→Deployment), so
# no field is shared with the CI/CD-owned Deployment — the invariant that keeps drift honest.
# DNS is handled the GKE-idiomatic way: an external-dns annotation on the Ingress (below),
# not a standalone Cloud DNS handler (that would need the gcloud seam + a hosted-zone topology
# this target doesn't carry — deferred). Reconcile is existence-based except HPA min/max
# (value-aware); spec-level drift on Service/Ingress/Certificate/PDB is the same Phase-3
# server-side-apply hardening tracked in ADR-003 open questions.
def ensure_service(cfg, ns):
    print("service:")
    svc = cfg.get("service") or {}
    if not svc:
        c("skip", "no service configured")
        return
    name = svc["name"]
    port = svc.get("port", 80)
    if kout(["get", "service", name, "-n", ns, "-o", "name"]):
        c("ok", f"service {name}")
    else:
        body = {"apiVersion": "v1", "kind": "Service",
                "metadata": {"name": name, "namespace": ns, "labels": MANAGED},
                "spec": {"type": svc.get("type", "ClusterIP"),
                         "selector": svc.get("selector") or {"app": name},
                         "ports": [{"port": port, "targetPort": svc.get("target_port", port)}]}}
        _apply(body, f"create service {name} :{port} → {svc.get('target_port', port)}")


def ensure_ingress(cfg, ns):
    print("ingress:")
    ing = cfg.get("ingress") or {}
    if not ing:
        c("skip", "no ingress configured")
        return
    name = ing["name"]
    if kout(["get", "ingress", name, "-n", ns, "-o", "name"]):
        c("ok", f"ingress {name} ({ing.get('host', '?')})")
    else:
        ann = dict(ing.get("annotations") or {})
        if ing.get("external_dns"):                     # DNS via external-dns (GKE-idiomatic)
            ann["external-dns.alpha.kubernetes.io/hostname"] = ing["external_dns"]
        rule = {"host": ing["host"], "http": {"paths": [{
            "path": ing.get("path", "/"), "pathType": ing.get("path_type", "Prefix"),
            "backend": {"service": {"name": ing["service"],
                                    "port": {"number": ing.get("port", 80)}}}}]}}
        spec = {"rules": [rule]}
        if ing.get("class"):
            spec["ingressClassName"] = ing["class"]
        if ing.get("tls_secret"):
            spec["tls"] = [{"hosts": [ing["host"]], "secretName": ing["tls_secret"]}]
        body = {"apiVersion": "networking.k8s.io/v1", "kind": "Ingress",
                "metadata": {"name": name, "namespace": ns, "labels": MANAGED, "annotations": ann},
                "spec": spec}
        _apply(body, f"create ingress {name} → {ing['host']}")


def ensure_certificate(cfg, ns):
    """cert-manager Certificate — the object that PRODUCES the TLS secret. (The Secret it
    writes is cert-manager's; we own the Certificate declaring it, not the Secret.)"""
    print("certificate:")
    cert = cfg.get("certificate") or {}
    if not cert:
        c("skip", "no certificate configured")
        return
    name = cert["name"]
    if kout(["get", "certificate", name, "-n", ns, "-o", "name"]):
        c("ok", f"certificate {name}")
    else:
        body = {"apiVersion": "cert-manager.io/v1", "kind": "Certificate",
                "metadata": {"name": name, "namespace": ns, "labels": MANAGED},
                "spec": {"secretName": cert.get("secret", name),
                         "dnsNames": cert.get("dns_names") or [],
                         "issuerRef": {"name": cert["issuer"],
                                       "kind": cert.get("issuer_kind", "ClusterIssuer")}}}
        _apply(body, f"create certificate {name} ({', '.join(cert.get('dns_names') or [])})")


def ensure_hpa(cfg, ns):
    """HorizontalPodAutoscaler — scales the CI/CD-owned Deployment from outside (scaleTargetRef
    by name, no shared field). Value-aware on min/max replicas."""
    print("hpa:")
    hpa = cfg.get("hpa") or {}
    if not hpa:
        c("skip", "no hpa configured")
        return
    name = hpa["name"]
    want_min, want_max = str(hpa.get("min", 1)), str(hpa["max"])
    exists = kout(["get", "hpa", name, "-n", ns, "-o", "name"])
    drift = []
    if exists:
        for field, want in (("minReplicas", want_min), ("maxReplicas", want_max)):
            live = kout(["get", "hpa", name, "-n", ns, "-o", "jsonpath={.spec." + field + "}"])
            if live != want:
                drift.append((field, live, want))
    body = {"apiVersion": "autoscaling/v2", "kind": "HorizontalPodAutoscaler",
            "metadata": {"name": name, "namespace": ns, "labels": MANAGED},
            "spec": {"scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment",
                                        "name": hpa.get("target", name)},
                     "minReplicas": int(want_min), "maxReplicas": int(want_max),
                     "metrics": [{"type": "Resource", "resource": {"name": "cpu",
                                  "target": {"type": "Utilization",
                                             "averageUtilization": hpa.get("cpu", 80)}}}]}}
    if not exists:
        _apply(body, f"create hpa {name} ({want_min}-{want_max})")
    elif drift:
        _apply(body, "update hpa %s (%s)" % (
            name, ", ".join(f"{f} {l or '∅'}→{w}" for f, l, w in drift)))
    else:
        c("ok", f"hpa {name} ({want_min}-{want_max})")


def ensure_pdb(cfg, ns):
    """PodDisruptionBudget — selects the workload's pods by LABEL (no ref to the Deployment)."""
    print("pdb:")
    pdb = cfg.get("pdb") or {}
    if not pdb:
        c("skip", "no pdb configured")
        return
    name = pdb["name"]
    if kout(["get", "pdb", name, "-n", ns, "-o", "name"]):
        c("ok", f"pdb {name}")
    else:
        spec = {"selector": {"matchLabels": pdb.get("selector") or {"app": name}}}
        if "max_unavailable" in pdb:
            spec["maxUnavailable"] = pdb["max_unavailable"]
        else:
            spec["minAvailable"] = pdb.get("min_available", 1)
        body = {"apiVersion": "policy/v1", "kind": "PodDisruptionBudget",
                "metadata": {"name": name, "namespace": ns, "labels": MANAGED}, "spec": spec}
        _apply(body, f"create pdb {name}")


HANDLERS = {
    # tenancy + identity (Phase 2)
    "namespace": ensure_namespace,
    "quota": ensure_quota,
    "network_policy": ensure_network_policies,
    "rbac": ensure_rbac,
    "ksa": ensure_ksa,
    # exposure infra (Phase 3) — service before ingress; certificate feeds ingress TLS
    "service": ensure_service,
    "certificate": ensure_certificate,
    "ingress": ensure_ingress,
    "hpa": ensure_hpa,
    "pdb": ensure_pdb,
}


# ── prune (removal reconcile) ────────────────────────────────────────────────
# Opt-in (--prune), destructive, and scoped to the managed-by label so it only ever
# removes objects THIS engine created. Deliberately NO namespace or quota pruner:
# deleting a Namespace cascades to everything in it, and dropping a ResourceQuota removes
# a tenant's guardrail — both need explicit human action, never an automatic sweep.
# See ADR-003 "k8s prune safety" open question.
def prune_network_policies(cfg, ns):
    print("prune network_policy:")
    want = {"default-deny-ingress"} if cfg.get("network_isolation") else set()
    extra = [n for n in _managed_names("networkpolicy", ns) if n not in want]
    if not extra:
        c("ok", "no extra networkpolicies")
    for n in extra:
        undo(["delete", "networkpolicy", n, "-n", ns], f"delete networkpolicy {n}")


def prune_rbac(cfg, ns):
    print("prune rbac:")
    rbac = cfg.get("rbac") or {}
    want_roles = {r["name"] for r in rbac.get("roles") or []}
    want_binds = {b["name"] for b in rbac.get("bindings") or []}
    extra_roles = [n for n in _managed_names("role", ns) if n not in want_roles]
    extra_binds = [n for n in _managed_names("rolebinding", ns) if n not in want_binds]
    if not extra_roles and not extra_binds:
        c("ok", "no extra rbac")
    for n in extra_binds:
        undo(["delete", "rolebinding", n, "-n", ns], f"delete rolebinding {n}")
    for n in extra_roles:
        undo(["delete", "role", n, "-n", ns], f"delete role {n}")


def prune_ksa(cfg, ns):
    """Remove engine-managed KSAs config no longer declares. The reciprocal GCP-side WI
    binding is left in place (destructive cross-provider cleanup is conservative — a
    dangling grant to a now-absent KSA principal is inert)."""
    print("prune ksa:")
    want = {sa["name"] for sa in cfg.get("service_accounts") or []}
    extra = [n for n in _managed_names("serviceaccount", ns) if n not in want]
    if not extra:
        c("ok", "no extra ksa")
    for n in extra:
        undo(["delete", "serviceaccount", n, "-n", ns], f"delete ksa {n}")


def _prune_singleton(cfg, ns, cfg_key, kind, singular):
    """Prune a managed single-object exposure resource (Service/Ingress/…): if config no
    longer declares it, delete the engine-managed one (label-scoped)."""
    print(f"prune {singular}:")
    declared = (cfg.get(cfg_key) or {}).get("name")
    want = {declared} if declared else set()
    extra = [n for n in _managed_names(kind, ns) if n not in want]
    if not extra:
        c("ok", f"no extra {singular}")
    for n in extra:
        undo(["delete", kind, n, "-n", ns], f"delete {singular} {n}")


def prune_service(cfg, ns):
    _prune_singleton(cfg, ns, "service", "service", "service")


def prune_ingress(cfg, ns):
    _prune_singleton(cfg, ns, "ingress", "ingress", "ingress")


def prune_certificate(cfg, ns):
    _prune_singleton(cfg, ns, "certificate", "certificate", "certificate")


def prune_hpa(cfg, ns):
    _prune_singleton(cfg, ns, "hpa", "hpa", "hpa")


def prune_pdb(cfg, ns):
    _prune_singleton(cfg, ns, "pdb", "pdb", "pdb")


PRUNERS = {
    "network_policy": prune_network_policies,
    "rbac": prune_rbac,
    "ksa": prune_ksa,
    "service": prune_service,
    "certificate": prune_certificate,
    "ingress": prune_ingress,
    "hpa": prune_hpa,
    "pdb": prune_pdb,
}


# ── provider contract (used by the multi-target CLI loop, ADR-003 §5) ─────────
def context(target):
    """A kubernetes target's handler context is the target Namespace name."""
    return target["namespace"]


def preflight(ns):
    """Cluster reachable? (The namespace itself may not exist yet — we create it — so we
    probe the API server, not the namespace.) check=False so an unreachable cluster is a
    clean False, not a fail-loud read."""
    r = kubectl(["version", "--request-timeout=5s", "-o", "json"], check=False)
    return r.returncode == 0


def label(key, target, ns):
    return f"kubernetes: namespace {ns} @ {target.get('cluster', '?')}"
