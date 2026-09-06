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
| **gcp** | service accounts + their project IAM roles (`service_accounts`), SA→SA impersonation (`act_as`), **roles scoped to a single resource** (`resource_roles`), app-repo Workload-Identity Federation (`wif`) |
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
| `only` | `''` | run a single subsystem/pruner |
| `config-root` | `${{ github.workspace }}` | root holding `projects/<key>/config.yaml` |

## Config schema

See the fully-worked [`projects/EXAMPLE/config.yaml`](projects/EXAMPLE/config.yaml). A bare
config (no `targets:` key) auto-wraps to a single `kind: gcp` target, so GCP-only users can
skip the wrapper. Explicit `targets:` mix `gcp` + `kubernetes`, applied **gcp-first** so a
KSA's Workload-Identity reference to a GCP SA resolves.

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

## The engine (`engine/`)

- `core.py` — provider-agnostic reconcile discipline: dry-run gate, `do()`/`undo()`,
  fail-loud `read()`, plan printer. **Dry-run is accurate** — existence is checked, only
  mutations are withheld, so a `~ would` line is a real change an apply would make.
- `providers/gcp.py` — the `gcloud` seam + access handlers/pruners.
- `providers/kubernetes.py` — the `kubectl` seam + tenancy/identity + exposure handlers/pruners.
- `provision.py` — the CLI: load config → dispatch to provider target(s).

```bash
python3 engine/provision.py <key>                 # dry-run all subsystems (uses ./projects)
python3 engine/provision.py <key> --apply         # apply
python3 engine/provision.py <key> --only wif      # one subsystem
python3 engine/provision.py <key> --prune         # plan removals (subtractive)
python3 tests/test_engine.py && python3 tests/test_kubernetes.py   # seams stubbed; no cloud
```

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
  holding them — 3 identity/IAM-admin roles in `provisioner-roles.txt`, plus one **custom**
  role, `jgdSecretIamAdmin`, that `resource_roles.secrets` needs. `projectIamAdmin` confers
  `setIamPolicy` on the **project**, not on a resource, so binding a role on a *secret* needs
  `secretmanager.secrets.setIamPolicy` on that secret. It is a custom role and not
  `roles/secretmanager.admin` deliberately: admin includes `versions.access`, which would let
  the identity broker read every secret **payload** in the fleet. The custom role can neither
  create, delete, nor read a secret — only manage who may access one. The anchor scripts create
  and reconcile it.
- **`principals` is guarded by a FILE, not by IAM.** `modifiedGrantsByRole` — the IAM Condition
  behind `grantable-roles.txt` — applies to **project/folder/org** allow policies only. A
  service-account resource policy has no equivalent attribute, so the provisioner's
  `iam.serviceAccountAdmin` is unconditioned there and IAM will not refuse a bad `principals:`
  entry. `bootstrap/principal-grantable-roles.txt` and the reviewer are the only mechanical
  controls. That is the same posture resource-scoped SA bindings have always had; `principals`
  is the first block that points it at a **human**, which is why the allow-list exists at all
  and why it starts with one role.
- **Keyless:** WIF/OIDC everywhere — no long-lived keys or secrets in a consumer repo.
- **Pin third-party actions to a commit SHA** for production (the examples use `@vN` tags for
  readability). This engine mints cloud credentials — treat a moved tag as a supply-chain event.
- **Prune is opt-in and conservative:** k8s prune is label-scoped and never deletes a Namespace
  or ResourceQuota.

## License

MIT — see [LICENSE](LICENSE).
