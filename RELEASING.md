# Releasing

Releases use PyPI Trusted Publishing. No long-lived PyPI token belongs in GitHub.

## One-time PyPI setup

Create a pending GitHub publisher for the `aimlib` project with these exact values:

- Owner: `rukouai`
- Repository: `aimlib-python`
- Workflow: `release.yml`
- Environment: `pypi`

The pending publisher does not reserve the project name. Publish the first release immediately after
the binding is created.

## Release process

1. Update the version in `pyproject.toml` and add dated notes to `CHANGELOG.md`.
2. Merge the reviewed change to `main` and confirm CI is green.
3. Create and publish a GitHub Release with tag `vX.Y.Z` from the intended `main` commit.
4. Approve the protected `pypi` environment deployment.
5. Verify the version, project links, provenance, wheel, and source archive on PyPI.

The release workflow rejects a tag that does not exactly match the package version. Build jobs do
not receive an OIDC token; only the final publish job has `id-token: write`, and it publishes the
artifacts produced by the preceding job.

PyPI versions and files are immutable. Never replace an uploaded artifact or reuse a released
version number.
