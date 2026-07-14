"""Provisioning engine providers. One module per target `kind` (gcp, kubernetes, …).

Each provider supplies its own CLI seam plus `HANDLERS` (additive reconcile) and
`PRUNERS` (subtractive reconcile) registries, built on the provider-agnostic
primitives in `engine/core.py`. See ADR-003.
"""
