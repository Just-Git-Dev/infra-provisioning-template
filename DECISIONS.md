# Decisions — infra-provisioning-template

The WHY log for this repo. Commit messages say *what* changed; this says *why*, and what
the alternatives were. Newest first.

## Index

- [2026-08-23 — the provisioner gets a custom role for secret IAM, because `projectIamAdmin` stops at the project](#2026-08-23--the-provisioner-gets-a-custom-role-for-secret-iam-because-projectiamadmin-stops-at-the-project)
- [2026-08-23 — RCA: `prune_resource_roles` planned to tear down every `act_as` grant](#2026-08-23--rca-prune_resource_roles-planned-to-tear-down-every-act_as-grant)
- [2026-08-23 — `resource_roles`: bind a role on one resource, because project scope was the only scope](#2026-08-23--resource_roles-bind-a-role-on-one-resource-because-project-scope-was-the-only-scope)
- [2026-08-21 — caller inputs move to `env:`; CI grows actionlint and lints `action.yml` itself](#2026-08-21--caller-inputs-move-to-env-ci-grows-actionlint-and-lints-actionyml-itself)
- [2026-08-20 — SHA-pin this repo's own actions and enforce it in CI](#2026-08-20--sha-pin-this-repos-own-actions-and-enforce-it-in-ci)
- [2026-08-20 — port the two mutation-path engine fixes from the consumer; release v1.0.1](#2026-08-20--port-the-two-mutation-path-engine-fixes-from-the-consumer-release-v101)

---

## 2026-08-23 — the provisioner gets a custom role for secret IAM, because `projectIamAdmin` stops at the project

Found the only way it could be: the **first real apply** of `resource_roles.secrets`, running as
the provisioner SA in CI, failed `PERMISSION_DENIED` on `secretmanager.secrets.get`.

**Problem.** This engine's central claim about the provisioner is that it *"GRANTS roles to app
SAs via `projectIamAdmin` without HOLDING them"*. That is true — for **project-level** bindings,
which need only `resourcemanager.projects.setIamPolicy`. It does not generalise to a resource.
Binding a role on a **secret** needs `secretmanager.secrets.setIamPolicy` **on that secret**, and
reading the policy first needs `secrets.get` / `secrets.getIamPolicy`. `projectIamAdmin` confers
none of the three. So `resource_roles.secrets` shipped in v1.1.0 could be *declared* and *planned*
but never *applied*.

(`resource_roles.service_accounts` works untouched: `iam.serviceAccountAdmin`, already in the
kept-set, includes `serviceAccounts.get`/`setIamPolicy`. The gap is specific to secrets.)

**Why not `roles/secretmanager.admin`.** It includes `secretmanager.versions.access`. Granting it
would let the identity broker — an SA that every consumer's CI can reach through WIF — read every
secret **payload** in the fleet. That trades a provisioning convenience for the crown jewels. No
predefined role offers `setIamPolicy` without payload access; this was checked against
`includedPermissions`, not inferred from names.

**Decision.** A project-scoped **custom role**, `jgdSecretIamAdmin`, with exactly three
permissions — `secretmanager.secrets.get`, `.getIamPolicy`, `.setIamPolicy` — created and
**reconciled** (not merely created) by the anchor scripts, which a human already runs once with
Owner. It cannot create, delete, or read a secret; it can only manage who may access one.

Not to be confused with `jgdSecretsProvisioner`, removed 2026-07-01 when this repo stopped
creating secrets. That role created them. This one deliberately cannot.

**Mechanism.** `provisioner-roles.txt` stays the single source of truth and gains a `{project}`
placeholder: a line carrying it is a project-scoped custom role, substituted by both the shell
and the engine. Three consequences fell out and are handled:

- `fleet-anchor.sh` grants at folder/org scope too, where a project-scoped custom role **does not
  exist**. Those grants are skipped there and made per-project instead.
- `prune_provisioner` compares against the live policy, which reports custom roles as
  `projects/<p>/roles/<id>`. Without substitution it would unbind the role the anchor had just
  granted, on every run. `provisioner_kept_roles()` now takes the project.
- `_qualify()` prefixed anything without a `roles/` prefix, so a custom role became
  `roles/projects/...` — rejected by gcloud only at apply time. It now leaves any
  `.../roles/...` alone, and the two open-coded copies of that logic were folded into it.

**Tradeoff.** A custom role is one more object to reconcile, and it puts a *definition* in the
anchor script rather than in config. That is the cost of least privilege here: the alternative is
a predefined role that reads secret payloads. The permission set is reconciled on every anchor
run, so adding a permission later reaches projects anchored earlier.

**Verified.** 59 tests pass (3 new: the qualifier, the substitution, and a `prune_provisioner`
guard proving the custom role survives a prune). `shellcheck` clean on both anchor scripts —
including no `tr` set-vs-word trap, using `--format='value[delimiter=","]'` so the separator is
deterministic rather than guessed (the SC2020 lesson of 2026-07-24).

---

## 2026-08-23 — RCA: `prune_resource_roles` planned to tear down every `act_as` grant

Found in the first dry-run of `resource_roles` against a live project, before any apply.

**Symptom.** `provision.py automahn --prune` planned four unbinds where two were expected:

```
~ would unbind roles/iam.serviceAccountUser on service account api-run from github-releaser
~ would unbind roles/iam.serviceAccountUser on service account api-run from github-rotator
```

Those two are the project's **`act_as` impersonation grants**. Applying that plan would have
stopped both CI identities from deploying Cloud Run as the runtime SA — a live outage, from the
pruner shipped one commit earlier.

**Root cause.** `prune_resource_roles` treated *every* binding on a named resource as its own to
reconcile. Its keep-rule was "is this member an SA this config declares, and is this exact
(resource, role, member) triple declared under `resource_roles`?" An `act_as` grant satisfies the
first half and fails the second — it is declared under `act_as:`, a different subsystem — so it
read as undeclared drift. The `api-run` SA is both an `act_as` target and (now) a `resource_roles`
resource, and nothing in the pruner knew the difference.

**Why it wasn't caught.** Every unit test drove one subsystem in isolation against a hand-written
policy fixture, and no fixture mixed subsystems: the `resource_roles` fixtures contained only
`resource_roles` bindings. The bug lives exactly in the overlap, which is a shape the test data
never had. `prune_act_as` had the mirror-image protection (it only touches serviceAccountUser)
purely because it was written to do one role — the invariant was never stated, so the new pruner
did not inherit it.

**What did catch it:** running the dry-run against a real project before applying. Dry-run is
accurate by construction here, so the plan showed the real bindings a fixture had never modelled.

**Fix.** An explicit `_FOREIGN_ROLES` table: bindings whose (kind, role) belongs to another
subsystem — `iam.serviceAccountUser` (act_as) and `iam.workloadIdentityUser` (wif) on
`service_accounts` — are skipped by `prune_resource_roles`. `deleted:` principals are exempt from
the exemption: no subsystem wants a dangling identity, and `prune_act_as` only ever touches
members it owns, so nothing else would ever clear one.

**Prevention.** Two regression tests, both red before the fix. One drives `prune_resource_roles`
against a policy carrying act_as and wif bindings and asserts it plans nothing; the other asserts
a `deleted:` holder of a foreign role IS still pruned, so the exemption cannot be widened into a
leak. The invariant is now written down in the docstring: **a pruner reconciles only the bindings
its own subsystem declares.** Any future pruner that names a shared resource inherits the rule.

---

## 2026-08-23 — `resource_roles`: bind a role on one resource, because project scope was the only scope

**Problem.** The gcp provider could bind IAM at **project scope only**. `act_as` was the single
exception, and it is hard-wired to one role (`iam.serviceAccountUser`) on one kind of resource.
That had two consequences, and the second is the worse one:

1. **It pushed configs toward over-granting.** An SA that needs `setIamPolicy` on exactly one
   service account had no way to say so, so it got project-wide `iam.serviceAccountAdmin`. Paired
   with `resourcemanager.projectIamAdmin` that is a full project-takeover primitive, and the
   consumer had exactly that live on a CI rotator impersonable from two repos.
2. **It made "zero drift" quietly narrower than it sounds.** Operators still needed those grants,
   so they bound them by hand at resource scope, where the engine could not see them. A clean
   dry-run said nothing about them: not planned, not pruned, not reported. In the consumer's fleet
   that turned out to be a `secretmanager.admin` on one project's secret and three more grants on
   another's — all real, all invisible.

**Decision.** A fourth subsystem, `resource_roles`, declared on the SA that receives the access:

```yaml
resource_roles:
  service_accounts: { app-run: [iam.serviceAccountAdmin] }
  secrets:          { app-env: [secretmanager.admin] }
```

It is a generalisation of a shape the provider already had rather than a new concept — `act_as`
is exactly a resource-scoped binding — so it costs one handler, one pruner, and no change to
`core.py`, the CLI, or the provider contract.

**The scope line is unmoved, and that is deliberate.** ADR-001 says this engine brokers *access*;
app repos own *resources*. `resource_roles` binds ON a resource and **never creates one**. An
absent target raises, with a message naming the app repo as its owner. The tempting alternative —
skip a missing resource with a warning — was rejected: it makes a clean plan a lie about access
that does not exist, which is the one property this engine sells.

**Prune conservatism.** Only resources the config *names* are examined, so blast radius is bounded
by the config rather than by everything in the project. Within those, a member is touched only if
it is an SA this config declares — same rule as `prune_act_as`. The one addition is `deleted:`
principals: they reference an identity that no longer exists, cannot be a legitimate grant, and
are the residue an SA rename leaves behind. Humans, Google-managed service agents, and SAs from
other projects are never touched.

**Tradeoff.** One extra `get-iam-policy` read per named resource per run, and a config that can
now fail the whole plan on a missing secret. Both are the intended shape: the read is what makes
the grant visible at all, and failing closed on a missing resource is the point.

**Verified.** 54 tests pass (8 new, written failing first). The live gcloud output shape the
pruner parses — `--flatten='bindings[].members' --format='csv[no-heading](bindings.role,bindings.members)'`
— was captured from a real secret policy before the parser was written, not guessed; the fixture
in `test_prune_resource_roles_removes_undeclared_owned_sa` is that exact output.

---

## 2026-08-21 — caller inputs move to `env:`; CI grows actionlint and lints `action.yml` itself

Found in an org-wide cleanup review of the four `Just-Git-Dev` repos.

**`action.yml` spliced caller inputs into the shell.** The Provision step interpolated
`${{ inputs.project }}`, `${{ inputs.apply }}`, `${{ inputs.prune }}` and
`${{ inputs.only }}` directly into its `run:` body. GitHub expands `${{ }}` into the
program *text* before bash parses it, so a value containing a quote or `$(…)` is code, not
data. These inputs arrive from a consumer's `workflow_dispatch` — from whoever can click
Run — and this action executes as their provisioner SA with IAM-admin rights. All four now
arrive as `env:` vars, the way `config-root` already did. `github.action_path` stays
interpolated: the runner sets it, not the caller.

Nothing was exploitable today, and that is the point of doing it now. The fix is two lines
per input and it removes the shape, so no future value has to be audited for it.

**CI never lint-checked this repo's own shell.** The `shellcheck` job ran
`shellcheck bootstrap/*.sh` — the two anchor scripts a human runs once — and nothing else.
The `run:` bodies in `ci.yml` and, more importantly, in `action.yml` were unchecked. Added
actionlint (SHA-pinned `raven-actions/actionlint@3d39aea`, matching the two consumer repos)
for the workflows.

`action.yml` needed its own step, because **actionlint lints workflows and does not read
`action.yml`** — a composite action is the one file in a repo like this that is guaranteed
to run in someone else's cloud, and it was the one file no linter looked at.
`scripts/extract_action_shell.py` pulls the `run:` bodies into one script and shellchecks
it. Two details it has to get right, both learned by hitting them: each body is wrapped in
a subshell so one step's `set -e` does not leak into the next (the runner gives each step
its own shell), and `${{ … }}` is replaced with a placeholder first — shellcheck reads it
as a malformed parameter expansion and fails SC2296, which is exactly why actionlint
substitutes expressions before it shellchecks a workflow body.

**The pin gate now scans `action.yml` too.** It only looked at `.github/workflows`, so a
`uses:` added to the composite action — the code that runs inside every consumer's
provisioning job — would have been unguarded by the job whose whole purpose is guarding it.

**Also in this pass:** `CODEOWNERS` and `.github/dependabot.yml` (both matching
`reusable-workflows`; dependabot bumps a pinned SHA *and* its `# vX.Y.Z` comment, so pinning
costs nothing to maintain), and an `AGENTS.md` — the org's convention, adopted here so the
three repos stop each using a different filename for the same thing.

**Repo settings changed alongside this, outside the diff:** `main` is now protected (PR
required, the three CI jobs required, admins not exempt) — it had no protection at all while
carrying a floating `v1` tag — and `v1.0.2` was cut so the `v1` alias points at a real semver
tag rather than an untagged commit.

---

## 2026-08-20 — SHA-pin this repo's own actions and enforce it in CI

**Context.** Both consumer repos (`infra-provisioning`, `reusable-workflows`) pin every
third-party action to a full commit SHA and enforce it with a CI job that rejects any
`uses: owner/repo@<non-40-hex>`. This repo did not — it used `actions/checkout@v5`,
`actions/setup-python@v5`, `google-github-actions/auth@v3`, `setup-gcloud@v3`.

**Why it matters more here than it looks.** This repo is now *on the supply chain* of every
consumer that pins the action. A moved tag in **our** workflows is one more way to change what
runs against **their** cloud — and the engine here mints cloud credentials and rewrites IAM.
The exemption was never justified; it was just never noticed. Surfaced while dogfooding the
action from `infra-provisioning` (that repo's TODO, 2026-08-20).

**Decision.** Pin all seven references to full SHAs (verified against upstream's tag →
commit mapping, and identical to what `infra-provisioning` already pins), and add the same
`third-party actions are SHA-pinned` job so the rule is enforced rather than remembered.
Also set `defaults.run.shell: bash` — GitHub's implicit default omits `pipefail`.

**Including the scaffold's self-reference.** `.github/workflows/provision.yml` is the caller a
new adopter copies via "Use this template", and its `uses:` pointed at our own `@v1`. Pinned to
`b35575b…` (`v1.0.1`) too. A tag there would be *teaching* the mutable-ref habit to every
adopter, and this repo has just demonstrated what a stale `@v1` costs: it sat three releases
behind the code it was extracted from. Adopters bump the SHA and the trailing comment together.

**Not done:** the `v1` alias still exists and still moves. It is the documented entry point in
the README and removing it is a breaking change for anyone already pinned to it. The gate makes
*our* references honest; consumers still have to choose a SHA.

## 2026-08-20 — port the two mutation-path engine fixes from the consumer; release v1.0.1

**Context.** This repo was extracted from `Just-Git-Dev/infra-provisioning` on 2026-07-14
(ADR-003 Phase 1c) and tagged `v1.0.0` / `v1`. It has not moved since. The consumer, still
running its **vendored** copy of the same engine, fixed two real bugs on 2026-07-24
(`dc66922`, RCA in that repo's `DECISIONS.md`). Those fixes never reached here, so the
published action was **behind the code it was extracted from**.

That is the blocking issue for the Phase 1c dogfood: pointing `infra-provisioning` at
`@v1` would delete its fixed vendored engine and call a buggy one — a silent regression of
two shipped fixes.

### RCA

- **Symptom.** `gcloud iam service-accounts create` failed with `INVALID_ARGUMENT`, and the
  engine printed nothing useful — the failure was invisible in the workflow log.
- **Root cause (two, stacked).**
  1. `providers/gcp.py` truncated the SA description to `[:100]` **characters**; GCP's
     `displayName` limit is 100 **bytes**. A description containing multibyte characters
     (an em-dash, 3 bytes) produced a >100-byte value and the create was rejected.
  2. `core.do()` / `core.undo()` called `run()` bare. `subprocess.run(check=True)` raises
     `CalledProcessError`, whose `stderr` was never read — so the real gcloud message was
     lost. `core.read()` had been fail-loud since ADR-003; the mutation path had not.
- **Why it wasn't caught.** The whole test suite drives handlers in **dry-run**, where
  `core.do` returns before `run()` is ever called. No test exercised the mutation path at
  all, so neither bug was reachable from CI. This is the same reason the dogfood's
  "zero `~ would` = parity" gate cannot catch it: dry-run withholds exactly the code that
  is broken.
- **Fix.** Truncate on the encoded byte string and drop a partial trailing multibyte char
  (`.encode()[:100].decode("utf-8", "ignore")`); wrap `run()` in `do`/`undo` and
  `sys.exit()` with gcloud's stderr, mirroring `core.read`.
- **Prevention.** Two regression tests in `tests/test_engine.py` that run with
  `core.DRY = False` — `test_do_surfaces_gcloud_stderr_on_failure` and
  `test_service_account_display_name_truncated_by_bytes` — the suite's first coverage of
  the apply path.

**Decision.** Port the fixes verbatim rather than re-deriving them, and verify by
**byte-identity**: `engine/core.py` and `engine/providers/gcp.py` are now `diff`-clean
against `infra-provisioning@main`. `engine/provision.py` deliberately stays divergent —
it carries the `--config-root` decoupling that only exists here, which is the extraction
itself.

**Alternative rejected.** "Fix it in the consumer's next release instead." There is no
consumer release — the consumer *is* the vendored copy. Leaving the drift means every
future adopter of `@v1` gets the bugs.

**Consequence.** Released as **`v1.0.1`**; `v1` moved to it. Callers should pin the
commit SHA with `# v1.0.1` in a trailing comment (the org's supply-chain rule), not the
mutable tag.
