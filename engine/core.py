#!/usr/bin/env python3
"""Provisioning engine — provider-agnostic reconcile discipline.

This module holds the parts of the engine that do NOT know about any specific
target (GCP, Kubernetes, …): the dry-run gate, the fail-loud read, and the plan
printer. Providers (`engine/providers/<kind>.py`) supply the CLI seam and the
resource handlers; they reuse these primitives so every provider inherits the
same guarantees:

  - Dry-run is the default and is ACCURATE — existence is checked, only mutations
    are withheld, so a `~ would` line is a real change an apply would make.
    Zero `~ would` = zero drift = parity proven.
  - Reads fail loud — a non-zero result that is NOT a recognised 'absent' signal
    (auth/permission/transient) is re-raised as a hard exit, never masqueraded as
    '' (which would produce a phantom create / non-idempotent apply).

See ADR-003 (multi-provider engine) for the core/provider split.
"""
import os
import subprocess
import sys

# Repo root (engine/core.py → engine → repo root). Providers read shared files
# (e.g. bootstrap/provisioner-roles.txt) relative to this.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DRY = True          # set by the CLI (provision.py): DRY = not --apply
_TTY = sys.stdout.isatty()

# Mutations REPORTED this run — planned ones in dry-run, performed ones under --apply.
# `do`/`undo` are the only places a mutation is ever printed, so counting here is what
# keeps the closing summary consistent with the plan above it by construction. Reset by
# provision.run(); a caller driving the engine in-process must reset it too.
MUTATIONS = 0


def c(tag, msg):
    col = {"ok": "32", "add": "33", "dry": "36", "err": "31", "skip": "90", "del": "35"}.get(tag, "0")
    sym = {"ok": "✓", "add": "+", "dry": "~", "err": "✗", "skip": "·", "del": "-"}.get(tag, "-")
    print(f"  \033[{col}m{sym}\033[0m {msg}" if _TTY else f"  {sym} {msg}")


def do(run, describe):
    """Mutating (additive) op — withheld in dry-run. `run` is a 0-arg callable
    that performs the mutation (a provider closes over its own CLI call)."""
    global MUTATIONS
    if DRY:
        MUTATIONS += 1
        c("dry", f"would {describe}")
        return
    try:
        run()
    except subprocess.CalledProcessError as e:
        sys.exit(f"failed to {describe}:\n{(e.stderr or '').strip()}")
    MUTATIONS += 1
    c("add", describe)


def undo(run, describe):
    """Destructive (removal) op — withheld in dry-run. Used by --prune."""
    global MUTATIONS
    if DRY:
        MUTATIONS += 1
        c("dry", f"would {describe}")
        return
    try:
        run()
    except subprocess.CalledProcessError as e:
        sys.exit(f"failed to {describe}:\n{(e.stderr or '').strip()}")
    MUTATIONS += 1
    c("del", describe)


def read(run, absent_markers, label=""):
    """Fail-loud read. `run` returns stdout (str) or raises CalledProcessError.

    '' means the resource is ABSENT (an expected result). A non-zero exit whose
    stderr contains none of `absent_markers` (e.g. PERMISSION_DENIED, a transient
    error) is re-raised as a hard exit — it must not masquerade as ''."""
    try:
        return run().strip()
    except subprocess.CalledProcessError as e:
        err = e.stderr or ""
        if any(m in err for m in absent_markers):
            return ""
        sys.exit(f"read failed (not an 'absent' result): {label}\n{err.strip()}")
