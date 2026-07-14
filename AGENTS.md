when need python, use `uv run python`

## Version Bump Workflow

- `jupyter-jcli` version must have a single source of truth: `[project].version` in `pyproject.toml`.
- Do not leave a hard-coded duplicate version in Python modules or docs.
- After bumping `[project].version`, run `uv lock` to refresh `uv.lock` so the editable `jupyter-jcli` entry matches the new release version; do not hand-edit `uv.lock`.
- When bumping a release version, update any user-facing docs that mention the current released version or version verification workflow.
- After a version bump, verify both:
  - `uv run python -m jupyter_jcli --version`
  - `j-cli --version`
- If a test asserts version output, make it compare against installed distribution metadata instead of a copied literal.

## Release Workflow

1. Start from a clean `main` branch synchronized with `origin/main`.
2. Choose the next semantic version. Update only `[project].version` in `pyproject.toml`, then run `uv lock`.
3. Run the local release checks:
   - `uv run python -m jupyter_jcli --version`
   - `j-cli --version`
   - `uv run pytest -v`
   - `uv build`
4. Confirm that both version commands report the new version and inspect the `pyproject.toml` and `uv.lock` diff.
5. Commit the bump using the established message format: `chore: bump version to X.Y.Z`.
6. Push the version commit to `main`, then wait for the GitHub Actions `Tests` workflow to succeed. Do not create the release tag before this workflow passes.
7. Create and push the lightweight tag matching the package version exactly:
   - `git tag vX.Y.Z`
   - `git push origin vX.Y.Z`
8. The tag triggers `.github/workflows/release.yml`, which builds the package, publishes it to PyPI with `PYPI_TOKEN`, and creates the GitHub Release with generated notes and `dist/*` assets. Do not create a second release manually.
9. Watch the `Release & Publish` workflow and verify the release after it succeeds:
   - `gh release view vX.Y.Z`
   - `uvx --from jupyter-jcli==X.Y.Z j-cli --version`
   - Confirm `https://pypi.org/project/jupyter-jcli/X.Y.Z/` is available.

The tag workflow does not run tests or verify that the tag and package versions match. Treat the successful `main` test run and the exact `vX.Y.Z` tag as mandatory release gates.
