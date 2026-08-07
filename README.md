# Neo Python CLI

Neo Python is a Python package port of the Neo CLI and is based on the upstream
project at https://github.com/owainlewis/neo. It keeps Neo's provider-neutral
agent loop, local-first configuration, built-in coding tools, project
instructions, skills, named phases, headless mode, and resumable sessions.
The interactive interface is a portable REPL rather than the Go version's
Bubble Tea TUI.

## Install

Python 3.11 or newer is required.

```bash
cd neo-py
python -m pip install -e .
neo help
```

The same CLI works without installing a script after setting `PYTHONPATH=src`:

```bash
python -m neo --help
```

## Configure

Neo loads the first file that exists: `./neo.yaml`, then
`~/.neo/config.yaml`, then built-in defaults. Files are not merged. Copy
[`neo.yaml.example`](neo.yaml.example) to `neo.yaml`, which is ignored by Git
and may contain local credentials. Never commit `neo.yaml`.

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
```

### Configuration support status

Not every setting carried over from the Go CLI is active in the Python port.
Inactive settings remain accepted so configuration files stay forward-compatible,
but changing them does not currently alter runtime behavior.

| Setting | Status | Current behavior |
| --- | --- | --- |
| `features.agents_file` | Active | Loads applicable `AGENTS.md` files into the system prompt. |
| `features.skills` | Active | Discovers skills and expands `$name` and `/name` invocations. |
| `compaction.context_window_tokens` | Reserved | Parsed and validated; transcript compaction is not implemented yet. |
| `features.prompt_caching` | Reserved | Parsed; provider prompt-cache controls are not emitted yet. |
| `output.verbose` | Reserved | Parsed; the Python REPL currently uses one fixed tool-activity view. |

The reserved settings should be tracked as implementation work in the issue
tracker before being described as supported features.

Keep model selection in `neo.yaml` and put credentials in the ignored `.env` file.
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

Custom OpenAI-compatible gateways can be defined as named profiles. The example
below is intentionally fictional; replace every endpoint, model, and credential
value with values supplied by your organization:

```yaml
provider: enterprise
providers:
  - id: enterprise
    name: Example Enterprise GPT
    provider: openai
    model: example.organization.language-model.gpt-5
    apiBase: https://llm-gateway.example.com/openai/v1
    api_key_env: ENTERPRISE_LLM_TOKEN
    protocol: chat_completions
    max_tokens_parameter: max_completion_tokens
```

Chat Completions gateways differ on the output-token field. Profiles use
`max_tokens` by default for compatibility with older models. Set
`max_tokens_parameter: max_completion_tokens` for GPT-5-class models or any
gateway that rejects `max_tokens`. The setting is restricted to those two field
names so arbitrary configuration cannot alter unrelated request fields.

Neo reads provider credentials from `.env` or the process environment:

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
| `neo` | Starts an interactive conversation and saves it as a session. |
| `neo run "..."` | Runs one prompt and exits without creating a session. |
| `neo run --json "..."` | Runs one headless prompt and returns JSON containing status, timing, tool counts, and the final answer. |
| `neo sessions` | Lists locally saved interactive sessions from `~/.neo/sessions/`. |
| `neo sessions search parser` | Searches saved transcripts for `parser`. |
| `neo resume <id>` | Continues a saved interactive session, including its messages, model, usage, and working directory. |
| `neo doctor` | Checks configuration, credentials, model, session storage, Git, and workspace status without contacting the model. |

`neo run "run tests"` asks the model to run tests; it does not execute a fixed
test command itself. The model normally fulfills that request with its shell
tool.

`neo run --timeout <duration>` applies one wall-clock deadline to the provider
calls, retry waits, agent loop, and tool calls. When a shell command is active,
Neo terminates its process tree before returning the timeout error. Durations
accept seconds or an `s`, `m`, or `h` suffix, such as `30s` or `10m`.

### Commands, tools, skills, and phases

- **CLI commands** are entered in the operating-system shell, such as `neo run`
  and `neo doctor`.
- **Interactive commands** are entered at the `neo>` prompt, such as `/help`,
  `/clear`, `/model`, and `/exit`.
- **Tools** are executable capabilities available to the model: `bash`,
  `read_file`, `write_file`, `edit_file`, `grep`, and `glob`.
- **Skills** are reusable instruction documents stored at
  `.neo/skills/<name>/SKILL.md`. They guide the model but do not execute code by
  themselves.
- **Named phases** are built-in one-turn instruction modes: `/design`, `/plan`,
  `/build`, and `/review`.

The tool retains the provider-facing name `bash` for compatibility, but selects
the native shell explicitly. On Windows it uses `pwsh`, then Windows PowerShell,
and falls back to `cmd.exe` only when neither is available. On Unix-like systems
it uses Bash and falls back to `/bin/sh`. Interactive `!command` uses the same
selection. Write commands in the syntax of the shell installed on the machine.

Interactive mode supports `/help`, `/clear`, `/model <id>`, `/exit`, the built-in
`/design`, `/plan`, `/build`, and `/review` phases, discovered skill commands,
and `!command` for a direct local shell command.

### Skills

Skills are reusable prompt instructions, not executable tools. Neo discovers:

```text
~/.neo/skills/<name>/SKILL.md             user-global skills
<workspace>/.neo/skills/<name>/SKILL.md  project skills
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
[`repo-summary`](.neo/skills/repo-summary/SKILL.md) example. Invoke project
skills in interactive mode:

```text
neo> /repo-summary
neo> Use $repo-summary to explain this repository.
```

`/name arguments` injects one skill and labels the trailing text as arguments.
`$name` references can expand multiple skills once each in mention order. Skill
expansion currently occurs only in interactive mode; `neo run` advertises the
catalog but does not expand `/name` or `$name` invocations.

### Project instructions (`AGENTS.md`)

`AGENTS.md` provides repository guidance that applies automatically; unlike a
skill, the user does not invoke it. Neo loads `~/.neo/AGENTS.md` first, then
project `AGENTS.md` files from the workspace root down to the current directory,
with more specific files appearing later in the prompt.

Copy [`AGENTS.md.example`](AGENTS.md.example) to `AGENTS.md` and adapt it to the
project's actual commands, architecture, safety rules, and delivery policy. The
example suffix is intentional: it demonstrates the convention without changing
this repository's active agent instructions.

The model can call `bash`, `read_file`, `write_file`, `edit_file`, `grep`, and
`glob`. Like the Go implementation, Neo is not a security sandbox. Run it inside
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
```

Matching is literal. It does not detect every wrapper, alias, shell chain, or
indirect package-manager invocation. Headless `neo run` does not use interactive
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

This project is a Python conversion of Neo:

- Upstream project: https://github.com/owainlewis/neo
- Upstream license: MIT

## License

This repository is licensed under the MIT License. See [LICENSE](LICENSE).
