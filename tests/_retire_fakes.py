"""Shared stubs for the SA-retirement tests (test_retire / test_sa_orphans / test_stray_roles).

Every cloud seam is replaced: `P.gout` (reads) is driven from a small model of a project, and
`P.gcloud` (mutations) records its argv. Nothing here can reach gcloud.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engine"))
import core  # noqa: E402
from providers import gcp as P  # noqa: E402

ORIG_GOUT, ORIG_GCLOUD = P.gout, P.gcloud
PROJECT = "p"


def email(name, project=PROJECT):
    return f"{name}@{project}.iam.gserviceaccount.com"


def model_gout(live=(), roles=None, extra_listed=()):
    """gout stub for one project.

    live: SA names that exist. roles: {sa name: [project roles it holds]}.
    extra_listed: raw emails that `service-accounts list` also returns (Google-managed etc.)."""
    roles = roles or {}
    by_email = {email(n): n for n in live}

    def gout(args, project=None):
        j = " ".join(args)
        if args[:2] == ["iam", "service-accounts"] and args[2] == "list":
            return "\n".join([email(n) for n in live] + list(extra_listed))
        if args[:2] == ["iam", "service-accounts"] and args[2] == "describe":
            return "x\t" + P._described("") if args[3] in by_email else ""
        if args[:2] == ["iam", "service-accounts"] and args[2] == "get-iam-policy":
            return json.dumps({"bindings": []}) if args[3] in by_email else ""
        if args[:2] == ["projects", "get-iam-policy"]:
            for em, n in by_email.items():
                if f"bindings.members=serviceAccount:{em}" in j:
                    return "\n".join(roles.get(n, []))
            return ""
        if args[:2] == ["projects", "describe"]:
            return "123"
        return ""

    return gout


class Recorder:
    """P.gcloud stub: records argv, never runs anything."""

    def __init__(self):
        self.calls = []

    def __call__(self, args, project=None, check=True):
        self.calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


def install(gout, dry=True):
    core.DRY = dry
    core.MUTATIONS = 0
    if hasattr(core, "STRAY"):
        core.STRAY = []
    P.gout = gout
    rec = Recorder()
    P.gcloud = rec
    return rec


def call(fn, *a, **k):
    """(stdout+stderr, SystemExit-or-None, return value). Never lets SystemExit escape."""
    buf, err = io.StringIO(), io.StringIO()
    exc, ret = None, None
    with redirect_stdout(buf), redirect_stderr(err):
        try:
            ret = fn(*a, **k)
        except SystemExit as e:
            exc = e
    return buf.getvalue() + err.getvalue(), exc, ret


def config_root(allow=("roles/run.developer",), cfg=None, key="k"):
    """A temp CONFIG ROOT (not the engine checkout) with its own grantable-roles.txt and,
    optionally, projects/<key>/config.yaml. Also exported as $PROVISION_CONFIG_ROOT."""
    import yaml
    scratch = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".scratch")
    os.makedirs(scratch, exist_ok=True)     # never the system tmp dir
    root = tempfile.mkdtemp(prefix="cfgroot-", dir=scratch)
    os.makedirs(os.path.join(root, "bootstrap"))
    with open(os.path.join(root, "bootstrap", "grantable-roles.txt"), "w") as fh:
        fh.write("# test allow-list\n" + "\n".join(allow) + "\n")
    if cfg is not None:
        os.makedirs(os.path.join(root, "projects", key))
        with open(os.path.join(root, "projects", key, "config.yaml"), "w") as fh:
            yaml.safe_dump({"project": {"gcp_id": PROJECT, "github_org": "o"}, **cfg}, fh)
    os.environ["PROVISION_CONFIG_ROOT"] = root
    return root


def deletes(rec):
    """Every recorded gcloud argv that deletes a service account."""
    return [a for a in rec.calls if a[:2] == ["iam", "service-accounts"] and "delete" in a]


def runner(globs):
    fns = [v for k, v in sorted(globs.items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except BaseException as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns)-failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
