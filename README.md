# HAL CLI

HAL is a Python coding-agent CLI based on the upstream Neo project at
https://github.com/owainlewis/neo. Its name is inspired by HAL 9000 from
*2001: A Space Odyssey*. HAL keeps the upstream provider-neutral agent loop,
local-first configuration, built-in coding tools, project instructions, skills,
named phases, headless mode, and resumable sessions.
The interactive interface is a portable REPL rather than the Go version's
Bubble Tea TUI.

## Install

Python 3.11 or newer is required.

```bash
cd <repository-checkout>
python -m pip install -e .
hal help
```

The same CLI works without installing a script after setting `PYTHONPATH=src`:

```bash
python -m hal --help
```

## Configure

HAL loads the first file that exists: `./hal.yaml`, then
`~/.hal/config.yaml`, then the legacy `./neo.yaml` and `~/.neo/config.yaml`
locations, then built-in defaults. Files are not merged. Copy
[`hal.yaml.example`](hal.yaml.example) to `hal.yaml`, which is ignored by Git
and may contain local credentials. Never commit `hal.yaml`.

```yaml
provider: anthropic
model: claude-opus-5

compaction:
  context_window_tokens: 200000

features:
  agents_file: true
  skills: true
  prompt_caching: true

output:
  verbose: false

git:
  backend: auto
```

### Configuration support status

Not every setting carried over from the Go CLI is active in the Python port.
Inactive settings remain accepted so configuration files stay forward-compatible,
but changing them does not currently alter runtime behavior.

| Setting | Status | Current behavior |
| --- | --- | --- |
| `features.agents_file` | Active | Loads applicable `AGENTS.md` files into the system prompt. |
| `features.skills` | Active | Discovers skills and expands `$name` and `/name` invocations. |
| `git.backend` | Active | Selects `auto`, `native`, or `dulwich`; `auto` prefers the Git executable and otherwise uses Dulwich. |
| `compaction.context_window_tokens` | Reserved | Parsed and validated; transcript compaction is not implemented yet. |
| `features.prompt_caching` | Reserved | Parsed; provider prompt-cache controls are not emitted yet. |
| `output.verbose` | Reserved | Parsed; the Python REPL currently uses one fixed tool-activity view. |

The reserved settings should be tracked as implementation work in the issue
tracker before being described as supported features.

Keep model selection in `hal.yaml` and put credentials in the ignored `.env` file.
This reduces the chance that routine configuration inspection displays a token:

```yaml
provider: openrouter
model: openai/gpt-4o-mini
```

```dotenv
OPENROUTER_API_KEY=replace-with-your-openrouter-token
```

When an OpenRouter configuration omits `model`, `OPENROUTER_MODEL` is used
before the built-in default.

Custom OpenAI-compatible gateways can be defined as named profiles. Keep private
endpoint URLs and credentials in `.env`; `hal.yaml` contains only their environment
variable names:

```dotenv
ENTERPRISE_LLM_BASE_URL=https://llm-gateway.example.com/openai/v1
ENTERPRISE_LLM_TOKEN=replace-with-your-enterprise-token
```

```yaml
provider: enterprise
providers:
  - id: enterprise
    name: Example Enterprise GPT
    provider: openai
    model: example.organization.language-model.gpt-5
    api_base_env: ENTERPRISE_LLM_BASE_URL
    api_key_env: ENTERPRISE_LLM_TOKEN
    protocol: chat_completions
    max_tokens_parameter: max_completion_tokens
```

The older inline `api_base` and `apiBase` profile fields remain supported for
compatibility, but `api_base_env` is recommended when an endpoint is private or
workplace-specific. Environment values override an inline endpoint when both are
present.

Chat Completions gateways differ on the output-token field. Profiles use
`max_tokens` by default for compatibility with older models. Set
`max_tokens_parameter: max_completion_tokens` for GPT-5-class models or any
gateway that rejects `max_tokens`. The setting is restricted to those two field
names so arbitrary configuration cannot alter unrelated request fields.

HAL reads provider credentials and configured private endpoints from `.env` or the
process environment. Built-in credential names are:

- `ANTHROPIC_API_KEY`
- `OPENAI_API_KEY`
- `OPENROUTER_API_KEY`
- `GOOGLE_API_KEY`

OpenAI API-key auth is supported. The Go CLI's experimental ChatGPT/Codex
device-code login is intentionally not emulated because that flow relies on a
separate subscription transport; use `openai_auth: api_key` in this port.

## Use

These are operating-system shell commands, not model tools or skills:

| Command | What it does |
| --- | --- |
| `hal` | Starts an interactive conversation and saves it as a session. |
| `hal run "..."` | Runs one prompt and exits without creating a session. |
| `hal run --json "..."` | Runs one headless prompt and returns JSON containing status, timing, tool counts, and the final answer. |
| `hal sessions` | Lists saved sessions in a compact view with short selectors. |
| `hal sessions --verbose` | Lists full provider, model, path, and title details. |
| `hal sessions search parser` | Searches saved transcripts for `parser`. |
| `hal resume ae5f63c2` | Continues a saved session using its short selector or full ID. |
| `hal doctor` | Checks configuration, credentials, model, session storage, Git, and workspace status without contacting the model. |

`hal run "run tests"` asks the model to run tests; it does not execute a fixed
test command itself. The model normally fulfills that request with its shell
tool.

`hal run --timeout <duration>` applies one wall-clock deadline to the provider
calls, retry waits, agent loop, and tool calls. When a shell command is active,
HAL terminates its process tree before returning the timeout error. Durations
accept seconds or an `s`, `m`, or `h` suffix, such as `30s` or `10m`.

### Commands, tools, skills, and phases

- **CLI commands** are entered in the operating-system shell, such as `hal run`
  and `hal doctor`.
- **Interactive commands** are entered at the `HAL>` prompt, such as `/help`,
  `/sessions`, `/resume`, `/clear`, `/model`, and `/exit`.
- **Tools** are executable capabilities available to the model: `bash`,
  `read_file`, `write_file`, `edit_file`, `grep`, `glob`, `git_init`,
  `git_stage`, `git_unstage`, `git_status`, `git_diff`, `git_log`, `git_commit`,
  and `git_push`.
- **Skills** are reusable instruction documents stored at
  `.hal/skills/<name>/SKILL.md`. They guide the model but do not execute code by
  themselves.
- **Named phases** are built-in one-turn instruction modes: `/design`, `/plan`,
  `/build`, and `/review`.

The tool retains the provider-facing name `bash` for compatibility, but selects
the native shell explicitly. On Windows it uses `pwsh`, then Windows PowerShell,
and falls back to `cmd.exe` only when neither is available. On Unix-like systems
it uses Bash and falls back to `/bin/sh`. Interactive `!command` uses the same
selection. Write commands in the syntax of the shell installed on the machine.

Interactive mode supports `/help`, `/sessions`, `/sessions --verbose`,
`/resume <selector>`, `/clear`, `/model <id>`, `/exit`, the built-in `/design`,
`/plan`, `/build`, and `/review` phases, discovered skill commands, and
`!command` for a direct local shell command.

Session listings use these compact columns by default:

```text
SHORT     ID                         UPDATED           MODEL                PROJECT
ae5f63c2  sess_ae5f63c2dd8b4abd    2026-08-07 16:44  laguna-s-2.1:free    neo-py
```

The eight-character `SHORT` value is a stable prefix of the random session ID,
not a position in the changing list. Use it with `hal resume ae5f63c2` or, from
inside HAL, `/resume ae5f63c2`. A unique prefix of four or more characters also
works; HAL rejects ambiguous prefixes and asks for more characters. `/sessions`
prints the exact active session before its table, and `/resume` switches sessions
without exiting. The full `sess_...` ID remains accepted. `!` stays reserved for
direct shell commands.

While a model turn or direct `!command` is active, Ctrl-C cancels that operation
and returns to the `HAL>` prompt. HAL completes any required cancelled/skipped
tool results before saving the session, so resumed provider transcripts remain
structurally valid. Ctrl-C while HAL is waiting at the prompt exits normally.

### Git backends and check-ins

HAL uses dedicated Git tools instead of requiring the model to construct shell
commands. With the default `git.backend: auto`, HAL uses the installed `git`
executable when available and falls back to the required
[Dulwich Python implementation](https://www.dulwich.io/getting-started/) when it is
not. Force one implementation when troubleshooting or
testing parity:

```yaml
git:
  backend: dulwich  # auto, native, or dulwich
```

Asking HAL to create or recreate repository metadata uses `git_init`. The tool
initializes the current workspace on branch `main` with the configured backend,
refuses to overwrite an existing or enclosing repository, and reports whether it
used native Git or Dulwich. HAL instructs the model not to probe for or install
`git.exe` and not to improvise Python/Dulwich shell scripts when structured Git
tools are available.

Use `git_stage` and `git_unstage` when explicitly managing the index. Staging
accepts only explicit paths and refuses known local configuration and credential
files. Unstaging preserves working files, including a sensitive file that was
staged outside HAL. `git_diff` omits known sensitive paths even if another process
put them in the index, so their contents do not enter the model transcript. HAL
reports the omitted path but must not quote or rewrite its contents merely to make
it committable.

Asking HAL to "check in" or "commit" changes authorizes one **local commit**. HAL
must inspect status/diffs first, pass an explicit list of intended paths to
`git_commit`, and report the resulting commit ID. The commit tool refuses to include
already-staged files outside that list. It never pushes. Remote changes require a
separate explicit request such as "push this commit", which uses `git_push`.

Dulwich supports repository initialization, local status, diff, log, staging,
commits, and pushes without a Git binary. Native Git remains preferred because it
inherits the machine's credential manager, SSH agent, hooks, signing configuration,
and other installation-specific behavior. Dulwich pushes use Dulwich's own transport
and may require separately available HTTPS or SSH credentials. Both backends read the
repository's configured author identity; set `user.name` and `user.email` (or the
corresponding Git author environment variables) before committing.

### Skills

Skills are reusable prompt instructions, not executable tools. HAL discovers HAL
paths and then legacy Neo paths for migration compatibility:

```text
~/.hal/skills/<name>/SKILL.md             user-global skills
<workspace>/.hal/skills/<name>/SKILL.md  project skills
~/.neo/skills/<name>/SKILL.md             legacy user-global skills
<workspace>/.neo/skills/<name>/SKILL.md  legacy project skills
```

A project skill overrides a global skill with the same name. Each `SKILL.md`
needs YAML frontmatter containing `name` and `description`, followed by concise
instructions:

```markdown
---
name: example-skill
description: Explain what the skill does and when it should be used.
---

# Example Skill

Inspect the relevant files, follow the project instructions, and report
evidence for the result.
```

This repository includes a working
[`repo-summary`](.hal/skills/repo-summary/SKILL.md) example. Invoke project
skills in interactive mode:

```text
HAL> /repo-summary
HAL> Use $repo-summary to explain this repository.
```

`/name arguments` injects one skill and labels the trailing text as arguments.
`$name` references can expand multiple skills once each in mention order. Skill
expansion currently occurs only in interactive mode; `hal run` advertises the
catalog but does not expand `/name` or `$name` invocations.

### Project instructions (`AGENTS.md`)

`AGENTS.md` provides repository guidance that applies automatically; unlike a
skill, the user does not invoke it. HAL loads legacy `~/.neo/AGENTS.md`, then
`~/.hal/AGENTS.md`, then
project `AGENTS.md` files from the workspace root down to the current directory,
with more specific files appearing later in the prompt.

Copy [`AGENTS.md.example`](AGENTS.md.example) to `AGENTS.md` and adapt it to the
project's actual commands, architecture, safety rules, and delivery policy. The
example suffix is intentional: it demonstrates the convention without changing
this repository's active agent instructions.

The model can call the coding and Git tools listed above. Like the Go
implementation, HAL is not a security sandbox. Run it inside
an environment whose filesystem, process, network, and credential access match
your trust requirements. `tool_approvals` adds optional interactive confirmation
for exact tool names and shell-command prefixes; it is user-interface friction,
not an authorization boundary.

For example, this configuration asks before common Python package installation
commands and model file writes:

```yaml
tool_approvals:
  - pip
  - python -m pip
  - py -m pip
  - write_file
  - edit_file
  - git_init
  - git_stage
  - git_unstage
  - git_commit
  - git_push
```

Matching is literal. It does not detect every wrapper, alias, shell chain, or
indirect package-manager invocation. Headless `hal run` does not use interactive
approvals, so its prompt must explicitly authorize any intended environment changes.

## Develop

```bash
python -m pip install -e ".[dev]"
pytest
```

The package layout mirrors the Go architecture at a smaller scale:

- `agent.py` owns the transcript and model/tool loop.
- `providers.py` translates the shared protocol to vendor HTTP APIs.
- `tools.py` owns executable local capabilities.
- `config.py`, `context.py`, and `sessions.py` provide product features.
- `cli.py` is the composition and process boundary.

## Upstream Attribution

HAL is based on the Neo coding-agent CLI:

- Upstream project: https://github.com/owainlewis/neo
- Upstream license: MIT

## License

This repository is licensed under the MIT License. See [LICENSE](LICENSE).
