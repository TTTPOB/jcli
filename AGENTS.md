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
