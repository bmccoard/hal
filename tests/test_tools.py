from pathlib import Path

import pytest

from neo.tools import EditFileTool, GrepTool


def test_edit_requires_exactly_one_match(tmp_path: Path) -> None:
    path = tmp_path / "a.txt"; path.write_text("x x", encoding="utf-8")
    with pytest.raises(ValueError, match="exactly once"):
        EditFileTool().run({"path": str(path), "old_string": "x", "new_string": "y"})


def test_grep_rejects_path_outside_workspace(tmp_path: Path) -> None:
    root = tmp_path / "root"; root.mkdir()
    with pytest.raises(ValueError, match="escapes"):
        GrepTool(root).run({"pattern": "x", "path": "../outside"})

