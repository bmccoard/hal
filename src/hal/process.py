from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Iterable, Sequence

from .cancellation import CancelledError, CancellationToken, cancellation_or_default


DEFAULT_OUTPUT_LIMIT = 256 * 1024
_MARKER_RESERVE = 160
_READ_CHUNK = 64 * 1024


class BoundedOutput:
    """A byte-counted head/tail buffer suitable for subprocess and Dulwich output."""

    def __init__(self, limit: int = DEFAULT_OUTPUT_LIMIT) -> None:
        if limit < _MARKER_RESERVE + 2:
            raise ValueError(f"output limit must be at least {_MARKER_RESERVE + 2} bytes")
        self.limit = limit
        payload = limit - _MARKER_RESERVE
        self._head_limit = payload // 2
        self._tail_limit = payload - self._head_limit
        self._head = bytearray()
        self._tail = bytearray()
        self.total_bytes = 0

    @property
    def captured_bytes(self) -> int:
        return len(self._head) + len(self._tail)

    @property
    def omitted_bytes(self) -> int:
        return max(0, self.total_bytes - self.captured_bytes)

    @property
    def truncated(self) -> bool:
        return self.omitted_bytes > 0

    def write(self, value: bytes | bytearray | memoryview | str) -> int:
        data = value.encode("utf-8", "replace") if isinstance(value, str) else bytes(value)
        size = len(data)
        self.total_bytes += size
        if len(self._head) < self._head_limit:
            take = min(size, self._head_limit - len(self._head))
            self._head.extend(data[:take])
            data = data[take:]
        if data:
            if len(data) >= self._tail_limit:
                self._tail = bytearray(data[-self._tail_limit:])
            else:
                self._tail.extend(data)
                overflow = len(self._tail) - self._tail_limit
                if overflow > 0:
                    del self._tail[:overflow]
        return size

    def flush(self) -> None:
        """Provide the minimal binary-stream interface expected by Dulwich."""

    def writelines(self, values: Iterable[bytes | str]) -> None:
        for value in values:
            self.write(value)

    def text(self) -> str:
        head = self._head.decode("utf-8", "replace")
        tail = self._tail.decode("utf-8", "replace")
        if not self.truncated:
            return head + tail
        marker = (
            f"\n... output truncated: {self.omitted_bytes} bytes omitted "
            f"({self.total_bytes} bytes total) ...\n"
        )
        return head + marker + tail


@dataclass(slots=True)
class ProcessResult:
    args: Sequence[str]
    returncode: int
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool


class ProcessTimeout(TimeoutError):
    def __init__(self, timeout: float, stdout: str, stderr: str) -> None:
        super().__init__(f"process timed out after {timeout:g}s")
        self.timeout = timeout
        self.stdout = stdout
        self.stderr = stderr


def terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
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


def run_bounded_process(
    arguments: Sequence[str],
    cwd: Path,
    cancellation: CancellationToken | None = None,
    *,
    timeout: float | None = None,
    output_limit: int = DEFAULT_OUTPUT_LIMIT,
) -> ProcessResult:
    """Drain both pipes concurrently while retaining at most a fixed head/tail window."""
    cancellation = cancellation_or_default(cancellation)
    cancellation.raise_if_cancelled()
    kwargs: dict[str, object] = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(
        list(arguments), cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        **kwargs,
    )
    assert process.stdout is not None and process.stderr is not None
    stdout_buffer = BoundedOutput(output_limit)
    stderr_buffer = BoundedOutput(output_limit)
    reader_errors: list[BaseException] = []

    def drain(stream, destination: BoundedOutput) -> None:
        try:
            while chunk := stream.read(_READ_CHUNK):
                destination.write(chunk)
        except (OSError, ValueError) as exc:
            reader_errors.append(exc)

    readers = [
        threading.Thread(target=drain, args=(process.stdout, stdout_buffer), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, stderr_buffer), daemon=True),
    ]
    for reader in readers:
        reader.start()
    started = time.monotonic()
    failure: BaseException | None = None
    try:
        while process.poll() is None:
            cancellation.raise_if_cancelled()
            wait_for = .1
            if timeout is not None:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise ProcessTimeout(timeout, "", "")
                wait_for = min(wait_for, remaining)
            token_remaining = cancellation.remaining()
            if token_remaining is not None:
                wait_for = min(wait_for, max(.001, token_remaining))
            try:
                process.wait(timeout=wait_for)
            except subprocess.TimeoutExpired:
                continue
        cancellation.raise_if_cancelled()
    except (CancelledError, ProcessTimeout) as exc:
        failure = exc
        terminate_process_tree(process)
    finally:
        if process.poll() is None:
            process.wait()
        for reader in readers:
            reader.join(timeout=2)
        for stream, reader in zip((process.stdout, process.stderr), readers):
            if reader.is_alive():
                stream.close()
                reader.join(timeout=.5)

    stdout, stderr = stdout_buffer.text(), stderr_buffer.text()
    if isinstance(failure, CancelledError):
        raise failure
    if isinstance(failure, ProcessTimeout):
        raise ProcessTimeout(failure.timeout, stdout, stderr) from failure
    if reader_errors:
        raise OSError(f"could not capture subprocess output: {reader_errors[0]}")
    return ProcessResult(
        arguments, process.returncode, stdout, stderr,
        stdout_buffer.truncated, stderr_buffer.truncated,
    )
