# Recorded gcloud output (read-only, 2026-10-21; org id and account replaced with placeholders)

- `role_describe_org_exists.out` — `gcloud iam roles describe repoAdmin --organization=123456789012 '--format=value(name,stage,deleted)'` (stdout, rc 0)
- `role_describe_org_missing.err` — `gcloud iam roles describe noSuchRoleXyz --organization=123456789012 '--format=value(name,stage,deleted)'` (stderr, rc 1, NOT_FOUND)
- `role_describe_denied.err` — `gcloud iam roles describe repoAdmin --project=no-such-proj-zz9 '--format=value(name,stage,deleted)'` (stderr, rc 1, PERMISSION_DENIED)

Not recorded: a DELETED role (none exists to record); the engine's handling of one is unverified.
