from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "new-project.py"


def load_generator():
    spec = importlib.util.spec_from_file_location("new_project", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_create_project_copies_and_specializes_template(tmp_path: Path) -> None:
    generator = load_generator()
    destination = generator.create_project("Weather-Tools", tmp_path)

    assert destination == tmp_path / "Weather-Tools"
    assert (destination / "src" / "hal_weather_tools" / "tools.py").is_file()
    pyproject = (destination / "pyproject.toml").read_text(encoding="utf-8")
    assert 'name = "hal-weather-tools"' in pyproject
    assert 'weather-tools = "hal_weather_tools:create_tools"' in pyproject
    assert "hal_simple" not in pyproject
    generated_tools = (
        destination / "src" / "hal_weather_tools" / "tools.py"
    ).read_text(encoding="utf-8")
    assert '"weather_tools_echo"' in generated_tools
    assert '"weather-tools_echo"' not in generated_tools
    assert (destination / ".env.example").read_text(encoding="utf-8") == (
        "OPENROUTER_API_KEY='token'"
    )
    hal_config = (destination / "hal.yaml.example").read_text(encoding="utf-8")
    assert "  - weather-tools" in hal_config
    assert "  weather-tools:" in hal_config
    assert "  - simple" not in hal_config


def test_create_project_refuses_existing_destination(tmp_path: Path) -> None:
    generator = load_generator()
    (tmp_path / "already-here").mkdir()

    with pytest.raises(FileExistsError, match="project already exists"):
        generator.create_project("already-here", tmp_path)


def test_cli_prints_environment_setup_instructions(tmp_path: Path, monkeypatch, capsys) -> None:
    generator = load_generator()
    destination = tmp_path / "demo"
    monkeypatch.setattr(generator, "create_project", lambda _name: destination)

    assert generator.main(["demo"]) == 0
    output = capsys.readouterr().out
    assert "deactivate" in output
    assert "python -m venv .venv" in output
    assert "source .venv/Scripts/activate" in output
    assert ".\\.venv\\Scripts\\Activate.ps1" in output
    assert 'python -m pip install -e ".[dev]"' in output
    assert 'python -m pip install -e "../hal[dev]"' in output
    assert output.index('"../hal[dev]"') < output.index('".[dev]"')


@pytest.mark.parametrize("name", ["../escape", "two words", ".hidden", ""])
def test_create_project_rejects_unsafe_names(tmp_path: Path, name: str) -> None:
    generator = load_generator()
    with pytest.raises(ValueError, match="project name"):
        generator.create_project(name, tmp_path)
