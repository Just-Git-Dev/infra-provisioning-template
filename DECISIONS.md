# Decisions — infra-provisioning-template

The WHY log for this repo. Commit messages say *what* changed; this says *why*, and what
the alternatives were. Newest first.

## Index

- [2026-08-21 — caller inputs move to `env:`; CI grows actionlint and lints `action.yml` itself](#2026-08-21--caller-inputs-move-to-env-ci-grows-actionlint-and-lints-actionyml-itself)
- [2026-08-20 — SHA-pin this repo's own actions and enforce it in CI](#2026-08-20--sha-pin-this-repos-own-actions-and-enforce-it-in-ci)
- [2026-08-20 — port the two mutation-path engine fixes from the consumer; release v1.0.1](#2026-08-20--port-the-two-mutation-path-engine-fixes-from-the-consumer-release-v101)

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
