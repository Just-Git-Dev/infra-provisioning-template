#!/usr/bin/env bash
#
# fleet-anchor.sh — ADR-003 Phase 4 FLEET trust anchor for Just-Git-Dev/infra-provisioning.
#
# WHAT THIS CHANGES vs. anchor.sh
#   anchor.sh (per-project) mints a SEPARATE `infra-provisioner` SA + WIF pool + provider
#   in EVERY target project. This script mints ONE fleet SA + ONE WIF pool + provider in a
#   single HOST project, then REACHES each target project by either:
#     • REACH=grant  (default, no GCP org needed) — one explicit IAM grant per project, or
#     • REACH=folder / REACH=org — grant the roles once at a folder/organization so every
#       project under it inherits (zero per-project step, incl. future projects).
#   The rule: 1 config-repo = 1 SA = 1 trust boundary. Onboarding a project then drops from
#   a full anchor run to at most one `add-iam-policy-binding` — or nothing under a folder/org.
#   NOTHING about the keyless two-tier WIF idea changes: the repo still impersonates a
#   keyless, least-privilege SA with no stored secrets; there is just ONE such SA now.
#
# WHAT IT ENSURES (idempotent, create-if-missing / verify-if-present):
#   In the HOST project:
#     1. required APIs enabled
#     2. a dedicated fleet WIF pool + OIDC provider, repo-scoped to EXACTLY the config repo
#     3. the fleet `infra-provisioner-fleet` service account
#     4. a workloadIdentityUser binding so ONLY Just-Git-Dev/infra-provisioning may impersonate it
#   Reach (per REACH mode) — the fleet SA is granted the identity/IAM-admin roles from
#     bootstrap/provisioner-roles.txt on each target project (grant) or once on the
#     folder/org (folder/org). Target projects also get the required APIs enabled.
#
# WHO RUNS IT
#   A human with Owner/admin on the HOST project AND on each target project (or on the
#   folder/org for those modes), authenticated as themselves:
#     gcloud auth login <you@example.com>
#
# USAGE
#   HOST_PROJECT=jgd-admin ./fleet-anchor.sh                 # host + reach ALL projects (grant mode)
#   HOST_PROJECT=jgd-admin ./fleet-anchor.sh automahn        # host + reach one project by key
#   HOST_PROJECT=jgd-admin REACH=folder FOLDER_ID=123456 ./fleet-anchor.sh   # host + folder-wide reach
#   HOST_PROJECT=jgd-admin REACH=org    ORG_ID=987654    ./fleet-anchor.sh   # host + org-wide reach
#   HOST_PROJECT=jgd-admin DRY_RUN=1 ./fleet-anchor.sh       # validate only: report gaps, change nothing
#   HOST_PROJECT=jgd-admin ACCOUNT=you@x.com ./fleet-anchor.sh   # force a specific gcloud account
#
# COEXISTENCE / CUTOVER
#   Add-only and idempotent — it does NOT touch the existing per-project anchors, so both
#   paths can coexist during cutover. To actually USE the fleet identity, repoint the
#   Provision workflow's WIF auth (`.github/workflows/provision.yml`) at the fleet
#   provider + SA printed at the end. Retiring the old per-project SAs/pools is a SEPARATE,
#   later step (do it only after the fleet path is verified). See docs/FLEET-ANCHOR.md.
#
set -euo pipefail

# ── Config ──────────────────────────────────────────────────────────────────
PROVISIONER_REPO="${PROVISIONER_REPO:-YourOrg/your-config-repo}"   # the only repo allowed to impersonate the fleet SA

# Fleet WIF names — distinct from anchor.sh's per-project `jgd-provisioner-pool` so the two
# trust anchors never collide during coexistence.
POOL_ID="jgd-fleet-pool"
PROVIDER_ID="jgd-fleet"
GITHUB_ISSUER="https://token.actions.githubusercontent.com"

SA_ID="infra-provisioner-fleet"
SA_DISPLAY="Infra provisioner FLEET — one keyless WIF admin for all projects"

# The HOST project where the fleet SA + WIF pool/provider live. Use a DEDICATED admin/host
# project you own (e.g. create `jgd-admin`), NOT an app project — so the trust anchor is not
# entangled with any workload project. Required.
HOST_PROJECT="${HOST_PROJECT:-}"

# Reach mode: how the fleet SA gets admin on the target projects.
#   grant  (default) — explicit per-project IAM grant (no GCP org required)
#   folder           — grant once on FOLDER_ID (projects under it inherit)
#   org              — grant once on ORG_ID (all projects under it inherit)
REACH="${REACH:-grant}"
FOLDER_ID="${FOLDER_ID:-}"
ORG_ID="${ORG_ID:-}"

# key:gcp_project_id — the target projects. Used for API enablement always, and (in grant
# mode) for the per-project role grants. (Indexed array → runs on macOS bash 3.2 + CI bash 4+.)
PROJECTS=(
  # EDIT: one row per project — "key:gcp_project_id" (key = folder under projects/)
  "example:your-gcp-project"
)
proj_for_key(){ local e; for e in "${PROJECTS[@]}"; do [ "${e%%:*}" = "$1" ] && { printf '%s' "${e#*:}"; return 0; }; done; return 1; }
all_keys(){ local e; for e in "${PROJECTS[@]}"; do printf '%s\n' "${e%%:*}"; done; }

# LEAST-PRIVILEGE — same three identity/IAM-admin roles as the per-project anchor, from the
# SINGLE SOURCE OF TRUTH bootstrap/provisioner-roles.txt (also read by the engine's --prune).
# The fleet SA GRANTS roles to app SAs via projectIamAdmin without HOLDING them; it needs only
# these three on each target. Edit that file — not this script — to change them.
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
do_or_dry(){ if [ "$DRY_RUN" = "1" ]; then printf '  \033[36m~\033[0m would: %s\n' "$*"; else "$@"; fi; }
g(){ gcloud ${GAUTH[@]+"${GAUTH[@]}"} "$@"; }

command -v gcloud >/dev/null || { err "gcloud not on PATH"; exit 1; }
ACTIVE="$(g config get-value account 2>/dev/null || true)"
[ -n "$ACTIVE" ] || { err "no active gcloud account; run 'gcloud auth login'"; exit 1; }
[ -n "$HOST_PROJECT" ] || { err "HOST_PROJECT is required (a dedicated admin/host project you own)"; exit 1; }
case "$REACH" in
  grant) ;;
  folder) [ -n "$FOLDER_ID" ] || { err "REACH=folder requires FOLDER_ID"; exit 1; } ;;
  org)    [ -n "$ORG_ID"    ] || { err "REACH=org requires ORG_ID"; exit 1; } ;;
  *) err "REACH must be one of: grant | folder | org (got '$REACH')"; exit 1 ;;
esac

FLEET_SA="${SA_ID}@${HOST_PROJECT}.iam.gserviceaccount.com"

echo "Active account  : $ACTIVE"
echo "Provisioner repo: $PROVISIONER_REPO"
echo "Host project    : $HOST_PROJECT"
echo "Fleet SA        : $FLEET_SA"
echo "Fleet WIF       : $POOL_ID / $PROVIDER_ID"
echo "Reach mode      : $REACH${FOLDER_ID:+ (folder $FOLDER_ID)}${ORG_ID:+ (org $ORG_ID)}"
[ "$DRY_RUN" = "1" ] && echo "MODE            : DRY-RUN (validate only, no changes)"

# ── idempotent binding checks (per scope) ────────────────────────────────────
project_has_role(){ # proj role member
  g projects get-iam-policy "$1" --flatten='bindings[].members' \
    --filter="bindings.role=$2 AND bindings.members=$3" \
    --format='value(bindings.role)' 2>/dev/null | grep -qx "$2"
}
folder_has_role(){ # folder role member
  g resource-manager folders get-iam-policy "$1" --flatten='bindings[].members' \
    --filter="bindings.role=$2 AND bindings.members=$3" \
    --format='value(bindings.role)' 2>/dev/null | grep -qx "$2"
}
org_has_role(){ # org role member
  g organizations get-iam-policy "$1" --flatten='bindings[].members' \
    --filter="bindings.role=$2 AND bindings.members=$3" \
    --format='value(bindings.role)' 2>/dev/null | grep -qx "$2"
}

ensure_apis(){ # proj
  local proj="$1" enabled api
  enabled="$(g services list --enabled --project="$proj" --format='value(config.name)' 2>/dev/null || true)"
  for api in "${REQUIRED_APIS[@]}"; do
    if grep -qx "$api" <<<"$enabled"; then ok "api $api"
    else do_or_dry g services enable "$api" --project="$proj" && add "api $api"; fi
  done
}

# ── HOST: fleet SA + WIF pool/provider + impersonation binding ───────────────
ensure_host(){
  echo ""
  echo "════════════ HOST  ($HOST_PROJECT) ════════════"
  if ! g projects describe "$HOST_PROJECT" >/dev/null 2>&1; then
    err "cannot access host '$HOST_PROJECT' as $ACTIVE — need Owner/admin."
    return 1
  fi
  local num; num="$(g projects describe "$HOST_PROJECT" --format='value(projectNumber)')"
  ok "host project reachable (number $num)"

  # 1) APIs on the host
  ensure_apis "$HOST_PROJECT"

  # 2) fleet WIF pool
  if g iam workload-identity-pools describe "$POOL_ID" --project="$HOST_PROJECT" --location=global >/dev/null 2>&1; then
    ok "wif pool $POOL_ID"
  else
    do_or_dry g iam workload-identity-pools create "$POOL_ID" --project="$HOST_PROJECT" --location=global \
      --display-name="JGD fleet provisioner"
    add "wif pool $POOL_ID"
  fi

  # 3) fleet OIDC provider — trust GitHub, restrict to EXACTLY the config repo
  if g iam workload-identity-pools providers describe "$PROVIDER_ID" --project="$HOST_PROJECT" --location=global \
       --workload-identity-pool="$POOL_ID" >/dev/null 2>&1; then
    ok "wif provider $PROVIDER_ID"
  else
    do_or_dry g iam workload-identity-pools providers create-oidc "$PROVIDER_ID" \
      --project="$HOST_PROJECT" --location=global --workload-identity-pool="$POOL_ID" \
      --issuer-uri="$GITHUB_ISSUER" \
      --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner" \
      --attribute-condition="assertion.repository=='${PROVISIONER_REPO}'"
    add "wif provider $PROVIDER_ID (repo-scoped: $PROVISIONER_REPO)"
  fi

  # 4) fleet SA
  if g iam service-accounts describe "$FLEET_SA" --project="$HOST_PROJECT" >/dev/null 2>&1; then
    ok "sa $FLEET_SA"
  else
    do_or_dry g iam service-accounts create "$SA_ID" --project="$HOST_PROJECT" --display-name="$SA_DISPLAY"
    add "sa $FLEET_SA"
  fi

  # 5) workloadIdentityUser — only the config repo may impersonate the fleet SA
  local member="principalSet://iam.googleapis.com/projects/${num}/locations/global/workloadIdentityPools/${POOL_ID}/attribute.repository/${PROVISIONER_REPO}"
  if g iam service-accounts get-iam-policy "$FLEET_SA" --project="$HOST_PROJECT" --flatten='bindings[].members' \
       --filter="bindings.role=roles/iam.workloadIdentityUser AND bindings.members=$member" \
       --format='value(bindings.role)' 2>/dev/null | grep -q .; then
    ok "wif impersonation binding"
  else
    do_or_dry g iam service-accounts add-iam-policy-binding "$FLEET_SA" --project="$HOST_PROJECT" \
      --role=roles/iam.workloadIdentityUser --member="$member" >/dev/null
    add "wif impersonation binding"
  fi

  HOST_NUM="$num"
}

# ── REACH: give the fleet SA admin on the targets ────────────────────────────
grant_project(){ # key
  local key="$1" proj role
  proj="$(proj_for_key "$key" || true)"
  [ -n "$proj" ] || { err "unknown project key '$key'"; return 1; }
  echo ""
  echo "──────────── reach: $key  ($proj) ────────────"
  if ! g projects describe "$proj" >/dev/null 2>&1; then
    err "cannot access '$proj' as $ACTIVE — need Owner/admin. Skipping."
    return 1
  fi
  ensure_apis "$proj"
  for role in "${PROVISIONER_ROLES[@]}"; do
    if project_has_role "$proj" "$role" "serviceAccount:$FLEET_SA"; then ok "role $role"
    else do_or_dry g projects add-iam-policy-binding "$proj" \
           --member="serviceAccount:$FLEET_SA" --role="$role" --condition=None >/dev/null && add "role $role"; fi
  done
}

grant_scope(){ # kind id  (folder|org)
  local kind="$1" id="$2" role
  echo ""
  echo "──────────── reach: $kind $id (inherited by all projects under it) ────────────"
  for role in "${PROVISIONER_ROLES[@]}"; do
    if [ "$kind" = folder ]; then
      if folder_has_role "$id" "$role" "serviceAccount:$FLEET_SA"; then ok "role $role"; else
        do_or_dry g resource-manager folders add-iam-policy-binding "$id" \
          --member="serviceAccount:$FLEET_SA" --role="$role" --condition=None >/dev/null && add "role $role"; fi
    else
      if org_has_role "$id" "$role" "serviceAccount:$FLEET_SA"; then ok "role $role"; else
        do_or_dry g organizations add-iam-policy-binding "$id" \
          --member="serviceAccount:$FLEET_SA" --role="$role" --condition=None >/dev/null && add "role $role"; fi
    fi
  done
  echo "  NB: folder/org grants IAM reach only — APIs are still per-project. This run enables"
  echo "      them on the listed target projects below; NEW projects need APIs enabled at creation."
}

# ── run ─────────────────────────────────────────────────────────────────────
rc=0
ensure_host || rc=1

if [ "$REACH" = grant ]; then
  KEYS=()
  if [ "$#" -gt 0 ]; then KEYS=("$@"); else while IFS= read -r _k; do KEYS+=("$_k"); done < <(all_keys); fi
  for k in "${KEYS[@]}"; do grant_project "$k" || rc=1; done
else
  # folder/org: grant reach once at scope, then still enable APIs on the known targets.
  if [ "$REACH" = folder ]; then grant_scope folder "$FOLDER_ID" || rc=1; else grant_scope org "$ORG_ID" || rc=1; fi
  KEYS=()
  if [ "$#" -gt 0 ]; then KEYS=("$@"); else while IFS= read -r _k; do KEYS+=("$_k"); done < <(all_keys); fi
  for k in "${KEYS[@]}"; do
    proj="$(proj_for_key "$k" || true)"; [ -n "$proj" ] || continue
    echo ""; echo "──────────── apis: $k ($proj) ────────────"
    if g projects describe "$proj" >/dev/null 2>&1; then
      ensure_apis "$proj"
    else
      err "cannot access '$proj' — enable APIs manually"; rc=1
    fi
  done
fi

echo ""
echo "  → Provision workflow inputs (repoint .github/workflows/provision.yml auth at these):"
echo "      wif_provider    = projects/${HOST_NUM:-<HOST_NUMBER>}/locations/global/workloadIdentityPools/${POOL_ID}/providers/${PROVIDER_ID}"
echo "      service_account = ${FLEET_SA}"
echo ""
if [ "$rc" = 0 ]; then
  [ "$DRY_RUN" = "1" ] && echo "Dry-run complete — no changes made." || echo "Fleet anchor complete (host: $HOST_PROJECT, reach: $REACH)."
else
  echo "Finished with errors (see ✗ above). Fix access and re-run — it is idempotent."
fi
exit "$rc"
