"""Trusted, deterministic verification for harness runs."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import time

from .cancellation import CancelledError, CancellationToken, cancellation_or_default
from .process import ProcessTimeout, run_bounded_process
from .tools import MAX_RESULT, bound_output, shell_argv


class VerificationStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    START_FAILED = "start_failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class VerificationCheck:
    name: str
    command: str
    timeout_seconds: float = 120
    required: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("verification check name must not be empty")
        if not isinstance(self.command, str) or not self.command.strip():
            raise ValueError("verification check command must not be empty")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or self.timeout_seconds <= 0
        ):
            raise ValueError("verification check timeout_seconds must be positive")
        if not isinstance(self.required, bool):
            raise ValueError("verification check required must be true or false")


@dataclass(slots=True)
class VerificationResult:
    name: str
    passed: bool
    output: str
    duration_ms: int
    required: bool = True
    status: VerificationStatus = VerificationStatus.PASSED
    returncode: int | None = None


def run_verification_check(
    check: VerificationCheck,
    workspace: Path,
    cancellation: CancellationToken | None = None,
) -> VerificationResult:
    """Run one trusted check and retain bounded output and a stable status."""
    cancellation = cancellation_or_default(cancellation)
    started = time.monotonic()

    def result(status: VerificationStatus, output: str,
               returncode: int | None = None) -> VerificationResult:
        return VerificationResult(
            name=check.name,
            passed=status == VerificationStatus.PASSED,
            output=bound_output(output),
            duration_ms=int((time.monotonic() - started) * 1000),
            required=check.required,
            status=status,
            returncode=returncode,
        )

    try:
        process = run_bounded_process(
            shell_argv(check.command), workspace, cancellation,
            timeout=check.timeout_seconds, output_limit=MAX_RESULT,
        )
    except ProcessTimeout as exc:
        return result(
            VerificationStatus.TIMED_OUT,
            f"check timed out after {check.timeout_seconds:g}s\n{exc.stdout}{exc.stderr}",
        )
    except CancelledError as exc:
        cancelled = result(VerificationStatus.CANCELLED, str(exc))
        setattr(exc, "verification_result", cancelled)
        raise
    except OSError as exc:
        return result(VerificationStatus.START_FAILED, str(exc))

    output = process.stdout + process.stderr
    if process.returncode:
        return result(VerificationStatus.FAILED, output, process.returncode)
    return result(VerificationStatus.PASSED, output, process.returncode)
