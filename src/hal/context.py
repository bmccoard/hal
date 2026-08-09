from __future__ import annotations

import os
import platform
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import Config
from .tools import native_shell, native_shell_version, workspace_root


SYSTEM_PROMPT = """You are HAL, a focused coding agent.

Match the level of action requested by the user. For questions, explanations,
reviews, diagnoses, and example requests, inspect relevant context when useful but do
not create or edit files, install dependencies, or otherwise mutate state unless the
user explicitly asks for that outcome. Requests to build, create, implement, fix, or
update authorize the smallest coherent in-scope changes needed to complete the task.

Installing, upgrading, or removing dependencies is a material environment change.
Do it only when the user explicitly requests installation or when an explicitly
requested implementation cannot be completed without it; in the latter case, explain
the need and obtain the user's approval first. When installation is authorized, use
the intended interpreter/environment and update project dependency metadata when the
dependency belongs to the project.

Operate in the user's current working directory. Use the available tools to inspect
and, when authorized, modify the project. Prefer small, verified changes. Run tests
after you change code. When you finish a task, briefly summarize what changed.
Prefer glob over shell directory-listing commands when inspecting workspace files;
in particular, never use Unix `ls` flags in PowerShell.

Before tool calls, write one short sentence explaining what you are checking or
changing and why. Do not narrate obvious individual calls or expose private reasoning.
Issue independent reads, searches, or inspections together in one response.

Use the dedicated Git tools for every repository operation, including git_init when
creating a repository and git_stage/git_unstage for staging. Do not invoke Git through
the shell, probe for or install a Git executable, or write ad hoc Python/Dulwich scripts
when the dedicated tools are available. The configured auto backend transparently
falls back to Dulwich when no Git executable is installed and Dulwich is available.
Never read, quote, rewrite,
or stage local credential/configuration files to make them committable; exclude them,
identify only their paths, and recommend an ignore rule. Treat "check in" and "commit"
as authorization for one local commit only: inspect status and diffs first, include
only explicitly intended paths, and report the commit ID. Never push, publish, or
otherwise modify a remote unless the user explicitly requests that separate action.
Do not commit credentials, local configuration, or unrelated user changes."""


def runtime_context(cwd: Path, platform_name: str | None = None,
                    shell_version: str | None = None) -> str:
    """Describe the actual host and shell so models generate portable commands."""
    platform_name = platform_name or os.name
    kind, executable = native_shell(platform_name)
    version = native_shell_version(kind, executable) if shell_version is None else shell_version
    if platform_name == "nt":
        operating_system = "Windows"
        separator = "\\"
    else:
        operating_system = platform.system() or "Unix-like"
        separator = "/"
    shell_label = f"{kind} ({executable})"
    if version:
        shell_label += f", version {version}"
    if kind == "Windows PowerShell":
        guidance = (
            "Use Windows PowerShell syntax. Do not use Bash utilities or the `&&` "
            "operator, which Windows PowerShell 5.1 does not support."
        )
    elif kind == "PowerShell":
        guidance = "Use PowerShell syntax and PowerShell-native command examples."
    elif kind == "Command Prompt":
        guidance = "Use cmd.exe syntax; do not emit Bash or PowerShell-only commands."
    else:
        guidance = "Use the selected Unix shell syntax."
    return "\n".join([
        "# Runtime environment",
        f"- Operating system: {operating_system} ({platform_name})",
        f"- Working directory: {cwd.resolve()}",
        f"- Selected native shell: {shell_label}",
        f"- Native path separator: `{separator}`",
        "- The model-facing tool is named `bash` for compatibility, but it executes "
        "the selected native shell above.",
        f"- {guidance}",
        "Use this same shell syntax for tool calls and user-facing command examples.",
    ])


@dataclass(slots=True)
class Skill:
    name: str
    description: str
    body: str
    path: Path


@dataclass(slots=True)
class Phase:
    name: str
    description: str
    prompt: str


_PHASES = {
    "design": ("Design a product change, feature, or bug fix", "Design the requested change before implementation. Read the repository instructions, relevant documentation, and current code. Define acceptance criteria and the smallest coherent scope. Stop after the design; do not change production code."),
    "plan": ("Break accepted work into small, verifiable tasks", "Plan the requested work without implementing it. Break the outcome into small ordered tasks with concrete results, dependencies, and verification. Stop before changing production code."),
    "build": ("Implement, test, and self-review a complete change", "Build the requested change completely. Implement the smallest coherent change without placeholders, run focused checks, review the complete diff, fix valid findings, and update documentation when behavior changes."),
    "review": ("Review and improve code, PR feedback, and CI results", "Review the requested scope with fresh context. Inspect the diff and surrounding code for correctness, regressions, security, failure handling, complexity, and tests. Fix valid findings in scope and rerun affected checks."),
}


def resolve_phases(config: Config) -> dict[str, Phase]:
    result = {name: Phase(name, values[0], values[1]) for name, values in _PHASES.items()}
    for name, value in config.phases.items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name) or name in {"help", "clear", "model"}:
            raise ValueError(f"invalid or reserved phase name {name!r}")
        previous = result.get(name, Phase(name, f"Run the {name.replace('_', ' ')} phase", ""))
        result[name] = Phase(name, str(value.get("description") or previous.description).strip(), str(value.get("prompt") or previous.prompt).strip())
        if not result[name].prompt:
            raise ValueError(f"phase {name!r} needs a prompt")
    return result


def load_skills(cwd: Path, home: Path | None = None) -> list[Skill]:
    found: dict[str, Skill] = {}
    home = home or Path.home()
    root = workspace_root(cwd)
    for parent in (
        home / ".neo" / "skills", home / ".hal" / "skills",
        root / ".neo" / "skills", root / ".hal" / "skills",
    ):
        if not parent.is_dir():
            continue
        for path in parent.glob("*/SKILL.md"):
            text = path.read_text(encoding="utf-8")
            meta: dict[str, object] = {}
            body = text
            if text.startswith("---\n"):
                match = re.match(r"---\n(.*?)\n---(?:\n|$)(.*)", text, re.DOTALL)
                if match:
                    meta = yaml.safe_load(match.group(1)) or {}
                    body = match.group(2)
            name = str(meta.get("name") or path.parent.name).strip().lower()
            if body.strip():
                found[name] = Skill(name, str(meta.get("description") or "").strip(), body.strip(), path)
    return sorted(found.values(), key=lambda x: x.name)


def load_agents_files(cwd: Path, home: Path | None = None) -> list[tuple[Path, str]]:
    docs: list[tuple[Path, str]] = []
    home = home or Path.home()
    for global_path in (home / ".neo" / "AGENTS.md", home / ".hal" / "AGENTS.md"):
        if global_path.is_file() and (text := global_path.read_text(encoding="utf-8").strip()):
            docs.append((global_path, text))
    root = workspace_root(cwd).resolve()
    current = cwd.resolve()
    chain = []
    while True:
        chain.append(current)
        if current == root: break
        if root not in current.parents: break
        current = current.parent
    for directory in reversed(chain):
        path = directory / "AGENTS.md"
        try:
            if path.is_file() and path.resolve().is_relative_to(root) and (text := path.read_text(encoding="utf-8").strip()):
                docs.append((path, text))
        except OSError:
            continue
    return docs


def build_system(config: Config, cwd: Path, skills: list[Skill], phases: dict[str, Phase]) -> str:
    text = SYSTEM_PROMPT + "\n\n" + runtime_context(cwd)
    text += (
        "\n\n# Git integration\n"
        f"- Configured backend preference: {config.git_backend}.\n"
        "- Use git_init, git_stage, git_unstage, git_status, git_diff, git_log, "
        "git_commit, and git_push; "
        "do not test for or install a Git executable.\n"
        "- The auto preference selects Dulwich when native Git is unavailable and "
        "Dulwich is installed."
    )
    text += "\n\n# Named phases\n" + "".join(f"\n- `/{p.name}`: {p.description}" for p in phases.values())
    if skills:
        text += "\n\n# Available skills\n" + "".join(f"\n- `${s.name}`" + (f": {s.description}" if s.description else "") for s in skills)
    if config.agents_file:
        docs = load_agents_files(cwd)
        if docs:
            text += "\n\n# Project instructions\nTreat these AGENTS.md files as authoritative user guidance."
            for path, body in docs:
                text += f"\n\n## {path}\n\n{body}"
    return text


def expand_user_input(text: str, skills: list[Skill], phases: dict[str, Phase]) -> tuple[str, str]:
    visible = text
    if text.startswith("/"):
        command, _, args = text[1:].partition(" ")
        if command in phases:
            phase = phases[command]
            request = args.strip() or "Apply this phase to the current repository and conversation context."
            return f"[named phase: {command}]\n{phase.prompt}\n\nUser request:\n{request}", visible
        skill = next((item for item in skills if item.name == command.lower()), None)
        if skill:
            suffix = f"\n\nArguments:\n{args.strip()}" if args.strip() else ""
            return f"[skill: {skill.name}]\n{skill.body}{suffix}", visible
    by_name = {skill.name: skill for skill in skills}
    used = []
    for name in re.findall(r"\$([A-Za-z0-9_-]+)", text):
        name = name.lower()
        if name in by_name and name not in used: used.append(name)
    if used:
        prefix = "\n\n".join(f"[skill: {name}]\n{by_name[name].body}" for name in used)
        return f"{prefix}\n\n{text}", visible
    return text, ""
