from pathlib import Path

from neo.context import load_skills


def test_bundled_repo_summary_skill_is_discoverable() -> None:
    root = Path(__file__).resolve().parents[1]
    skills = load_skills(root, root / "tests" / "missing-home")
    skill = next(item for item in skills if item.name == "repo-summary")
    assert "evidence-based overview" in skill.description
    assert "tests/*.py" in skill.body


def test_agents_example_uses_the_expected_filename_convention() -> None:
    root = Path(__file__).resolve().parents[1]
    path = root / "AGENTS.md.example"
    example = path.read_text(encoding="utf-8")
    assert path.name == "AGENTS.md.example"
    assert "python -m pytest -q" in example
