import os
from pathlib import Path
import sys
import time

import pytest

from neo.cancellation import CancelledError, CancellationToken
from neo.tools import (
    BashTool,
    MAX_RESULT,
    EditFileTool,
    GrepTool,
    ReadFileTool,
    WriteFileTool,
    default_registry,
    shell_argv,
)


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

    monkeypatch.setattr("neo.tools.os.replace", record_replace)
    WriteFileTool().run({"path": str(path), "content": "hello world"})
    EditFileTool().run({
        "path": str(path), "old_string": "world", "new_string": "Neo",
    })

    assert path.read_text(encoding="utf-8") == "hello Neo"
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


def test_shell_uses_powershell_on_windows(monkeypatch) -> None:
    monkeypatch.setattr("neo.tools.shutil.which", lambda name: "C:/PowerShell/pwsh.exe" if name == "pwsh" else None)
    argv = shell_argv("Get-ChildItem", platform="nt")
    assert argv[0] == "C:/PowerShell/pwsh.exe"
    assert argv[-2:] == ["-Command", "Get-ChildItem"]


def test_shell_uses_bash_on_unix(monkeypatch) -> None:
    monkeypatch.setattr("neo.tools.shutil.which", lambda name: "/bin/bash" if name == "bash" else None)
    assert shell_argv("ls -la", platform="posix") == ["/bin/bash", "-lc", "ls -la"]


def test_shell_has_platform_fallbacks(monkeypatch) -> None:
    monkeypatch.setattr("neo.tools.shutil.which", lambda _name: None)
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
        "git_status", "git_diff", "git_log", "git_commit", "git_push",
    } <= names
