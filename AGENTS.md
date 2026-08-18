# Repository Instructions

These instructions apply to all work in this repository.

## Development workflow

- Read `README.md` and `pyproject.toml` before changing package behavior.
- Keep provider-specific translation in `src/hal/providers.py`; keep the agent
  loop provider-neutral.
- Preserve compatibility with Python 3.11 and newer.
- Prefer focused changes and avoid adding dependencies when the standard
  library is sufficient.
- Never commit API keys, credentials, private endpoints, or internal model
  identifiers. Keep secrets in `.env` and use fictional values in tracked
  examples and tests.

## Versioning

- HAL uses a two-part version scheme `X.Y` (e.g. `0.1`, `0.2`, `0.3`).
  Do not use a three-part `X.Y.Z` / semver patch suffix. The version lives in
  two places that must stay in sync: `src/hal/__init__.py` (`__version__`) and
  `pyproject.toml` (`project.version`).
- **Whenever a `git push` occurs (via `git_push` tool or otherwise), you MUST
  increment the version before pushing.** Example: `0.1` → `0.2`, `0.2` → `0.3`.
  Increment the minor part by `0.1` (i.e. `Y + 1`). If `Y` reaches `9`, roll to
  the next major: `0.9` → `1.0`, `1.9` → `2.0`, etc.
- Include the version bump in the **same commit** that is being pushed. Do not
  create a separate version-only commit unless the push contains only the bump.
- Steps before every push:
  1. Read `src/hal/__init__.py` and `pyproject.toml` to confirm the current version.
  2. Bump both files to the next `X.Y` value.
  3. Verify they match with `python -c "import hal; print(hal.__version__)"`.
  4. Include the bumped files in the staged paths passed to `git_commit`.
  5. Then `git_push` as authorized by the user.
- If the push is denied, cancelled, or fails, do not leave a bumped version
  unpushed — either revert the bump or retry the push in the same turn so the
  repository does not diverge.
- `hal version` / `hal --version` and the TUI status bar read from
  `src/hal/__init__.py`; keeping the two sources in sync is required for the
  display to be accurate.

  > **Why not automate with a hook?** A `pre-push` Git hook or CI check could
  > enforce the bump automatically. HAL keeps the rule as an agent instruction
  > instead so the bump is intentional, reviewable, and happens atomically with
  > the feature commit inside the agent loop — without requiring every
  > contributor to install hooks locally. If the team later adopts a hook,
  > retain this instruction as the fallback so headless or hook-less pushes
  > still bump correctly.

## Verification

- Run `python -m pytest -q` after code changes.
- Run `python -m compileall -q src` after changing package modules.
- Run `python -m hal doctor` after changing configuration or provider setup.
- Build a wheel after changing packaging metadata.

## Delivery

- Summarize changed behavior and verification results.
- Call out anything that could not be tested locally.
- Do not commit or push unless the user explicitly requests it.
