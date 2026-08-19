import json
import os
from pathlib import Path
import sys
import time

import pytest

from hal.cancellation import CancelledError, CancellationToken
from hal.tools import (
    BashTool,
    MAX_RESULT,
    EditFileTool,
    GrepTool,
    ReadFileTool,
    WriteFileTool,
    Registry,
    Tool,
    ToolEffect,
    default_registry,
    shell_argv,
)
from hal.models import ToolSpec


class NamedTool(Tool):
    def __init__(self, name: str) -> None:
        self.name = name

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, "test tool", {"type": "object"})

    def run(self, arguments, cancellation=None) -> str:
        return "ok"


def test_edit_requires_exactly_one_match(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"; path.write_text("x x", encoding="utf-8")
    with pytest.raises(ValueError, match="exactly once"):
        EditFileTool().run({"path": str(path), "old_string": "x", "new_string": "y"})


def test_write_and_edit_replace_atomically_and_preserve_mode(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "nested" / "a.txt"
    path.parent.mkdir()
    path.write_text("before", encoding="utf-8")
    path.chmod(0o640)
    original_replace = os.replace
    replacements = []

    def record_replace(source, destination):
        replacements.append((Path(source), Path(destination)))
        original_replace(source, destination)

    monkeypatch.setattr("hal.tools.os.replace", record_replace)
    WriteFileTool().run({"path": str(path), "content": "hello world"})
    EditFileTool().run({
        "path": str(path), "old_string": "world", "new_string": "HAL",
    })

    assert path.read_text(encoding="utf-8") == "hello HAL"
    assert len(replacements) == 2
    assert all(destination == path for _, destination in replacements)
    assert not list(path.parent.glob(".a.txt.*"))
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o640


def test_file_tools_reject_empty_paths() -> None:
    with pytest.raises(ValueError, match="path"):
        ReadFileTool().run({"path": ""})
    with pytest.raises(ValueError, match="path"):
        WriteFileTool().run({"path": "", "content": "x"})
    with pytest.raises(ValueError, match="path"):
        EditFileTool().run({"path": "", "old_string": "x", "new_string": "y"})


@pytest.mark.parametrize("name", [".env", ".env.local", ".ENV.production"])
def test_file_tools_reject_env_files(name: str, tmp_path: Path) -> None:
    path = tmp_path / name
    path.write_text("SECRET=original", encoding="utf-8")

    with pytest.raises(PermissionError, match="protected .env file"):
        ReadFileTool().run({"path": str(path)})
    with pytest.raises(PermissionError, match="protected .env file"):
        WriteFileTool().run({"path": str(path), "content": "SECRET=changed"})
    with pytest.raises(PermissionError, match="protected .env file"):
        EditFileTool().run({
            "path": str(path), "old_string": "original", "new_string": "changed",
        })
    assert path.read_text(encoding="utf-8") == "SECRET=original"


@pytest.mark.parametrize("name", ["mail.eml", "settings.yaml", "hal.local.yaml", "auth.json"])
def test_file_tools_allow_email_yaml_and_other_config(name: str, tmp_path: Path) -> None:
    path = tmp_path / name

    WriteFileTool().run({"path": str(path), "content": "before"})
    assert ReadFileTool().run({"path": str(path)}) == "before"
    EditFileTool().run({
        "path": str(path), "old_string": "before", "new_string": "after",
    })
    assert path.read_text(encoding="utf-8") == "after"


def test_grep_skips_env_files_but_searches_yaml_and_email(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("needle-secret", encoding="utf-8")
    (tmp_path / "settings.yaml").write_text("needle-yaml", encoding="utf-8")
    (tmp_path / "mail.eml").write_text("needle-email", encoding="utf-8")

    matches = json.loads(GrepTool(tmp_path).run({"pattern": "needle"}))["matches"]

    assert {match["path"] for match in matches} == {"settings.yaml", "mail.eml"}
    with pytest.raises(PermissionError, match="protected .env file"):
        GrepTool(tmp_path).run({"pattern": "needle", "path": ".env"})


def test_read_file_requires_paging_for_oversized_files(tmp_path: Path) -> None:
    path = tmp_path / "large.txt"
    path.write_bytes(b"x" * (MAX_RESULT + 1))

    with pytest.raises(ValueError, match="use offset/limit"):
        ReadFileTool().run({"path": str(path)})
    with pytest.raises(ValueError, match="selection exceeds"):
        ReadFileTool().run({"path": str(path), "offset": 1, "limit": 1})


def test_read_file_pages_by_one_indexed_line_window(tmp_path: Path) -> None:
    path = tmp_path / "lines.txt"
    path.write_bytes(b"first\r\nsecond\nthird")

    assert ReadFileTool().run({
        "path": str(path), "offset": 2, "limit": 1,
    }) == "second\n"
    assert ReadFileTool().run({
        "path": str(path), "offset": 3,
    }) == "third"
    with pytest.raises(ValueError, match="offset 4 is past end"):
        ReadFileTool().run({"path": str(path), "offset": 4, "limit": 1})
    with pytest.raises(ValueError, match="offset must be"):
        ReadFileTool().run({"path": str(path), "offset": 0})
    with pytest.raises(ValueError, match="limit must be"):
        ReadFileTool().run({"path": str(path), "limit": 0})


def test_read_file_treats_trailing_newline_as_empty_final_line(tmp_path: Path) -> None:
    path = tmp_path / "trailing.txt"
    path.write_bytes(b"first\n")

    assert ReadFileTool().run({
        "path": str(path), "offset": 2, "limit": 1,
    }) == ""
    with pytest.raises(ValueError, match="offset 3 is past end"):
        ReadFileTool().run({"path": str(path), "offset": 3, "limit": 1})


def test_grep_rejects_path_outside_workspace(tmp_path: Path) -> None:
    root = tmp_path / "root"; root.mkdir()
    with pytest.raises(ValueError, match="escapes"):
        GrepTool(root).run({"pattern": "x", "path": "../outside"})


def test_grep_literal_mode_matches_regex_metacharacters(tmp_path: Path) -> None:
    path = tmp_path / "README.md"
    path.write_text("- **Workflows** are useful\n", encoding="utf-8")

    result = GrepTool(tmp_path).run({
        "pattern": "- **Workflows**", "path": "README.md", "literal": True,
    })

    assert json.loads(result)["matches"][0]["line"] == 1


def test_tool_schemas_explain_weak_model_constraints(tmp_path: Path) -> None:
    specs = {spec.name: spec for spec in default_registry(
        tmp_path, tmp_path, git_backend="dulwich",
    ).specs}

    assert specs["read_file"].input_schema["properties"]["offset"]["minimum"] == 1
    assert specs["grep"].input_schema["properties"]["literal"]["type"] == "boolean"


def test_glob_prunes_dependency_and_cache_directories(tmp_path: Path) -> None:
    from hal.tools import GlobTool

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("", encoding="utf-8")
    for directory in (".git", ".venv", "node_modules", "__pycache__"):
        path = tmp_path / directory
        path.mkdir()
        (path / "ignored.py").write_text("", encoding="utf-8")

    matches = json.loads(GlobTool(tmp_path).run({"pattern": "**/*"}))["matches"]

    assert matches == ["src/app.py"]


def test_shell_uses_powershell_on_windows(monkeypatch) -> None:
    monkeypatch.setattr("hal.tools.shutil.which", lambda name: "C:/PowerShell/pwsh.exe" if name == "pwsh" else None)
    argv = shell_argv("Get-ChildItem", platform="nt")
    assert argv[0] == "C:/PowerShell/pwsh.exe"
    assert argv[-2:] == ["-Command", "Get-ChildItem"]


def test_shell_uses_bash_on_unix(monkeypatch) -> None:
    monkeypatch.setattr("hal.tools.shutil.which", lambda name: "/bin/bash" if name == "bash" else None)
    assert shell_argv("ls -la", platform="posix") == ["/bin/bash", "-lc", "ls -la"]


def test_shell_has_platform_fallbacks(monkeypatch) -> None:
    monkeypatch.setattr("hal.tools.shutil.which", lambda _name: None)
    monkeypatch.setenv("COMSPEC", "C:/Windows/System32/cmd.exe")
    assert shell_argv("dir", platform="nt") == ["C:/Windows/System32/cmd.exe", "/d", "/s", "/c", "dir"]
    assert shell_argv("ls", platform="posix") == ["/bin/sh", "-c", "ls"]


def test_shell_cancellation_terminates_the_process_tree(tmp_path: Path) -> None:
    command = f'"{sys.executable}" -c "import time; time.sleep(10)"'
    started = time.monotonic()

    with pytest.raises(CancelledError, match="timed out"):
        BashTool(tmp_path).run(
            {"command": command}, CancellationToken.with_timeout(.1),
        )

    assert time.monotonic() - started < 3


def test_default_registry_exposes_structured_git_tools(tmp_path: Path) -> None:
    names = {spec.name for spec in default_registry(
        tmp_path, tmp_path, git_backend="dulwich",
    ).specs}
    assert {
        "git_init", "git_stage", "git_unstage", "git_status", "git_diff",
        "git_log", "git_commit", "git_push", "git_checkout", "git_restore",
        "git_show",
    } <= names


def test_default_registry_exposes_document_tools(tmp_path: Path) -> None:
    registry = default_registry(tmp_path, tmp_path, git_backend="dulwich")
    names = {spec.name for spec in registry.specs}

    assert {"pdf_read", "pdf_write", "pdf_form_write", "docx_read", "docx_write"} <= names
    assert registry.metadata("pdf_read")["effect"] == "read_only"
    assert registry.metadata("docx_write")["effect"] == "mutating"


def test_registry_can_be_extended_but_rejects_name_collisions() -> None:
    registry = Registry([NamedTool("one")])
    registry.extend([NamedTool("two")])
    assert {spec.name for spec in registry.specs} == {"one", "two"}
    with pytest.raises(ValueError, match="duplicate tool name: one"):
        registry.extend([NamedTool("one")])


def test_local_write_policy_allows_workspace_and_denies_outside_headlessly(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"; root.mkdir()
    outside = tmp_path / "outside.txt"
    registry = Registry(
        [WriteFileTool(), EditFileTool()], write_root=root, cwd=root,
    )

    registry.run("write_file", {"path": "inside.txt", "content": "ok"})
    assert (root / "inside.txt").read_text(encoding="utf-8") == "ok"
    with pytest.raises(PermissionError, match="outside workspace was denied"):
        registry.run("write_file", {"path": str(outside), "content": "bad"})
    assert not outside.exists()


def test_local_write_policy_can_approve_outside_path(tmp_path: Path) -> None:
    root = tmp_path / "repo"; root.mkdir()
    outside = tmp_path / "outside.txt"
    prompts = []
    registry = Registry(
        [WriteFileTool()], confirm=lambda prompt: prompts.append(prompt) or True,
        write_root=root, cwd=root,
    )

    registry.run("write_file", {"path": str(outside), "content": "approved"})

    assert outside.read_text(encoding="utf-8") == "approved"
    assert "outside workspace" in prompts[0]


def test_local_write_policy_resolves_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "repo"; root.mkdir()
    outside = tmp_path / "outside"; outside.mkdir()
    try:
        (root / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    registry = Registry([WriteFileTool()], write_root=root, cwd=root)

    with pytest.raises(PermissionError, match="outside workspace was denied"):
        registry.run(
            "write_file", {"path": "link/escaped.txt", "content": "bad"},
        )
    assert not (outside / "escaped.txt").exists()


def test_workflow_write_file_cannot_replace_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "README.md"
    target.write_text("original", encoding="utf-8")
    registry = Registry([WriteFileTool()], cwd=tmp_path)

    with pytest.raises(PermissionError, match="use edit_file"):
        registry.run(
            "write_file", {"path": "README.md", "content": "replacement"},
            protect_existing_files=True,
        )
    assert target.read_text(encoding="utf-8") == "original"


@pytest.mark.parametrize("policy", ["approve", "deny"])
def test_bash_policy_denies_without_approval(tmp_path: Path, policy: str) -> None:
    registry = Registry([BashTool(tmp_path)], cwd=tmp_path, bash_policy=policy)

    with pytest.raises(PermissionError, match="bash"):
        registry.run("bash", {"command": "printf should-not-run"})


def test_parallel_safety_respects_tool_policy_and_approval_barriers(tmp_path: Path) -> None:
    registry = Registry(
        [ReadFileTool(), BashTool(tmp_path)], approvals=["read_file"], cwd=tmp_path,
    )

    assert registry.is_parallel_safe("read_file") is False
    registry.approvals.clear()
    assert registry.is_parallel_safe("read_file") is True
    assert registry.is_parallel_safe("read_file", denied={"read_file"}) is False
    assert registry.is_parallel_safe("read_file", allowed={"bash"}) is False
    assert registry.is_parallel_safe("bash") is False
    assert registry.is_parallel_safe("missing") is False


def test_registry_exposes_resolved_tool_effect_metadata(tmp_path: Path) -> None:
    registry = Registry(
        [ReadFileTool(), WriteFileTool(), BashTool(tmp_path)],
        approvals=["read_file"], bash_policy="approve", cwd=tmp_path,
    )

    assert registry.metadata("read_file") == {
        "effect": ToolEffect.READ_ONLY.value,
        "parallel_safe": True,
        "approval_gated": True,
    }
    assert registry.metadata("write_file")["effect"] == "mutating"
    assert registry.metadata("bash") == {
        "effect": "external",
        "parallel_safe": False,
        "approval_gated": True,
    }
    with pytest.raises(ValueError, match="unknown tool"):
        registry.metadata("missing")
