# AGENTS.md — infra-provisioning-template

Guidance for coding agents working in this repo. (`CLAUDE.md` is not maintained separately —
Claude Code reads this file. One convention across the `Just-Git-Dev` org.)

## What this repo is

A **topology-free** provisioning engine, published two ways: a composite action
(`Just-Git-Dev/infra-provisioning-template@v1`) and a GitHub template. It carries **zero**
project ids, service accounts, or secrets — consumers supply all of that as
`projects/<key>/config.yaml` in their own repo.

Its first and currently only consumer is the private `Just-Git-Dev/infra-provisioning`,
which pins a **full commit SHA**, not `@v1`.

## The rules that matter here

- **This repo is on a consumer's supply chain.** The action runs as their provisioner
  service account, with IAM-admin rights over their projects. Third-party actions are
  SHA-pinned (CI job `third-party actions are SHA-pinned` enforces it across
  `.github/workflows` *and* `action.yml`), and a `run:` body must not `curl | bash`
  anything — the pin gate greps `uses:` lines and cannot see what a step downloads.
- **Caller-controlled values reach the shell via `env:`, never `${{ }}` interpolation**
  into a `run:` body. Interpolation splices the raw string into the program text before
  bash sees it. `github.*` context set by the runner is fine to interpolate.
- **Dry-run is the default and it is accurate.** Existence is checked; only mutations are
  withheld. A `~ would` line is a real change an apply would make, so **zero `~ would`
  lines = zero drift**. Never add a code path that makes dry-run guess.
- **The engine only owns objects it can drift-detect honestly.** That is why it provisions
  Namespace/RBAC/KSA/Service/Ingress but *not* the Deployment or ConfigMap/Secret — it
  selects the workload from outside, by label or name, so it never fights the consumer's CD.
- **Adding a provider is a new `providers/<kind>.py`** satisfying the contract core drives
  (`HANDLERS`/`PRUNERS`, `context(target)`, `preflight(ctx)`, `label(key, target, ctx)`).
  Core, the CLI and existing providers stay untouched.

## Before you push

```bash
python3 tests/test_engine.py && python3 tests/test_kubernetes.py   # seams stubbed, no cloud
shellcheck bootstrap/*.sh
python3 scripts/extract_action_shell.py > /tmp/a.sh && shellcheck --shell=bash /tmp/a.sh
actionlint
```

`main` is protected: PR required, `test` / `shellcheck` / `third-party actions are
SHA-pinned` must pass, and admins are not exempt.

## Releasing

Tag `vX.Y.Z` on the merge commit, then move the floating `v1` alias to it. Keep `v1`
pointing at a **real** semver tag — never at an untagged commit — so `v1` and the newest
`v1.x` never disagree. Record the why in `DECISIONS.md`.
