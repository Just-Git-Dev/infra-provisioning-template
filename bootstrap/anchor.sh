#!/usr/bin/env bash
#
# anchor.sh — one-time (idempotent) trust anchor for Just-Git-Dev/infra-provisioning.
#
# WHY THIS EXISTS
#   A brand-new GCP project starts with only its creator as admin. Before the
#   central infra-provisioning workflow can provision anything hands-off, someone
#   who already has admin must establish the keyless GitHub->GCP (WIF) path. This
#   script IS that step. Run it once per project (re-running is safe), then the
#   workflow takes over — no more terminal.
#
# WHAT IT ENSURES (per project, create-if-missing / verify-if-present):
#   1. required APIs enabled
#   2. a DEDICATED WIF pool + OIDC provider for the provisioning repo
#      (deliberately NOT the app repos' `github-pool`, to avoid collisions)
#   3. an `infra-provisioner` service account
#   4. the identity/IAM admin roles that SA needs to broker ACCESS (create app SAs,
#      bind their roles, manage app-repo WIF) — NOT resource roles (this repo does
#      not create APIs/secrets/pubsub/alerts; app repos own those). See DECISIONS.
#   5. a workloadIdentityUser binding so ONLY Just-Git-Dev/infra-provisioning
#      may impersonate that SA
#
# WHO RUNS IT
#   A human with Owner/admin on the target projects, authenticated as themselves:
#     gcloud auth login <you@example.com>
#
# USAGE
#   ./anchor.sh                 # ensure ALL projects in the table below
#   ./anchor.sh automahn        # just one, by key
#   DRY_RUN=1 ./anchor.sh       # validate only: report gaps, change nothing
#   ACCOUNT=you@x.com ./anchor.sh   # force a specific gcloud account
#
set -euo pipefail

# ── Config ──────────────────────────────────────────────────────────────────
PROVISIONER_REPO="${PROVISIONER_REPO:-YourOrg/your-config-repo}"   # the only repo allowed to impersonate the SA

# Dedicated WIF names — kept distinct from the app repos' `github-pool`/`github-provider`
# so this never conflicts with the existing app-repo federation in the project.
POOL_ID="jgd-provisioner-pool"
PROVIDER_ID="jgd-provisioner"
GITHUB_ISSUER="https://token.actions.githubusercontent.com"

SA_ID="infra-provisioner"
SA_DISPLAY="Infra provisioner — keyless WIF admin, access/identity broker"

# key:gcp_project_id — one row per project. (Indexed array, not associative, so
# this runs on macOS's stock bash 3.2 as well as CI's bash 4+.)
PROJECTS=(
  # EDIT: one row per project — "key:gcp_project_id" (key = folder under projects/)
  "example:your-gcp-project"
)
proj_for_key(){ local e; for e in "${PROJECTS[@]}"; do [ "${e%%:*}" = "$1" ] && { printf '%s' "${e#*:}"; return 0; }; done; return 1; }
all_keys(){ local e; for e in "${PROJECTS[@]}"; do printf '%s\n' "${e%%:*}"; done; }

# LEAST-PRIVILEGE. This repo is the ACCESS/IDENTITY BROKER only — the engine manages
# exactly three access subsystems: `service_accounts` (create SAs + bind their project
# roles), `act_as` (SA→SA serviceAccountUser impersonation between our SAs), and `wif`
# (app-repo WIF pool/provider + impersonation bindings). It does NOT create resources
# (APIs, secrets, pub/sub, alerts) — those are app-owned (scope narrowed 2026-07-01,
# act_as added 2026-07-02, see DECISIONS + ADR-001). So the provisioner needs ONLY the
# identity/IAM admin roles listed in bootstrap/provisioner-roles.txt — no serviceusage/
# pubsub/monitoring/logging/secrets grants. (iam.serviceAccountAdmin already covers
# act_as: it includes get/set-IamPolicy on service accounts.) The provisioner GRANTS
# roles to app SAs via projectIamAdmin without HOLDING them.
#
# SINGLE SOURCE OF TRUTH: bootstrap/provisioner-roles.txt (also read by the engine's
# `--prune`). Edit that file — not this array — to change the provisioner's roles.
_ROLES_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/provisioner-roles.txt"
PROVISIONER_ROLES=()
while IFS= read -r _line; do
  _line="${_line%%#*}"; _line="${_line//[[:space:]]/}"
  [ -n "$_line" ] && PROVISIONER_ROLES+=("$_line")
done < "$_ROLES_FILE"

REQUIRED_APIS=(
  iam.googleapis.com
  iamcredentials.googleapis.com
  sts.googleapis.com
  cloudresourcemanager.googleapis.com
  serviceusage.googleapis.com
)

DRY_RUN="${DRY_RUN:-0}"
ACCOUNT="${ACCOUNT:-}"
GAUTH=(); [ -n "$ACCOUNT" ] && GAUTH=(--account="$ACCOUNT")

# ── helpers ─────────────────────────────────────────────────────────────────
ok(){  printf '  \033[32m✓\033[0m %s\n' "$*"; }
add(){ printf '  \033[33m+\033[0m %s\n' "$*"; }
err(){ printf '  \033[31m✗\033[0m %s\n' "$*" >&2; }
# run a real argv (correct quoting); in dry-run just print it
do_or_dry(){ if [ "$DRY_RUN" = "1" ]; then printf '  \033[36m~\033[0m would: %s\n' "$*"; else "$@"; fi; }
g(){ gcloud ${GAUTH[@]+"${GAUTH[@]}"} "$@"; }

command -v gcloud >/dev/null || { err "gcloud not on PATH"; exit 1; }
ACTIVE="$(g config get-value account 2>/dev/null || true)"
[ -n "$ACTIVE" ] || { err "no active gcloud account; run 'gcloud auth login'"; exit 1; }

echo "Active account : $ACTIVE"
echo "Provisioner repo: $PROVISIONER_REPO"
echo "WIF pool/provider: $POOL_ID / $PROVIDER_ID (dedicated)"
[ "$DRY_RUN" = "1" ] && echo "MODE           : DRY-RUN (validate only, no changes)"


# ── project-scoped custom roles ─────────────────────────────────────────────
# A line in provisioner-roles.txt carrying `{project}` names a PROJECT-SCOPED custom
# role. It must exist before it can be granted, and it is reconciled (not just created)
# so a permission added to CUSTOM_ROLE_PERMS below reaches projects anchored earlier.
#
# Only jgdSecretIamAdmin is defined today: the three permissions the engine needs to
# manage IAM ON a secret (resource_roles.secrets, ADR-005). Deliberately NOT
# roles/secretmanager.admin — that includes secretmanager.versions.access, which would
# let the identity broker read every secret payload in the fleet.
ensure_custom_role(){ # proj role_id
  local proj="$1" id="$2" perms title desc live want
  case "$id" in
    jgdSecretIamAdmin)
      # Exactly what the engine needs to manage IAM ON a secret, and nothing more.
      perms="secretmanager.secrets.get,secretmanager.secrets.getIamPolicy,secretmanager.secrets.setIamPolicy"
      title="JGD Secret IAM Admin"
      desc="Manage WHO may access a secret. Cannot create, delete, or read one (no versions.access). Held by infra-provisioner so resource_roles.secrets can bind."
      ;;
    *)
      err "provisioner-roles.txt names custom role '$id', which this script does not define"
      return 1
      ;;
  esac
  # A deleted-but-not-purged role still describes (GCP keeps it ~7 days); an update on one
  # fails loud, which is the right outcome — undelete is a human decision, not a script's.
  live="$(g iam roles describe "$id" --project="$proj" \
            --format='value[delimiter=","](includedPermissions)' 2>/dev/null || true)"
  want="$(printf '%s' "$perms" | tr ',' '\n' | sort | tr '\n' ' ')"
  if [ -z "$live" ]; then
    do_or_dry g iam roles create "$id" --project="$proj" --title="$title" \
      --description="$desc" --permissions="$perms" --stage=GA >/dev/null && add "custom role $id"
  elif [ "$(printf '%s' "$live" | tr ',' '\n' | sort | tr '\n' ' ')" != "$want" ]; then
    # Reconcile: the role exists but its permissions have drifted from this definition.
    do_or_dry g iam roles update "$id" --project="$proj" --title="$title" \
      --description="$desc" --permissions="$perms" --stage=GA >/dev/null \
      && add "custom role $id (permissions synced)"
  else
    ok "custom role $id"
  fi
}

# has_binding <policy-json-source...> — generic idempotent role check on a project
project_has_role(){ # proj role member
  g projects get-iam-policy "$1" --flatten='bindings[].members' \
    --filter="bindings.role=$2 AND bindings.members=$3" \
    --format='value(bindings.role)' 2>/dev/null | grep -qx "$2"
}

anchor_one(){
  local key="$1"
  local proj; proj="$(proj_for_key "$key" || true)"
  [ -n "$proj" ] || { err "unknown project key '$key'"; return 1; }
  echo ""
  echo "════════════ $key  ($proj) ════════════"

  # 0) reachability / admin
  if ! g projects describe "$proj" >/dev/null 2>&1; then
    err "cannot access '$proj' as $ACTIVE — need Owner/admin. Skipping."
    return 1
  fi
  local num; num="$(g projects describe "$proj" --format='value(projectNumber)')"
  ok "project reachable (number $num)"

  # 1) APIs
  local enabled; enabled="$(g services list --enabled --project="$proj" --format='value(config.name)' 2>/dev/null || true)"
  local api
  for api in "${REQUIRED_APIS[@]}"; do
    if grep -qx "$api" <<<"$enabled"; then ok "api $api"
    else do_or_dry g services enable "$api" --project="$proj" && add "api $api"; fi
  done

  # 2) WIF pool
  if g iam workload-identity-pools describe "$POOL_ID" --project="$proj" --location=global >/dev/null 2>&1; then
    ok "wif pool $POOL_ID"
  else
    do_or_dry g iam workload-identity-pools create "$POOL_ID" --project="$proj" --location=global \
      --display-name="JGD provisioner"
    add "wif pool $POOL_ID"
  fi

  # 3) WIF OIDC provider — trust GitHub, restrict to EXACTLY the provisioning repo
  if g iam workload-identity-pools providers describe "$PROVIDER_ID" --project="$proj" --location=global \
       --workload-identity-pool="$POOL_ID" >/dev/null 2>&1; then
    ok "wif provider $PROVIDER_ID"
  else
    do_or_dry g iam workload-identity-pools providers create-oidc "$PROVIDER_ID" \
      --project="$proj" --location=global --workload-identity-pool="$POOL_ID" \
      --issuer-uri="$GITHUB_ISSUER" \
      --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner" \
      --attribute-condition="assertion.repository=='${PROVISIONER_REPO}'"
    add "wif provider $PROVIDER_ID (repo-scoped: $PROVISIONER_REPO)"
  fi

  # 4) provisioner SA
  local sa="${SA_ID}@${proj}.iam.gserviceaccount.com"
  if g iam service-accounts describe "$sa" --project="$proj" >/dev/null 2>&1; then
    ok "sa $sa"
  else
    do_or_dry g iam service-accounts create "$SA_ID" --project="$proj" --display-name="$SA_DISPLAY"
    add "sa $sa"
  fi

  # 5) admin roles on the provisioner SA (idempotent). A `{project}` line is a
  #    project-scoped CUSTOM role: create/reconcile it first, then grant it.
  local role role_id
  for role in "${PROVISIONER_ROLES[@]}"; do
    if [ "${role#*\{project\}}" != "$role" ]; then
      role_id="${role##*/}"
      ensure_custom_role "$proj" "$role_id" || return 1
      role="${role//\{project\}/$proj}"
    fi
    if project_has_role "$proj" "$role" "serviceAccount:$sa"; then ok "role $role"
    else do_or_dry g projects add-iam-policy-binding "$proj" \
           --member="serviceAccount:$sa" --role="$role" --condition=None >/dev/null && add "role $role"; fi
  done

  # (The custom `jgdSecretsProvisioner` role was removed 2026-07-01 — this repo no
  # longer CREATES secrets; app repos own them. `jgdSecretIamAdmin`, added 2026-08-23,
  # is a different thing: it manages who may ACCESS a secret and can neither create,
  # delete, nor read one. See DECISIONS + ADR-005.)

  # 6) workloadIdentityUser — only the provisioning repo may impersonate the SA
  local member="principalSet://iam.googleapis.com/projects/${num}/locations/global/workloadIdentityPools/${POOL_ID}/attribute.repository/${PROVISIONER_REPO}"
  if g iam service-accounts get-iam-policy "$sa" --project="$proj" --flatten='bindings[].members' \
       --filter="bindings.role=roles/iam.workloadIdentityUser AND bindings.members=$member" \
       --format='value(bindings.role)' 2>/dev/null | grep -q .; then
    ok "wif impersonation binding"
  else
    do_or_dry g iam service-accounts add-iam-policy-binding "$sa" --project="$proj" \
      --role=roles/iam.workloadIdentityUser --member="$member" >/dev/null
    add "wif impersonation binding"
  fi

  echo "  → workflow inputs for this project:"
  echo "      wif_provider    = projects/${num}/locations/global/workloadIdentityPools/${POOL_ID}/providers/${PROVIDER_ID}"
  echo "      service_account = ${sa}"
}

# ── run ─────────────────────────────────────────────────────────────────────
KEYS=()
if [ "$#" -gt 0 ]; then KEYS=("$@"); else while IFS= read -r _k; do KEYS+=("$_k"); done < <(all_keys); fi

rc=0
for k in "${KEYS[@]}"; do anchor_one "$k" || rc=1; done

echo ""
if [ "$rc" = 0 ]; then
  [ "$DRY_RUN" = "1" ] && echo "Dry-run complete — no changes made." || echo "Anchor complete for: ${KEYS[*]}"
else
  echo "Finished with errors (see ✗ above). Fix access and re-run — it is idempotent."
fi
exit "$rc"
