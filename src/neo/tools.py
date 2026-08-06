from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from collections.abc import Callable
from typing import Any

from .models import ToolSpec

MAX_RESULT = 256 * 1024


def _bounded(text: str, limit: int = MAX_RESULT) -> str:
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= limit:
        return text
    half = max(1, (limit - 100) // 2)
    return raw[:half].decode("utf-8", "replace") + "\n... output truncated ...\n" + raw[-half:].decode("utf-8", "replace")


def shell_argv(command: str, platform: str | None = None) -> list[str]:
    """Select an explicit native shell instead of relying on shell=True."""
    platform = platform or os.name
    if platform == "nt":
        executable = shutil.which("pwsh") or shutil.which("powershell")
        if executable:
            return [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command]
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", command]
    executable = shutil.which("bash")
    if executable:
        return [executable, "-lc", command]
    return [shutil.which("sh") or "/bin/sh", "-c", command]


class Tool(ABC):
    parallel_safe = False

    @property
    @abstractmethod
    def spec(self) -> ToolSpec: ...

    @abstractmethod
    def run(self, arguments: dict[str, Any]) -> str: ...


class BashTool(Tool):
    def __init__(self, cwd: Path, timeout: float = 120) -> None:
        self.cwd = cwd
        self.timeout = timeout

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec("bash", "Run a command in the native shell (PowerShell on Windows; Bash on Unix).", {
            "type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "number"}}, "required": ["command"]
        })

    def run(self, arguments: dict[str, Any]) -> str:
        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must be a non-empty string")
        timeout = min(float(arguments.get("timeout", self.timeout)), self.timeout)
        try:
            result = subprocess.run(shell_argv(command), cwd=self.cwd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            partial = (exc.stdout or "") + (exc.stderr or "")
            raise RuntimeError(f"command timed out after {timeout:g}s\n{_bounded(partial)}") from exc
        output = (result.stdout or "") + (result.stderr or "")
        if result.returncode:
            raise RuntimeError(f"command exited with status {result.returncode}\n{_bounded(output)}")
        return _bounded(output) or "command completed successfully"


class ReadFileTool(Tool):
    parallel_safe = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec("read_file", "Read a UTF-8 text file, optionally by 1-indexed line window.", {
            "type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, "required": ["path"]
        })

    def run(self, arguments: dict[str, Any]) -> str:
        path = Path(str(arguments.get("path", "")))
        if not str(path):
            raise ValueError("path is required")
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines(keepends=True)
        offset = max(1, int(arguments.get("offset", 1)))
        limit = int(arguments.get("limit", len(lines) or 1))
        if limit <= 0:
            raise ValueError("limit must be positive")
        return _bounded("".join(lines[offset - 1:offset - 1 + limit]))


class WriteFileTool(Tool):
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec("write_file", "Write a UTF-8 file, creating parent directories.", {
            "type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]
        })

    def run(self, arguments: dict[str, Any]) -> str:
        path = Path(str(arguments.get("path", "")))
        content = arguments.get("content")
        if not str(path) or not isinstance(content, str):
            raise ValueError("path and string content are required")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"wrote {len(content.encode('utf-8'))} bytes to {path}"


class EditFileTool(Tool):
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec("edit_file", "Replace exactly one occurrence of text in a UTF-8 file.", {
            "type": "object", "properties": {"path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}}, "required": ["path", "old_string", "new_string"]
        })

    def run(self, arguments: dict[str, Any]) -> str:
        path = Path(str(arguments.get("path", "")))
        old = arguments.get("old_string")
        new = arguments.get("new_string")
        if not str(path) or not isinstance(old, str) or not isinstance(new, str) or not old:
            raise ValueError("path, non-empty old_string, and new_string are required")
        text = path.read_text(encoding="utf-8")
        count = text.count(old)
        if count != 1:
            raise ValueError(f"old_string must occur exactly once (found {count})")
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
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

    def run(self, arguments: dict[str, Any]) -> str:
        pattern = str(arguments.get("pattern", ""))
        if not pattern:
            raise ValueError("pattern is required")
        matches: list[str] = []
        for path in self.root.rglob("*"):
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

    def run(self, arguments: dict[str, Any]) -> str:
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
            if len(matches) >= 200:
                break
            if not path.is_file() or not self._within_root(path) or not fnmatch.fnmatch(path.name, include):
                continue
            try:
                if path.stat().st_size > 4 * 1024 * 1024:
                    continue
                for number, line in enumerate(path.read_text(encoding="utf-8", errors="strict").splitlines(), 1):
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

    def run(self, name: str, arguments: dict[str, Any]) -> str:
        try:
            tool = self._tools[name]
        except KeyError as exc:
            raise ValueError(f"unknown tool: {name}") from exc
        target = str(arguments.get("command", "")) if name == "bash" else name
        needs_approval = any(target == rule or (name == "bash" and target.startswith(rule) and (len(target) == len(rule) or target[len(rule)].isspace())) for rule in self.approvals)
        if needs_approval and (self.confirm is None or not self.confirm(f"Allow {name}: {target}?")):
            raise PermissionError(f"{name} was denied by the user")
        return _bounded(tool.run(arguments))


def default_registry(cwd: Path, root: Path | None = None, approvals: list[str] | None = None,
                     confirm: Callable[[str], bool] | None = None) -> Registry:
    root = (root or workspace_root(cwd)).resolve()
    return Registry([BashTool(cwd), ReadFileTool(), WriteFileTool(), EditFileTool(), GrepTool(root), GlobTool(root)], approvals, confirm)


def workspace_root(cwd: Path) -> Path:
    current = cwd.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return current
