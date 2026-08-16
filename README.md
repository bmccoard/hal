# HAL CLI

HAL is a Python coding-agent CLI inspired by HAL 9000 from *2001: A Space
Odyssey*. It provides a provider-neutral agent loop, local-first
configuration, built-in coding tools, project instructions, skills, named
phases, headless mode, and resumable sessions.

The interactive interface is a responsive Textual TUI inspired by the Go version's
Bubble Tea experience, with a portable basic REPL retained as a fallback.

## Install

Python 3.11 or newer is required.

```bash
cd <repository-checkout>
python -m pip install -e .
hal help
```

Optional install profiles:

```bash
# Full interactive + Git fallback support
python -m pip install -e ".[tui,git]"

# Development/test environment
python -m pip install -e ".[dev]"
```

The same CLI works without installing a script after setting `PYTHONPATH=src`:

```bash
python -m hal --help
```

## Configure

HAL loads the first file that exists: `./hal.yaml`, then
`~/.hal/config.yaml`, then built-in defaults. Files are not merged. Copy
[`hal.yaml.example`](hal.yaml.example) to `hal.yaml`, keep secrets in `.env`,
keep machine-specific overrides in ignored `*.local.yaml` files, and commit
`hal.yaml` only when it contains shareable configuration.

```yaml
provider: anthropic
model: claude-opus-5
max_output_tokens: 8192
max_output_continuations: 2

compaction:
  context_window_tokens: 200000

features:
  agents_file: true
  skills: true
  streaming: true
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
| `features.streaming` | Active | Streams interactive responses incrementally when supported; disable it to require buffered responses. |
| `max_output_tokens` | Active | Sets the maximum output tokens requested on every provider call. |
| `max_output_continuations` | Active | Automatically resumes clean text-only token truncations up to this limit; `0` disables continuation. |
| `git.backend` | Active | Selects `auto`, `native`, or `dulwich`; `auto` prefers the Git executable and otherwise uses Dulwich when the optional `git` extra is installed. |
| `harness.budgets` | Active | Applies per-send provider-call, tool-call, elapsed-time, input-token, and output-token limits in headless and interactive modes. |
| `harness.default_capability` | Active | Optionally constrains ordinary sends to the built-in `inspect`, `plan`, `change`, or `review` policy. |
| `harness.capabilities` | Active | Defines additional named tool policies and stricter per-capability budgets for ordinary sends and configured phases. |
| `harness.verification` | Active | Runs trusted, serial workspace checks after a successful agent turn and records bounded typed results. |
| `harness.repair_attempts` | Active | Allows a bounded number of repair turns after required verification failures without resetting policy or budgets. |
| `compaction.context_window_tokens` | Reserved | Parsed and validated; transcript compaction is not implemented yet. |
| `features.prompt_caching` | Reserved | Parsed; provider prompt-cache controls are not emitted yet. |
| `output.verbose` | Active in TUI | Concise receipts by default; shows full tool calls and results when enabled. The basic REPL retains its compact fixed view. |

The reserved settings should be tracked as implementation work in the issue
tracker before being described as supported features.

### Harness budgets

Harness budgets place hard limits on each agent send, including each phase of a
workflow. They apply to `hal run`, the basic REPL, the TUI, and agents reconstructed
when a session is resumed. Omit the section to preserve unlimited legacy behavior:

```yaml
harness:
  default_capability: change
  budgets:
    provider_calls: 50
    tool_calls: 200
    elapsed_seconds: 900
    input_tokens: null
    output_tokens: null
  verification:
    - name: tests
      command: .venv/bin/pytest -q
      timeout_seconds: 120
      required: true
  repair_attempts: 1
```

Provider and tool limits are checked before starting another call. Token usage is
reported by providers after a response, so reaching a token limit prevents subsequent
tool or provider calls. A call already in progress is allowed to finish unless it is
cancelled. Individual `null` values disable that limit; an empty `budgets` mapping uses
the defaults shown above.

`default_capability` is optional and accepts `inspect`, `plan`, `change`, or `review`.
It constrains ordinary sends. Workflow phases add their own capability, with all
restrictions composed so a phase cannot restore access removed by the configured
default. `inspect` and `plan` expose only read tools; `change` and `review` deny Git
initialization, index changes, commits, and pushes and protect existing files from
whole-file replacement through `write_file`.

Additional capabilities can be defined under `harness.capabilities` with
`allowed_tools`, `denied_tools`, `protect_existing_files`, and optional `budgets`.
Built-ins cannot be redefined. Tool names are validated after extensions load, so
extension tools can be referenced safely. A configured phase can select one with a
`capability` field. Every applicable allowed-tool set is intersected, denied-tool sets
are combined, and each budget field resolves to its smallest finite value.

Verification commands come only from configuration, never from model output. They run
serially after the agent turn in the workspace. A nonzero exit, timeout, or command
start failure from a required check fails the run; an optional check is recorded but
does not change a successful outcome. Output is bounded with the same head/tail policy
as tool results, and cancellation stops verification immediately.

`repair_attempts` defaults to `0`. When enabled, a required verification failure is
returned to the agent as a bounded report. Repair uses the same capability and caller
restrictions and the remaining counters from the original send. Cancellation or an
exhausted hard budget prevents repair from starting.

Every CLI-created agent also writes a versioned post-run journal under
`~/.hal/sessions/runs`. Journals contain resolved policy, budgets, counters, check
results, repair counts, and terminal status. They intentionally omit prompts,
transcripts, final model text, environment variables, and credentials.

`hal run --json` includes the run ID, terminal harness status and reason, counters,
verification summaries, and repair count. Exit code `3` indicates budget exhaustion,
`4` indicates required verification failure, and `130` indicates cancellation; other
failures use `1`.

Use `hal harness [capability] --json` to inspect the fully resolved policy without
starting a provider request. The command loads extensions, validates configured tool
references, and reports available and denied tools, effective budgets, verification,
repair attempts, approvals, shell policy, and workspace-write protection. Without a
name it selects the configured default capability, or `change` when no default exists.
JSON output also includes each available tool's effect classification, parallel-safety
marker, and resolved approval-gated status.

The TUI shows verification starts and results, repair attempts, and terminal run
status. Verbose mode additionally shows budget usage, complete bounded check output,
and repair failure reports.

Harness integrations can use `Agent.run_subagent(...)` during an active parent run.
The child must supply a strictly narrower capability and an explicit budget. HAL caps
that budget by the parent's remaining limits, keeps child counters and outcomes, then
attributes child usage to the parent. Child lifecycle events and journals contain both
run IDs. This API does not yet expose arbitrary model-selected subagents.

Trusted model-facing delegation is enabled by configured profiles:

```yaml
subagents:
  researcher:
    description: Inspect relevant code without modifying it
    model: small-model
    capability: inspect
    budgets:
      provider_calls: 10
      tool_calls: 20
      elapsed_seconds: 300
```

When at least one profile exists, HAL registers the serial `delegate` tool. The model
can select only a configured profile and supply task text. Model, capability, tools,
and budgets cannot be supplied through tool arguments. Child policy is intersected
with the active parent and its budget is capped by the parent's remaining limits.

### Feature flags

The `features` mapping controls optional runtime behavior. Omitted flags use the
defaults shown here:

```yaml
features:
  agents_file: true     # Load applicable AGENTS.md project instructions.
  skills: true          # Discover and expand HAL skills and named invocations.
  streaming: true       # Update interactive responses as provider deltas arrive.
  prompt_caching: true  # Reserved; currently has no runtime effect.
```

Set `streaming: false` when an enterprise gateway needs traditional buffered
Chat Completions. This affects interactive TUI and basic-REPL conversations;
`hal run` is always buffered. `agents_file` and `skills` take effect when HAL
builds a new agent, so restart or resume into a newly constructed session after
changing them. Unknown configuration keys are retained only where explicitly
documented; do not assume an unlisted feature flag is active.

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

Meta Model API uses a Responses-compatible endpoint:

```yaml
provider: meta
model: muse-spark-1.2-contributor
max_output_tokens: 16384
max_output_continuations: 2
```

```dotenv
META_API_KEY=replace-with-your-meta-model-api-key
```

HAL defaults to Meta's endpoint at `https://api.meta.ai/v1/responses`. During the
public preview its base URL can be overridden
without putting the endpoint in source-controlled configuration by setting
`META_API_BASE_URL` in `.env`.

`max_output_tokens` is the per-response request limit, not the total run budget.
It defaults to `8192`. If Meta or another provider returns a clean text-only
response with a token-limit stop reason, HAL preserves that text and asks the
model to continue exactly where it stopped. It makes at most
`max_output_continuations` additional calls (default `2`), and those calls still
consume the same harness provider-call, elapsed-time, input-token, and output-token
budgets. Recognized provider reasoning metadata may accompany the text and is
preserved opaquely. HAL does not automatically continue empty, unknown structured,
or malformed truncations because replaying an incomplete structure could be unsafe.

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

Interactive streaming supports OpenAI Responses, OpenAI-compatible Chat Completions
(including OpenRouter and GPT-5.1 enterprise profiles), Anthropic Messages, and Google
Gemini. A Chat Completions gateway that rejects `stream: true` before sending data is
retried once using the existing buffered request. If it simply returns a normal JSON
response, HAL consumes that same response without issuing a duplicate request. To
force the compatibility path for a workplace gateway, set `features.streaming: false`.
Headless `hal run` remains buffered so its output and JSON contract do not change.

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
- `META_API_KEY`

OpenAI API-key auth is supported. The Go CLI's experimental ChatGPT/Codex
device-code login is intentionally not emulated because that flow relies on a
separate subscription transport; use `openai_auth: api_key` in this port.

## Use

These are operating-system shell commands, not model tools or skills:

| Command | What it does |
| --- | --- |
| `hal` | Starts the full-screen TUI when supported, otherwise the basic REPL, and saves the conversation as a session. |
| `hal chat --no-tui` | Starts the portable line-oriented fallback explicitly. |
| `hal tui` | Requires the full-screen interface and reports an error on unsupported terminals. |
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

The TUI uses plain `Enter` or `F2` to send. Use `Ctrl+J`, `F3`, `Shift+Enter`,
or the visible **New line** button to add a line. Use `F4` or the visible **Paste**
button to read the clipboard directly. `Ctrl-C` or
`Escape` cancels active work, `Ctrl+L` clears the
conversation, and `Ctrl+Q` quits safely. Some terminals—especially when VS Code
has claimed a shortcut—never send modified Enter combinations to terminal apps;
plain Enter, `Ctrl+J`, `F2`/`F3`, and the buttons remain available in that case. Windows
console hosts commonly reserve `Alt+Enter` for full-screen mode, so HAL does not
use or advertise that combination.

Large pastes are shown in the composer as compact `[Pasted block …]` markers so the
terminal does not have to render the entire payload. HAL keeps the complete text in
memory and expands it unchanged only when the message is sent; pasted slash commands
and shell-looking lines remain message data rather than executing automatically.
On Windows, `Ctrl+V` reads Unicode text directly from the native clipboard; terminal
bracketed paste and Textual's local clipboard remain available on other platforms.
If Windows Terminal intercepts `Ctrl+V` or shows its large-paste warning, use `F4`
or **Paste** to bypass the terminal host and read the clipboard directly.

Transcript text may be selected with `Shift`+drag and copied with `Ctrl+Shift+C`.
HAL also provides an internal selection-copy action; on Windows that action uses the
native clipboard API rather than relying on OSC 52 support.

Every interactive startup displays one randomly selected HAL quotation. The small,
central catalog lives in `src/hal/sayings.py`, so startup lines can be reviewed or
extended without changing either interactive interface. Headless `hal run` does not
print a quotation and retains its machine-readable output contract.
Quitting during a turn first cancels the work and saves a structurally valid session.
Set `HAL_NO_TUI=1` or use `hal chat --no-tui` when a terminal does not render the
full-screen interface correctly. See [the terminal-interface design](docs/designs/terminal-interface.md)
for the worker, event, cancellation, and fallback architecture.

After pulling an update that adds or changes dependencies, refresh an existing
editable virtual-environment installation with `python -m pip install -e .`. If
TUI dependencies were split, use `python -m pip install -e ".[tui]"`. If
`rich` or `textual` is missing, automatic interactive mode warns and falls back to
the basic REPL; explicit `hal tui` reports the missing packages and exits.

`hal run --timeout <duration>` applies one wall-clock deadline to the provider
calls, retry waits, agent loop, and tool calls. When a shell command is active,
HAL terminates its process tree before returning the timeout error. Durations
accept seconds or an `s`, `m`, or `h` suffix, such as `30s` or `10m`.

Shell and Git subprocess output is drained continuously into bounded buffers instead
of being accumulated without limit. HAL retains the beginning and end, caps each
captured stream at 256 KiB, and inserts the total and omitted byte counts when
truncation occurs. Timed-out commands retain the same bounded partial output. Native
Git operations that require a complete path list fail closed when that internal output
exceeds the limit rather than making commit-safety decisions from truncated data.

### Commands, tools, skills, and phases

- **CLI commands** are entered in the operating-system shell, such as `hal run`
  and `hal doctor`.
- **Interactive commands** are entered at the `HAL>` prompt, such as `/help`,
  `/sessions`, `/resume`, `/clear`, `/model`, and `/exit`.
- **Tools** are executable capabilities available to the model: `bash`,
  `read_file`, `write_file`, `edit_file`, `grep`, `glob`, `git_init`,
  `git_stage`, `git_unstage`, `git_status`, `git_diff`, `git_log`, `git_commit`,
  and `git_push`.
- **Tool extensions** are separately installed Python packages that add tools
  through the `hal.tools` entry-point group. HAL loads only extensions explicitly
  enabled by name in `hal.yaml`.
- **Skills** are reusable instruction documents stored at
  `.hal/skills/<name>/SKILL.md`. They guide the model but do not execute code by
  themselves.
- **Named phases** are built-in one-turn instruction modes: `/design`, `/plan`,
  `/build`, and `/review`.
- **Workflows** run bounded ordered phases in one interactive request. Use
  `/workflow feature <request>` for `design -> plan -> build -> review`, and
  `/workflows` to list available workflows.

### Workflow example

Workflows run bounded ordered phases in one request. The `feature` workflow
executes `design -> plan -> build -> review` to take a feature from idea to
implemented code.

List available workflows:

```text
HAL> /workflows
feature  design -> plan -> build -> review  Design, plan, build, and review one requested repository change
```

Run a workflow with a feature request:

```text
HAL> /workflow feature Add a status-bar preference to the terminal interface
```

Each phase is a separate agent turn preserved in the session. To keep model input
bounded, a new phase receives only the final responses from earlier phases (up to
4,000 characters each), not their file contents, test output, or tool-call history.
The complete transcript remains available in the saved session. Press Ctrl-C to
cancel the current step and prevent later steps from starting.
Feature workflows cannot initialize a repository, change the index, commit, or push.
Review the completed changes first, then request any Git mutation separately.

### Tool extensions

Extensions keep service-specific code out of HAL while making their tools available
in the normal CLI, TUI, headless runs, and resumed sessions. Installing an extension
does not activate it. Enable its registered entry-point name explicitly:

```yaml
extensions:
  - jellyfin

extension_config:
  jellyfin:
    url: http://localhost:8096
```

Keep API keys in `.env` or the process environment rather than `hal.yaml`. HAL passes
the mapping under `extension_config.<name>` to that extension but does not interpret
it.

An extension package registers a factory in its `pyproject.toml`:

```toml
[project.entry-points."hal.tools"]
jellyfin = "hal_jellyfin:create_tools"
```

The factory accepts an [`ExtensionContext`](src/hal/extensions.py) and returns an
iterable of [`Tool`](src/hal/tools.py) instances:

```python
from hal.extensions import ExtensionContext

def create_tools(context: ExtensionContext):
    return [JellyfinSearchTool(context.settings)]
```

Entry-point names must be unique among installed distributions. Tool names must also
be unique across HAL and all enabled extensions; HAL stops with a configuration error
instead of silently replacing a tool. Extensions can add tools through the public
`Registry.extend()` method when constructing registries in application code.

To start a new sibling extension project from the included working example, run:

```bash
python new-project.py my-extension
```

This copies `example/simple` to `../my-extension`, specializes its distribution,
entry-point, package, and tool names, and refuses to overwrite an existing path.

The tool retains the provider-facing name `bash` for compatibility, but selects
the native shell explicitly. On Windows it uses `pwsh`, then Windows PowerShell,
and falls back to `cmd.exe` only when neither is available. On Unix-like systems
it uses Bash and falls back to `/bin/sh`. Interactive `!command` uses the same
selection. Write commands in the syntax of the shell installed on the machine.

Malformed JSON tool arguments are never executed. HAL returns a matching error
result to the model so it can correct the call without aborting the entire turn.
Three malformed calls to the same tool stop that turn to prevent runaway retries.
Three identical calls with the same result also stop the turn, preventing status or
diff polling loops from consuming the remaining context window.
The `grep` tool continues to interpret `pattern` as a regular expression by default;
use `literal: true` for exact text containing characters such as `*`, `[`, or `(`.

Interactive mode supports `/help`, `/sessions`, `/sessions --verbose`,
`/resume <selector>`, `/clear`, `/model <id>`, `/exit`, the built-in `/design`,
`/plan`, `/build`, and `/review` phases, discovered skill commands, and
`!command` for a direct local shell command.

Session listings use these compact columns by default:

```text
SHORT     ID                         UPDATED           MODEL                PROJECT
ae5f63c2  sess_ae5f63c2dd8b4abd    2026-08-07 16:44  laguna-s-2.1:free    hal
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
executable when available and falls back to the optional
[Dulwich Python implementation](https://www.dulwich.io/getting-started/) when it is
not and the `git` extra is installed. Force one implementation when troubleshooting or
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
Dulwich and native Git diffs use the same bounded head/tail output policy.

### Skills

Skills are reusable prompt instructions, not executable tools. HAL discovers:

```text
~/.hal/skills/<name>/SKILL.md             user-global skills
<workspace>/.hal/skills/<name>/SKILL.md  project skills
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
skill, the user does not invoke it. HAL loads `~/.hal/AGENTS.md`, then
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

For stricter built-in file writes, configure:

```yaml
only_write_locally: true
bash_policy: normal  # normal, approve, or deny
```

With `only_write_locally`, `write_file` and `edit_file` resolve symlinks and may
write inside the Git workspace without prompting. Outside paths require interactive
approval and are denied in headless mode. During a feature workflow, `write_file`
also cannot replace an existing file; the model must use exact-match `edit_file`.

Shell commands and extension code are not confined by `only_write_locally`.
`bash_policy: approve` asks before every model-issued shell command, while `deny`
disables the model-facing shell tool. `normal` preserves current shell behavior.
Direct `!command` input is user-authored and remains outside this model-tool policy.
These controls reduce accidental writes but do not replace an OS sandbox.

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

## License

This repository is licensed under the MIT License. See [LICENSE](LICENSE).
