#!/usr/bin/env python3
"""Extract action.yml's `run:` bodies into one bash script, for shellcheck.

actionlint shellchecks every `run:` body in a *workflow*, but it does not read
`action.yml` — so the composite action this repo actually ships, the step that invokes
the engine as a provisioner SA, had nothing checking its shell at all.

Each body is wrapped in a subshell so one step's `set -e` or variable does not leak into
the next, matching how the runner executes them (a separate shell per step).

GitHub expression syntax is not shell: shellcheck reads `<dollar>{{ inputs.x }}` as a
malformed parameter expansion (SC2296). We substitute a placeholder token first, which is
what actionlint does before shellchecking a workflow body.
"""
import re
import sys

import yaml

GH_EXPR = re.compile(r"\$\{\{[^}]*\}\}")


def main(path="action.yml"):
    action = yaml.safe_load(open(path))
    out = ["#!/bin/bash"]
    for step in action.get("runs", {}).get("steps", []):
        body = step.get("run")
        if body:
            out.append("(\n" + GH_EXPR.sub("GHEXPR", body) + "\n)")
    print("\n".join(out))


if __name__ == "__main__":
    main(*sys.argv[1:])
