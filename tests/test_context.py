from pathlib import Path

from neo.config import Config
from neo.context import (
    build_system,
    expand_user_input,
    load_skills,
    resolve_phases,
    runtime_context,
)


def test_project_skill_overrides_global_and_expands(tmp_path: Path) -> None:
    home = tmp_path / "home"; repo = tmp_path / "repo"; (repo / ".git").mkdir(parents=True)
    for root, body in [(home / ".neo", "global"), (repo / ".neo", "project")]:
        path = root / "skills" / "demo"; path.mkdir(parents=True)
        (path / "SKILL.md").write_text(f"---\nname: demo\ndescription: Demo\n---\n{body}\n", encoding="utf-8")
    skills = load_skills(repo, home)
    assert skills[0].body == "project"
    expanded, visible = expand_user_input("use $demo now", skills, resolve_phases(Config()))
    assert "[skill: demo]\nproject" in expanded
    assert visible == "use $demo now"


def test_runtime_context_identifies_windows_powershell_51(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "neo.context.native_shell",
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
        "neo.context.native_shell",
        lambda _platform: ("PowerShell", "C:/Program Files/PowerShell/7/pwsh.exe"),
    )

    text = runtime_context(tmp_path, platform_name="nt", shell_version="7.5.2")

    assert "Selected native shell: PowerShell" in text
    assert "version 7.5.2" in text
    assert "PowerShell-native command examples" in text
    assert "Windows PowerShell 5.1 does not support" not in text


def test_runtime_context_identifies_unix_bash(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "neo.context.native_shell", lambda _platform: ("Bash", "/bin/bash"),
    )

    text = runtime_context(tmp_path, platform_name="posix", shell_version="GNU bash 5.2")

    assert "Selected native shell: Bash (/bin/bash), version GNU bash 5.2" in text
    assert "Native path separator: `/`" in text
    assert "Use the selected Unix shell syntax" in text


def test_runtime_context_identifies_posix_shell_fallback(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "neo.context.native_shell", lambda _platform: ("POSIX shell", "/bin/sh"),
    )

    text = runtime_context(tmp_path, platform_name="posix", shell_version="")

    assert "Selected native shell: POSIX shell (/bin/sh)" in text
    assert "Use the selected Unix shell syntax" in text


def test_system_prompt_defaults_questions_and_examples_to_read_only(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(
        "neo.context.runtime_context", lambda _cwd: "# Runtime environment\n- test shell",
    )

    system = build_system(Config(agents_file=False), tmp_path, [], resolve_phases(Config()))
    normalized = " ".join(system.split())

    assert "questions, explanations" in normalized
    assert "example requests" in normalized
    assert "do not create or edit files" in normalized
    assert "unless the user explicitly asks" in normalized
    assert "Installing, upgrading, or removing dependencies" in normalized
    assert "obtain the user's approval first" in normalized
    assert "# Runtime environment\n- test shell" in system
