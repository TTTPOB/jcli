when need python, use `uv run python`

## Version Bump Workflow

- `jupyter-jcli` version must have a single source of truth: `[project].version` in `pyproject.toml`.
- Do not leave a hard-coded duplicate version in Python modules or docs.
- After bumping `[project].version`, run `uv lock` to refresh `uv.lock` so the editable `jupyter-jcli` entry matches the new release version; do not hand-edit `uv.lock`.
- When bumping a release version, update user-facing docs only when they mention the current released version or version verification workflow.
- If a test asserts version output, make it compare against installed distribution metadata instead of a copied literal.

## Release Workflow

1. Start from `main`, fetch `origin/main`, and require a clean worktree. If this release's code or docs commits are local, keep them unpushed so the version commit can share their push.
2. Choose the next semantic version. Update only `[project].version` in `pyproject.toml`, then run `uv lock`.
3. Run the local release checks:
   - `uv run python -m jupyter_jcli --version`
   - `uv run j-cli --version`
   - `uv run pytest -v`
   - `uv build`
4. Confirm that both version commands report the new version and inspect the `pyproject.toml` and `uv.lock` diff.
5. Commit the bump using the established message format: `chore: bump version to X.Y.Z`.
6. Push all pending release commits to `main` once, including any unpushed code, docs, and version commits. Wait for the resulting GitHub Actions `Tests` workflow to succeed. Do not create the release tag before this workflow passes.
7. Create and push the lightweight tag matching the package version exactly:
   - `git tag vX.Y.Z`
   - `git push origin vX.Y.Z`
8. The tag triggers `.github/workflows/release.yml`, which builds the package, publishes it to PyPI with `PYPI_TOKEN`, and creates the GitHub Release with generated notes and `dist/*` assets. Do not create a second release manually.
9. Watch the `Release & Publish` workflow and verify the release after it succeeds:
   - `gh release view vX.Y.Z`
   - Confirm `https://pypi.org/project/jupyter-jcli/X.Y.Z/` is available.
   - no need for local install test.

The tag workflow does not run tests or verify that the tag and package versions match. Treat the successful `main` test run and the exact `vX.Y.Z` tag as mandatory release gates.
