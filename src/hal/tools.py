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
from abc import ABC, abstractmethod
from pathlib import Path
from collections.abc import Callable
from typing import Any

from .cancellation import CancellationToken, cancellation_or_default
from .models import ToolSpec
from .process import BoundedOutput, DEFAULT_OUTPUT_LIMIT, ProcessTimeout, run_bounded_process

MAX_RESULT = DEFAULT_OUTPUT_LIMIT


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


class Tool(ABC):
    parallel_safe = False

    @property
    @abstractmethod
    def spec(self) -> ToolSpec: ...

    @abstractmethod
    def run(self, arguments: dict[str, Any],
            cancellation: CancellationToken | None = None) -> str: ...


class BashTool(Tool):
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

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec("read_file", "Read a UTF-8 text file, optionally by 1-indexed line window.", {
            "type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, "required": ["path"]
        })

    def run(self, arguments: dict[str, Any],
            cancellation: CancellationToken | None = None) -> str:
        cancellation = cancellation_or_default(cancellation)
        cancellation.raise_if_cancelled()
        raw_path = arguments.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError("path is required")
        path = Path(raw_path)
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
            raise ValueError("offset must be a positive 1-indexed line number")
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
            raise ValueError(f"read_file: offset {offset} is past end of file")
        return b"".join(selected).decode("utf-8", "replace")


class WriteFileTool(Tool):
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec("write_file", "Write a UTF-8 file, creating parent directories.", {
            "type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]
        })

    def run(self, arguments: dict[str, Any],
            cancellation: CancellationToken | None = None) -> str:
        raw_path = arguments.get("path")
        content = arguments.get("content")
        if not isinstance(raw_path, str) or not raw_path or not isinstance(content, str):
            raise ValueError("path and string content are required")
        path = Path(raw_path)
        _atomic_write(path, content, cancellation)
        return f"wrote {len(content.encode('utf-8'))} bytes to {path}"


class EditFileTool(Tool):
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec("edit_file", "Replace exactly one occurrence of text in a UTF-8 file.", {
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
        for path in self.root.rglob("*"):
            cancellation.raise_if_cancelled()
            if len(matches) >= 200:
                break
            rel = path.relative_to(self.root).as_posix()
            if path.is_file() and self._within_root(path) and (fnmatch.fnmatch(rel, pattern) or Path(rel).match(pattern)):
                matches.append(rel)
        return json.dumps({"matches": sorted(matches), "truncated": len(matches) >= 200})


class GrepTool(_RootedTool):
    parallel_safe = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec("grep", "Search text files under the workspace with a regular expression.", {
            "type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}, "include": {"type": "string"}}, "required": ["pattern"]
        })

    def run(self, arguments: dict[str, Any],
            cancellation: CancellationToken | None = None) -> str:
        cancellation = cancellation_or_default(cancellation)
        cancellation.raise_if_cancelled()
        try:
            regex = re.compile(str(arguments.get("pattern", "")))
        except re.error as exc:
            raise ValueError(f"invalid regular expression: {exc}") from exc
        base = (self.root / str(arguments.get("path", "."))).resolve()
        if not self._within_root(base):
            raise ValueError("search path escapes workspace root")
        include = str(arguments.get("include", "*"))
        matches: list[dict[str, Any]] = []
        candidates = [base] if base.is_file() else base.rglob("*")
        for path in candidates:
            cancellation.raise_if_cancelled()
            if len(matches) >= 200:
                break
            if not path.is_file() or not self._within_root(path) or not fnmatch.fnmatch(path.name, include):
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
                 confirm: Callable[[str], bool] | None = None) -> None:
        self._tools = {tool.spec.name: tool for tool in tools}
        self.approvals = approvals or []
        self.confirm = confirm

    @property
    def specs(self) -> list[ToolSpec]:
        return [self._tools[name].spec for name in sorted(self._tools)]

    def run(self, name: str, arguments: dict[str, Any],
            cancellation: CancellationToken | None = None) -> str:
        cancellation = cancellation_or_default(cancellation)
        cancellation.raise_if_cancelled()
        try:
            tool = self._tools[name]
        except KeyError as exc:
            raise ValueError(f"unknown tool: {name}") from exc
        target = str(arguments.get("command", "")) if name == "bash" else name
        needs_approval = any(target == rule or (name == "bash" and target.startswith(rule) and (len(target) == len(rule) or target[len(rule)].isspace())) for rule in self.approvals)
        if needs_approval and (self.confirm is None or not self.confirm(f"Allow {name}: {target}?")):
            raise PermissionError(f"{name} was denied by the user")
        cancellation.raise_if_cancelled()
        output = tool.run(arguments, cancellation)
        cancellation.raise_if_cancelled()
        return bound_output(output)


def default_registry(cwd: Path, root: Path | None = None, approvals: list[str] | None = None,
                     confirm: Callable[[str], bool] | None = None,
                     git_backend: str = "auto") -> Registry:
    root = (root or workspace_root(cwd)).resolve()
    from .git_tools import git_tools

    return Registry([
        BashTool(cwd), ReadFileTool(), WriteFileTool(), EditFileTool(),
        GrepTool(root), GlobTool(root), *git_tools(root, git_backend),
    ], approvals, confirm)


def workspace_root(cwd: Path) -> Path:
    current = cwd.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return current
