# Decisions — infra-provisioning-template

The WHY log for this repo. Commit messages say *what* changed; this says *why*, and what
the alternatives were. Newest first.

## Index

- [2026-09-06 — `v1` had been stranded on v1.3.0 for three releases; moved, and the release step given a verify command](#2026-09-06--v1-had-been-stranded-on-v130-for-three-releases-moved-and-the-release-step-given-a-verify-command)
- [2026-09-06 — `principals`: a human/group member on one service account, guarded by a file rather than by IAM](#2026-09-06--principals-a-humangroup-member-on-one-service-account-guarded-by-a-file-rather-than-by-iam)
- [2026-09-05 — RCA: the run trailer was a fixed string, so it contradicted the plan above it](#2026-09-05--rca-the-run-trailer-was-a-fixed-string-so-it-contradicted-the-plan-above-it)
- [2026-08-24 — `resource_roles` grows pub/sub and Artifact Registry kinds](#2026-08-24--resource_roles-grows-pubsub-and-artifact-registry-kinds)
- [2026-08-24 — RCA: the engine test runner sat mid-file, so 20 of 37 tests never ran](#2026-08-24--rca-the-engine-test-runner-sat-mid-file-so-20-of-37-tests-never-ran)
- [2026-08-23 — RCA: a dry-run anchor printed `+`, so it read as if it had changed something](#2026-08-23--rca-a-dry-run-anchor-printed--so-it-read-as-if-it-had-changed-something)
- [2026-08-23 — the provisioner gets a custom role for secret IAM, because `projectIamAdmin` stops at the project](#2026-08-23--the-provisioner-gets-a-custom-role-for-secret-iam-because-projectiamadmin-stops-at-the-project)
- [2026-08-23 — RCA: `prune_resource_roles` planned to tear down every `act_as` grant](#2026-08-23--rca-prune_resource_roles-planned-to-tear-down-every-act_as-grant)
- [2026-08-23 — `resource_roles`: bind a role on one resource, because project scope was the only scope](#2026-08-23--resource_roles-bind-a-role-on-one-resource-because-project-scope-was-the-only-scope)
- [2026-08-21 — caller inputs move to `env:`; CI grows actionlint and lints `action.yml` itself](#2026-08-21--caller-inputs-move-to-env-ci-grows-actionlint-and-lints-actionyml-itself)
- [2026-08-20 — SHA-pin this repo's own actions and enforce it in CI](#2026-08-20--sha-pin-this-repos-own-actions-and-enforce-it-in-ci)
- [2026-08-20 — port the two mutation-path engine fixes from the consumer; release v1.0.1](#2026-08-20--port-the-two-mutation-path-engine-fixes-from-the-consumer-release-v101)

---

## 2026-09-06 — `v1` had been stranded on v1.3.0 for three releases; moved, and the release step given a verify command

**What.** The floating `v1` alias moved from `123ef51` (v1.3.0) to `bdf536a` (v1.6.0), and
`AGENTS.md`'s Releasing section gained a copy-pasteable command block ending in a
**remote** verification.

**What was wrong.** `AGENTS.md` has always said to move `v1` onto each release and to keep it
from disagreeing with the newest `v1.x`. Three consecutive releases — v1.4.0, v1.5.x and
v1.6.0 — did not, so `@v1` served code predating three real fixes: the dry-run trailer that
contradicted its own plan (#17), the WIF provider `displayName` reconcile (#16), and the
metadata reconcile that never converged (#15). For a tool that runs as a consumer's
provisioner SA with IAM-admin rights, the first of those is the worst kind of bug to serve
from a floating pointer: an operator reading "no changes" while changes were planned.

**Why it went unnoticed for three releases, which is the part worth fixing.** The rule was
correct and written down; it simply was not executed, and nothing observed the omission. The
real consumer pins a full commit SHA — so `v1` being stale broke nothing anyone tested, and
the only signal would have been someone manually comparing two refs. A rule whose violation
produces no symptom will drift.

**The fix is a verify step, not a stronger sentence.** The new block ends with
`git ls-remote origin 'refs/tags/v1^{}'` and the same for the version tag, and states that a
release is not finished until both print the same SHA. It checks the **remote**, because the
failure mode is forgetting to push, and a local clone looks correct either way. It also notes
that `tag.annotate` may be enabled globally, which makes a bare `git tag` fail.

**Considered and rejected: retiring `v1` entirely.** Deleting it forces everyone onto an
explicit `vX.Y.Z` or SHA, which is the posture this repo actually wants. But it hard-breaks
any unknown `@v1` consumer instead of silently correcting them, and the README examples use
`@vN` for readability. Measured blast radius made moving it clearly safe: public repo, **0
forks, 0 stars**, and the one known consumer pins a SHA. Retiring it stays available later if
the alias keeps drifting.

**A CI guard was considered and not built** — a job asserting `v1` matches the newest `v1.x`
would catch this mechanically. It is the right end state; it was left out to keep this change
docs-only, and because a tag-push is not a PR event, so the guard needs its own trigger design.

---

## 2026-09-06 — `principals`: a human/group member on one service account, guarded by a file rather than by IAM

**What.** A new top-level `principals:` block, one handler (`ensure_principals`) and one pruner
(`prune_principals`), plus `bootstrap/principal-grantable-roles.txt` seeded with exactly
`roles/iam.serviceAccountTokenCreator`.

**Why.** Every member this provider built was `serviceAccount:{email}` — `ensure_act_as`,
`ensure_wif`, `ensure_resource_roles` and both pruners. A human could therefore be granted
nothing, which is how the consumer shipped three `log-reader` SAs that nobody could impersonate:
`roles/owner` carries `iam.serviceAccounts.actAs` but **not** `getAccessToken`, so every
`--impersonate-service-account` call returned PERMISSION_DENIED. The missing grant is
`serviceAccountTokenCreator` for the person, bound *on* the SA.

**Why not a one-time terminal grant in the consumer.** It would be invisible to the plan, not
prunable, and not covered by "zero drift" — the exact failure `resource_roles` was added to end.

**Why a separate block, not a member type inside `resource_roles`.** `resource_roles` is nested
under the SA that *receives* the access; a human has no owning SA to nest under. More
importantly, `prune_resource_roles`' safety rule — "only ever touch a member that is a service
account THIS config declares" — is what makes it safe against a live project. Admitting humans
would force that rule to grow a second clause inside the function whose blast radius is already
the hardest to reason about. A separate pruner leaves it untouched and gives the new member type
its own narrower rule.

**The residual risk, stated rather than mitigated.** `grantable-roles.txt` is enforced by an IAM
Condition (`modifiedGrantsByRole`) and a role missing from it is denied by GCP. That attribute
covers **project/folder/org** allow policies only — a service-account resource policy has no
equivalent, so a provisioner holding `iam.serviceAccountAdmin` is unconditioned here.
`principal-grantable-roles.txt` is therefore a CI gate and a review artifact, not a security
control: a config naming `roles/owner` under `principals` is caught by the consumer's
`tests/test_configs.py`, by the reviewer, and by nothing else. This is the posture every
resource-scoped SA binding has always had; what is new is a **human** on the receiving end,
which is a genuine widening and the reason the allow-list starts at one role and the kind is
restricted to `service_accounts`.

**Alternatives rejected.** (a) No allow-list, rely on review alone — nothing then fails in CI, and
the diff that widens this is exactly the diff that most needs a mechanical check. (b) Open all
five `_RESOURCE_KINDS` to human principals — adding a kind later is a one-line change; opening
them all in the same commit is not a decision anyone made. (c) Accept `group:` only, to force the
group shape from day one — that blocks the immediate unblock on creating a group first; the
schema accepts `group:` so the migration is a config edit when a second person needs access.

**Guarded by.** `tests/test_engine.py` — twelve new cases, including
`test_prune_principals_leaves_foreign_human_bindings` (two human subjects, so it cannot pass
vacuously) and `test_principal_grantable_roles_is_seeded_with_exactly_token_creator`, which turns
widening the file into a deliberate test edit. `test_handlers_are_access_only` was updated
deliberately: growing that set is a scope decision by design. The scope line itself is unmoved —
`principals` grants access and creates nothing. Full reasoning in the consumer's ADR-008.

---

## 2026-09-05 — RCA: the run trailer was a fixed string, so it contradicted the plan above it

**Symptom.** A prune dry-run planned a removal and then closed by denying it. On
infra-provisioning run 33943386538 the body printed
`~ would unbind roles/artifactregistry.repoAdmin from github-cleaner` and the last line read
`prune dry-run complete — no removals.` The additive branch had the same defect: run
33943215546 planned `~ would bind roles/artifactregistry.admin → github-cleaner` and closed
`dry-run complete — no changes.` An operator who trusts the summary over the body concludes the
prune is a no-op and skips it — and skipping *this* prune would have stranded a binding, because
removing the role from the allow-list first makes it undeletable by the fleet SA.

**Root cause.** `provision.run()` selected the trailer from `prune`/`DRY` alone
(`provision.py:93`) — the flags describing what the run was *asked* to do — with no reference to
what it actually reported. The phrase "no removals" was therefore never a finding; it was a
label on a mode, and it was printed identically whether zero or fifty removals were planned. The
originating mistake is asserting an outcome next to the evidence rather than deriving it from
the evidence.

**Why it wasn't caught.** The test suite drove handlers directly (`drive()` calls one handler
and asserts its printed lines) and never called `provision.run()`, so no test had ever observed
a trailer. The output was also self-consistent in the common case — most runs genuinely plan
nothing, so the string was right for the wrong reason on almost every run anyone looked at.

**Fix.** `core.do`/`core.undo` are the only places a mutation is ever printed, so they now
increment `core.MUTATIONS`; `run()` resets it and derives the trailer from the count
(`prune dry-run complete — 1 removal planned.`). This makes the summary consistent with the body
*by construction* rather than by the author remembering to keep them in step — the same
correction as the [2026-08-23 dry-run anchor
RCA](#2026-08-23--rca-a-dry-run-anchor-printed--so-it-read-as-if-it-had-changed-something),
where reporting also drifted from what the run had done.

**Prevention.** Four tests, driving `provision.run()` end-to-end against a stub provider — the
outermost level where a trailer is observable. Two are the red repros (one per branch), one
pins the zero case so the fix cannot simply invert the bug, and one guards the counter against
leaking between runs, since it is module state.

## 2026-08-24 — `resource_roles` grows pub/sub and Artifact Registry kinds

**Decision.** `resource_roles` accepts three more kinds — `pubsub_topics`,
`pubsub_subscriptions` and `artifact_repositories` — alongside the original `secrets` and
`service_accounts`.

**Why.** The kinds shipped in ADR-005 were the two we happened to need that week, not a
considered boundary. Everything the ADR says about secrets is equally true of a topic: it is
an **app-owned resource** the broker only ever *binds* on, and a grant on it was invisible to
the plan, unprunable, and **not covered by "zero drift"**. Leaving the other kinds out meant
the honest answer to "is this project's access fully described?" stayed *no* for any project
using pub/sub or GAR — which is all of them.

A live read of `auto-mahn` while scoping this proved the gap is not theoretical: topic
`automahn.outbox.poke` still carries `roles/pubsub.publisher` for `automahn-api-run`, the
**pre-rename** SA (`deleted:` principal, residue of the 2026-08-20 `<component>-<role>`
rename). Four months of clean project-scope plans could never see it, because it is not a
project-scope binding.

**Shape.** No new code paths — `_RESOURCE_KINDS` is a table, and these are three more rows.
That is the point of the table, and it is the argument for having generalised `act_as` into
`resource_roles` rather than special-casing secrets.

**The one wrinkle: GAR needs a location.** `gcloud artifacts repositories describe backend`
fails *argument parsing* ("Failed to find attribute [location]") before it reaches the API — a
repository is per-location and a bare name is ambiguous. Options considered:

1. **A separate `location:` key per repo** — makes the value a mapping instead of a role list,
   so this one kind stops looking like every other kind.
2. **Default the location** from a project-level setting — a wrong default binds IAM on a repo
   the operator did not mean, silently, and only in projects that have several.
3. **Encode it in the key: `<location>/<repo>`** ← chosen. The key stays a plain string, the
   value stays a role list, and the engine builds the fully-qualified
   `projects/<p>/locations/<l>/repositories/<r>` — which gcloud accepts as the positional and
   which already carries the project, so `--project` is not passed alongside it. A key with no
   location **fails loud at plan time**, consistent with how an absent resource is handled.

**`_FOREIGN_ROLES` is empty for all three.** That set exists because an SA named under
`resource_roles` is often an `act_as` target too, and `serviceAccountUser` there belongs to
another subsystem. No subsystem writes bindings on topics, subscriptions or GAR repos, so
nothing is exempt and the pruner sees all of it. Revisit if one ever does.

**Also fixed here:** an unbound resource prints a bare `,` under the engine's flatten+csv
format pair (seen live on both GAR repos and on an unbound subscription). `resource_policy`
split that into a `('', '')` pair. It was harmless in both callers by luck — `ensure` compares
against a member and `prune` skips anything not `serviceAccount:` — so this is hardening, not
a bug fix. Filtering it at the seam means the next kind cannot inherit the trap.

**Not done:** no config declares the new kinds yet. Adopting them on `automahn`/`traide-co` —
and deciding whether that stale `deleted:` topic binding gets pruned — is a separate,
apply-bearing change.

---

## 2026-08-24 — RCA: the engine test runner sat mid-file, so 20 of 37 tests never ran

**Symptom.** `python3 tests/test_engine.py` printed `17/17 passed` and CI was green — while
the file defined **37** tests. The 20 below the runner had never executed, including *every*
`resource_roles` test (the whole of ADR-005) and the `jgdSecretIamAdmin` /
`provisioner_kept_roles` tests written for v1.2.0. Found on 2026-08-24 when new tests appended
to the file were Red-by-construction and the suite still reported all-pass.

**Root cause.** The `if __name__ == "__main__":` block collects tests with
`[v for k, v in sorted(globals().items()) if k.startswith("test_")]` — a snapshot of
`globals()` **at the moment it runs**. Python executes top to bottom, so it can only ever see
definitions *above* it. The block sat at line 264 of 439. Every test appended afterwards
landed below it and was silently invisible. Nothing appends to `globals()` retroactively, so
the failure is total and permanent for anything below that line.

**Why it wasn't caught.** The runner reports `N/N passed` where **N is what it collected**,
not what the file defines. A denominator computed from the same wrong set can never disagree
with itself, so the output looked healthier the more tests it dropped. The two later suites
(`test_kubernetes.py`, `test_anchor.py`) happen to keep their runner last and are unaffected —
verified, not assumed. This is the "a green tick means the check did not run" class: the gate
stopped checking and said nothing.

**Fix.** Move the block to the end of the file, with a comment saying it must stay there and
why. Also broadened its `except AssertionError` to `except BaseException`, so a handler that
raises `SystemExit` (several fail-loud paths do, by design) fails **one** test instead of
aborting the whole suite at that point — the same under-reporting failure by a different
route. With both fixed: 37/37 collected, 30 passing before the new feature landed, 37 after.

**Prevention.** The structural fix is the comment plus the end-of-file placement; a test that
asserts its own collection count would just be another number derived from the same snapshot.
The honest guard is the one now in place: the runner cannot silently under-collect when there
is nothing below it, and CI fails loudly on an unexpected exception rather than stopping.

---

## 2026-08-23 — RCA: a dry-run anchor printed `+`, so it read as if it had changed something

**Symptom.** `DRY_RUN=1 ./anchor.sh` printed 14 yellow `+` lines — the same vocabulary a real
run uses for "created it" — and printed **no** `~ would:` line for the `jgdSecretIamAdmin`
custom role or the WIF impersonation binding. The operator's one pre-flight check before a
fleet-wide IAM change read as a run that had already mutated, while silently omitting two of
the things it was about to do. Found while reviewing the v1.2.0 custom-role work; the defect
is older than that work and affects both `anchor.sh` and `fleet-anchor.sh`.

**Root cause.** Two independent mistakes, both from treating `do_or_dry` as if the dry-run
gate lived inside the *call site* rather than inside the *helper*:

1. `do_or_dry ... >/dev/null && add "…"` — the `>/dev/null` belongs to the whole `do_or_dry`
   invocation, not to the `gcloud` inside it. It was there to swallow gcloud's chatter on a
   real run; in dry-run there is no gcloud, and the only thing on stdout is the `~ would:`
   announcement, so the redirect ate exactly the line it must not.
2. `add` was unconditional. `do_or_dry` returns 0 in dry-run (nothing ran, nothing failed),
   so `&& add` always fired — and the four call sites that put `add` on its own line never
   consulted `DRY_RUN` at all.

**Why it wasn't caught.** The bootstrap scripts had **no behavioural test** — CI ran
`shellcheck bootstrap/*.sh`, which checks that the shell is well-formed, not that a dry run
tells the truth. Both symptoms are visible only by *running* the script, and running it
appeared to need GCP access, so nobody did. And the failure is a silent lie rather than an
error: every dry run "worked".

**Fix.** The gate moves entirely into the two helpers. The script saves its own stdout as
fd 3 (`exec 3>&1`) and `do_or_dry` prints its `~ would:` line there, so no call site's
redirect can hide it; `add` returns early when `DRY_RUN=1`, since `do_or_dry` has already
announced the intent. No call site changed — which is the point: 20-odd sites cannot each be
relied on to remember the rule.

**Prevention.** `tests/test_anchor.py` runs **both** anchors end-to-end with a stub `gcloud`
on `PATH` that answers every read as "absent", and asserts: no `+` in dry-run, a `~ would:`
line for each of the two previously-swallowed operations, zero gcloud mutations in dry-run —
and, so the guard cannot swing the other way, that a real run *does* still print `+` and
*does* mutate. Wired into CI's engine-test job. The stub also removes the "you need GCP to
run it" excuse: these scripts are now testable like any other code.

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
