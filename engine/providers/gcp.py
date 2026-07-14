#!/usr/bin/env python3
"""GCP provider — the ACCESS/IDENTITY broker for a Just-Git-Dev GCP project.

Manages exactly three access subsystems (idempotent, plan-first):

  - service_accounts : the SAs we own + their project IAM roles
  - act_as           : SA→SA impersonation (roles/iam.serviceAccountUser) between our SAs
  - wif              : the app-repo WIF pool/provider + per-SA impersonation bindings

Everything else a project needs (enabling APIs, creating secrets, pub/sub topics,
Cloud Monitoring alert policies) is a **resource** owned by the app repo itself,
NOT by this provider. See DECISIONS 2026-07-01 (access-broker scope) and ADR-001.

This module owns the `gcloud` seam (`gcloud` mutations / `gout` reads); the
dry-run gate, fail-loud discipline, and printer live in `engine/core.py` (ADR-003).
Tests stub `gcloud`/`gout` here (no GCP access needed).
"""
import os
import subprocess

import core
from core import c


# ── gcloud seam (stubbed in tests) ───────────────────────────────────────────
def gcloud(args, project=None, check=True):
    cmd = ["gcloud"] + args + ([f"--project={project}"] if project else [])
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


# gcloud stderr fragments that genuinely mean "the resource is absent" (an expected
# result → '') as opposed to a real failure (auth/permission/network/transient).
_ABSENT_MARKERS = ("NOT_FOUND", "was not found", "does not exist",
                   "Listed 0 items", "could not be found")


def gout(args, project=None):
    """Read via gcloud; '' means the resource is ABSENT (fail-loud otherwise)."""
    return core.read(lambda: gcloud(args, project=project, check=True).stdout,
                     _ABSENT_MARKERS, label=" ".join(args))


def do(args, project, describe):
    """Mutating gcloud call — withheld in dry-run."""
    core.do(lambda: gcloud(args, project=project), describe)


def undo(args, project, describe):
    """Destructive (removal) gcloud call — withheld in dry-run. Used by --prune."""
    core.undo(lambda: gcloud(args, project=project), describe)


# ── helpers ──────────────────────────────────────────────────────────────────
def sa_email(name, project):
    return f"{name}@{project}.iam.gserviceaccount.com"


def sa_has_role(project, email, role):
    out = gout(["projects", "get-iam-policy", project, "--flatten=bindings[].members",
                f"--filter=bindings.role={role} AND bindings.members=serviceAccount:{email}",
                "--format=value(bindings.role)"])
    return role in out.splitlines()


def sa_has_member(project, email, role, member):
    out = gout(["iam", "service-accounts", "get-iam-policy", email, "--project", project,
                "--flatten=bindings[].members",
                f"--filter=bindings.role={role} AND bindings.members={member}",
                "--format=value(bindings.role)"])
    return role in out.splitlines()


def sa_project_roles(project, email):
    """All project-level roles currently bound to this SA member (live)."""
    out = gout(["projects", "get-iam-policy", project, "--flatten=bindings[].members",
                f"--filter=bindings.members=serviceAccount:{email}",
                "--format=value(bindings.role)"])
    return [r for r in out.splitlines() if r]


def sa_user_members(project, email):
    """All members holding roles/iam.serviceAccountUser ON this SA (i.e. who may act as it)."""
    out = gout(["iam", "service-accounts", "get-iam-policy", email, "--project", project,
                "--flatten=bindings[].members",
                "--filter=bindings.role=roles/iam.serviceAccountUser",
                "--format=value(bindings.members)"])
    return [m for m in out.splitlines() if m]


PROVISIONER_SA = "infra-provisioner"   # anchor-owned SA (not declared in configs)


def provisioner_kept_roles():
    """The provisioner's allowed roles — single source of truth shared with anchor.sh."""
    path = os.path.join(core.ROOT, "bootstrap", "provisioner-roles.txt")
    roles = set()
    with open(path) as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line:
                roles.add(line if line.startswith("roles/") else f"roles/{line}")
    return roles


# ── subsystem handlers ───────────────────────────────────────────────────────
def ensure_service_accounts(cfg, project):
    print("service_accounts:")
    for sa in cfg.get("service_accounts", []):
        name, email = sa["name"], sa_email(sa["name"], project)
        if gout(["iam", "service-accounts", "describe", email], project):
            c("ok", f"sa {email}")
        else:
            do(["iam", "service-accounts", "create", name,
                f"--display-name={sa.get('description', name)[:100]}"], project, f"create sa {email}")
        for role in sa.get("roles", []):
            role = role if role.startswith("roles/") else f"roles/{role}"
            if sa_has_role(project, email, role):
                c("ok", f"  role {role}")
            else:
                do(["projects", "add-iam-policy-binding", project, f"--member=serviceAccount:{email}",
                    f"--role={role}", "--condition=None"], project, f"  bind {role} → {name}")


def ensure_wif(cfg, project):
    print("wif:")
    wif = cfg.get("wif") or {}
    pool, provider = wif.get("pool"), wif.get("provider")
    org = cfg["project"]["github_org"]
    num = str(cfg["project"].get("gcp_number") or
              gout(["projects", "describe", project, "--format=value(projectNumber)"], project))
    if not pool:
        c("skip", "no wif.pool configured")
        return
    if gout(["iam", "workload-identity-pools", "describe", pool, "--location=global"], project):
        c("ok", f"pool {pool}")
    else:
        do(["iam", "workload-identity-pools", "create", pool, "--location=global",
            f"--display-name={pool}"], project, f"create pool {pool}")
    if provider:
        if gout(["iam", "workload-identity-pools", "providers", "describe", provider,
                 "--location=global", f"--workload-identity-pool={pool}"], project):
            c("ok", f"provider {provider}")
        else:
            do(["iam", "workload-identity-pools", "providers", "create-oidc", provider,
                "--location=global", f"--workload-identity-pool={pool}",
                "--issuer-uri=https://token.actions.githubusercontent.com",
                "--attribute-mapping=google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner",
                f"--attribute-condition=assertion.repository_owner=='{org}'"],
               project, f"create provider {provider} (org-scoped: {org})")
    for sa in cfg.get("service_accounts", []):
        for repo in sa.get("wif_repos") or []:
            email = sa_email(sa["name"], project)
            member = (f"principalSet://iam.googleapis.com/projects/{num}/locations/global/"
                      f"workloadIdentityPools/{pool}/attribute.repository/{repo}")
            if sa_has_member(project, email, "roles/iam.workloadIdentityUser", member):
                c("ok", f"  wif {repo} → {sa['name']}")
            else:
                do(["iam", "service-accounts", "add-iam-policy-binding", email, "--project", project,
                    "--role=roles/iam.workloadIdentityUser", f"--member={member}"],
                   project, f"  bind wif {repo} → {sa['name']}")


def ensure_act_as(cfg, project):
    """act-as (impersonation) bindings between SAs we own: a deployer SA (member) is granted
    roles/iam.serviceAccountUser ON a runtime SA (target) so it may deploy/run-as it. This IS
    access — an impersonation grant — so the access broker owns it. Declared on the deployer as
    `act_as: [<target-sa-name>, ...]`; both SAs live in this project. Runs after
    service_accounts so freshly-created targets already exist."""
    print("act_as:")
    declared = False
    for sa in cfg.get("service_accounts", []):
        targets = sa.get("act_as") or []
        if not targets:
            continue
        declared = True
        member = f"serviceAccount:{sa_email(sa['name'], project)}"
        for tname in targets:
            target = sa_email(tname, project)
            if sa_has_member(project, target, "roles/iam.serviceAccountUser", member):
                c("ok", f"act_as {sa['name']} → {tname}")
            else:
                do(["iam", "service-accounts", "add-iam-policy-binding", target, "--project", project,
                    "--role=roles/iam.serviceAccountUser", f"--member={member}"],
                   project, f"bind act_as {sa['name']} → {tname}")
    if not declared:
        c("skip", "no act_as declared")


HANDLERS = {
    "service_accounts": ensure_service_accounts,
    "act_as": ensure_act_as,
    "wif": ensure_wif,
}


# ── prune (removal reconcile) ────────────────────────────────────────────────
# A subtractive pass: remove project role bindings that config no longer declares.
# Off by default; only runs under --prune (destructive). This is the sanctioned way
# to reconcile removals via the Provision workflow (never a terminal `gcloud`).
# See ADR-001 "prune" open question + DECISIONS 2026-07-01.
def _remove_binding(project, email, role, who):
    undo(["projects", "remove-iam-policy-binding", project,
          f"--member=serviceAccount:{email}", f"--role={role}", "--condition=None"],
         project, f"unbind {role} from {who}")


def prune_service_accounts(cfg, project):
    print("prune service_accounts:")
    for sa in cfg.get("service_accounts", []):
        name, email = sa["name"], sa_email(sa["name"], project)
        want = {(r if r.startswith("roles/") else f"roles/{r}") for r in sa.get("roles", [])}
        live = sa_project_roles(project, email)
        extra = [r for r in live if r not in want]
        if not extra:
            c("ok", f"{name}: no extra roles")
            continue
        for role in extra:
            _remove_binding(project, email, role, name)


def prune_provisioner(cfg, project):
    """Remove any project role on the anchor-owned infra-provisioner SA that is not in
    bootstrap/provisioner-roles.txt (the shared kept-set). Safe: projectIamAdmin is kept,
    so the SA never removes its own ability to finish the prune."""
    print("prune provisioner SA:")
    email = sa_email(PROVISIONER_SA, project)
    keep = provisioner_kept_roles()
    live = sa_project_roles(project, email)
    if not live:
        c("skip", f"{PROVISIONER_SA}: no readable roles (need projectIamAdmin to prune)")
        return
    for role in live:
        if role in keep:
            c("ok", f"keep {role}")
        else:
            _remove_binding(project, email, role, PROVISIONER_SA)


def prune_act_as(cfg, project):
    """Remove serviceAccountUser bindings BETWEEN SAs WE OWN that config no longer declares.
    Conservative: only members that are SAs declared in THIS config are ever touched — Google-
    managed service agents, the default compute SA, and human/user grants are never pruned."""
    print("prune act_as:")
    sas = cfg.get("service_accounts", [])
    owned = {sa_email(sa["name"], project) for sa in sas}
    wanted = set()
    for sa in sas:
        deployer = sa_email(sa["name"], project)
        for t in sa.get("act_as") or []:
            wanted.add((deployer, sa_email(t, project)))
    extra = False
    for sa in sas:
        target = sa_email(sa["name"], project)
        for member in sa_user_members(project, target):
            if not member.startswith("serviceAccount:"):
                continue
            m_email = member.split(":", 1)[1]
            if m_email not in owned or (m_email, target) in wanted:
                continue                              # unmanaged member, or a wanted binding → keep
            extra = True
            undo(["iam", "service-accounts", "remove-iam-policy-binding", target, "--project", project,
                  "--role=roles/iam.serviceAccountUser", f"--member={member}"],
                 project, f"unbind act_as {m_email.split('@')[0]} → {sa['name']}")
    if not extra:
        c("ok", "no extra act_as bindings")


PRUNERS = {
    "service_accounts": prune_service_accounts,
    "act_as": prune_act_as,
    "provisioner": prune_provisioner,
}


# ── provider contract (used by the multi-target CLI loop, ADR-003 §5) ─────────
# Every provider exposes: context(target) → the ctx its handlers take; preflight(ctx)
# → cheap reachability check; label(key, target, ctx) → the plan header string.
def context(target):
    """A gcp target's handler context is just the GCP project id string."""
    return target["project"]["gcp_id"]


def preflight(project):
    """Reachable + authorized? A NOT_FOUND/permission read returns '' (→ False);
    a transient/auth error still fails loud inside gout."""
    return bool(gout(["projects", "describe", project, "--format=value(projectNumber)"], project))


def label(key, target, project):
    return f"project: {key} ({project})"
