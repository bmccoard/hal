# HAL Simple Extension

This is a minimal, separately installed tool extension for HAL. It exports three
tools: `simple_echo`, `simple_add`, and `simple_project_info`.

## Install and enable

Create a fresh virtual environment and install HAL before this sibling project.
HAL must be installed first because `hal-agent-cli` is a local dependency rather
than a package fetched from the package index.

```bash
deactivate  # if a virtual environment is currently active
python -m venv .venv
source .venv/Scripts/activate  # Git Bash
# .\.venv\Scripts\Activate.ps1  # PowerShell alternative
python -m pip install -e "../hal[dev]"
python -m pip install -e ".[dev]"
```

Copy `hal.yaml.example` to the directory where you run HAL, or add this section
to its existing `hal.yaml`:

```yaml
extensions:
  - simple

extension_config:
  simple:
    greeting: Hello
```

Run `pytest` to test the tools directly, then use `hal run` or interactive `hal`
to exercise them through an AI provider. Keep secrets in `.env` or environment
variables, not in `hal.yaml`.

## Develop

Tool implementations live in `src/hal_simple/tools.py`. Every tool supplies a
provider-facing `ToolSpec` and a `run` method. `create_tools` receives HAL's
`ExtensionContext` and returns the tool instances to register.

```bash
pytest
```
