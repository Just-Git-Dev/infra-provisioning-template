#!/usr/bin/env python3
"""GCP provider — the ACCESS/IDENTITY broker for a Just-Git-Dev GCP project.

Manages exactly four access subsystems (idempotent, plan-first):

  - service_accounts : the SAs we own + their PROJECT-scope IAM roles
  - act_as           : SA→SA impersonation (roles/iam.serviceAccountUser) between our SAs
  - resource_roles   : roles bound on a SINGLE resource (a secret, one SA) instead of
                       project-wide — the least-privilege alternative to a project role
  - principals       : a HUMAN/GROUP member granted a role ON one of our SAs (the mirror of
                       resource_roles: who may assume this SA, rather than what it may reach)
  - wif              : the app-repo WIF pool/provider + per-SA impersonation bindings

Everything else a project needs (enabling APIs, creating secrets, pub/sub topics,
Cloud Monitoring alert policies) is a **resource** owned by the app repo itself,
NOT by this provider. `resource_roles` does not weaken that line: it BINDS access on a
resource, and fails loud rather than creating one that is missing. See DECISIONS 2026-07-01 (access-broker scope) and ADR-001.

This module owns the `gcloud` seam (`gcloud` mutations / `gout` reads); the
dry-run gate, fail-loud discipline, and printer live in `engine/core.py` (ADR-003).
Tests stub `gcloud`/`gout` here (no GCP access needed).
"""
import os
import subprocess
import sys

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


# Every resource this engine creates says WHO manages it. Without this a console reader
# cannot tell a config-managed SA from a hand-made one, and edits it by hand — which the
# next apply silently reverts. Audited 2026-08-30: provenance was determinable for zero
# resources in the fleet.
PROVENANCE = "Managed by the infra-provisioning config repo — edit projects/*/config.yaml, not the console."

# GCP limits: displayName 100 BYTES, description 256 BYTES. Both truncate on the byte
# string, dropping any partial trailing multibyte char (a raw [:n] on a description
# containing an em-dash fails create with INVALID_ARGUMENT).
_DISPLAY_NAME_MAX = 100
_DESCRIPTION_MAX = 256


def _clip(s, limit):
    return s.encode("utf-8")[:limit].decode("utf-8", "ignore")


def _described(purpose):
    """A description that carries both purpose and provenance, within the byte limit."""
    return _clip(f"{purpose} | {PROVENANCE}" if purpose else PROVENANCE, _DESCRIPTION_MAX)


PROVISIONER_SA = "infra-provisioner"   # anchor-owned SA (not declared in configs)

# Mirrors SCOPE_READER_ROLE_ID in bootstrap/fleet-anchor.sh; kept in sync by
# tests/test_anchor.py::test_engine_and_script_agree_on_the_scope_reader_role.
SCOPE_READER_ROLE_ID = "jgdScopeIamViewer"


def provisioner_kept_roles(project=None):
    """The provisioner's allowed roles — single source of truth shared with anchor.sh.

    `{project}` in a line marks a PROJECT-SCOPED custom role (e.g.
    `projects/{project}/roles/jgdSecretIamAdmin`) and is substituted with the project id.
    Called without a project the placeholder is left as-is, which is only useful for
    inspection — never for a live comparison."""
    path = os.path.join(core.ROOT, "bootstrap", "provisioner-roles.txt")
    roles = set()
    with open(path) as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line:
                if project:
                    line = line.replace("{project}", project)
                roles.add(_qualify(line))
    return roles


# ── subsystem handlers ───────────────────────────────────────────────────────
def ensure_service_accounts(cfg, project):
    print("service_accounts:")
    for sa in cfg.get("service_accounts", []):
        name, email = sa["name"], sa_email(sa["name"], project)
        purpose = sa.get("description", "")
        want_dn = _clip(purpose or name, _DISPLAY_NAME_MAX)
        want_desc = _described(purpose)
        # TAB-separated, and deliberately NOT a custom delimiter: gcloud silently IGNORES
        # `value[delimiter=<control char>]` and falls back to a tab, so splitting on the
        # requested delimiter never matched, every SA compared as drifted, and the reconcile
        # rewrote the same metadata on every single apply. displayName cannot contain a tab;
        # partitioning on the FIRST one keeps any tab inside a description with the description.
        live = gout(["iam", "service-accounts", "describe", email,
                     "--format=value(displayName,description)"], project)
        if live:
            # RECONCILE, don't just create. Metadata used to be written at create only, so a
            # config `description:` edit never reached GCP and displayNames silently went
            # stale (six SAs were found still carrying their bare id — audit 2026-08-30).
            have_dn, _, have_desc = live.partition("\t")
            # strip BOTH sides: a clip landing on a space leaves a trailing space that GCP
            # trims on write, which would also make this compare unequal forever.
            if have_dn.strip() != want_dn.strip() or have_desc.strip() != want_desc.strip():
                do(["iam", "service-accounts", "update", email,
                    f"--display-name={want_dn}", f"--description={want_desc}"],
                   project, f"  sync metadata on {email}")
            else:
                c("ok", f"sa {email}")
        else:
            do(["iam", "service-accounts", "create", name,
                f"--display-name={want_dn}", f"--description={want_desc}"],
               project, f"create sa {email}")
        for role in sa.get("roles", []):
            role = _qualify(role)
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
            f"--display-name={_clip(pool, _DISPLAY_NAME_MAX)}",
            f"--description={_described('App-repo GitHub Actions WIF pool')}"],
           project, f"create pool {pool}")
    if provider:
        if gout(["iam", "workload-identity-pools", "providers", "describe", provider,
                 "--location=global", f"--workload-identity-pool={pool}"], project):
            c("ok", f"provider {provider}")
        else:
            do(["iam", "workload-identity-pools", "providers", "create-oidc", provider,
                "--location=global", f"--workload-identity-pool={pool}",
                "--issuer-uri=https://token.actions.githubusercontent.com",
                "--attribute-mapping=google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner",
                f"--attribute-condition=assertion.repository_owner=='{org}'",
                f"--display-name={_clip(provider, _DISPLAY_NAME_MAX)}",
                f"--description={_described(f'OIDC provider for GitHub org {org}')}"],
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


# ── resource-scoped IAM (resource_roles) ─────────────────────────────────────
# Roles bound ON A SINGLE RESOURCE rather than project-wide. This is still ACCESS, so it
# belongs to the broker — and `act_as` was already exactly this shape (serviceAccountUser
# ON one SA), so `resource_roles` generalises an existing pattern rather than adding a new
# concept. The ownership line holds: we BIND on resources, we never CREATE them (an absent
# target fails loud — see ensure_resource_roles).
#
# Why it exists: without it the provider could only bind at project scope, so real grants
# on individual secrets and SAs were invisible to the plan AND unprunable. "Zero drift"
# therefore asserted nothing about them, which is exactly how full-project roles get handed
# out when a single-resource grant was what was needed. See ADR-005 / DECISIONS 2026-08-23.
#
# Declared on the SA that RECEIVES the access:
#   resource_roles:
#     service_accounts:      { api-run: [iam.serviceAccountAdmin] }
#     secrets:               { app-secrets: [secretmanager.admin] }
#     pubsub_topics:         { booking.cancelled: [pubsub.publisher] }
#     pubsub_subscriptions:  { poke-sub: [pubsub.subscriber] }
#     artifact_repositories: { asia-southeast1/backend: [artifactregistry.writer] }
_RESOURCE_KINDS = {
    "secrets": {
        "noun": "secret",
        "ref": lambda name, project: name,
        "args": lambda verb, ref, project: ["secrets", verb, ref, "--project", project],
    },
    "service_accounts": {
        "noun": "service account",
        "ref": lambda name, project: sa_email(name, project),
        "args": lambda verb, ref, project: ["iam", "service-accounts", verb, ref, "--project", project],
    },
    "pubsub_topics": {
        "noun": "topic",
        "ref": lambda name, project: name,
        "args": lambda verb, ref, project: ["pubsub", "topics", verb, ref, "--project", project],
    },
    "pubsub_subscriptions": {
        "noun": "subscription",
        "ref": lambda name, project: name,
        "args": lambda verb, ref, project: ["pubsub", "subscriptions", verb, ref, "--project", project],
    },
    # GAR is the one kind whose resource is not addressable by bare name — `describe backend`
    # fails argument parsing on a missing `location` attribute before any API call. The config
    # name therefore carries one (`<location>/<repo>`) and we build the fully-qualified path,
    # which already encodes the project, so `--project` is not passed alongside it.
    "artifact_repositories": {
        "noun": "repository",
        "ref": lambda name, project: _gar_ref(name, project),
        "args": lambda verb, ref, project: ["artifacts", "repositories", verb, ref],
    },
}


def _gar_ref(name, project):
    location, _, repo = name.partition("/")
    if not repo:
        raise SystemExit(
            f"resource_roles: artifact repository {name!r} has no location. GAR repositories "
            f"are per-location and a bare name is ambiguous, so declare it as "
            f"'<location>/{name}' (e.g. 'asia-southeast1/{name}').")
    return f"projects/{project}/locations/{location}/repositories/{repo}"


# Bindings on these (kind, role) pairs belong to ANOTHER subsystem, so prune_resource_roles
# must not touch them even when resource_roles names the same resource. Without this the
# pruner tears down every act_as impersonation grant on any SA that also appears under
# resource_roles — caught by a dry-run against a live project, see DECISIONS 2026-08-23.
# `deleted:` members are exempt from the exemption: no subsystem wants a dangling principal.
_FOREIGN_ROLES = {
    "service_accounts": {"roles/iam.serviceAccountUser",        # owned by act_as
                         "roles/iam.workloadIdentityUser"},     # owned by wif
    "secrets": set(),
    # No other subsystem binds on these, so nothing here is exempt from pruning.
    "pubsub_topics": set(),
    "pubsub_subscriptions": set(),
    "artifact_repositories": set(),
}


def _qualify(role):
    """`run.admin` -> `roles/run.admin`. A fully-qualified role — predefined (`roles/x`) or a
    custom one (`projects/<p>/roles/x`, `organizations/<o>/roles/x`) — is left alone; prefixing
    a custom role would silently produce `roles/projects/...`, which gcloud rejects only at
    apply time."""
    return role if role.startswith("roles/") or "/roles/" in role else f"roles/{role}"


def _declared_resource_roles(cfg, project):
    """[(sa_name, kind, resource_name, ref, [qualified roles]), ...] in declaration order."""
    out = []
    for sa in cfg.get("service_accounts", []):
        for kind, resources in (sa.get("resource_roles") or {}).items():
            if kind not in _RESOURCE_KINDS:
                raise SystemExit(f"unknown resource_roles kind {kind!r} on {sa['name']}; "
                                 f"expected one of {sorted(_RESOURCE_KINDS)}")
            spec = _RESOURCE_KINDS[kind]
            for name, roles in (resources or {}).items():
                out.append((sa["name"], kind, name, spec["ref"](name, project),
                            [_qualify(r) for r in (roles or [])]))
    return out


def resource_policy(kind, ref, project):
    """Live (role, member) pairs on one resource. Real gcloud emits
    `roles/x,serviceAccount:y` per line under this flatten+csv pair."""
    args = _RESOURCE_KINDS[kind]["args"]("get-iam-policy", ref, project) + [
        "--flatten=bindings[].members",
        "--format=csv[no-heading](bindings.role,bindings.members)"]
    pairs = []
    for line in gout(args).splitlines():
        if line.strip(" ,"):   # an unbound resource prints a bare `,` (seen on GAR repos)
            role, member = line.split(",", 1)
            pairs.append((role.strip(), member.strip()))
    return pairs


def ensure_resource_roles(cfg, project):
    print("resource_roles:")
    declared = _declared_resource_roles(cfg, project)
    if not declared:
        c("skip", "no resource_roles declared")
        return
    for sa_name, kind, name, ref, roles in declared:
        spec = _RESOURCE_KINDS[kind]
        member = f"serviceAccount:{sa_email(sa_name, project)}"
        if not gout(spec["args"]("describe", ref, project)):
            # Never auto-create: the resource is app-owned (ADR-001). Silently skipping
            # would make a clean plan a lie about access that does not exist.
            raise SystemExit(
                f"resource_roles: {spec['noun']} {name!r} does not exist in {project}. "
                f"It is app-owned — create it in the app repo's own bootstrap/ops workflow, "
                f"then re-run. This provider binds on resources, it never creates them.")
        held = {r for r, m in resource_policy(kind, ref, project) if m == member}
        for role in roles:
            if role in held:
                c("ok", f"  {spec['noun']} {name}: {role}")
            else:
                do(spec["args"]("add-iam-policy-binding", ref, project) +
                   [f"--member={member}", f"--role={role}"],
                   project, f"  bind {role} on {spec['noun']} {name} → {sa_name}")


def prune_resource_roles(cfg, project):
    """Remove undeclared bindings from the resources config NAMES — and only those resources,
    so blast radius is bounded by the config rather than by everything in the project.

    Conservative, same rule as prune_act_as: a member is only ever touched if it is a service
    account THIS config declares. The one addition is `deleted:` principals, which reference an
    identity that no longer exists and so can never be a legitimate grant (they are the residue
    an SA rename leaves behind). Humans, Google-managed service agents and SAs from other
    projects are never touched.

    Bindings another subsystem owns (`_FOREIGN_ROLES`) are also left alone: an SA named under
    resource_roles is very often an act_as target too, and its serviceAccountUser grants are
    prune_act_as's business, not this pruner's."""
    print("prune resource_roles:")
    declared = _declared_resource_roles(cfg, project)
    if not declared:
        c("skip", "no resource_roles declared")
        return
    owned = {sa_email(sa["name"], project) for sa in cfg.get("service_accounts", [])}
    wanted = {(kind, name, role, f"serviceAccount:{sa_email(sa_name, project)}")
              for sa_name, kind, name, _ref, roles in declared for role in roles}
    seen, extra = set(), False
    for _sa_name, kind, name, ref, _roles in declared:
        if (kind, name) in seen:          # a resource may be named by several SAs
            continue
        seen.add((kind, name))
        spec = _RESOURCE_KINDS[kind]
        for role, member in resource_policy(kind, ref, project):
            deleted = member.startswith("deleted:")
            email = member.split(":")[-1].split("?")[0]
            if not deleted:
                if role in _FOREIGN_ROLES[kind]:
                    continue                                   # another subsystem owns it
                if not member.startswith("serviceAccount:") or email not in owned:
                    continue                                   # unmanaged member → keep
                if (kind, name, role, member) in wanted:
                    continue                                   # declared → keep
            extra = True
            who = email.split("@")[0] + (" (deleted)" if deleted else "")
            undo(spec["args"]("remove-iam-policy-binding", ref, project) +
                 [f"--member={member}", f"--role={role}"],
                 project, f"unbind {role} on {spec['noun']} {name} from {who}")
    if not extra:
        c("ok", "no extra resource_roles bindings")


# ── principals: a HUMAN/GROUP member on one of our service accounts (ADR-008) ─
# resource_roles asks "what may this SA reach"; principals asks the mirror question, "who may
# assume this SA". It is a SEPARATE top-level block rather than a member type inside
# resource_roles because resource_roles is nested UNDER the SA that receives the access, and a
# human has no owning SA to nest under — and because prune_resource_roles' safety rule ("only
# ever touch a member that is an SA THIS config declares") is what makes it safe to run against
# a live project. Admitting humans there would have to weaken exactly that rule.
#
#   principals:                                    # TOP-LEVEL, not under a service account
#     - member: user:someone@example.com           # or group:log-readers@example.com
#       resource_roles:
#         service_accounts:
#           log-reader: [iam.serviceAccountTokenCreator]
#
# NOTHING IN IAM ENFORCES THE NARROWNESS BELOW. modifiedGrantsByRole — the condition behind
# grantable-roles.txt — covers project/folder/org allow policies only; a service-account
# resource policy has no equivalent attribute, so the provisioner's iam.serviceAccountAdmin is
# unconditioned here. The allow-list file, these checks and the reviewer are the only controls.
_PRINCIPAL_KIND = "service_accounts"      # deliberately the ONLY kind open to a human
_PRINCIPAL_PREFIXES = ("user:", "group:")


def principal_grantable_roles():
    """Roles a human/group principal may be granted on an SA — read from the shared file, the
    same way provisioner_kept_roles() reads its own. Resolved against core.ROOT (this engine's
    checkout), which at run time is the action path, NOT the caller's config root."""
    path = os.path.join(core.ROOT, "bootstrap", "principal-grantable-roles.txt")
    roles = set()
    with open(path) as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line:
                roles.add(_qualify(line))
    return roles


def _declared_principals(cfg, project):
    """[(member, kind, resource_name, ref, [qualified roles]), ...] in declaration order.

    Validates as it goes and exits loud on anything it cannot safely bind — at PLAN time, so a
    bad config never reaches a live setIamPolicy."""
    allowed = principal_grantable_roles()
    out = []
    for entry in cfg.get("principals") or []:
        member = (entry or {}).get("member")
        if not member:
            raise SystemExit("principals: an entry has no `member`")
        if member.startswith("serviceAccount:"):
            raise SystemExit(
                f"principals: {member!r} is a service account. Service-account grants are "
                f"declared under that SA's own `resource_roles:`, which the pruner can reason "
                f"about; `principals:` is for human and group members only.")
        if not member.startswith(_PRINCIPAL_PREFIXES):
            raise SystemExit(
                f"principals: member {member!r} has no principal prefix. Write "
                f"'user:{member}' or 'group:{member}' — a bare string is passed to gcloud "
                f"verbatim and would bind something unintended.")
        for kind, resources in (entry.get("resource_roles") or {}).items():
            if kind != _PRINCIPAL_KIND:
                raise SystemExit(
                    f"principals: {member} declares kind {kind!r}. Human principals may only "
                    f"be bound on '{_PRINCIPAL_KIND}' — opening another kind is a decision "
                    f"(ADR-008), not a config edit.")
            spec = _RESOURCE_KINDS[kind]
            for name, roles in (resources or {}).items():
                qualified = [_qualify(r) for r in (roles or [])]
                for role in qualified:
                    if role not in allowed:
                        raise SystemExit(
                            f"principals: {member} would be granted {role} on {name}, which is "
                            f"not in bootstrap/principal-grantable-roles.txt. IAM will NOT "
                            f"refuse this binding — that file is the control, so widening it "
                            f"is a reviewed change (ADR-008 §4).")
                out.append((member, kind, name, spec["ref"](name, project), qualified))
    return out


def ensure_principals(cfg, project):
    print("principals:")
    declared = _declared_principals(cfg, project)
    if not declared:
        c("skip", "no principals declared")
        return
    for member, kind, name, ref, roles in declared:
        spec = _RESOURCE_KINDS[kind]
        if not gout(spec["args"]("describe", ref, project)):
            # Same fail-loud rule as ensure_resource_roles: we BIND, never create. A silent
            # skip would make a clean plan a lie about access that does not exist.
            raise SystemExit(
                f"principals: {spec['noun']} {name!r} does not exist in {project}. Declare it "
                f"under `service_accounts:` and apply that first — this block binds on an SA, "
                f"it never creates one.")
        held = {r for r, m in resource_policy(kind, ref, project) if m == member}
        for role in roles:
            if role in held:
                c("ok", f"  {spec['noun']} {name}: {role} → {member}")
            else:
                do(spec["args"]("add-iam-policy-binding", ref, project) +
                   [f"--member={member}", f"--role={role}"],
                   project, f"  bind {role} on {spec['noun']} {name} → {member}")


def prune_principals(cfg, project):
    """Remove human/group bindings this config no longer declares — and ONLY on the members
    and resources it names.

    Conservative in the same shape as prune_act_as and prune_resource_roles: a binding is
    touched only when the MEMBER, the RESOURCE and the role are all config-declared. Another
    person's grant on the same SA survives, and so does every `serviceAccount:` member —
    those belong to prune_act_as / prune_resource_roles, and double-owning a binding is how a
    pruner tears down a grant a different subsystem legitimately declares.

    No `_FOREIGN_ROLES` exemption is needed here: act_as and wif bind service accounts only,
    so a serviceAccountUser/workloadIdentityUser binding held by a HUMAN this config declares
    is owned by no other subsystem and is genuinely undeclared."""
    print("prune principals:")
    declared = _declared_principals(cfg, project)
    if not declared:
        c("skip", "no principals declared")
        return
    members = {m for m, _k, _n, _r, _roles in declared}
    wanted = {(kind, name, role, member)
              for member, kind, name, _ref, roles in declared for role in roles}
    seen, extra = set(), False
    for _member, kind, name, ref, _roles in declared:
        if (kind, name) in seen:          # a resource may be named by several principals
            continue
        seen.add((kind, name))
        spec = _RESOURCE_KINDS[kind]
        for role, member in resource_policy(kind, ref, project):
            if member not in members:                          # unmanaged member → keep
                continue
            if (kind, name, role, member) in wanted:           # declared → keep
                continue
            extra = True
            undo(spec["args"]("remove-iam-policy-binding", ref, project) +
                 [f"--member={member}", f"--role={role}"],
                 project, f"unbind {role} on {spec['noun']} {name} from {member}")
    if not extra:
        c("ok", "no extra principals bindings")


HANDLERS = {
    "service_accounts": ensure_service_accounts,
    "act_as": ensure_act_as,
    "resource_roles": ensure_resource_roles,
    # AFTER service_accounts — dict order is run order (provision.run), and this binds ON an
    # SA, so the SA has to exist first on a fresh project.
    "principals": ensure_principals,
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
        want = {_qualify(r) for r in sa.get("roles", [])}
        live = sa_project_roles(project, email)
        extra = [r for r in live if r not in want]
        if not extra:
            c("ok", f"{name}: no extra roles")
            continue
        for role in extra:
            _remove_binding(project, email, role, name)


def provisioner_identity(cfg, project):
    """(email, scope) for whichever SA brokers access to this project.

    Default — no `provisioner:` block — is the per-project anchor SA, whose roles are bound
    ON the project, which is what `sa_project_roles` reads. A FLEET anchor instead puts one
    SA in a host project and grants it at an org/folder node; those bindings are not on the
    project and are invisible to a project-level read. `scope` (e.g. "organizations/123")
    says where to go looking, and is None for the per-project shape.
    """
    p = cfg.get("provisioner") or {}
    return (p.get("service_account") or sa_email(PROVISIONER_SA, project),
            p.get("scope") or None)


def _scope_roles(scope, email):
    """Roles bound to `email` at an org/folder node. Fails loud if it cannot read: a
    permission gap must never be indistinguishable from 'no extra roles'."""
    kind, _, ident = scope.partition("/")
    member = f"serviceAccount:{email}"
    common = ["get-iam-policy", ident, "--flatten=bindings[].members",
              f"--filter=bindings.members={member}", "--format=value(bindings.role)"]
    if kind == "organizations":
        args = ["organizations"] + common
    elif kind == "folders":
        args = ["resource-manager", "folders"] + common
    else:
        sys.exit(f"provisioner.scope must be organizations/<id> or folders/<id>, got {scope!r}")
    return [r for r in gout(args).splitlines() if r]


def _audit_scope_grants(email, scope, keep):
    """DETECT drift on a scope-scoped provisioner. Deliberately never removes anything.

    The identity holding these grants must not be able to rewrite them — that is why it
    holds a read-only custom role here and not organizationAdmin. An identity that can
    silently 'fix' its own privilege drift is not being policed. So this reports and fails;
    a human decides.
    """
    print(f"audit provisioner grants at {scope}:")
    allowed = set(keep) | {f"{scope}/roles/{SCOPE_READER_ROLE_ID}"}
    live = _scope_roles(scope, email)
    if not live:
        # Not 'clean': the SA is supposed to hold its predefined roles here. Empty means
        # the grants moved, or this is the wrong scope — either way, say so.
        c("err", f"{email} holds NO roles at {scope} — wrong scope, or the fleet grant is gone")
        return [f"no roles bound to {email} at {scope}"]
    extra = [r for r in live if r not in allowed]
    for role in live:
        if role in allowed:
            c("ok", f"keep {role}")
    for role in extra:
        c("err", f"UNDECLARED at {scope}: {role}")
    if extra:
        c("err", "not removed by design — a scope grant is changed by a human, "
                 "not by the identity that benefits from it")
    return [f"undeclared role {r}" for r in extra]


def prune_provisioner(cfg, project):
    """Remove any PROJECT role on the provisioner SA that is not in
    bootstrap/provisioner-roles.txt (the shared kept-set). Safe: projectIamAdmin is kept,
    so the SA never removes its own ability to finish the prune.

    Under a fleet anchor the SA's predefined roles live at an org/folder node instead. Those
    are audited, not pruned — see `_audit_scope_grants`. Without that audit this function
    would read a project policy that legitimately contains almost nothing and report a clean
    run while policing nothing at all.
    """
    print("prune provisioner SA:")
    email, scope = provisioner_identity(cfg, project)
    keep = provisioner_kept_roles(project)
    live = sa_project_roles(project, email)
    if live:
        for role in live:
            if role in keep:
                c("ok", f"keep {role}")
            else:
                _remove_binding(project, email, role, email.split("@")[0])
    elif not scope:
        c("skip", f"{email}: no readable roles (need projectIamAdmin to prune)")
    else:
        c("ok", f"{email}: no project-level roles (expected — this is a fleet provisioner)")
    if scope:
        problems = _audit_scope_grants(email, scope, keep)
        if problems:
            sys.exit(f"provisioner scope audit FAILED at {scope}: " + "; ".join(problems))


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
    "resource_roles": prune_resource_roles,
    "principals": prune_principals,
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
