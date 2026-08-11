import json
from pathlib import Path
import shutil

import pytest
from dulwich import porcelain
from dulwich.repo import Repo

from hal.git import (
    DulwichGitBackend,
    GitError,
    NativeGitBackend,
    create_git_backend,
    normalize_paths,
)
from hal.git_tools import (
    GitCommitTool,
    GitDiffTool,
    GitInitTool,
    GitLogTool,
    GitPushTool,
    GitStageTool,
    GitUnstageTool,
)
from hal.process import ProcessResult
from hal.tools import MAX_RESULT


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    values = {
        "GIT_AUTHOR_NAME": "HAL Test",
        "GIT_AUTHOR_EMAIL": "hal@example.test",
        "GIT_COMMITTER_NAME": "HAL Test",
        "GIT_COMMITTER_EMAIL": "hal@example.test",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def new_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    porcelain.init(root)
    return root


def exercise_local_backend(backend, root: Path) -> None:
    (root / "one.txt").write_text("one\n", encoding="utf-8")
    assert backend.status().untracked == ["one.txt"]
    backend.stage(["one.txt"])
    assert backend.status().staged == ["one.txt"]
    backend.unstage(["one.txt"])
    assert backend.status().untracked == ["one.txt"]
    commit_id = backend.commit("Add one", ["one.txt"])
    assert len(commit_id) == 40
    assert backend.status().staged == []
    assert backend.status().unstaged == []
    assert backend.status().untracked == []

    (root / "one.txt").write_text("two\n", encoding="utf-8")
    assert "one.txt" in backend.status().unstaged
    backend.stage(["one.txt"])
    backend.unstage(["one.txt"])
    assert backend.status().staged == []
    assert "one.txt" in backend.status().unstaged
    assert "-one" in backend.diff()
    history = backend.log(1)
    assert history[0].commit == commit_id
    assert history[0].subject == "Add one"
    assert history[0].author_email == "hal@example.test"


def test_dulwich_backend_supports_local_workflow_without_git_binary(tmp_path: Path) -> None:
    root = new_repo(tmp_path)
    exercise_local_backend(DulwichGitBackend(root), root)


def test_native_backend_matches_local_workflow_when_available(tmp_path: Path) -> None:
    executable = shutil.which("git")
    if not executable:
        pytest.skip("native Git is unavailable")
    root = new_repo(tmp_path)
    exercise_local_backend(NativeGitBackend(root, executable), root)


def test_auto_backend_falls_back_to_dulwich(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("hal.git.shutil.which", lambda _name: None)
    assert create_git_backend(tmp_path, "auto").name == "dulwich"
    with pytest.raises(GitError, match="not installed"):
        create_git_backend(tmp_path, "native")


def test_dulwich_init_creates_main_without_a_git_executable(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "repo"; root.mkdir()
    monkeypatch.setattr("hal.git.shutil.which", lambda _name: None)
    backend = create_git_backend(root, "auto")

    result = json.loads(GitInitTool(backend).run({}))

    assert result == {
        "backend": "dulwich", "branch": "main", "root": str(root), "initialized": True,
    }
    assert backend.status().branch == "main"
    (root / "README.md").write_text("# New project\n", encoding="utf-8")
    committed = json.loads(GitCommitTool(backend).run({
        "message": "Initial commit", "paths": ["README.md"],
    }))
    assert backend.log(1)[0].commit == committed["commit"]
    with pytest.raises(GitError, match="existing Git workspace"):
        backend.init()


def test_native_init_creates_main_when_available(tmp_path: Path) -> None:
    executable = shutil.which("git")
    if not executable:
        pytest.skip("native Git is unavailable")
    root = tmp_path / "repo"; root.mkdir()
    backend = NativeGitBackend(root, executable)

    assert backend.init() == "main"
    assert backend.status().branch == "main"


def test_initialized_dulwich_repo_allows_yaml_config_commit(tmp_path: Path) -> None:
    root = tmp_path / "repo"; root.mkdir()
    backend = DulwichGitBackend(root)
    GitInitTool(backend).run({})
    (root / "hal.yaml").write_text("provider: openai\n", encoding="utf-8")

    committed = json.loads(GitCommitTool(backend).run({
        "message": "Initial commit", "paths": ["hal.yaml"],
    }))

    assert backend.log(1)[0].commit == committed["commit"]


def test_stage_tool_rejects_sensitive_paths_and_unstage_preserves_files(tmp_path: Path) -> None:
    root = new_repo(tmp_path)
    backend = DulwichGitBackend(root)
    (root / "safe.txt").write_text("safe", encoding="utf-8")
    (root / ".env").write_text("API_KEY=secret", encoding="utf-8")

    with pytest.raises(ValueError, match="refusing to stage"):
        GitStageTool(backend).run({"paths": ["safe.txt", ".env"]})
    GitStageTool(backend).run({"paths": ["safe.txt"]})
    assert backend.status().staged == ["safe.txt"]
    GitUnstageTool(backend).run({"paths": ["safe.txt"]})
    assert (root / "safe.txt").read_text(encoding="utf-8") == "safe"
    assert backend.status().staged == []


def test_diff_tool_omits_sensitive_staged_content(tmp_path: Path) -> None:
    root = new_repo(tmp_path)
    backend = DulwichGitBackend(root)
    (root / "safe.txt").write_text("public content", encoding="utf-8")
    secret = "sk-test-secret-that-must-not-reach-the-model"
    (root / ".env").write_text(f"API_KEY={secret}", encoding="utf-8")
    porcelain.add(root, paths=["safe.txt", ".env"])

    output = GitDiffTool(backend).run({"staged": True})

    assert "public content" in output
    assert secret not in output
    assert "omitted sensitive paths: .env" in output
    with pytest.raises(ValueError, match="refusing to display a diff"):
        GitDiffTool(backend).run({"staged": True, "paths": [".env"]})


def test_dulwich_diff_bounds_large_output_and_preserves_head_and_tail(tmp_path: Path) -> None:
    root = new_repo(tmp_path)
    backend = DulwichGitBackend(root)
    (root / "large.txt").write_text(
        "BEGIN\n" + "x\n" * MAX_RESULT + "END\n", encoding="utf-8",
    )
    porcelain.add(root, paths=["large.txt"])

    output = backend.diff(staged=True)

    assert "BEGIN" in output[:1000]
    assert "END" in output[-1000:]
    assert "bytes omitted" in output
    assert len(output.encode("utf-8")) <= MAX_RESULT


def test_native_git_refuses_truncated_path_output(monkeypatch, tmp_path: Path) -> None:
    backend = NativeGitBackend(tmp_path, "git")
    monkeypatch.setattr(
        backend, "_run",
        lambda *_args, **_kwargs: ProcessResult(
            ["git"], 0, "partial\0...", "", True, False,
        ),
    )

    with pytest.raises(GitError, match="exceeded the safety limit"):
        backend._names(["status"], None)


def test_commit_refuses_unrelated_staged_paths(tmp_path: Path) -> None:
    root = new_repo(tmp_path)
    backend = DulwichGitBackend(root)
    (root / "intended.txt").write_text("wanted", encoding="utf-8")
    (root / "unrelated.txt").write_text("leave me staged", encoding="utf-8")
    porcelain.add(root, paths=["unrelated.txt"])

    with pytest.raises(GitError, match="already-staged"):
        backend.commit("Only intended", ["intended.txt"])


def test_commit_tool_is_local_only_and_push_is_explicit(tmp_path: Path) -> None:
    root = new_repo(tmp_path)
    remote = tmp_path / "remote.git"
    porcelain.init(remote, bare=True)
    backend = DulwichGitBackend(root)
    branch = backend.status().branch
    branch_ref = f"refs/heads/{branch}".encode("utf-8")
    (root / "a.txt").write_text("content", encoding="utf-8")

    committed = json.loads(GitCommitTool(backend).run({
        "message": "Add content", "paths": ["a.txt"],
    }))
    assert committed["pushed"] is False
    with Repo(remote) as remote_repo:
        assert branch_ref not in remote_repo.refs

    pushed = json.loads(GitPushTool(backend).run({"remote": str(remote)}))
    assert f"pushed {branch}" in pushed["result"]
    with Repo(remote) as remote_repo:
        assert remote_repo.refs[branch_ref].decode("ascii") == committed["commit"]


def test_git_paths_are_repository_relative_and_cannot_escape(tmp_path: Path) -> None:
    root = new_repo(tmp_path)
    assert normalize_paths(root, ["folder/../file.txt", "file.txt"]) == ["file.txt"]
    with pytest.raises(ValueError, match="escapes"):
        normalize_paths(root, ["../outside.txt"])
    with pytest.raises(ValueError, match="working-tree"):
        normalize_paths(root, [".git/config"])


@pytest.mark.parametrize(
    "path", [".env", ".hal/auth.json", ".neo/auth.json"],
)
def test_commit_tool_rejects_known_local_configuration(path: str, tmp_path: Path) -> None:
    root = new_repo(tmp_path)
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("secret-placeholder", encoding="utf-8")

    with pytest.raises(ValueError, match="credentials"):
        GitCommitTool(DulwichGitBackend(root)).run({
            "message": "Do not commit this", "paths": [path],
        })


def test_push_tool_rejects_option_and_control_character_arguments(tmp_path: Path) -> None:
    tool = GitPushTool(DulwichGitBackend(new_repo(tmp_path)))
    with pytest.raises(ValueError, match="must not begin"):
        tool.run({"remote": "--force"})
    with pytest.raises(ValueError, match="control characters"):
        tool.run({"remote": "origin\nother"})


def test_git_tools_reject_malformed_scalar_arguments(tmp_path: Path) -> None:
    backend = DulwichGitBackend(new_repo(tmp_path))
    with pytest.raises(ValueError, match="staged must be"):
        GitDiffTool(backend).run({"staged": "false"})
    with pytest.raises(ValueError, match="count must be"):
        GitLogTool(backend).run({"count": True})
