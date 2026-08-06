from pathlib import Path

from neo.config import Config
from neo.context import expand_user_input, load_skills, resolve_phases


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
