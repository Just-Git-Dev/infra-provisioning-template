#!/usr/bin/env python3
"""Reject `uses: owner/repo@ref` where ref is not a 40-char hex SHA, under a workflows dir.

This was an inline `grep -rnP` in ci.yml. BSD grep (macOS) has no -P, so the same line run
locally errored and the step still reported success; a regex in Python behaves identically
on every machine. Local (`./...`) refs are excluded by `[^./]`. Prints how many files and
`uses:` lines it examined and fails when it examined none.

Each argument is a directory (scanned for *.yml / *.yaml) or a single file (action.yml).

Usage: check_pinned_actions.py [PATH ...]   (default .github/workflows)
"""
import pathlib
import re
import sys

USES = re.compile(r'^\s*(-\s*)?uses:\s*([^./][^@]+)@(?P<ref>\S*)')
SHA = re.compile(r'[0-9a-f]{40}\s*($|#)')


def main(argv):
    roots = [pathlib.Path(a) for a in (argv[1:] or [".github/workflows"])]
    missing = [str(r) for r in roots if not r.exists()]
    if missing:
        print(f"::error::path(s) given to check_pinned_actions do not exist: {' '.join(missing)}")
        return 1
    files = []
    for root in roots:
        files += [root] if root.is_file() else sorted(list(root.rglob("*.yml")) + list(root.rglob("*.yaml")))
    bad, uses = [], 0
    for f in files:
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            m = USES.match(line)
            if not m:
                continue
            uses += 1
            if not SHA.match(line[m.start("ref"):]):
                bad.append(f"{f}:{n}:{line.strip()}")
    if not files or not uses:
        print(f"::error::examined {len(files)} files / {uses} uses: lines under {roots}; expected some")
        return 1
    if bad:
        print("\n".join(bad))
        print("::error::Third-party actions must be pinned to a full commit SHA.")
        return 1
    print(f"All third-party action references are SHA-pinned (examined {uses} uses: lines in {len(files)} files).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
