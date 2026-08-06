from pathlib import Path

import pytest

from neo.tools import EditFileTool, GrepTool, shell_argv


def test_edit_requires_exactly_one_match(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"; path.write_text("x x", encoding="utf-8")
    with pytest.raises(ValueError, match="exactly once"):
        EditFileTool().run({"path": str(path), "old_string": "x", "new_string": "y"})


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
