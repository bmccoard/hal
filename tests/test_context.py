from pathlib import Path

from hal.config import Config
from hal.context import (
    build_system,
    expand_user_input,
    load_skills,
    load_agents_files,
    resolve_phases,
    runtime_context,
)


def test_project_skill_overrides_global_and_expands(tmp_path: Path) -> None:
    home = tmp_path / "home"; repo = tmp_path / "repo"; (repo / ".git").mkdir(parents=True)
    for root, body in [
        (home / ".hal", "global"),
        (repo / ".hal", "project"),
    ]:
        path = root / "skills" / "demo"; path.mkdir(parents=True)
        (path / "SKILL.md").write_text(f"---\nname: demo\ndescription: Demo\n---\n{body}\n", encoding="utf-8")
    skills = load_skills(repo, home)
    assert skills[0].body == "project"
    expanded, visible = expand_user_input("use $demo now", skills, resolve_phases(Config()))
    assert "[skill: demo]\nproject" in expanded
    assert visible == "use $demo now"


def test_hal_global_agents_file_is_loaded(tmp_path: Path) -> None:
    home = tmp_path / "home"; repo = tmp_path / "repo"; (repo / ".git").mkdir(parents=True)
    (home / ".hal").mkdir(parents=True)
    (home / ".hal" / "AGENTS.md").write_text("hal", encoding="utf-8")

    docs = load_agents_files(repo, home)

    assert [body for _path, body in docs] == ["hal"]


def test_runtime_context_identifies_windows_powershell_51(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "hal.context.native_shell",
        lambda _platform: ("Windows PowerShell", "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"),
    )

    text = runtime_context(tmp_path, platform_name="nt", shell_version="5.1.22621.2506")

    assert "Operating system: Windows (nt)" in text
    assert "Selected native shell: Windows PowerShell" in text
    assert "version 5.1.22621.2506" in text
    assert "Do not use Bash utilities" in text
    assert "`&&`" in text
    assert "user-facing command examples" in text


def test_runtime_context_identifies_powershell_7(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "hal.context.native_shell",
        lambda _platform: ("PowerShell", "C:/Program Files/PowerShell/7/pwsh.exe"),
    )

    text = runtime_context(tmp_path, platform_name="nt", shell_version="7.5.2")

    assert "Selected native shell: PowerShell" in text
    assert "version 7.5.2" in text
    assert "PowerShell-native command examples" in text
    assert "Windows PowerShell 5.1 does not support" not in text


def test_runtime_context_identifies_unix_bash(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "hal.context.native_shell", lambda _platform: ("Bash", "/bin/bash"),
    )

    text = runtime_context(tmp_path, platform_name="posix", shell_version="GNU bash 5.2")

    assert "Selected native shell: Bash (/bin/bash), version GNU bash 5.2" in text
    assert "Native path separator: `/`" in text
    assert "Use the selected Unix shell syntax" in text


def test_runtime_context_identifies_posix_shell_fallback(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "hal.context.native_shell", lambda _platform: ("POSIX shell", "/bin/sh"),
    )

    text = runtime_context(tmp_path, platform_name="posix", shell_version="")

    assert "Selected native shell: POSIX shell (/bin/sh)" in text
    assert "Use the selected Unix shell syntax" in text


def test_system_prompt_defaults_questions_and_examples_to_read_only(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(
        "hal.context.runtime_context", lambda _cwd: "# Runtime environment\n- test shell",
    )

    system = build_system(Config(agents_file=False), tmp_path, [], resolve_phases(Config()))
    normalized = " ".join(system.split())

    assert "questions, explanations" in normalized
    assert "example requests" in normalized
    assert "do not create or edit files" in normalized
    assert "unless the user explicitly asks" in normalized
    assert "Installing, upgrading, or removing dependencies" in normalized
    assert "obtain the user's approval first" in normalized
    assert 'Treat "check in" and "commit"' in normalized
    assert "Do not invoke Git through the shell" in normalized
    assert "Configured backend preference: auto" in normalized
    assert "Use git_init" in normalized
    assert "git_stage" in normalized
    assert "Never read, quote, rewrite, or stage .env or .env.* files" in normalized
    assert "including email files, YAML files, .hal/auth.json" in normalized
    assert "Never push" in normalized
    assert "# Runtime environment\n- test shell" in system
