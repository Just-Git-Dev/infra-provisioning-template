# infra-provisioning-template

A **topology-free, config-driven provisioning engine** for GCP access/identity and GKE
tenancy — consumable **two ways**:

1. **As a composite action** — `uses: Just-Git-Dev/infra-provisioning-template@v1`. Your
   private repo holds only `projects/<key>/config.yaml`; this repo holds the shared engine.
2. **As a GitHub template** — click **“Use this template”** to scaffold your own config repo
   (engine + example config + bootstrap + workflow), then customize.

The engine carries **zero topology** — no project ids, no service accounts, no secrets. You
supply those as config in your own repo. Dry-run is the default; nothing changes without
`apply: true`.

## What it provisions

Two providers, selected per-target via a `kind` discriminator in `config.yaml`:

| Provider | Owns (access/identity + tenancy — NOT workloads) |
|---|---|
| **gcp** | service accounts + their project IAM roles (`service_accounts`), SA→SA impersonation (`act_as`, and `deployed_by: [<d>-deployer]` declared on the runtime SA; `service_agents: [pubsub]` declares a Google service agent's fixed role on a `<d>-run` SA so `sa_audit` accepts it, declare-only), **roles scoped to a single resource** (`resource_roles`), app-repo Workload-Identity Federation (`wif`) |
| **kubernetes** | Namespace, ResourceQuota, NetworkPolicy, RBAC, KSA + Workload-Identity (`namespace`/`quota`/`network_policy`/`rbac`/`ksa`); and standing exposure infra — Service, Certificate, Ingress, HPA, PDB |

**Deliberately not owned:** the Deployment/workload + image (your CD owns those) and
ConfigMap/Secret. Every object the engine owns is one it can drift-detect honestly — it
selects the workload by label or by name from outside, so it never fights your deploys.

## Quick start (template mode)

1. **Use this template** → create your (private) config repo.
2. Copy `projects/EXAMPLE/` → `projects/<your-key>/` and edit `config.yaml`
   ([schema below](#config-schema)).
3. **Bootstrap trust once** (a human with Owner, keyless WIF — see [`bootstrap/`](bootstrap/)):
   ```bash
   PROVISIONER_REPO=YourOrg/your-config-repo ./bootstrap/anchor.sh          # per-project anchor
   # or the fleet model (one SA reaches all projects):
   HOST_PROJECT=your-admin-project ./bootstrap/fleet-anchor.sh
   ```
4. **Dry-run, then apply** via the Provision workflow (`.github/workflows/provision.yml`):
   dispatch it with your project key; leave `apply` unticked to plan. **Zero `~ would` lines
   = zero drift.** Tick `apply` to converge.

## Quick start (action mode)

Keep your configs in your own repo and call the engine:

```yaml
# .github/workflows/provision.yml (in YOUR repo)
permissions: { contents: read, id-token: write }
jobs:
  provision:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - uses: google-github-actions/auth@v3          # keyless WIF; pin to a SHA for production
        with: { workload_identity_provider: <...>, service_account: <...> }
      - uses: google-github-actions/setup-gcloud@v3
      # for kubernetes targets: gcloud container clusters get-credentials <cluster> ...
      - uses: Just-Git-Dev/infra-provisioning-template@v1
        with:
          project: <your-key>
          apply: ${{ inputs.apply }}      # default false = dry-run
```

The action runs the engine against **your** checked-out `projects/<key>/config.yaml`
(`config-root` defaults to the caller workspace). **You authenticate first** (GCP, and
kubectl for k8s targets); the action just runs the reconcile.

### Action inputs

| Input | Default | Meaning |
|---|---|---|
| `project` | — (required) | folder under `projects/` |
| `apply` | `false` | apply changes (default: dry-run/plan) |
| `prune` | `false` | subtractive reconcile — remove bindings/objects config no longer declares |
| `only` | `''` | run a single subsystem/pruner (prune mode adds `wif`, see below) |
| `retire` | `false` | the ONE deleting mode: delete service accounts marked `retire: true` (see below). Mutually exclusive with `prune`; `only` is invalid with it |
| `config-root` | `${{ github.workspace }}` | root holding `projects/<key>/config.yaml` |

## Config schema

See the fully-worked [`projects/EXAMPLE/config.yaml`](projects/EXAMPLE/config.yaml). A bare
config (no `targets:` key) auto-wraps to a single `kind: gcp` target, so GCP-only users can
skip the wrapper. Explicit `targets:` mix `gcp` + `kubernetes`, applied **gcp-first** so a
KSA's Workload-Identity reference to a GCP SA resolves.

### `capability:<name>` — one friendly name for a SET of roles

A capability is an alias for the roles some job actually needs. `capability:observability-read`
is a reviewable statement of intent; `[logging.viewer, monitoring.viewer]` repeated across
projects is a list nobody re-reads.

```yaml
service_accounts:
  - name: log-reader
    roles:
      - capability:observability-read       # expands to every role the capability names
      - run.viewer                          # plain roles are unchanged
```

The map lives **with the configs**, at `<config-root>/bootstrap/capabilities.yaml`:

```yaml
observability-read: [logging.viewer, monitoring.viewer]
```

Three things about that are deliberate:

- **It is read from the config root, never from this repo.** The engine is consumed as a
  SHA-pinned composite action, so a map living here would need an engine release plus a SHA
  bump for every new capability — the whole cost this mechanism exists to avoid. Definition
  and use then land in the same PR, reviewed together.
- **Expansion happens before the binding loop**, so a capability of N roles still emits N
  one-role bindings. That is load-bearing, not cosmetic: the fleet provisioner's grant is
  fenced by `modifiedGrantsByRole ... hasOnly([...])`, capped at 10 roles and chunked across
  several conditional bindings, and a call touching two chunks satisfies neither.
- **An unknown capability fails at PLAN time**, listing the ones that exist. An unknown *role*
  is not caught here — it fails later, in `gcloud` — but a friendly name that behaved the same
  way would add a new silent-failure mode to a mechanism whose point is legible grants.

The pruner expands capabilities too. It has to: comparing live roles against the *unexpanded*
list would make every capability-granted role look undeclared, and the next `--prune` would
unbind exactly what the config just asked for.

### `resource_roles` — bind a role on ONE resource, not the whole project

`roles:` grants at **project** scope. When an SA needs a permission on exactly one secret or
one other service account, a project role is far more than it needs — and until this existed,
the resource-scoped grant people reached for instead was invisible to the engine: not in the
plan, not prunable, and **not covered by "zero drift"**.

```yaml
service_accounts:
  - name: ci-rotator
    roles: [run.admin]                      # project-scope
    resource_roles:                         # resource-scope
      service_accounts:      { app-run: [iam.serviceAccountAdmin] }
      secrets:               { app-env: [secretmanager.admin] }
      pubsub_topics:         { orders.created: [pubsub.publisher] }
      pubsub_subscriptions:  { orders-worker-sub: [pubsub.subscriber] }
      artifact_repositories: { asia-southeast1/backend: [artifactregistry.writer] }
```

Supported kinds: `service_accounts`, `secrets`, `pubsub_topics`, `pubsub_subscriptions` and
`artifact_repositories`. An Artifact Registry repository is per-location and a bare name is
ambiguous to `gcloud`, so its key carries one: **`<location>/<repo>`** — a name without a
location fails loud at plan time rather than binding somewhere unintended.
The engine **binds** on these resources and
never creates them — a resource that does not exist fails loud rather than being skipped,
because a silent skip would make a clean plan a lie. `--prune` removes undeclared bindings
**only from the resources your config names**, and only for members that are SAs the config
declares, plus `deleted:` principals (residue an SA rename leaves behind — never a real grant).
Humans, Google-managed service agents and SAs from other projects are never touched.

### `principals` — grant a HUMAN or GROUP a role on one service account

`resource_roles` answers "which resources may this **service account** reach". `principals`
answers the mirror question: "which **human** may assume this service account". The two are
separate blocks on purpose — `resource_roles` is nested *under* the SA that receives the
access, and a human principal has no owning SA to nest under (see
`docs/adr/ADR-008` in the consumer repo for the full reasoning).

```yaml
principals:                                     # TOP-LEVEL, not under a service account
  - member: user:someone@example.com            # or group:log-readers@example.com
    resource_roles:
      service_accounts:
        log-reader: [iam.serviceAccountTokenCreator]
```

Deliberately narrow, and it fails loud rather than widening quietly:

- **Kind `service_accounts` only.** Secrets, pub/sub and GAR are not open to human principals.
- **The role must appear in `bootstrap/principal-grantable-roles.txt`** — an allow-list seeded
  with exactly `roles/iam.serviceAccountTokenCreator`. Anything else exits non-zero at plan time.
- **The member must carry a `user:` or `group:` prefix.** A bare string would be handed to
  `gcloud` and could bind something unintended; a `serviceAccount:` member belongs in
  `resource_roles`, not here, and is rejected with that message.
- **The target SA must already exist** — same fail-loud rule as `resource_roles`.

`--prune principals` removes a binding only when the member, the resource **and** the role are
all ones this config declares. A human binding the config does not mention — anyone else's
grant on the same SA — is left alone.

### `retire: true` — mark a service account for deletion

```yaml
service_accounts:
  - name: old-bot
    retire: true
    reason: replaced by new-bot   # optional; `description` is also allowed
```

A retired entry may carry only `name`, `retire`, and an optional `description`/`reason`.
Validation fails the run if it also has `roles`, `act_as`, `wif_repos`, `wif_subjects`,
`deployed_by`, `service_agents`, `resource_roles`, or if any other entry names it in `act_as`
or `deployed_by` or a `resource_roles.service_accounts` / `principals` target.

- Add mode never creates it and never binds to it.
- `--prune` strips every project role it holds (`want = {}`).
- `--retire` (action input `retire: true`) is the only mode that deletes. It runs ONLY the
  retire pass: no add handlers, no pruners. It deletes an SA only if the SA is marked
  `retire: true`, is live, and holds zero project roles, and it checks every marked SA first and
  deletes nothing if any one still holds a role. Dry-run is the default: the deletion is counted
  and printed `~ would delete`, never performed. An already-absent SA reports
  `deleted, drop the entry`; drop the entry in a follow-up change.
- `sa_audit` reports an **orphan**: a live `<name>@<project>.iam.gserviceaccount.com` that config
  neither declares, nor marks `retire: true`, nor names in a `deployed_by` list. Google-managed
  agents, the default compute SA, `firebase-adminsdk-*` and the anchor SA are excluded. It warns
  on every run and fails under `--prune`.
- **Stray roles.** A project role an SA holds that config does not declare AND that is outside
  `<config-root>/bootstrap/grantable-roles.txt` cannot be removed by the provisioner (IAM denies
  the revoke: GCP cannot express "revoke but never grant"). `--prune` reports it as
  `stray`, does not count it as a removal, and exits non-zero at the end; `sa_audit` reports it
  too. A human removes it. The allow-list is read from the CONFIG root, not the engine checkout.
  If that file is missing, `--prune` and the `--prune` audit fail; the plain add-mode audit only
  prints a `::warning::` that the stray-role check was SKIPPED.
- `--retire` is all-or-nothing across every target of a config: all targets are checked first,
  and nothing is deleted if any marked SA in any target fails the check. A failed or EMPTY live
  service-account list fails the audit; it never reads as "0 orphans".

### `--prune --only wif` — remove WIF members config no longer declares

`prune_wif` removes a `roles/iam.workloadIdentityUser` member from a service account only when
ALL of these hold: the SA is declared in this config; the member comes from THIS project's own
WIF pool (`principalSet:` or `principal:` followed by
`//iam.googleapis.com/projects/<number>/locations/global/workloadIdentityPools/<pool>/`); and
config no longer declares that member (`wif_repos` / `wif_subjects`). The own-pool prefix is the
ownership proof: ANY undeclared member of this project's own pool is removed, including member
types `ensure` never writes (`attribute.repository_owner/...`, `/*`, `subject/...`). The pool is
treated as engine territory; that is an owner ruling, not a claim that the engine wrote each one.

Everything else is reported `not ours, kept` and left alone: a member of another pool, a `user:`,
a `serviceAccount:`, and any SA the config does not declare. Only UNconditional bindings are
removed: a member under a CONDITIONAL `workloadIdentityUser` binding is reported
`conditional binding (<title>) not engine-owned, kept`. A `retire: true` SA declares no
members, so its own-pool members are removed (a malformed tombstone exits first, via the same
`validate_retired` guard as `prune service_accounts`). With no `wif.pool` in the config the
pruner reports `skip`. The project number is read with `_project_number` (read-only, fails loudly
on a non-number); it wins over any `project.gcp_number` in the config. Every run prints
`examined N SAs, M wif members`, also when nothing is extra. A dry-run prints
`would unbind wif <member> → <sa>` and counts it in the trailer; with nothing
extra it prints `ok no extra wif bindings`. A plain `--prune` runs it too. The nightly `sa_audit`
stays report-only.

Example: narrowing `wif_repos: [org/app]` to `wif_subjects: [{repo: org/app, environment: ops}]`
leaves the old repo-wide `principalSet` live until `--prune --only wif` removes it.

### `human_managed_roles` — a conditional project role a human owns

```yaml
service_accounts:
  - name: releaser
    human_managed_roles:
      - role: roles/secretmanager.admin
        condition:
          title: base-secrets-only
          expression: resource.name.startsWith("projects/42/secrets/base-")
```

The engine **never grants or removes** these. It only DECLARES them so the audit and the pruner
stop calling a human-approved conditional binding a stray. `role` must be a qualified string;
`condition.title` and `condition.expression` must be non-empty strings; anything else exits at
plan time. `grantable-roles.txt` is unchanged, so the provisioner gains no power.

- `sa_audit`: a live binding matching role + member + condition title + expression exactly prints
  `ok human-managed <role> (<title>)`. The same role under a different condition is a finding
  `human-managed binding drifted`, and so is ANY other live conditional binding of a
  human-managed role that is not an exact declared entry, even when the exact one is also live
  (a wider `startsWith(...)` binding cannot hide behind it). The same role held UNconditionally
  is still a stray (the declaration covers only the conditional form). A declared entry absent
  live is a finding `human-managed binding missing`, and it **fails the audit** (config is the
  source of truth).
- A role listed in `human_managed_roles` that the same SA also wants (`roles`, or a capability's
  expansion) exits at plan time: ensure would bind it unconditionally.
- `--prune service_accounts`: the engine only ever writes UNconditional bindings, so it only
  removes those. Every live CONDITIONAL binding (declared human-managed, drifted, or any other
  role) is never planned for removal; it prints `conditional binding <role> (<title>) not
  engine-owned, kept`. A role held both conditionally and unconditionally plans at most ONE
  unconditional unbind, and only if an unconditional binding really exists.

## The engine (`engine/`)

- `core.py` — provider-agnostic reconcile discipline: dry-run gate, `do()`/`undo()`,
  fail-loud `read()`, plan printer. **Dry-run is accurate** — existence is checked, only
  mutations are withheld, so a `~ would` line is a real change an apply would make.
- `providers/gcp.py` — the `gcloud` seam + access handlers/pruners. A custom role
  (`organizations/<id>/roles/x`, `projects/<id>/roles/x`) is verified to exist
  (`gcloud iam roles describe`, needs `iam.roles.get`) before its binding is planned; a missing
  one fails the run instead of printing `~ would`.
- `providers/kubernetes.py` — the `kubectl` seam + tenancy/identity + exposure handlers/pruners.
- `provision.py` — the CLI: load config → dispatch to provider target(s).

```bash
python3 engine/provision.py <key>                 # dry-run all subsystems (uses ./projects)
python3 engine/provision.py <key> --apply         # apply
python3 engine/provision.py <key> --only wif      # one subsystem
python3 engine/provision.py <key> --prune         # plan removals (subtractive)
python3 engine/provision.py <key> --prune --only wif   # remove own-pool WIF members config dropped
python3 engine/provision.py <key> --retire        # plan deletion of `retire: true` SAs
python3 tests/test_engine.py && python3 tests/test_kubernetes.py   # seams stubbed; no cloud
```

## Local gate

`make ci` runs exactly the checks `.github/workflows/ci.yml` runs, in CI's order; `ci.yml` calls
the same make targets, so the two cannot drift (`tests/test_ci_make_targets.py` fails if they do).

- `make hooks` once per clone: installs the committed pre-push hook (`core.hooksPath=.githooks`).
- `make ci` before pushing (the hook runs it for you). It stops at the first failing check, names
  the target, and prints how many checks ran.
- What it runs: `venv` (`.venv` built from `requirements-ci.txt`, the same file CI installs),
  `tests` (engine, kubernetes, bootstrap anchors, and the gate's own tests), `tools` (local
  shellcheck/actionlint must equal CI's pinned versions, else it prints the install hint),
  `actionlint`, `canary`, `shellcheck`, `composite` (the composite action's `run:` bodies), `pinned`.
- The hook checks the commit being pushed, not just the tree: it refuses a dirty tree, or a pushed
  ref whose sha is not `HEAD`, and says what to do. It scrubs git's env before running checks.
- Over ssh GitHub can drop the idle connection while the hook runs (rc 141/128); then push over
  HTTPS with the global git config skipped:
  `GIT_CONFIG_GLOBAL=/dev/null git push https://github.com/Just-Git-Dev/infra-provisioning-template.git <branch>`.
- Emergency bypass only: `git push --no-verify`. CI still runs the same checks.
- Why: DECISIONS.md, "Local gate == CI".

## Bootstrap (`bootstrap/`)

`anchor.sh` (per-project) or `fleet-anchor.sh` (one SA reaches all projects) establish the
keyless GitHub→GCP WIF path a human runs once with Owner. `provisioner-roles.txt` is the
single source of truth for the provisioner's least-privilege roles (also read by `--prune`).
A line carrying `{project}` names a **project-scoped custom role**: the anchor scripts create
and reconcile it before granting, and `fleet-anchor.sh` skips it when granting at folder/org
scope (a project-scoped role does not exist there — it is granted per-project instead).
Edit the `PROVISIONER_REPO` / `PROJECTS` placeholders (or set `PROVISIONER_REPO` via env).

## Safety notes

- **Least privilege:** the provisioner GRANTS roles to app SAs via `projectIamAdmin` without
  holding them — 2 identity/IAM-admin roles in `provisioner-roles.txt`, the org **custom** role
  `jgdFleetSaAdmin` (service-account admin **without** `delete`/`undelete`/`disable`; defined by
  `fleet-anchor.sh`, allowed by name in the scope audit, so a leaked provisioner cannot delete a
  service account), plus one more **custom** role, `jgdSecretIamAdmin`, that
  `resource_roles.secrets` needs. `projectIamAdmin` confers
  `setIamPolicy` on the **project**, not on a resource, so binding a role on a *secret* needs
  `secretmanager.secrets.setIamPolicy` on that secret. It is a custom role and not
  `roles/secretmanager.admin` deliberately: admin includes `versions.access`, which would let
  the identity broker read every secret **payload** in the fleet. The custom role can neither
  create, delete, nor read a secret — only manage who may access one. The anchor scripts create
  and reconcile it.
- **`principals` is guarded by a FILE, not by IAM.** `modifiedGrantsByRole` — the IAM Condition
  behind `grantable-roles.txt` — applies to **project/folder/org** allow policies only. A
  service-account resource policy has no equivalent attribute, so the provisioner's
  `jgdFleetSaAdmin` is unconditioned there and IAM will not refuse a bad `principals:`
  entry. `bootstrap/principal-grantable-roles.txt` and the reviewer are the only mechanical
  controls. That is the same posture resource-scoped SA bindings have always had; `principals`
  is the first block that points it at a **human**, which is why the allow-list exists at all
  and why it starts with one role.
- **Keyless:** WIF/OIDC everywhere — no long-lived keys or secrets in a consumer repo.
- **Pin third-party actions to a commit SHA** for production (the examples use `@vN` tags for
  readability). This engine mints cloud credentials — treat a moved tag as a supply-chain event.
- **Deletion is a separate, explicit mode.** Only `--retire` deletes a service account, and only one marked
  `retire: true`, live, and holding zero project roles; no add or prune path emits
  `service-accounts delete` (a test scans every argv). The separation is in code, not in identity:
  the identity running the engine may still hold delete permission.
- **Prune is opt-in and conservative:** the WIF pruner removes only members of this project's own
  pool; k8s prune is label-scoped and never deletes a Namespace
  or ResourceQuota.

## License

MIT — see [LICENSE](LICENSE).
