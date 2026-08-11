from __future__ import annotations

import json
from pathlib import Path

import pytest

from hal.extensions import ExtensionContext
from hal_simple import create_tools


def tools(tmp_path: Path):
    context = ExtensionContext(
        name="simple",
        cwd=tmp_path,
        root=tmp_path,
        settings={"greeting": "Hi"},
    )
    return {tool.spec.name: tool for tool in create_tools(context)}


def test_factory_exports_three_tools(tmp_path: Path) -> None:
    assert set(tools(tmp_path)) == {
        "simple_echo",
        "simple_add",
        "simple_project_info",
    }


def test_echo_uses_extension_config(tmp_path: Path) -> None:
    assert tools(tmp_path)["simple_echo"].run({"message": "HAL"}) == "Hi, HAL"


def test_add_validates_and_adds_numbers(tmp_path: Path) -> None:
    tool = tools(tmp_path)["simple_add"]
    assert json.loads(tool.run({"left": 2, "right": 3.5}))["sum"] == 5.5
    with pytest.raises(ValueError, match="left must be a number"):
        tool.run({"left": "2", "right": 3})


def test_project_info_uses_context(tmp_path: Path) -> None:
    result = json.loads(tools(tmp_path)["simple_project_info"].run({}))
    assert result == {
        "extension": "simple",
        "cwd": str(tmp_path),
        "workspace_root": str(tmp_path),
    }
