# Neo Python

This is a Python package port of the Go Neo CLI in the adjacent `neo-main`
directory. It keeps Neo's provider-neutral agent loop, local-first configuration,
built-in coding tools, project instructions, skills, named phases, headless mode,
and resumable sessions. The interactive interface is a portable REPL rather than
the Go version's Bubble Tea TUI.

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
`~/.neo/config.yaml`, then built-in defaults. Files are not merged.
It also loads `./.env` when present, without replacing variables already
exported by the parent shell. Dotenv files are ignored by Git by default.

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

Keep model selection in `neo.yaml` and credentials in `.env`. For example:

```yaml
provider: openrouter
model: openai/gpt-4o-mini
```

```dotenv
OPENROUTER_API_KEY=replace-with-your-openrouter-token
```

For backward compatibility, when an OpenRouter configuration omits `model`,
`OPENROUTER_MODEL` is used before the built-in default.

Custom OpenAI-compatible gateways can be defined as named profiles. This
repository includes an inactive `enterprise` profile; switch the top-level setting
to `provider: enterprise` when connected to that network and set
`ENTERPRISE_LLM_TOKEN` in `.env`:

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
```

Set the credential for the selected provider:

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
