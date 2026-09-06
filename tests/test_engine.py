"""Engine plan tests — stub the gcloud seam, assert the computed plan.

No GCP access needed: we monkeypatch the gcp provider's gout (reads) and gcloud
(mutations), and core.DRY (the shared dry-run gate), then assert what the handlers
print in dry-run. Run: python3 -m pytest tests/ -q   (or: python3 tests/test_engine.py)

Scope: the gcp provider is the access/identity broker — three subsystems,
`service_accounts`, `act_as`, and `wif`. Resources (apis/secrets/pubsub/alerts) are
app-owned and have no handlers here (see DECISIONS 2026-07-01 + ADR-001). The
core/provider split is ADR-003.
"""
import io
import os
import sys
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engine"))
import core  # noqa: E402
from providers import gcp as P  # noqa: E402  (handlers + seam live in the gcp provider)

_ORIG_GOUT = P.gout   # drive() monkeypatches P.gout; keep the real one for gout tests


def drive(handler, cfg, gout_map, project="p"):
    """Run a handler in dry-run with gout stubbed from gout_map(args)->str."""
    core.DRY = True   # the dry-run gate lives in core (shared across providers)
    P.gout = lambda args, project=None: gout_map(args)
    P.gcloud = lambda *a, **k: (_ for _ in ()).throw(AssertionError("mutation in dry-run"))
    buf = io.StringIO()
    with redirect_stdout(buf):
        handler(cfg, project)
    return buf.getvalue()


def test_service_account_role_present_vs_missing():
    cfg = {"service_accounts": [{"name": "sa1", "roles": ["run.admin", "pubsub.admin"]}]}

    def gmap(args):
        j = " ".join(args)
        if "describe" in args:
            # displayName \x1f description, exactly as the engine asks gcloud to format it,
            # and already matching what the config wants -- so no metadata sync is planned.
            return "sa1\t" + P._described("")
        if "run.admin" in j:
            return "roles/run.admin"              # role present
        return ""                                  # pubsub.admin missing

    out = drive(P.ensure_service_accounts, cfg, gmap)
    assert "✓ sa sa1@p.iam.gserviceaccount.com" in out
    assert "✓   role roles/run.admin" in out
    assert "would   bind roles/pubsub.admin → sa1" in out


def test_service_account_created_when_missing():
    cfg = {"service_accounts": [{"name": "newsa", "roles": []}]}
    out = drive(P.ensure_service_accounts, cfg, lambda args: "")
    assert "would create sa newsa@p.iam.gserviceaccount.com" in out


def test_wif_binding_present_vs_missing():
    cfg = {
        "project": {"github_org": "AutoMahn", "gcp_number": "42"},
        "wif": {"pool": "github-actions", "provider": "github"},
        "service_accounts": [{"name": "rot", "wif_repos": ["AutoMahn/api", "AutoMahn/ui"]}],
    }

    def gmap(args):
        j = " ".join(args)
        if "workload-identity-pools" in args and "describe" in args:
            return "exists"                        # pool + provider exist
        if "AutoMahn/api" in j:
            return "roles/iam.workloadIdentityUser"  # api binding present
        return ""                                    # ui binding missing

    out = drive(P.ensure_wif, cfg, gmap)
    assert "✓ pool github-actions" in out
    assert "✓   wif AutoMahn/api → rot" in out
    assert "would   bind wif AutoMahn/ui → rot" in out


def test_wif_pool_and_provider_created_when_missing():
    cfg = {
        "project": {"github_org": "AutoMahn", "gcp_number": "42"},
        "wif": {"pool": "github-actions", "provider": "github"},
        "service_accounts": [],
    }
    out = drive(P.ensure_wif, cfg, lambda args: "")
    assert "would create pool github-actions" in out
    assert "would create provider github (org-scoped: AutoMahn)" in out


def test_wif_skipped_when_no_pool():
    cfg = {"project": {"github_org": "AutoMahn", "gcp_number": "42"}, "wif": {}, "service_accounts": []}
    out = drive(P.ensure_wif, cfg, lambda args: "")
    assert "no wif.pool configured" in out


def test_handlers_are_access_only():
    # Guard the scope: ONLY access subsystems are registered — no resource CRUD ever.
    # `resource_roles` (2026-08-23) binds roles on a single resource; it still grants
    # access and never creates a resource, so the scope line is unmoved. Adding an
    # entry here is a scope decision — it needs a DECISIONS entry, not just a commit.
    # `principals` (2026-09-06, ADR-008) binds a HUMAN/GROUP member on one service account.
    # Still access-only and still creates nothing, so the scope line is again unmoved — but it
    # is the first block whose member is a person, so it took a decision record of its own.
    assert set(P.HANDLERS) == {"service_accounts", "act_as", "resource_roles", "principals",
                               "wif"}
    assert set(P.PRUNERS) == {"service_accounts", "act_as", "resource_roles", "principals",
                              "provisioner"}


def test_handler_registry_includes_principals():
    """Order matters: `principals` binds ON a service account, so the SA handler must have
    run first. Dict order IS run order in provision.run()."""
    keys = list(P.HANDLERS)
    assert "principals" in keys and "principals" in P.PRUNERS
    assert keys.index("principals") > keys.index("service_accounts")


# ── act_as (SA→SA impersonation) ─────────────────────────────────────────────
def test_act_as_binding_present_vs_missing():
    cfg = {"service_accounts": [
        {"name": "releaser", "act_as": ["run-a", "run-b"]},
        {"name": "run-a"}, {"name": "run-b"},
    ]}

    def gmap(args):
        j = " ".join(args)
        # run-a already bound to releaser; run-b not yet
        return "roles/iam.serviceAccountUser" if "run-a@" in j else ""

    out = drive(P.ensure_act_as, cfg, gmap)
    assert "act_as releaser → run-a" in out
    assert "would bind act_as releaser → run-b" in out


def test_act_as_none_declared():
    cfg = {"service_accounts": [{"name": "run-a"}, {"name": "run-b"}]}
    out = drive(P.ensure_act_as, cfg, lambda args: "")
    assert "no act_as declared" in out


def test_prune_act_as_removes_own_undeclared_keeps_external():
    # releaser SHOULD act as run-a (wanted). Live on run-a: releaser (wanted, keep),
    # a stale owned SA `old` (undeclared → remove), and an external member (keep).
    cfg = {"service_accounts": [
        {"name": "releaser", "act_as": ["run-a"]},
        {"name": "run-a"}, {"name": "old"},
    ]}

    def gmap(args):
        j = " ".join(args)
        if "run-a@" in j:
            return ("serviceAccount:releaser@p.iam.gserviceaccount.com\n"
                    "serviceAccount:old@p.iam.gserviceaccount.com\n"
                    "serviceAccount:svc-123@gcp-sa-pubsub.iam.gserviceaccount.com")
        return ""

    out = drive(P.prune_act_as, cfg, gmap)
    assert "would unbind act_as old → run-a" in out          # owned + undeclared → pruned
    assert "unbind act_as releaser" not in out               # wanted → kept
    assert "svc-123" not in out                              # external/Google-managed → untouched


# ── gout fail-loud ───────────────────────────────────────────────────────────
def _raise_gcloud(stderr):
    import subprocess
    def boom(args, project=None, check=True):
        raise subprocess.CalledProcessError(1, ["gcloud"], stderr=stderr)
    return boom


def test_gout_absent_returns_empty():
    P.gout = _ORIG_GOUT
    P.gcloud = _raise_gcloud("ERROR: (gcloud...) NOT_FOUND: ... does not exist")
    assert P.gout(["iam", "service-accounts", "describe", "x"]) == ""


def test_gout_real_failure_is_loud():
    P.gout = _ORIG_GOUT
    P.gcloud = _raise_gcloud("ERROR: (gcloud...) PERMISSION_DENIED: caller lacks permission")
    try:
        P.gout(["projects", "get-iam-policy", "x"])
        raise AssertionError("expected SystemExit on non-absent failure")
    except SystemExit:
        pass


# ── prune (subtractive) ──────────────────────────────────────────────────────
def test_prune_service_accounts_removes_only_extra():
    cfg = {"service_accounts": [{"name": "sa1", "roles": ["run.admin"]}]}
    # live: the wanted role + one extra config no longer declares
    out = drive(P.prune_service_accounts, cfg,
                lambda args: "roles/run.admin\nroles/monitoring.editor")
    assert "would unbind roles/monitoring.editor from sa1" in out
    assert "unbind roles/run.admin" not in out          # wanted role is kept


def test_prune_service_accounts_noop_when_matching():
    cfg = {"service_accounts": [{"name": "sa1", "roles": ["run.admin"]}]}
    out = drive(P.prune_service_accounts, cfg, lambda args: "roles/run.admin")
    assert "no extra roles" in out
    assert "unbind" not in out


def test_prune_provisioner_keeps_kept_removes_rest():
    live = "\n".join([
        "roles/iam.serviceAccountAdmin",
        "roles/resourcemanager.projectIamAdmin",
        "roles/iam.workloadIdentityPoolAdmin",
        "roles/monitoring.editor",          # stale over-grant
        "roles/pubsub.editor",              # stale over-grant
    ])
    out = drive(P.prune_provisioner, {}, lambda args: live)
    assert "would unbind roles/monitoring.editor from infra-provisioner" in out
    assert "would unbind roles/pubsub.editor from infra-provisioner" in out
    assert "keep roles/resourcemanager.projectIamAdmin" in out
    # never removes projectIamAdmin — it is what lets the prune finish
    assert "unbind roles/resourcemanager.projectIamAdmin" not in out


def test_provisioner_kept_roles_reads_shared_file():
    assert P.provisioner_kept_roles("p") == {
        "roles/iam.serviceAccountAdmin",
        "roles/resourcemanager.projectIamAdmin",
        "roles/iam.workloadIdentityPoolAdmin",
        "projects/p/roles/jgdSecretIamAdmin",
    }


# ── mutation-path regressions (ported from the infra-provisioning consumer) ──
def _raise_on_call(stderr):
    """0-arg callable that fails the way a real `gcloud` mutation does."""
    import subprocess
    def boom():
        raise subprocess.CalledProcessError(1, ["gcloud"], stderr=stderr)
    return boom


def test_do_surfaces_gcloud_stderr_on_failure():
    """A failed mutation must exit with gcloud's stderr, not swallow it — `read`
    has been fail-loud since ADR-003; `do`/`undo` were not, so a rejected create
    printed a bare traceback-free '+ create sa …' and moved on."""
    core.DRY = False
    try:
        for op in (core.do, core.undo):
            try:
                op(_raise_on_call("ERROR: INVALID_ARGUMENT: display name too long"), "create sa x")
                raise AssertionError(f"expected SystemExit from core.{op.__name__}")
            except SystemExit as e:
                assert "INVALID_ARGUMENT" in str(e), str(e)
    finally:
        core.DRY = True


def test_service_account_display_name_truncated_by_bytes():
    """GCP's displayName limit is 100 BYTES, not characters. Truncating the
    description by character count let a multibyte description (e.g. an em-dash)
    overflow, and `service-accounts create` failed with INVALID_ARGUMENT."""
    desc = "\u2014" * 60                  # em-dash: 1 char, 3 bytes → 60 chars / 180 bytes
    cfg = {"service_accounts": [{"name": "sa1", "description": desc, "roles": []}]}
    seen = []
    core.DRY = False
    P.gout = lambda args, project=None: ""          # SA absent → take the create path
    P.gcloud = lambda args, project=None, check=True: seen.append(args)
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            P.ensure_service_accounts(cfg, "p")
    finally:
        core.DRY = True
    flag = next(a for a in seen[0] if a.startswith("--display-name="))
    value = flag.split("=", 1)[1]
    n = len(value.encode("utf-8"))
    assert n <= 100, f"--display-name is {n} bytes, over GCP's 100-byte limit"
    assert value == desc[:33], "expected 33 em-dashes (99 bytes); the 34th would overflow"



# ── resource_roles: resource-scoped IAM (ADR-005) ────────────────────────────
# The gap these close: the provider bound roles at PROJECT scope only, so a grant on a
# single secret or a single SA was invisible to the plan and unprunable — "zero drift"
# asserted nothing about it. `act_as` was already a resource-scoped binding; this
# generalises that shape to arbitrary (kind, resource, role) triples.

def test_resource_roles_secret_present_vs_missing():
    cfg = {"service_accounts": [{
        "name": "rotator",
        "resource_roles": {"secrets": {"app-secrets": ["secretmanager.admin",
                                                       "secretmanager.viewer"]}},
    }]}

    def gmap(args):
        if "describe" in args:
            return "exists"                                  # the secret exists
        # one policy read per resource (not one per role); admin bound, viewer missing
        return "roles/secretmanager.admin,serviceAccount:rotator@p.iam.gserviceaccount.com"

    out = drive(P.ensure_resource_roles, cfg, gmap)
    assert "✓   secret app-secrets: roles/secretmanager.admin" in out
    assert "would   bind roles/secretmanager.viewer on secret app-secrets → rotator" in out


def test_resource_roles_service_account_scope():
    """The narrowing case: serviceAccountAdmin on ONE SA instead of project-wide."""
    cfg = {"service_accounts": [{
        "name": "rotator",
        "resource_roles": {"service_accounts": {"api-run": ["iam.serviceAccountAdmin"]}},
    }]}
    out = drive(P.ensure_resource_roles, cfg,
                lambda args: "exists" if "describe" in args else "")
    assert "would   bind roles/iam.serviceAccountAdmin on service account api-run → rotator" in out


def test_resource_roles_absent_resource_fails_loud():
    """We BIND on resources, never CREATE them — an absent target is real drift, and must
    not be silently skipped (that would make a clean plan a lie)."""
    cfg = {"service_accounts": [{
        "name": "rotator",
        "resource_roles": {"secrets": {"gone": ["secretmanager.admin"]}},
    }]}
    try:
        drive(P.ensure_resource_roles, cfg, lambda args: "")
    except SystemExit as e:
        assert "gone" in str(e) and "app-owned" in str(e)
    else:
        raise AssertionError("expected a loud exit for an absent resource")


def test_resource_roles_none_declared_skips():
    out = drive(P.ensure_resource_roles, {"service_accounts": [{"name": "sa1"}]},
                lambda args: "exists")
    assert "no resource_roles declared" in out


def test_prune_resource_roles_removes_undeclared_owned_sa():
    cfg = {"service_accounts": [
        {"name": "rotator", "resource_roles": {"secrets": {"app-secrets": ["secretmanager.admin"]}}},
        {"name": "api-run"},
    ]}
    # live policy on app-secrets: rotator/admin (declared, keep), api-run/accessor (owned but
    # NOT declared → prune), a human and a foreign SA (unmanaged → never touched)
    # exactly the shape real gcloud emits, verified against auto-mahn's app-secrets:
    #   --flatten='bindings[].members' --format='csv[no-heading](bindings.role,bindings.members)'
    live = ("roles/secretmanager.admin,serviceAccount:rotator@p.iam.gserviceaccount.com\n"
            "roles/secretmanager.secretAccessor,serviceAccount:api-run@p.iam.gserviceaccount.com\n"
            "roles/secretmanager.viewer,user:someone@example.com\n"
            "roles/secretmanager.viewer,serviceAccount:other@elsewhere.iam.gserviceaccount.com")
    out = drive(P.prune_resource_roles, cfg, lambda args: live)
    assert "would unbind roles/secretmanager.secretAccessor on secret app-secrets from api-run" in out
    assert "secretmanager.admin" not in out.split("would unbind")[-1]   # declared → kept
    assert "someone@example.com" not in out                            # human → untouched
    assert "other@elsewhere" not in out                                # foreign SA → untouched


def test_prune_resource_roles_removes_deleted_principal():
    """A `deleted:` member references a principal that no longer exists — it can never be a
    legitimate grant, so it is pruned even though it is not an SA this config declares."""
    cfg = {"service_accounts": [{"name": "rotator",
                                 "resource_roles": {"secrets": {"app-secrets": []}}}]}
    live = ("roles/secretmanager.secretAccessor,deleted:serviceAccount:"
            "old-name@p.iam.gserviceaccount.com?uid=117781001628895155170")
    out = drive(P.prune_resource_roles, cfg, lambda args: live)
    assert "would unbind roles/secretmanager.secretAccessor on secret app-secrets from old-name" in out


def test_prune_resource_roles_nothing_to_do():
    cfg = {"service_accounts": [{"name": "rotator",
                                 "resource_roles": {"secrets": {"app-secrets": ["secretmanager.admin"]}}}]}
    live = "roles/secretmanager.admin,serviceAccount:rotator@p.iam.gserviceaccount.com"
    out = drive(P.prune_resource_roles, cfg, lambda args: live)
    assert "no extra resource_roles bindings" in out


def test_resource_roles_registered_in_both_registries():
    assert "resource_roles" in P.HANDLERS and "resource_roles" in P.PRUNERS


def test_prune_resource_roles_leaves_other_subsystems_alone():
    """An SA named under resource_roles is very often an act_as target too. Pruning its
    serviceAccountUser grants here would tear down impersonation that act_as declares and
    prune_act_as owns. Caught by a dry-run against a live project before any apply."""
    cfg = {"service_accounts": [
        {"name": "rotator", "act_as": ["api-run"],
         "resource_roles": {"service_accounts": {"api-run": ["iam.serviceAccountAdmin"]}}},
        {"name": "releaser", "act_as": ["api-run"]},
        {"name": "api-run"},
    ]}
    live = ("roles/iam.serviceAccountAdmin,serviceAccount:rotator@p.iam.gserviceaccount.com\n"
            "roles/iam.serviceAccountUser,serviceAccount:rotator@p.iam.gserviceaccount.com\n"
            "roles/iam.serviceAccountUser,serviceAccount:releaser@p.iam.gserviceaccount.com\n"
            "roles/iam.workloadIdentityUser,serviceAccount:releaser@p.iam.gserviceaccount.com")
    out = drive(P.prune_resource_roles, cfg, lambda args: live)
    assert "serviceAccountUser" not in out
    assert "workloadIdentityUser" not in out
    assert "no extra resource_roles bindings" in out


def test_prune_resource_roles_still_removes_deleted_holder_of_a_foreign_role():
    """The foreign-role exemption does NOT cover `deleted:` principals — no subsystem wants
    a dangling identity, and prune_act_as only ever touches members it owns."""
    cfg = {"service_accounts": [
        {"name": "rotator",
         "resource_roles": {"service_accounts": {"api-run": ["iam.serviceAccountAdmin"]}}},
        {"name": "api-run"},
    ]}
    live = ("roles/iam.serviceAccountUser,deleted:serviceAccount:"
            "old-releaser@p.iam.gserviceaccount.com?uid=112137767601029596304")
    out = drive(P.prune_resource_roles, cfg, lambda args: live)
    assert ("would unbind roles/iam.serviceAccountUser on service account api-run "
            "from old-releaser (deleted)") in out


def test_qualify_leaves_custom_roles_alone():
    """A custom role is already fully qualified. Prefixing it would produce
    `roles/projects/...`, which gcloud rejects only at apply time."""
    assert P._qualify("run.admin") == "roles/run.admin"
    assert P._qualify("roles/run.admin") == "roles/run.admin"
    assert P._qualify("projects/p/roles/jgdSecretIamAdmin") == "projects/p/roles/jgdSecretIamAdmin"
    assert P._qualify("organizations/1/roles/x") == "organizations/1/roles/x"


def test_provisioner_kept_roles_substitutes_the_project():
    """`{project}` marks a project-scoped custom role — the provisioner needs one to set IAM
    on a secret, since projectIamAdmin confers setIamPolicy on the PROJECT, not on a resource."""
    keep = P.provisioner_kept_roles("auto-mahn")
    assert "projects/auto-mahn/roles/jgdSecretIamAdmin" in keep
    assert not any("{project}" in r for r in keep)
    assert "roles/resourcemanager.projectIamAdmin" in keep


def test_prune_provisioner_keeps_the_custom_secret_role():
    """Live policy reports a custom role as `projects/<p>/roles/<id>`. If the kept-set did not
    substitute, prune would unbind the very role the anchor just granted — every run."""
    out = drive(P.prune_provisioner, {},
                lambda args: ("roles/resourcemanager.projectIamAdmin\n"
                              "projects/p/roles/jgdSecretIamAdmin\n"
                              "roles/editor"),
                project="p")
    assert "keep projects/p/roles/jgdSecretIamAdmin" in out
    assert "would unbind roles/editor" in out


# ── resource_roles: pub/sub + Artifact Registry kinds (ADR-005, extended) ────
# Secrets and SAs were the first two kinds; topics, subscriptions and GAR repositories are
# the rest of what this fleet actually grants on. Every one is a resource the APP repo owns
# and the broker only BINDS on, so they fit the existing table rather than needing new code
# paths. GAR is the odd one: a repository is not addressable by bare name — it needs a
# location — so its config name carries one (`<location>/<repo>`).

def test_resource_kinds_cover_pubsub_and_artifact_registry():
    assert set(P._RESOURCE_KINDS) == {"secrets", "service_accounts", "pubsub_topics",
                                      "pubsub_subscriptions", "artifact_repositories"}
    # every kind must declare a foreign-role set, or prune_resource_roles KeyErrors on it
    assert set(P._FOREIGN_ROLES) == set(P._RESOURCE_KINDS)


def test_resource_roles_pubsub_topic_scope():
    """The live case: `pubsub.publisher` on ONE topic, not project-wide `pubsub.admin`."""
    cfg = {"service_accounts": [{
        "name": "api-run",
        "resource_roles": {"pubsub_topics": {"booking.cancelled": ["pubsub.publisher"]}},
    }]}
    seen = []

    def gmap(args):
        seen.append(args)
        return "exists" if "describe" in args else ""

    out = drive(P.ensure_resource_roles, cfg, gmap)
    assert "would   bind roles/pubsub.publisher on topic booking.cancelled → api-run" in out
    assert ["pubsub", "topics", "describe", "booking.cancelled", "--project", "p"] == seen[0]


def test_resource_roles_pubsub_subscription_scope():
    cfg = {"service_accounts": [{
        "name": "eventworker",
        "resource_roles": {"pubsub_subscriptions": {"poke-sub": ["pubsub.subscriber"]}},
    }]}
    seen = []

    def gmap(args):
        seen.append(args)
        return "exists" if "describe" in args else ""

    out = drive(P.ensure_resource_roles, cfg, gmap)
    assert "would   bind roles/pubsub.subscriber on subscription poke-sub → eventworker" in out
    assert ["pubsub", "subscriptions", "describe", "poke-sub", "--project", "p"] == seen[0]


def test_resource_roles_artifact_repository_builds_a_qualified_ref():
    """A GAR repo is only addressable as projects/<p>/locations/<l>/repositories/<r>; the bare
    name fails argument parsing before it ever reaches the API. The fully-qualified form
    carries the project, so `--project` is not passed alongside it."""
    cfg = {"service_accounts": [{
        "name": "deployer",
        "resource_roles": {"artifact_repositories": {
            "asia-southeast1/backend": ["artifactregistry.writer"]}},
    }]}
    seen = []

    def gmap(args):
        seen.append(args)
        return "exists" if "describe" in args else ""

    out = drive(P.ensure_resource_roles, cfg, gmap)
    assert ("would   bind roles/artifactregistry.writer on repository "
            "asia-southeast1/backend → deployer") in out
    assert seen[0] == ["artifacts", "repositories", "describe",
                       "projects/p/locations/asia-southeast1/repositories/backend"]
    assert "--project" not in seen[0]


def test_resource_roles_artifact_repository_without_location_fails_loud():
    """`backend` alone is ambiguous. Guessing a location would bind IAM on the wrong repo (or
    on nothing), so this must fail at plan time, not at apply time."""
    cfg = {"service_accounts": [{
        "name": "deployer",
        "resource_roles": {"artifact_repositories": {"backend": ["artifactregistry.writer"]}},
    }]}
    try:
        drive(P.ensure_resource_roles, cfg, lambda args: "exists")
    except SystemExit as e:
        assert "location" in str(e) and "backend" in str(e)
    else:
        raise AssertionError("expected a loud exit for a repository with no location")


def test_prune_resource_roles_removes_a_deleted_principal_from_a_topic():
    """Live on auto-mahn 2026-08-24: `automahn.outbox.poke` still carried pubsub.publisher for
    `automahn-api-run`, the pre-rename SA. Project-scope-only pruning could never see it."""
    cfg = {"service_accounts": [
        {"name": "api-run",
         "resource_roles": {"pubsub_topics": {"automahn.outbox.poke": ["pubsub.publisher"]}}},
    ]}
    live = ("roles/pubsub.publisher,deleted:serviceAccount:automahn-api-run@p.iam."
            "gserviceaccount.com?uid=117781001628895155170\n"
            "roles/pubsub.publisher,serviceAccount:api-run@p.iam.gserviceaccount.com")
    out = drive(P.prune_resource_roles, cfg, lambda args: live)
    assert "would unbind roles/pubsub.publisher on topic automahn.outbox.poke" in out
    assert "automahn-api-run (deleted)" in out
    assert out.count("unbind") == 1, "the live, declared api-run binding must be kept"


def test_resource_policy_ignores_an_empty_policy():
    """A resource with no bindings prints a bare `,` under this flatten+csv pair (seen on both
    GAR repos and on an unbound subscription). Split naively that becomes a ('', '') pair."""
    P.gout = lambda args, project=None: ","
    assert P.resource_policy("secrets", "s", "p") == []


# This block MUST stay at the very END of the file: it snapshots globals() at the moment it
# runs, so any test defined below it is silently never collected. It sat mid-file until
# 2026-08-24, hiding 20 of 37 tests — every resource_roles test among them — behind a green
# "17/17 passed". See DECISIONS 2026-08-24.


# ── fleet provisioner: the scope audit ───────────────────────────────────────
# A fleet-anchored provisioner's predefined roles live at an org/folder node, NOT on the
# project. prune_provisioner reads PROJECT bindings, so without the audit it would read a
# policy that legitimately contains almost nothing and print a clean run while policing
# nothing. These guard that it looks at the scope, and that it never mutates there.
FLEET_CFG = {"provisioner": {
    "service_account": "infra-provisioner-fleet@jgd-admin.iam.gserviceaccount.com",
    "scope": "organizations/250926570441"}}


def test_provisioner_identity_defaults_to_the_per_project_anchor():
    email, scope = P.provisioner_identity({}, "p")
    assert email == "infra-provisioner@p.iam.gserviceaccount.com", email
    assert scope is None, scope


def test_provisioner_identity_reads_the_fleet_block():
    email, scope = P.provisioner_identity(FLEET_CFG, "p")
    assert email == "infra-provisioner-fleet@jgd-admin.iam.gserviceaccount.com", email
    assert scope == "organizations/250926570441", scope


def _fleet_gmap(scope_roles):
    def gmap(args):
        j = " ".join(args)
        if "organizations" in j or "folders" in j:
            return "\n".join(scope_roles)
        return ""      # no project-level roles: the normal fleet shape
    return gmap


def test_scope_audit_reports_kept_roles_and_does_not_prune_them():
    out = drive(P.prune_provisioner, FLEET_CFG, _fleet_gmap([
        "roles/iam.serviceAccountAdmin",
        "roles/resourcemanager.projectIamAdmin",
        "roles/iam.workloadIdentityPoolAdmin",
        "organizations/250926570441/roles/jgdScopeIamViewer"]))
    assert "audit provisioner grants at organizations/250926570441" in out, out
    assert "keep roles/resourcemanager.projectIamAdmin" in out, out
    assert "jgdScopeIamViewer" in out, out
    # an empty project policy is EXPECTED here and must not read as a skip/failure
    assert "no project-level roles (expected" in out, out


def test_scope_audit_flags_an_undeclared_role_and_refuses_to_remove_it():
    try:
        out = drive(P.prune_provisioner, FLEET_CFG, _fleet_gmap([
            "roles/iam.serviceAccountAdmin",
            "roles/owner"]))
    except SystemExit as e:
        assert "undeclared" in str(e), e
    else:
        raise AssertionError("undeclared scope role did not fail the run:\n" + out)


def test_scope_audit_treats_an_empty_scope_policy_as_an_error_not_as_clean():
    """The SA is supposed to hold roles here. Empty means wrong scope or a vanished grant --
    the one thing it must not do is print nothing and look fine, so it FAILS the run and
    says why (not "undeclared role", which is a different problem)."""
    try:
        out = drive(P.prune_provisioner, FLEET_CFG, _fleet_gmap([]))
    except SystemExit as e:
        assert "no roles bound" in str(e), e
    else:
        raise AssertionError("an empty scope policy passed as clean:\n" + out)


def test_per_project_provisioner_is_unchanged_by_the_fleet_support():
    live = ["roles/iam.serviceAccountAdmin", "roles/editor"]
    out = drive(P.prune_provisioner, {}, lambda args: "\n".join(live))
    assert "keep roles/iam.serviceAccountAdmin" in out, out
    assert "roles/editor" in out, out
    assert "audit provisioner grants" not in out, "no scope configured; must not audit"


def test_one_role_per_set_iam_policy_call():
    """LOAD-BEARING FOR THE IAM CONDITION, not a style preference.

    The fleet provisioner's projectIamAdmin binding is constrained by
    `modifiedGrantsByRole ... hasOnly([...])`, capped at 10 roles. More than 10 roles means
    several conditional BINDINGS, and a setIamPolicy call touching roles from two groups
    satisfies NEITHER hasOnly() and is denied. Binding one role per call is what keeps that
    from happening -- batch them and provisioning breaks with a permission error that looks
    nothing like its cause. See bootstrap/grantable-roles.txt.
    """
    cfg = {"service_accounts": [{"name": "sa1",
                                 "roles": ["run.developer", "pubsub.admin", "logging.logWriter"]}]}
    calls = []
    core.DRY = False
    P.gout = lambda args, project=None: "exists" if "describe" in args else ""
    P.gcloud = lambda args, project=None, check=True: calls.append(args) or _Ok()
    buf = io.StringIO()
    with redirect_stdout(buf):
        P.ensure_service_accounts(cfg, "p")
    core.DRY = True
    binds = [a for a in calls if "add-iam-policy-binding" in a]
    assert len(binds) == 3, "expected one binding call per role, got %d" % len(binds)
    for a in binds:
        roles = [x for x in a if x.startswith("--role=")]
        assert len(roles) == 1, "a single call bound %d roles: %s" % (len(roles), roles)
    assert not any("set-iam-policy" in " ".join(a) for a in calls), (
        "set-iam-policy replaces a whole policy document, which modifies many roles in one "
        "call and cannot satisfy a hasOnly() condition")


class _Ok:
    returncode = 0
    stdout = ""
    stderr = ""


def _capture(handler, cfg, gout_map, project="p"):
    """Run a handler for REAL (not dry-run) against stubs, returning the gcloud argv lists.
    Dry-run prints only the human label, never the args, so anything asserting on FLAGS
    (metadata, conditions) has to inspect the calls themselves."""
    calls = []
    core.DRY = False
    P.gout = lambda args, project=None: gout_map(args)
    P.gcloud = lambda args, project=None, check=True: calls.append(args) or _Ok()
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            handler(cfg, project)
    finally:
        core.DRY = True
    return calls


def test_service_account_metadata_is_reconciled_not_only_created():
    """Metadata used to be written at create ONLY, so editing a config's `description:`
    never reached GCP: an audit found six SAs still carrying their bare id as displayName
    while the config described them properly. An existing SA whose metadata has drifted
    must be UPDATED, not reported ok."""
    cfg = {"service_accounts": [{"name": "sa1", "description": "CI releaser (owner: platform)",
                                 "roles": []}]}
    calls = _capture(P.ensure_service_accounts, cfg,
                     lambda args: "sa1\t" if "describe" in args else "")
    upd = [a for a in calls if "update" in a]
    assert upd, "drifted metadata was not updated: %s" % calls
    flat = " ".join(upd[0])
    assert "CI releaser (owner: platform)" in flat, flat
    assert P.PROVENANCE in flat, flat


def test_every_created_resource_carries_provenance():
    """A console reader must be able to tell what manages a resource. Audited 2026-08-30:
    provenance was determinable for ZERO resources in the fleet."""
    sa = _capture(P.ensure_service_accounts,
                  {"service_accounts": [{"name": "newsa", "roles": []}]}, lambda args: "")
    wif = _capture(P.ensure_wif,
                   {"project": {"github_org": "AutoMahn", "gcp_number": "42"},
                    "wif": {"pool": "github-actions", "provider": "github"}},
                   lambda args: "")
    creates = [a for a in sa + wif if "create" in a or "create-oidc" in a]
    assert creates, "nothing was created"
    for a in creates:
        flat = " ".join(a)
        assert P.PROVENANCE in flat, "created without provenance: %s" % flat
        assert "--display-name=" in flat, "created without a displayName: %s" % flat


def test_description_is_clipped_on_bytes_not_characters():
    """256-byte limit. A raw [:n] on a multibyte string fails create with INVALID_ARGUMENT."""
    long_purpose = "\u2014" * 300          # em-dashes: 3 bytes each
    got = P._described(long_purpose)
    assert len(got.encode("utf-8")) <= P._DESCRIPTION_MAX, len(got.encode("utf-8"))
    got.encode("utf-8").decode("utf-8")   # must not be a partial trailing char


def test_metadata_reconcile_converges():
    """The first version of this compared against a custom `delimiter=` that gcloud silently
    IGNORES (it emits a TAB), so have_desc was always empty, every SA compared as drifted, and
    an apply rewrote identical metadata forever. A reconcile that never converges is a bug even
    when every write is correct -- so assert the SETTLED state plans nothing."""
    cfg = {"service_accounts": [{"name": "sa1", "description": "CI releaser (owner: platform)",
                                 "roles": []}]}
    settled = "%s\t%s" % (P._clip("CI releaser (owner: platform)", P._DISPLAY_NAME_MAX),
                          P._described("CI releaser (owner: platform)"))
    calls = _capture(P.ensure_service_accounts, cfg,
                     lambda args: settled if "describe" in args else "")
    assert not [a for a in calls if "update" in a], \
        "already-correct metadata still planned an update: %s" % calls


def test_metadata_reconcile_tolerates_a_trailing_space_from_clipping():
    """A byte-clip landing on a space leaves a trailing space that GCP trims on write; without
    stripping both sides that also never converges."""
    purpose = "x" * 99 + " tail"
    cfg = {"service_accounts": [{"name": "sa1", "description": purpose, "roles": []}]}
    settled = "%s\t%s" % (P._clip(purpose, P._DISPLAY_NAME_MAX).strip(), P._described(purpose))
    calls = _capture(P.ensure_service_accounts, cfg,
                     lambda args: settled if "describe" in args else "")
    assert not [a for a in calls if "update" in a], \
        "trailing-space clip planned a needless update: %s" % calls


def _drive_run(tmpdir, plan, prune=False, apply=False):
    """Run provision.run() end-to-end against a stub provider, and return its full stdout.

    The trailer is only observable from a whole run, so these tests drive the CLI
    orchestration rather than a single handler. `plan` is a 0-arg callable that stands in
    for a subsystem: it calls core.do/core.undo to declare what the run would change.
    """
    import provision

    os.makedirs(os.path.join(tmpdir, "projects", "p"), exist_ok=True)
    with open(os.path.join(tmpdir, "projects", "p", "config.yaml"), "w") as fh:
        fh.write("project_id: stub\n")

    class _Stub:
        HANDLERS = {"sub": lambda target, ctx: plan()}
        PRUNERS = {"sub": lambda target, ctx: plan()}
        context = staticmethod(lambda target: "ctx")
        preflight = staticmethod(lambda ctx: True)
        label = staticmethod(lambda key, target, ctx: f"project: {key}")

    orig_providers, orig_root = provision.PROVIDERS, os.environ.get("PROVISION_CONFIG_ROOT")
    provision.PROVIDERS = {"gcp": _Stub}
    os.environ["PROVISION_CONFIG_ROOT"] = tmpdir
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            provision.run("p", apply=apply, prune=prune)
    finally:
        provision.PROVIDERS = orig_providers
        os.environ.pop("PROVISION_CONFIG_ROOT", None)
        if orig_root is not None:
            os.environ["PROVISION_CONFIG_ROOT"] = orig_root
        core.DRY = True
    return buf.getvalue()


def test_prune_dry_run_trailer_reports_the_removals_it_planned():
    """REGRESSION: the trailer said 'no removals' while the body planned one.

    Observed on infra-provisioning run 33943386538 — the plan printed
    `~ would unbind roles/artifactregistry.repoAdmin from github-cleaner` and the run still
    closed with 'prune dry-run complete — no removals.' A reader trusting the summary would
    conclude the prune was a no-op and skip it.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        out = _drive_run(td, lambda: core.undo(lambda: None, "unbind roles/x from sa"), prune=True)
    assert "would unbind roles/x" in out, out
    assert "no removals" not in out, \
        "trailer claims no removals while the body planned one:\n%s" % out
    assert "1 removal" in out, out


def test_dry_run_trailer_reports_the_changes_it_planned():
    """Same defect on the additive branch: 'dry-run complete — no changes.' was printed
    unconditionally, including on the run that planned the artifactregistry.admin bind."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        out = _drive_run(td, lambda: core.do(lambda: None, "bind roles/x to sa"))
    assert "would bind roles/x" in out, out
    assert "no changes" not in out, \
        "trailer claims no changes while the body planned one:\n%s" % out
    assert "1 change" in out, out


def test_trailers_still_say_none_when_nothing_is_planned():
    """The zero case must keep reading as zero — the fix must not invert the bug."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        assert "no removals" in _drive_run(td, lambda: None, prune=True)
        assert "no changes" in _drive_run(td, lambda: None)


def test_mutation_count_does_not_leak_between_runs():
    """The counter is module state, so a second run must not inherit the first one's total."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        _drive_run(td, lambda: core.do(lambda: None, "bind roles/x to sa"))
        second = _drive_run(td, lambda: None)
    assert "no changes" in second, "count leaked into the next run:\n%s" % second


# ── principals: a HUMAN/GROUP member on one service account (ADR-008) ────────
# The gap these close: every member the provider built was `serviceAccount:{email}`, so a
# human could be granted nothing — which is how `log-reader` shipped as three SAs nobody
# could impersonate (PERMISSION_DENIED on iam.serviceAccounts.getAccessToken, 2026-09-06).
# Narrow by construction: kind `service_accounts` only, role must be allow-listed, member
# must carry a `user:`/`group:` prefix. IAM does NOT enforce any of that — the file and the
# reviewer do (modifiedGrantsByRole covers project/folder/org policies, not SA policies).

_TOKEN_CREATOR = "roles/iam.serviceAccountTokenCreator"


def _principals_cfg(member="user:someone@example.com", roles=("iam.serviceAccountTokenCreator",),
                    kind="service_accounts", resource="log-reader"):
    return {
        "service_accounts": [{"name": "log-reader", "roles": ["logging.viewer"]}],
        "principals": [{"member": member,
                        "resource_roles": {kind: {resource: list(roles)}}}],
    }


def test_principals_binds_missing_role():
    out = drive(P.ensure_principals, _principals_cfg(),
                lambda args: "exists" if "describe" in args else "")
    assert (f"would   bind {_TOKEN_CREATOR} on service account log-reader "
            f"→ user:someone@example.com") in out


def test_principals_idempotent_when_present():
    def gmap(args):
        if "describe" in args:
            return "exists"
        return f"{_TOKEN_CREATOR},user:someone@example.com"

    out = drive(P.ensure_principals, _principals_cfg(), gmap)
    assert f"✓   service account log-reader: {_TOKEN_CREATOR}" in out
    assert "would" not in out


def test_principals_rejects_non_allowlisted_role():
    """The allow-list is the ONLY mechanical control here, so it must fail at plan time."""
    cfg = _principals_cfg(roles=("owner",))
    try:
        drive(P.ensure_principals, cfg, lambda args: "exists")
    except SystemExit as e:
        assert "roles/owner" in str(e) and "principal-grantable-roles.txt" in str(e)
    else:
        raise AssertionError("expected a loud exit for a role outside the allow-list")


def test_principals_rejects_service_account_member():
    cfg = _principals_cfg(member="serviceAccount:rotator@p.iam.gserviceaccount.com")
    try:
        drive(P.ensure_principals, cfg, lambda args: "exists")
    except SystemExit as e:
        assert "resource_roles" in str(e)
    else:
        raise AssertionError("expected a loud exit for a serviceAccount: member")


def test_principals_rejects_bare_member():
    """A bare string is passed to gcloud verbatim, which would bind something unintended."""
    cfg = _principals_cfg(member="someone@example.com")
    try:
        drive(P.ensure_principals, cfg, lambda args: "exists")
    except SystemExit as e:
        assert "user:" in str(e) and "group:" in str(e)
    else:
        raise AssertionError("expected a loud exit for a member with no principal prefix")


def test_principals_rejects_unknown_kind():
    cfg = _principals_cfg(kind="secrets", resource="app-secrets")
    try:
        drive(P.ensure_principals, cfg, lambda args: "exists")
    except SystemExit as e:
        assert "service_accounts" in str(e)
    else:
        raise AssertionError("expected a loud exit for a kind other than service_accounts")


def test_principals_absent_target_sa_fails_loud():
    """Mirrors ensure_resource_roles: we BIND, never create — and a silent skip would make
    a clean plan a lie about access that does not exist."""
    try:
        drive(P.ensure_principals, _principals_cfg(), lambda args: "")
    except SystemExit as e:
        assert "log-reader" in str(e)
    else:
        raise AssertionError("expected a loud exit for an absent service account")


def test_principals_none_declared_skips():
    out = drive(P.ensure_principals, {"service_accounts": [{"name": "sa1"}]},
                lambda args: "exists")
    assert "no principals declared" in out


def test_prune_principals_removes_undeclared():
    """Only a member AND resource the config declares: the human is declared and the SA is
    declared, but this particular role is not, so it goes."""
    cfg = _principals_cfg()
    live = (f"{_TOKEN_CREATOR},user:someone@example.com\n"
            "roles/iam.serviceAccountUser,user:someone@example.com")
    out = drive(P.prune_principals, cfg, lambda args: live)
    assert ("would unbind roles/iam.serviceAccountUser on service account log-reader "
            "from user:someone@example.com") in out
    assert _TOKEN_CREATOR not in out.split("would unbind")[-1]   # declared → kept


def test_prune_principals_leaves_foreign_human_bindings():
    """The important negative. Two human subjects so it cannot pass vacuously: the declared
    member's undeclared role is pruned, a DIFFERENT person's binding on the same SA survives.
    Service-account members are prune_resource_roles'/prune_act_as' business, not this one's."""
    cfg = _principals_cfg()
    live = (f"{_TOKEN_CREATOR},user:someone@example.com\n"
            "roles/iam.serviceAccountUser,user:someone@example.com\n"
            f"{_TOKEN_CREATOR},user:stranger@example.com\n"
            "roles/iam.serviceAccountUser,serviceAccount:rotator@p.iam.gserviceaccount.com")
    out = drive(P.prune_principals, cfg, lambda args: live)
    assert ("would unbind roles/iam.serviceAccountUser on service account log-reader "
            "from user:someone@example.com") in out
    assert "stranger@example.com" not in out          # another human's grant → untouched
    assert "rotator@" not in out                      # an SA member → another pruner's job


def test_prune_principals_none_declared_skips():
    out = drive(P.prune_principals, {"service_accounts": [{"name": "sa1"}]},
                lambda args: "")
    assert "no principals declared" in out


def test_principal_grantable_roles_is_seeded_with_exactly_token_creator():
    """Widening this file is the whole risk surface — IAM will not stop a bad entry."""
    assert P.principal_grantable_roles() == {_TOKEN_CREATOR}


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
        except BaseException as e:                 # SystemExit included: a handler that bails
            failed += 1                            # must fail ONE test, not abort the suite
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns)-failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
