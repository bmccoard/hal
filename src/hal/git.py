from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import io
import os
from pathlib import Path
import shutil
import signal
import subprocess
from typing import Protocol

from dulwich import porcelain
from dulwich.errors import NotGitRepository
from dulwich.index import IndexEntry
from dulwich.repo import Repo

from .cancellation import CancelledError, CancellationToken, cancellation_or_default


class GitError(RuntimeError):
    pass


@dataclass(slots=True)
class GitStatus:
    branch: str
    staged: list[str]
    unstaged: list[str]
    untracked: list[str]


@dataclass(slots=True)
class GitCommit:
    commit: str
    author_name: str
    author_email: str
    timestamp: str
    subject: str


class GitBackend(Protocol):
    name: str
    root: Path

    def init(self, cancellation: CancellationToken | None = None) -> str: ...
    def is_repository(self, cancellation: CancellationToken | None = None) -> bool: ...
    def status(self, cancellation: CancellationToken | None = None) -> GitStatus: ...
    def stage(self, paths: list[str],
              cancellation: CancellationToken | None = None) -> None: ...
    def unstage(self, paths: list[str],
                cancellation: CancellationToken | None = None) -> None: ...
    def diff(self, staged: bool = False, paths: list[str] | None = None,
             cancellation: CancellationToken | None = None) -> str: ...
    def log(self, count: int = 10,
            cancellation: CancellationToken | None = None) -> list[GitCommit]: ...
    def commit(self, message: str, paths: list[str],
               cancellation: CancellationToken | None = None) -> str: ...
    def push(self, remote: str = "origin", branch: str = "",
             cancellation: CancellationToken | None = None) -> str: ...


def _decode(value: bytes | str) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


def _paths(values: list[bytes | str]) -> list[str]:
    return sorted({_decode(value).replace("\\", "/") for value in values})


def normalize_paths(root: Path, values: object) -> list[str]:
    if not isinstance(values, list) or not values:
        raise ValueError("paths must be a non-empty list of repository files")
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("paths must contain non-empty strings")
        candidate = Path(value)
        resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
        try:
            relative = resolved.relative_to(root.resolve())
        except ValueError as exc:
            raise ValueError(f"git path escapes repository: {value}") from exc
        if not relative.parts or relative.parts[0].casefold() == ".git":
            raise ValueError(f"git path is not a working-tree file: {value}")
        path = relative.as_posix()
        if path not in normalized:
            normalized.append(path)
    return normalized


def _terminate(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        taskkill = shutil.which("taskkill")
        if taskkill:
            try:
                subprocess.run(
                    [taskkill, "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True, text=True, timeout=5,
                )
            except (OSError, subprocess.SubprocessError):
                process.terminate()
        else:
            process.terminate()
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        process.wait(timeout=.5)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


class NativeGitBackend:
    name = "native"

    def __init__(self, root: Path, executable: str) -> None:
        self.root = root.resolve()
        self.executable = executable

    def _run(self, arguments: list[str], cancellation: CancellationToken | None = None,
             check: bool = True) -> subprocess.CompletedProcess[str]:
        cancellation = cancellation_or_default(cancellation)
        cancellation.raise_if_cancelled()
        kwargs: dict[str, object] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        process = subprocess.Popen(
            [self.executable, *arguments], cwd=self.root,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", **kwargs,
        )
        try:
            while True:
                cancellation.raise_if_cancelled()
                try:
                    stdout, stderr = process.communicate(timeout=.1)
                    break
                except subprocess.TimeoutExpired:
                    continue
        except CancelledError:
            _terminate(process)
            process.communicate()
            raise
        result = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
        if check and result.returncode:
            detail = (result.stderr or result.stdout).strip()
            raise GitError(detail or f"git exited with status {result.returncode}")
        cancellation.raise_if_cancelled()
        return result

    def is_repository(self, cancellation: CancellationToken | None = None) -> bool:
        result = self._run(
            ["rev-parse", "--is-inside-work-tree"], cancellation, check=False,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"

    def init(self, cancellation: CancellationToken | None = None) -> str:
        cancellation = cancellation_or_default(cancellation)
        cancellation.raise_if_cancelled()
        if self.is_repository(cancellation) or (self.root / ".git").exists():
            raise GitError(f"refusing to initialize an existing Git workspace: {self.root}")
        self._run(["init", "--quiet"], cancellation)
        self._run(["symbolic-ref", "HEAD", "refs/heads/main"], cancellation)
        cancellation.raise_if_cancelled()
        return "main"

    def _require_repository(self, cancellation: CancellationToken | None = None) -> None:
        if not self.is_repository(cancellation):
            raise GitError(f"not a Git repository: {self.root}")

    def _names(self, arguments: list[str], cancellation: CancellationToken | None) -> list[str]:
        output = self._run(arguments, cancellation).stdout
        return sorted({value.replace("\\", "/") for value in output.split("\0") if value})

    def status(self, cancellation: CancellationToken | None = None) -> GitStatus:
        self._require_repository(cancellation)
        branch_result = self._run(
            ["symbolic-ref", "--quiet", "--short", "HEAD"], cancellation,
            check=False,
        )
        branch = branch_result.stdout.strip() or "(detached)"
        return GitStatus(
            branch=branch,
            staged=self._names(["diff", "--cached", "--name-only", "-z"], cancellation),
            unstaged=self._names(["diff", "--name-only", "-z"], cancellation),
            untracked=self._names(
                ["ls-files", "--others", "--exclude-standard", "-z"], cancellation,
            ),
        )

    def stage(self, paths: list[str],
              cancellation: CancellationToken | None = None) -> None:
        self._require_repository(cancellation)
        self._run(["add", "--", *paths], cancellation)

    def unstage(self, paths: list[str],
                cancellation: CancellationToken | None = None) -> None:
        self._require_repository(cancellation)
        has_head = not self._run(
            ["rev-parse", "--verify", "HEAD"], cancellation, check=False,
        ).returncode
        arguments = (["reset", "--quiet", "HEAD", "--"] if has_head else [
            "rm", "--cached", "--quiet", "--ignore-unmatch", "--",
        ])
        self._run([*arguments, *paths], cancellation)

    def diff(self, staged: bool = False, paths: list[str] | None = None,
             cancellation: CancellationToken | None = None) -> str:
        self._require_repository(cancellation)
        arguments = ["diff"]
        if staged:
            arguments.append("--cached")
        if paths:
            arguments.extend(["--", *paths])
        return self._run(arguments, cancellation).stdout

    def log(self, count: int = 10,
            cancellation: CancellationToken | None = None) -> list[GitCommit]:
        self._require_repository(cancellation)
        if self._run(["rev-parse", "--verify", "HEAD"], cancellation, check=False).returncode:
            return []
        result = self._run([
            "log", f"--max-count={count}",
            "--format=%H%x1f%an%x1f%ae%x1f%aI%x1f%s",
        ], cancellation, check=False)
        if result.returncode:
            raise GitError((result.stderr or result.stdout).strip())
        commits: list[GitCommit] = []
        for line in result.stdout.splitlines():
            fields = line.split("\x1f", 4)
            if len(fields) == 5:
                commits.append(GitCommit(*fields))
        return commits

    def commit(self, message: str, paths: list[str],
               cancellation: CancellationToken | None = None) -> str:
        current = self.status(cancellation)
        outside = sorted(set(current.staged) - set(paths))
        if outside:
            raise GitError(
                "refusing to include already-staged paths outside this commit: "
                + ", ".join(outside)
            )
        self._run(["add", "--", *paths], cancellation)
        staged = self.status(cancellation).staged
        if not staged:
            raise GitError("no changes are staged for commit")
        outside = sorted(set(staged) - set(paths))
        if outside:
            raise GitError("refusing to commit unexpected staged paths: " + ", ".join(outside))
        self._run(["commit", "-m", message], cancellation)
        return self._run(["rev-parse", "HEAD"], cancellation).stdout.strip()

    def push(self, remote: str = "origin", branch: str = "",
             cancellation: CancellationToken | None = None) -> str:
        current = self.status(cancellation)
        branch = branch or current.branch
        if branch == "(detached)":
            raise GitError("cannot push a detached HEAD without an explicit branch")
        self._run(["push", "--", remote, branch], cancellation)
        return f"pushed {branch} to {remote}"


class DulwichGitBackend:
    name = "dulwich"

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _repo(self) -> Repo:
        try:
            return Repo.discover(self.root)
        except NotGitRepository as exc:
            raise GitError(f"not a Git repository: {self.root}") from exc

    def init(self, cancellation: CancellationToken | None = None) -> str:
        cancellation = cancellation_or_default(cancellation)
        cancellation.raise_if_cancelled()
        if self.is_repository(cancellation) or (self.root / ".git").exists():
            raise GitError(f"refusing to initialize an existing Git workspace: {self.root}")
        try:
            repo = porcelain.init(self.root)
            try:
                repo.refs.set_symbolic_ref(b"HEAD", b"refs/heads/main")
            finally:
                repo.close()
        except CancelledError:
            raise
        except Exception as exc:
            raise GitError(f"could not initialize repository at {self.root}: {exc}") from exc
        cancellation.raise_if_cancelled()
        return "main"

    def is_repository(self, cancellation: CancellationToken | None = None) -> bool:
        cancellation = cancellation_or_default(cancellation)
        cancellation.raise_if_cancelled()
        try:
            repo = Repo.discover(self.root)
        except NotGitRepository:
            return False
        repo.close()
        cancellation.raise_if_cancelled()
        return True

    def status(self, cancellation: CancellationToken | None = None) -> GitStatus:
        cancellation = cancellation_or_default(cancellation)
        cancellation.raise_if_cancelled()
        with self._repo() as repo:
            value = porcelain.status(repo, untracked_files="all")
            try:
                branch = _decode(porcelain.active_branch(repo))
            except (KeyError, ValueError):
                branch = "(detached)"
        cancellation.raise_if_cancelled()
        staged = [item for group in value.staged.values() for item in group]
        return GitStatus(branch, _paths(staged), _paths(value.unstaged), _paths(value.untracked))

    def stage(self, paths: list[str],
              cancellation: CancellationToken | None = None) -> None:
        cancellation = cancellation_or_default(cancellation)
        cancellation.raise_if_cancelled()
        self._repo().close()
        porcelain.add(self.root, paths=paths)
        cancellation.raise_if_cancelled()

    def unstage(self, paths: list[str],
                cancellation: CancellationToken | None = None) -> None:
        cancellation = cancellation_or_default(cancellation)
        cancellation.raise_if_cancelled()
        with self._repo() as repo:
            index = repo.open_index()
            try:
                commit = repo[repo.head()]
                tree = repo[commit.tree]
            except KeyError:
                tree = None
            for path in paths:
                cancellation.raise_if_cancelled()
                encoded = path.encode("utf-8")
                try:
                    mode, sha = tree.lookup_path(repo.object_store.__getitem__, encoded) if tree else (0, b"")
                except KeyError:
                    mode, sha = 0, b""
                if not sha:
                    try:
                        del index[encoded]
                    except KeyError:
                        pass
                    continue
                # Do not reuse the modified working file's stat metadata for the
                # restored HEAD object. Zero values force Dulwich to re-hash the
                # working file and correctly report any remaining unstaged change.
                blob = repo[sha]
                entry = IndexEntry(0, 0, 0, 0, mode, 0, 0, len(blob.data), sha)
                index[encoded] = entry
            index.write()
        cancellation.raise_if_cancelled()

    def diff(self, staged: bool = False, paths: list[str] | None = None,
             cancellation: CancellationToken | None = None) -> str:
        cancellation = cancellation_or_default(cancellation)
        cancellation.raise_if_cancelled()
        output = io.BytesIO()
        porcelain.diff(self.root, staged=staged, paths=paths, outstream=output)
        cancellation.raise_if_cancelled()
        return output.getvalue().decode("utf-8", "replace")

    def log(self, count: int = 10,
            cancellation: CancellationToken | None = None) -> list[GitCommit]:
        cancellation = cancellation_or_default(cancellation)
        cancellation.raise_if_cancelled()
        commits: list[GitCommit] = []
        with self._repo() as repo:
            try:
                walker = repo.get_walker(max_entries=count)
                for entry in walker:
                    cancellation.raise_if_cancelled()
                    commit = entry.commit
                    offset = timezone(timedelta(seconds=commit.commit_timezone))
                    commits.append(GitCommit(
                        commit.id.decode("ascii"),
                        _decode(commit.author).rsplit(" <", 1)[0],
                        _decode(commit.author).rsplit(" <", 1)[-1].rstrip(">"),
                        datetime.fromtimestamp(commit.commit_time, offset).isoformat(),
                        _decode(commit.message).splitlines()[0] if commit.message else "",
                    ))
            except KeyError:
                return []
        return commits

    def commit(self, message: str, paths: list[str],
               cancellation: CancellationToken | None = None) -> str:
        cancellation = cancellation_or_default(cancellation)
        current = self.status(cancellation)
        outside = sorted(set(current.staged) - set(paths))
        if outside:
            raise GitError(
                "refusing to include already-staged paths outside this commit: "
                + ", ".join(outside)
            )
        porcelain.add(self.root, paths=paths)
        cancellation.raise_if_cancelled()
        staged = self.status(cancellation).staged
        if not staged:
            raise GitError("no changes are staged for commit")
        outside = sorted(set(staged) - set(paths))
        if outside:
            raise GitError("refusing to commit unexpected staged paths: " + ", ".join(outside))
        try:
            commit_id = porcelain.commit(self.root, message=message.encode("utf-8"))
        except CancelledError:
            raise
        except Exception as exc:
            raise GitError(str(exc)) from exc
        cancellation.raise_if_cancelled()
        return commit_id.decode("ascii")

    def push(self, remote: str = "origin", branch: str = "",
             cancellation: CancellationToken | None = None) -> str:
        cancellation = cancellation_or_default(cancellation)
        cancellation.raise_if_cancelled()
        branch = branch or self.status(cancellation).branch
        if branch == "(detached)":
            raise GitError("cannot push a detached HEAD without an explicit branch")
        output, errors = io.BytesIO(), io.BytesIO()
        try:
            porcelain.push(
                self.root, remote_location=remote,
                refspecs=f"refs/heads/{branch}:refs/heads/{branch}",
                outstream=output, errstream=errors,
            )
        except CancelledError:
            raise
        except Exception as exc:
            detail = errors.getvalue().decode("utf-8", "replace").strip()
            raise GitError(detail or str(exc)) from exc
        cancellation.raise_if_cancelled()
        return f"pushed {branch} to {remote}"


def create_git_backend(root: Path, preference: str = "auto") -> GitBackend:
    executable = shutil.which("git")
    if preference == "native":
        if not executable:
            raise GitError("native Git backend requested, but git is not installed or in PATH")
        return NativeGitBackend(root, executable)
    if preference == "dulwich":
        return DulwichGitBackend(root)
    if preference != "auto":
        raise ValueError(f"unknown Git backend: {preference}")
    return NativeGitBackend(root, executable) if executable else DulwichGitBackend(root)
