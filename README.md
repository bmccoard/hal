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

This local setup keeps both model selection and its token in the ignored
`neo.yaml`:

```yaml
provider: openrouter
model: openai/gpt-4o-mini
api_key: replace-with-your-openrouter-token
```

Environment variables remain supported for installations that prefer them.
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
    api_key: replace-with-your-enterprise-token
    protocol: chat_completions
    max_tokens_parameter: max_completion_tokens
```

Chat Completions gateways differ on the output-token field. Profiles use
`max_tokens` by default for compatibility with older models. Set
`max_tokens_parameter: max_completion_tokens` for GPT-5-class models or any
gateway that rejects `max_tokens`. The setting is restricted to those two field
names so arbitrary configuration cannot alter unrelated request fields.

Alternatively, omit `api_key` and set the credential for the selected provider
in the process environment:

- `ANTHROPIC_API_KEY`
- `OPENAI_API_KEY`
- `OPENROUTER_API_KEY`
- `GOOGLE_API_KEY`

OpenAI API-key auth is supported. The Go CLI's experimental ChatGPT/Codex
device-code login is intentionally not emulated because that flow relies on a
separate subscription transport; use `openai_auth: api_key` in this port.

## Use

```bash
neo                         # interactive chat
neo run "explain this repo" # one prompt, no session
neo run --json "run tests"
neo sessions
neo sessions search parser
neo resume sess_0123456789abcdef
neo doctor
```

Interactive mode supports `/help`, `/clear`, `/model <id>`, `/exit`, the built-in
`/design`, `/plan`, `/build`, and `/review` phases, discovered skill commands,
and `!command` for a direct local shell command.

The model can call `bash`, `read_file`, `write_file`, `edit_file`, `grep`, and
`glob`. Like the Go implementation, Neo is not a security sandbox. Run it inside
an environment whose filesystem, process, network, and credential access match
your trust requirements. `tool_approvals` adds optional interactive confirmation
for exact tool names and shell-command prefixes; it is user-interface friction,
not an authorization boundary.

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
