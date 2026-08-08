from pathlib import Path
import os
import shlex
import sys

import pytest

from hal.process import BoundedOutput, ProcessTimeout, run_bounded_process
from hal.tools import BashTool, MAX_RESULT


def test_bounded_output_preserves_head_tail_and_reports_omitted_bytes() -> None:
    output = BoundedOutput(512)
    output.write(b"BEGIN" + b"x" * 2_000 + b"END")

    rendered = output.text()

    assert rendered.startswith("BEGIN")
    assert rendered.endswith("END")
    assert "output truncated:" in rendered
    assert "bytes omitted" in rendered
    assert output.total_bytes == 2_008
    assert output.omitted_bytes > 0
    assert len(rendered.encode("utf-8")) <= 512


def test_process_capture_bounds_stdout_and_stderr_independently(tmp_path: Path) -> None:
    code = (
        "import sys; "
        "sys.stdout.write('OUT-BEGIN' + 'x' * 5000 + 'OUT-END'); "
        "sys.stderr.write('ERR-BEGIN' + 'y' * 5000 + 'ERR-END')"
    )

    result = run_bounded_process(
        [sys.executable, "-c", code], tmp_path, output_limit=1024,
    )

    assert result.returncode == 0
    assert result.stdout.startswith("OUT-BEGIN") and result.stdout.endswith("OUT-END")
    assert result.stderr.startswith("ERR-BEGIN") and result.stderr.endswith("ERR-END")
    assert result.stdout_truncated and result.stderr_truncated
    assert len(result.stdout.encode("utf-8")) <= 1024
    assert len(result.stderr.encode("utf-8")) <= 1024


def test_bash_tool_preserves_head_and_tail_of_large_output(tmp_path: Path) -> None:
    if os.name == "nt":
        command = f"[Console]::Out.Write('BEGIN' + ('x' * {MAX_RESULT * 2}) + 'END')"
    else:
        code = f"import sys; sys.stdout.write('BEGIN' + 'x' * {MAX_RESULT * 2} + 'END')"
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"

    output = BashTool(tmp_path).run({"command": command})

    assert output.startswith("BEGIN")
    assert output.endswith("END")
    assert "bytes omitted" in output
    assert len(output.encode("utf-8")) <= MAX_RESULT


def test_timed_out_process_returns_bounded_partial_output(tmp_path: Path) -> None:
    code = (
        "import sys,time; "
        "sys.stdout.write('BEGIN' + 'x' * 5000 + 'END'); "
        "sys.stdout.flush(); time.sleep(10)"
    )

    with pytest.raises(ProcessTimeout) as raised:
        run_bounded_process(
            [sys.executable, "-c", code], tmp_path, timeout=.1, output_limit=1024,
        )

    assert raised.value.stdout.startswith("BEGIN")
    assert raised.value.stdout.endswith("END")
    assert "bytes omitted" in raised.value.stdout
    assert len(raised.value.stdout.encode("utf-8")) <= 1024
