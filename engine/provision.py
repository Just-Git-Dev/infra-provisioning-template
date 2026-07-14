#!/usr/bin/env python3
"""Provisioning engine — provision one project from its config.yaml.

This is the CLI + orchestration. The reconcile discipline (dry-run gate, fail-loud
read, plan printer) lives in `engine/core.py`; the resource handlers live in
`engine/providers/<kind>.py`: `gcp` (access/identity broker: service_accounts +
act_as + wif) and `kubernetes` (tenancy/identity + exposure infra). See ADR-003.

Plan-first and idempotent: every handler describes-then-creates / add-binding, so
re-runs converge and never duplicate. Dry-run prints an ACCURATE plan (existence is
checked even in dry-run — only mutations are withheld) and changes nothing.

  python3 engine/provision.py <key>                         # plan (add) all subsystems
  python3 engine/provision.py <key> --apply                 # apply (add)
  python3 engine/provision.py <key> --only wif              # one subsystem
  python3 engine/provision.py <key> --prune                # plan removals (subtractive)
  python3 engine/provision.py <key> --config-root /path     # configs live elsewhere (see below)

`<key>` is a folder under `<config-root>/projects/`. **--config-root** decouples the
engine (this repo) from the CONFIGS (the caller's repo): it defaults to
$PROVISION_CONFIG_ROOT or the current directory, so when this engine is consumed as a
composite action the caller's checked-out workspace supplies `projects/<key>/config.yaml`
while the engine code lives at $GITHUB_ACTION_PATH. Run in-repo (configs under ./projects)
and it just works with the default.

Auth: in CI the workflow authenticates via WIF as the provisioner SA before this runs;
locally it uses your active gcloud account.
"""
import argparse
import os
import sys

try:
    import yaml
except ImportError:
    sys.exit("PyYAML required: pip install pyyaml")

import core
from providers import gcp
from providers import kubernetes

# kind → provider module (ADR-003 §5). Order is topological: gcp before kubernetes, so a
# KSA's Workload-Identity reference to a GCP SA resolves.
PROVIDERS = {"gcp": gcp, "kubernetes": kubernetes}

# Union of subsystem names across providers — for --only validation + help text.
ALL_HANDLERS = [n for p in PROVIDERS.values() for n in p.HANDLERS]
ALL_PRUNERS = [n for p in PROVIDERS.values() for n in p.PRUNERS]


def targets_of(cfg):
    """The provider targets for a config. A config with explicit `targets:` is used as-is;
    a bare/legacy config (no `targets:`) auto-wraps to a single `kind: gcp` target that IS
    the whole config — so the three existing access-broker configs are unchanged (ADR-003
    §5, backwards-compatible)."""
    targets = cfg.get("targets")
    if targets:
        return [{**t, "kind": t.get("kind", "gcp")} for t in targets]
    return [{**cfg, "kind": "gcp"}]


def config_root():
    """Root that holds `projects/<key>/config.yaml`. Decouples the engine (this repo) from
    the CONFIGS (the caller's repo): $PROVISION_CONFIG_ROOT, else the current directory. As
    a composite action the caller's workspace supplies the configs while the engine code
    lives at $GITHUB_ACTION_PATH; run in-repo and the default (cwd) just works."""
    return os.environ.get("PROVISION_CONFIG_ROOT") or os.getcwd()


def run(project_key, apply=False, only=None, prune=False):
    core.DRY = not apply
    cfg_path = os.path.join(config_root(), "projects", project_key, "config.yaml")
    if not os.path.exists(cfg_path):
        sys.exit(f"no config at {cfg_path}")
    with open(cfg_path) as fh:
        cfg = yaml.safe_load(fh)

    mode = ("PRUNE " if prune else "") + ("APPLY" if apply else "DRY-RUN")
    for target in targets_of(cfg):
        provider = PROVIDERS[target["kind"]]
        ctx = provider.context(target)
        registry = provider.PRUNERS if prune else provider.HANDLERS
        print(f"{provider.label(project_key, target, ctx)}   mode: {mode}")
        if not provider.preflight(ctx):
            sys.exit(f"cannot access {target['kind']} target ({ctx}) — check auth/permissions")
        # --only names one subsystem; run it only on targets whose provider has it.
        for name in ([only] if only else list(registry)):
            if name not in registry:
                continue
            print()
            registry[name](target, ctx)

    tail = ("prune dry-run complete — no removals." if prune and core.DRY else
            "prune complete." if prune else
            "dry-run complete — no changes." if core.DRY else "apply complete.")
    print("\n" + tail)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("--apply", action="store_true", help="apply changes (default: dry-run)")
    ap.add_argument("--prune", action="store_true",
                    help="subtractive: REMOVE bindings/objects config no longer declares "
                         "(gcp: role bindings; kubernetes: managed netpol/rbac/ksa — never a namespace)")
    ap.add_argument("--only", help="single subsystem/pruner (add: %s; prune: %s)"
                    % (", ".join(ALL_HANDLERS), ", ".join(ALL_PRUNERS)))
    ap.add_argument("--config-root", help="root holding projects/<key>/config.yaml "
                    "(default: $PROVISION_CONFIG_ROOT or cwd)")
    args = ap.parse_args()
    if args.config_root:
        os.environ["PROVISION_CONFIG_ROOT"] = args.config_root
    valid = set(ALL_PRUNERS if args.prune else ALL_HANDLERS)
    if args.only and args.only not in valid:
        ap.error(f"--only {args.only!r} invalid for {'prune' if args.prune else 'add'} mode; "
                 f"choose from {sorted(valid)}")
    run(args.project, apply=args.apply, only=args.only, prune=args.prune)


if __name__ == "__main__":
    main()
