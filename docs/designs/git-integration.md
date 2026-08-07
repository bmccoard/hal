# Portable Git integration design

| Field | Value |
| --- | --- |
| Status | Implemented |
| Target | `HAL` |
| Backends | Native Git and Dulwich |

## Decision

HAL exposes structured Git tools backed by one normalized Python interface. The
default `auto` selection uses the native Git executable when it is present and
otherwise falls back to Dulwich, which is a required package dependency. Users can
force `native` or `dulwich` through `git.backend`.

This boundary makes agent behavior independent of shell syntax and allows HAL to run
on managed Windows computers where installing `git.exe` is prohibited but Python
packages are permitted.

## Tool contract

| Tool | Mutation | Contract |
| --- | --- | --- |
| `git_status` | None | Return backend, branch, and staged/unstaged/untracked paths. |
| `git_diff` | None | Return working-tree or staged patches, optionally limited to paths. |
| `git_log` | None | Return normalized recent local commits. |
| `git_commit` | Local | Stage explicit repository-relative paths and create one local commit. |
| `git_push` | Remote | Push an explicitly requested branch and remote. |

`git_commit` rejects empty path lists, paths outside the repository, `.git` internals,
known local credential/configuration files (`.env`, `hal.yaml`, legacy `neo.yaml`,
`.hal/auth.json`, and legacy `.neo/auth.json`), and already-staged paths outside the requested set. Its result
always states that the commit was not pushed. `git_push` remains separate so local
check-in authorization cannot silently become a remote mutation.

## Language policy

"Check in" and "commit" mean a local commit only. Before committing, the agent must
inspect status and relevant diffs, exclude unrelated or sensitive files, and pass the
intended paths explicitly. "Push" or "publish" must be stated separately by the user.

## Backend differences

Native Git is preferred because it naturally uses installed credential helpers, SSH
agents, hooks, signing tools, and system configuration. Dulwich provides the portable
local workflow and its own remote transports, but authentication, hooks, and signing
can differ. Backend tests therefore assert normalized outcomes rather than identical
console output.

## Failure behavior

- Requiring `native` without an executable is a configuration/runtime error.
- `auto` selects Dulwich when no executable is found.
- Missing repositories, identities, remotes, authentication, conflicts, and rejected
  pushes are returned as tool errors without corrupting the agent transcript.
- Cancellation is checked at backend boundaries; native subprocess trees are
  terminated, and interactive SIGINT can interrupt Dulwich Python/network work.
