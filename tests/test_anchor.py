"""Bootstrap-anchor dry-run tests — stub the gcloud seam, assert what a DRY_RUN=1 run says.

No GCP access needed: a stub `gcloud` on PATH answers every read as "nothing exists yet",
so both anchors take their create/grant branches. In dry-run those branches must ANNOUNCE
(`~ would:`) and must NOT claim (`+`), and must invoke no mutation at all.

Run: python3 -m pytest tests/ -q   (or: python3 tests/test_anchor.py)
"""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Reads answer empty/absent so every branch is a would-create; anything else is a mutation
# and gets logged, which is how we assert dry-run touched nothing.
STUB = r"""#!/usr/bin/env bash
args="$*"
case "$args" in
  *"config get-value account"*)  echo tester@example.com; exit 0 ;;
  *"projects describe"*)         case "$args" in *projectNumber*) echo 123456789 ;; esac; exit 0 ;;
  *describe*)                    exit 1 ;;
  *"get-iam-policy"*|*"services list"*) exit 0 ;;
  *)                             echo "MUTATION: $args" >> "$STUB_LOG"; exit 0 ;;
esac
"""


def run_anchor(script, env_extra=None, dry="1"):
    """Run <script> with a stub gcloud; returns (stdout+stderr, logged mutations)."""
    with tempfile.TemporaryDirectory() as tmp:
        stub = os.path.join(tmp, "gcloud")
        with open(stub, "w") as fh:
            fh.write(STUB)
        os.chmod(stub, 0o755)
        log = os.path.join(tmp, "mutations.log")
        env = dict(os.environ, PATH=tmp + os.pathsep + os.environ["PATH"],
                   DRY_RUN=dry, STUB_LOG=log)
        env.update(env_extra or {})
        proc = subprocess.run(["bash", os.path.join(ROOT, "bootstrap", script)],
                              capture_output=True, text=True, env=env)
        mutations = open(log).read() if os.path.exists(log) else ""
    assert proc.returncode == 0, f"{script} exited {proc.returncode}\n{proc.stdout}{proc.stderr}"
    return proc.stdout + proc.stderr, mutations


ANCHORS = [("anchor.sh", {}), ("fleet-anchor.sh", {"HOST_PROJECT": "host-project"})]


def test_dry_run_never_claims_it_added_anything():
    """A `+` line is the vocabulary of a real run. In dry-run it reads as "changed" and is a lie."""
    for script, env in ANCHORS:
        out, _ = run_anchor(script, env)
        offenders = [ln for ln in out.splitlines() if "\033[33m+\033[0m" in ln]
        assert not offenders, f"{script} printed {len(offenders)} '+' line(s) in dry-run: {offenders[:3]}"


def test_dry_run_announces_every_would_change():
    """`do_or_dry ... >/dev/null` used to swallow the `~ would:` line — the redirect is the
    call site's, and it took the announcement with it. Nothing may be silently planned."""
    for script, env in ANCHORS:
        out, _ = run_anchor(script, env)
        would = [ln for ln in out.splitlines() if "would:" in ln]
        assert would, f"{script} planned nothing at all — the stub should make everything absent"
        for needle in ("roles create jgdSecretIamAdmin", "add-iam-policy-binding"):
            assert any(needle in ln for ln in would), \
                f"{script}: no `~ would:` line for '{needle}' (swallowed by a redirect?)"


def test_dry_run_mutates_nothing():
    for script, env in ANCHORS:
        _, mutations = run_anchor(script, env)
        assert not mutations, f"{script} invoked gcloud mutations in dry-run:\n{mutations}"


def test_a_real_run_still_claims_and_mutates():
    """The dry-run guard in `add` must not silence a REAL run — that would trade one
    misleading output for another."""
    for script, env in ANCHORS:
        out, mutations = run_anchor(script, env, dry="0")
        assert "\033[33m+\033[0m" in out, f"{script}: a real run printed no '+' line"
        assert "MUTATION:" in mutations, f"{script}: a real run invoked no gcloud mutation"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
