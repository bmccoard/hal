from __future__ import annotations

import fnmatch
import functools
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from enum import Enum
from abc import ABC, abstractmethod
from pathlib import Path
from collections.abc import Callable
from typing import Any

from .cancellation import CancellationToken, cancellation_or_default
from .models import ToolSpec
from .process import BoundedOutput, DEFAULT_OUTPUT_LIMIT, ProcessTimeout, run_bounded_process

MAX_RESULT = DEFAULT_OUTPUT_LIMIT
DEFAULT_IGNORED_DIRECTORIES = {
    ".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv",
    "__pycache__", "node_modules", "venv",
}


def is_env_file(path: str | Path) -> bool:
    """Return whether a path names a protected dotenv file."""
    candidate = Path(path)
    names = {candidate.name.casefold()}
    try:
        names.add(candidate.resolve().name.casefold())
    except OSError:
        pass
    return any(name == ".env" or name.startswith(".env.") for name in names)


def _reject_env_file(path: Path, action: str) -> None:
    if is_env_file(path):
        raise PermissionError(f"refusing to {action} protected .env file: {path}")


def bound_output(text: str, limit: int = MAX_RESULT) -> str:
    output = BoundedOutput(limit)
    output.write(text)
    return output.text()


def _atomic_write(path: Path, content: str,
                  cancellation: CancellationToken | None = None) -> None:
    """Replace a UTF-8 file atomically while retaining its existing mode."""
    cancellation = cancellation_or_default(cancellation)
    cancellation.raise_if_cancelled()
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        cancellation.raise_if_cancelled()
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def shell_argv(command: str, platform: str | None = None) -> list[str]:
    """Select an explicit native shell instead of relying on shell=True."""
    kind, executable = native_shell(platform)
    if kind in {"PowerShell", "Windows PowerShell"}:
        return [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command]
    if kind == "Command Prompt":
        return [executable, "/d", "/s", "/c", command]
    if kind == "Bash":
        return [executable, "-lc", command]
    return [executable, "-c", command]


def native_shell(platform: str | None = None) -> tuple[str, str]:
    """Return the native shell kind and executable selected for tool calls."""
    platform = platform or os.name
    if platform == "nt":
        if executable := shutil.which("pwsh"):
            return "PowerShell", executable
        if executable := shutil.which("powershell"):
            return "Windows PowerShell", executable
        return "Command Prompt", os.environ.get("COMSPEC", "cmd.exe")
    if executable := shutil.which("bash"):
        return "Bash", executable
    return "POSIX shell", shutil.which("sh") or "/bin/sh"


@functools.lru_cache(maxsize=8)
def native_shell_version(kind: str, executable: str) -> str:
    """Best-effort shell version for the generated runtime context."""
    if kind in {"PowerShell", "Windows PowerShell"}:
        command = [
            executable, "-NoLogo", "-NoProfile", "-NonInteractive",
            "-Command", "$PSVersionTable.PSVersion.ToString()",
        ]
    elif kind in {"Bash", "POSIX shell"}:
        command = [executable, "--version"]
    else:
        command = [executable, "/d", "/c", "ver"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return ""
    lines = [line.strip() for line in (result.stdout + result.stderr).splitlines() if line.strip()]
    return lines[0][:120] if lines else ""


class ToolEffect(str, Enum):
    READ_ONLY = "read_only"
    MUTATING = "mutating"
    EXTERNAL = "external"
    UNKNOWN = "unknown"


class Tool(ABC):
    parallel_safe = False
    effect = ToolEffect.UNKNOWN

    @property
    @abstractmethod
    def spec(self) -> ToolSpec: ...

    @abstractmethod
    def run(self, arguments: dict[str, Any],
            cancellation: CancellationToken | None = None) -> str: ...


class BashTool(Tool):
    effect = ToolEffect.EXTERNAL
    def __init__(self, cwd: Path, timeout: float = 120) -> None:
        self.cwd = cwd
        self.timeout = timeout

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec("bash", "Run a command in the native shell (PowerShell on Windows; Bash on Unix).", {
            "type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "number"}}, "required": ["command"]
        })

    def run(self, arguments: dict[str, Any],
            cancellation: CancellationToken | None = None) -> str:
        cancellation = cancellation_or_default(cancellation)
        cancellation.raise_if_cancelled()
        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must be a non-empty string")
        timeout = min(float(arguments.get("timeout", self.timeout)), self.timeout)
        try:
            result = run_bounded_process(
                shell_argv(command), self.cwd, cancellation,
                timeout=timeout, output_limit=MAX_RESULT,
            )
        except ProcessTimeout as exc:
            partial = exc.stdout + exc.stderr
            raise RuntimeError(
                f"command timed out after {timeout:g}s\n{bound_output(partial)}"
            ) from exc
        output = result.stdout + result.stderr
        if result.returncode:
            raise RuntimeError(f"command exited with status {result.returncode}\n{bound_output(output)}")
        return bound_output(output) or "command completed successfully"


class ReadFileTool(Tool):
    parallel_safe = True
    effect = ToolEffect.READ_ONLY

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec("read_file", "Read a UTF-8 text file other than .env files. offset is a positive 1-indexed line number; omit it to start at line 1.", {
            "type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer", "minimum": 1}, "limit": {"type": "integer", "minimum": 1}}, "required": ["path"]
        })

    def run(self, arguments: dict[str, Any],
            cancellation: CancellationToken | None = None) -> str:
        cancellation = cancellation_or_default(cancellation)
        cancellation.raise_if_cancelled()
        raw_path = arguments.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError("path is required")
        path = Path(raw_path)
        _reject_env_file(path, "read")
        has_window = "offset" in arguments or "limit" in arguments
        if not has_window:
            if path.stat().st_size > MAX_RESULT:
                raise ValueError(
                    f"read_file: file exceeds {MAX_RESULT} bytes; use offset/limit"
                )
            with path.open("rb") as handle:
                raw = handle.read(MAX_RESULT + 1)
            if len(raw) > MAX_RESULT:
                raise ValueError(
                    f"read_file: file exceeds {MAX_RESULT} bytes; use offset/limit"
                )
            return raw.decode("utf-8", "replace")

        offset = int(arguments.get("offset", 1))
        raw_limit = arguments.get("limit")
        limit = int(raw_limit) if raw_limit is not None else None
        if offset <= 0:
            raise ValueError("offset must be a positive 1-indexed line number; retry with offset=1 or omit offset")
        if limit is not None and limit <= 0:
            raise ValueError("limit must be positive")

        selected: list[bytes] = []
        selected_bytes = 0
        selected_lines = 0
        line_number = 0
        last_line_ended_newline = False
        with path.open("rb") as handle:
            while limit is None or selected_lines < limit:
                cancellation.raise_if_cancelled()
                line = handle.readline(MAX_RESULT + 1)
                if not line:
                    break
                line_number += 1
                complete = line.endswith(b"\n") or len(line) <= MAX_RESULT
                if line_number < offset:
                    while not complete:
                        line = handle.readline(MAX_RESULT + 1)
                        if not line:
                            complete = True
                        else:
                            complete = line.endswith(b"\n") or len(line) <= MAX_RESULT
                    last_line_ended_newline = line.endswith(b"\n")
                    continue
                if not complete:
                    raise ValueError(
                        f"read_file: selection exceeds {MAX_RESULT} bytes; narrow offset/limit"
                    )
                if selected_bytes + len(line) > MAX_RESULT:
                    raise ValueError(
                        f"read_file: selection exceeds {MAX_RESULT} bytes; narrow offset/limit"
                    )
                selected.append(line)
                selected_bytes += len(line)
                selected_lines += 1
                last_line_ended_newline = line.endswith(b"\n")

        logical_lines = line_number + int(last_line_ended_newline)
        empty_file_at_first_line = logical_lines == 0 and offset == 1
        if selected_lines == 0 and offset > logical_lines and not empty_file_at_first_line:
            raise ValueError(
                f"read_file: offset {offset} is past end of file "
                f"({logical_lines} logical lines); retry with an offset from 1 to {max(logical_lines, 1)}"
            )
        return b"".join(selected).decode("utf-8", "replace")


class WriteFileTool(Tool):
    effect = ToolEffect.MUTATING
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec("write_file", "Write a UTF-8 file other than .env files, creating parent directories.", {
            "type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]
        })

    def run(self, arguments: dict[str, Any],
            cancellation: CancellationToken | None = None) -> str:
        raw_path = arguments.get("path")
        content = arguments.get("content")
        if not isinstance(raw_path, str) or not raw_path or not isinstance(content, str):
            raise ValueError("path and string content are required")
        path = Path(raw_path)
        _reject_env_file(path, "write")
        _atomic_write(path, content, cancellation)
        return f"wrote {len(content.encode('utf-8'))} bytes to {path}"


class EditFileTool(Tool):
    effect = ToolEffect.MUTATING
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec("edit_file", "Replace exactly one occurrence of text in a UTF-8 file other than .env files.", {
            "type": "object", "properties": {"path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}}, "required": ["path", "old_string", "new_string"]
        })

    def run(self, arguments: dict[str, Any],
            cancellation: CancellationToken | None = None) -> str:
        cancellation = cancellation_or_default(cancellation)
        cancellation.raise_if_cancelled()
        raw_path = arguments.get("path")
        old = arguments.get("old_string")
        new = arguments.get("new_string")
        if not isinstance(raw_path, str) or not raw_path or not isinstance(old, str) or not isinstance(new, str) or not old:
            raise ValueError("path, non-empty old_string, and new_string are required")
        path = Path(raw_path)
        _reject_env_file(path, "edit")
        text = path.read_text(encoding="utf-8")
        count = text.count(old)
        if count != 1:
            raise ValueError(f"old_string must occur exactly once (found {count})")
        _atomic_write(path, text.replace(old, new, 1), cancellation)
        return f"edited {path}"


class _RootedTool(Tool):
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _within_root(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.root)
            return True
        except (OSError, ValueError):
            return False


class GlobTool(_RootedTool):
    parallel_safe = True
    effect = ToolEffect.READ_ONLY

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec("glob", "Find files under the workspace using a glob pattern.", {
            "type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]
        })

    def run(self, arguments: dict[str, Any],
            cancellation: CancellationToken | None = None) -> str:
        cancellation = cancellation_or_default(cancellation)
        pattern = str(arguments.get("pattern", ""))
        if not pattern:
            raise ValueError("pattern is required")
        matches: list[str] = []
        stopped = False
        for directory, names, files in os.walk(self.root):
            cancellation.raise_if_cancelled()
            names[:] = sorted(
                name for name in names
                if name not in DEFAULT_IGNORED_DIRECTORIES
            )
            base = Path(directory)
            for filename in sorted(files):
                cancellation.raise_if_cancelled()
                path = base / filename
                rel = path.relative_to(self.root).as_posix()
                if self._within_root(path) and (
                    fnmatch.fnmatch(rel, pattern) or Path(rel).match(pattern)
                ):
                    matches.append(rel)
                    if len(matches) >= 200:
                        stopped = True
                        break
            if stopped:
                break
        return json.dumps({"matches": sorted(matches), "truncated": len(matches) >= 200})


class GrepTool(_RootedTool):
    parallel_safe = True
    effect = ToolEffect.READ_ONLY

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec("grep", "Search text files under the workspace. pattern is a Python regular expression unless literal=true.", {
            "type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}, "include": {"type": "string"}, "literal": {"type": "boolean", "default": False}}, "required": ["pattern"]
        })

    def run(self, arguments: dict[str, Any],
            cancellation: CancellationToken | None = None) -> str:
        cancellation = cancellation_or_default(cancellation)
        cancellation.raise_if_cancelled()
        pattern = str(arguments.get("pattern", ""))
        try:
            regex = re.compile(re.escape(pattern) if arguments.get("literal") is True else pattern)
        except re.error as exc:
            raise ValueError(
                f"invalid regular expression: {exc}; retry with literal=true "
                "for exact text or escape regex metacharacters"
            ) from exc
        base = (self.root / str(arguments.get("path", "."))).resolve()
        if not self._within_root(base):
            raise ValueError("search path escapes workspace root")
        if base.is_file() and is_env_file(base):
            raise PermissionError(f"refusing to search protected .env file: {base}")
        include = str(arguments.get("include", "*"))
        matches: list[dict[str, Any]] = []
        candidates = [base] if base.is_file() else base.rglob("*")
        for path in candidates:
            cancellation.raise_if_cancelled()
            if len(matches) >= 200:
                break
            if (
                not path.is_file() or not self._within_root(path)
                or is_env_file(path) or not fnmatch.fnmatch(path.name, include)
            ):
                continue
            try:
                if path.stat().st_size > 4 * 1024 * 1024:
                    continue
                for number, line in enumerate(path.read_text(encoding="utf-8", errors="strict").splitlines(), 1):
                    cancellation.raise_if_cancelled()
                    if regex.search(line):
                        matches.append({"path": path.relative_to(self.root).as_posix(), "line": number, "text": line[:2000]})
                        if len(matches) >= 200:
                            break
            except (OSError, UnicodeError):
                continue
        return json.dumps({"matches": matches, "truncated": len(matches) >= 200})


class Registry:
    def __init__(self, tools: list[Tool], approvals: list[str] | None = None,
                 confirm: Callable[[str], bool] | None = None,
                 write_root: Path | None = None, cwd: Path | None = None,
                 bash_policy: str = "normal") -> None:
        self._tools: dict[str, Tool] = {}
        self.extend(tools)
        self.approvals = approvals or []
        self.confirm = confirm
        self.write_root = write_root.resolve() if write_root else None
        self.cwd = (cwd or Path.cwd()).resolve()
        self.bash_policy = bash_policy

    def extend(self, tools: list[Tool]) -> None:
        """Add tools while protecting the registry from ambiguous names."""
        for tool in tools:
            name = tool.spec.name
            if not name:
                raise ValueError("tool name must not be empty")
            if name in self._tools:
                raise ValueError(f"duplicate tool name: {name}")
            self._tools[name] = tool

    def bind_agent(self, agent: object) -> None:
        """Bind agent-aware tools after registry and agent construction."""
        for tool in self._tools.values():
            bind = getattr(tool, "bind_agent", None)
            if callable(bind):
                bind(agent)

    @property
    def specs(self) -> list[ToolSpec]:
        return [self._tools[name].spec for name in sorted(self._tools)]

    def is_parallel_safe(
        self, name: str, allowed: set[str] | None = None,
        denied: set[str] | None = None,
    ) -> bool:
        """Return true only when a call can bypass every serial policy barrier."""
        denied = denied or set()
        if name in denied or (allowed is not None and name not in allowed):
            return False
        tool = self._tools.get(name)
        if tool is None or not tool.parallel_safe or name == "bash":
            return False
        return not any(
            rule == name or rule.startswith(name + " ")
            for rule in self.approvals
        )

    def metadata(self, name: str) -> dict[str, object]:
        """Return scheduling metadata with current approval policy resolved."""
        try:
            tool = self._tools[name]
        except KeyError as exc:
            raise ValueError(f"unknown tool {name!r}") from exc
        target_approval = any(
            rule == name or rule.startswith(name + " ")
            for rule in self.approvals
        )
        approval_gated = target_approval or (
            name == "bash" and self.bash_policy == "approve"
        )
        effect = getattr(tool, "effect", ToolEffect.UNKNOWN)
        if not isinstance(effect, ToolEffect):
            try:
                effect = ToolEffect(effect)
            except ValueError:
                effect = ToolEffect.UNKNOWN
        return {
            "effect": effect.value,
            "parallel_safe": bool(getattr(tool, "parallel_safe", False)),
            "approval_gated": approval_gated,
        }

    def specs_for(self, allowed: set[str] | None = None,
                  denied: set[str] | None = None) -> list[ToolSpec]:
        """Return tool specs filtered by an optional phase policy."""
        denied = denied or set()
        return [
            self._tools[name].spec for name in sorted(self._tools)
            if (allowed is None or name in allowed) and name not in denied
        ]

    def run(self, name: str, arguments: dict[str, Any],
            cancellation: CancellationToken | None = None,
            allowed: set[str] | None = None,
            denied: set[str] | None = None,
            protect_existing_files: bool = False) -> str:
        cancellation = cancellation_or_default(cancellation)
        cancellation.raise_if_cancelled()
        denied = denied or set()
        if name in denied or (allowed is not None and name not in allowed):
            available = ", ".join(
                spec.name for spec in self.specs_for(allowed, denied)
            ) or "none"
            raise PermissionError(
                f"tool {name!r} is not available in this workflow phase; "
                f"available tools: {available}"
            )
        try:
            tool = self._tools[name]
        except KeyError as exc:
            available = ", ".join(sorted(self._tools))
            hint = "; use grep to search file contents" if name == "search" else ""
            raise ValueError(
                f"unknown tool: {name}{hint}; available tools: {available}"
            ) from exc
        execution_arguments = dict(arguments)
        if name == "bash":
            if self.bash_policy == "deny":
                raise PermissionError("bash is disabled by bash_policy")
            if self.bash_policy == "approve" and (
                self.confirm is None
                or not self.confirm(
                    "Allow unrestricted bash command? Shell effects are not "
                    "confined by only_write_locally."
                )
            ):
                raise PermissionError("bash was denied by bash_policy")
        if name in {"write_file", "edit_file"}:
            raw_path = arguments.get("path")
            if isinstance(raw_path, str) and raw_path:
                path = Path(raw_path)
                resolved = (
                    path.resolve() if path.is_absolute()
                    else (self.cwd / path).resolve()
                )
                execution_arguments["path"] = str(resolved)
                if self.write_root is not None:
                    try:
                        resolved.relative_to(self.write_root)
                    except ValueError:
                        if self.confirm is None or not self.confirm(
                            f"Allow {name} outside workspace: {resolved}?"
                        ):
                            raise PermissionError(
                                f"{name} outside workspace was denied: {resolved}"
                            )
                if name == "write_file" and protect_existing_files and resolved.exists():
                    raise PermissionError(
                        f"write_file cannot replace existing file {resolved} in a workflow; "
                        "use edit_file for an exact replacement"
                    )
        target = str(arguments.get("command", "")) if name == "bash" else name
        needs_approval = any(target == rule or (name == "bash" and target.startswith(rule) and (len(target) == len(rule) or target[len(rule)].isspace())) for rule in self.approvals)
        if needs_approval and (self.confirm is None or not self.confirm(f"Allow {name}: {target}?")):
            raise PermissionError(f"{name} was denied by the user")
        cancellation.raise_if_cancelled()
        output = tool.run(execution_arguments, cancellation)
        cancellation.raise_if_cancelled()
        return bound_output(output)


def default_registry(cwd: Path, root: Path | None = None, approvals: list[str] | None = None,
                     confirm: Callable[[str], bool] | None = None,
                     git_backend: str = "auto", only_write_locally: bool = False,
                     bash_policy: str = "normal") -> Registry:
    root = (root or workspace_root(cwd)).resolve()
    from .git_tools import git_tools

    return Registry([
        BashTool(cwd), ReadFileTool(), WriteFileTool(), EditFileTool(),
        GrepTool(root), GlobTool(root), *git_tools(root, git_backend),
    ], approvals, confirm, root if only_write_locally else None, cwd, bash_policy)


def workspace_root(cwd: Path) -> Path:
    current = cwd.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return current
