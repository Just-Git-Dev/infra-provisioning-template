# Decisions — infra-provisioning-template

The WHY log for this repo. Commit messages say *what* changed; this says *why*, and what
the alternatives were. Newest first.

## Index

- [2026-08-20 — port the two mutation-path engine fixes from the consumer; release v1.0.1](#2026-08-20--port-the-two-mutation-path-engine-fixes-from-the-consumer-release-v101)

---

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
